"""Agent Plan Phase 8: Image Pool cho thumbnail — quét/kiểm tra theo nội dung, chọn shuffle/random/sequential, chốt ảnh vào workspace job (retry ổn định),
batch không trùng ảnh, đổi ảnh (reroll) chỉ làm lại đúng phần cần, migration v6."""
import json
import os
import random
import shutil
import sqlite3
import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path

from contentfactory.contracts import StageError
from contentfactory.jobs import pipeline as P
from contentfactory.jobs.db import SCHEMA_VERSION, JobStore
from contentfactory.media import image_pool as IP
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.runner import Orchestrator
from tests.support import REPO, RootCase, params, wait_until
from tests.test_automode import write_channel, write_config
from tests.test_job_control import Loop, sleeper, spec
from tests.test_templates import HAVE_REAL, REAL_PY


# ---------------------------------------------------------------------------------------------- ảnh giả (đủ để `sniff` đọc đúng)
def make_png(w=800, h=600, seed=0) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"tEXt", b"seed\x00" + str(seed).encode()) + chunk(b"IEND", b"")


def make_jpeg(w=800, h=600, seed=0) -> bytes:
    com = b"seed" + str(seed).encode()
    return (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + b"\xff\xfe" + struct.pack(">H", len(com) + 2) + com
            + b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00" + b"\xff\xd9")


def make_webp(w=800, h=600, seed=0) -> bytes:
    body = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little") + str(seed).encode().ljust(8, b"x")
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def fill(folder: Path, n: int, kind="png", start=0) -> list[str]:
    folder.mkdir(parents=True, exist_ok=True)
    maker, ext = {"png": (make_png, ".png"), "jpg": (make_jpeg, ".jpg"), "webp": (make_webp, ".webp")}[kind]
    names = []
    for i in range(start, start + n):
        (folder / f"img_{i:03d}{ext}").write_bytes(maker(seed=i))
        names.append(f"img_{i:03d}{ext}")
    return names


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="cf-ip-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_validates_by_content_not_extension(self):
        (self.d / "a.png").write_bytes(make_png())
        (self.d / "b.jpg").write_bytes(make_jpeg())
        (self.d / "c.jpeg").write_bytes(make_jpeg(seed=1))
        (self.d / "d.webp").write_bytes(make_webp())
        (self.d / "fake.jpg").write_text("đây là văn bản, không phải ảnh " * 5, encoding="utf-8")
        (self.d / "renamed.png").write_bytes(make_jpeg(seed=2))                                  # JPEG đổi đuôi .png vẫn dùng được (theo nội dung)
        (self.d / "cut.jpg").write_bytes(make_jpeg()[:12] + b"\x00" * 40)                          # hỏng
        (self.d / "tiny.png").write_bytes(make_png(10, 10))
        (self.d / "notes.txt").write_text("x")
        (self.d / ".hidden.png").write_bytes(make_png(seed=9))
        res = IP.scan(self.d)
        by = {r["rel"]: r for r in res["files"]}
        self.assertEqual(IP.valid_rels(res), ["a.png", "b.jpg", "c.jpeg", "d.webp", "renamed.png"])
        self.assertEqual((res["valid"], res["invalid"], res["other_files"]), (5, 3, 1))
        self.assertEqual((by["a.png"]["width"], by["a.png"]["height"], by["a.png"]["format"]), (800, 600, "png"))
        self.assertEqual(by["d.webp"]["format"], "webp")
        self.assertEqual(by["renamed.png"]["format"], "jpeg")
        self.assertIn("JPEG/PNG/WebP", by["fake.jpg"]["problem"])
        self.assertIn("quá nhỏ", by["tiny.png"]["problem"])
        self.assertNotIn(".hidden.png", by)
        self.assertTrue(any("không hợp lệ" in w for w in res["warnings"]))

    def test_warns_when_small_and_when_empty_or_missing(self):
        (self.d / "s.png").write_bytes(make_png(300, 300))
        self.assertTrue(any("nhỏ hơn" in w for w in IP.scan(self.d)["warnings"]))
        e = IP.scan(self.d / "khong_co")
        self.assertEqual((e["exists"], e["valid"]), (False, 0))
        self.assertTrue(e["warnings"])
        empty = self.d / "empty"
        empty.mkdir()
        self.assertTrue(any("Chưa có ảnh hợp lệ" in w for w in IP.scan(empty)["warnings"]))

    def test_depth_limit_and_symlinks_are_not_followed(self):
        (self.d / "a").mkdir()
        (self.d / "a" / "b").mkdir()
        (self.d / "a" / "b" / "c").mkdir()
        (self.d / "top.png").write_bytes(make_png())
        (self.d / "a" / "x.png").write_bytes(make_png(seed=1))
        (self.d / "a" / "b" / "y.png").write_bytes(make_png(seed=2))
        (self.d / "a" / "b" / "c" / "deep.png").write_bytes(make_png(seed=3))
        outside = Path(tempfile.mkdtemp(prefix="cf-out-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "secret.png").write_bytes(make_png(seed=4))
        linked = True
        try:
            os.symlink(outside / "secret.png", self.d / "link.png")
            os.symlink(outside, self.d / "dirlink", target_is_directory=True)
        except (OSError, NotImplementedError):
            linked = False
        res = IP.scan(self.d)
        self.assertEqual(IP.valid_rels(res), ["a/b/y.png", "a/x.png", "top.png"])                 # sâu hơn 2 cấp: bỏ qua
        if linked:
            self.assertNotIn("link.png", [r["rel"] for r in res["files"]])
            self.assertTrue(any("symlink" in w for w in res["warnings"]))


class PickTest(unittest.TestCase):
    RELS = [f"i{n:02d}.png" for n in range(10)]

    def draw(self, mode, n, rels=None, seed=1, state=None, avoid=()):
        rng, out = random.Random(seed), []
        for _ in range(n):
            rel, state = IP.pick(state, rels or self.RELS, mode, rng, avoid)
            out.append(rel)
        return out, state

    def test_shuffle_never_repeats_until_the_pool_is_exhausted_then_reshuffles(self):
        for seed in range(30):
            out, _ = self.draw("shuffle", 30, seed=seed)
            for c in range(3):
                self.assertEqual(sorted(out[c * 10:(c + 1) * 10]), self.RELS, f"seed {seed} cycle {c}")      # mỗi vòng dùng đủ từng ảnh đúng một lần
            self.assertNotEqual(out[9], out[10], f"seed {seed}: không lặp ngay ở ranh giới vòng")
            self.assertNotEqual(out[19], out[20])
        self.assertNotEqual(self.draw("shuffle", 10, seed=1)[0], self.draw("shuffle", 10, seed=2)[0])         # có xáo thật, không cố định thứ tự

    def test_shuffle_state_survives_restart_and_tracks_folder_changes(self):
        first, st = self.draw("shuffle", 4, seed=3)
        st = json.loads(json.dumps(st))                                                          # lưu bền = JSON
        rest, st = self.draw("shuffle", 6, seed=4, state=st)
        self.assertEqual(sorted(first + rest), self.RELS)                                        # nối tiếp đúng vòng, không lặp
        _, st = self.draw("shuffle", 3, seed=5)
        grown = self.RELS + ["new.png"]
        more, _ = self.draw("shuffle", 8, rels=grown, seed=6, state=st)
        self.assertEqual(len(set(more)), 8)                                                      # ảnh mới chen vào vòng hiện tại, không ai bị lặp
        self.assertIn("new.png", more)
        _, st = self.draw("shuffle", 3, seed=7)
        shrunk = [r for r in self.RELS if r != "i05.png"]
        more, _ = self.draw("shuffle", 9, rels=shrunk, seed=8, state=st)
        self.assertNotIn("i05.png", more)                                                        # ảnh đã xoá không bao giờ được chọn

    def test_sequential_is_stable_and_wraps(self):
        out, st = self.draw("sequential", 12)
        self.assertEqual(out, (self.RELS * 2)[:12])
        out2, _ = self.draw("sequential", 4, state={"last": "i03.png"}, rels=[r for r in self.RELS if r != "i04.png"])
        self.assertEqual(out2, ["i05.png", "i06.png", "i07.png", "i08.png"])                    # ảnh kế tiếp theo tên, kể cả khi ảnh "tiếp theo" đã bị xoá
        self.assertEqual(self.draw("sequential", 5, seed=1)[0], self.draw("sequential", 5, seed=99)[0])      # không phụ thuộc rng

    def test_random_may_repeat_and_respects_avoid(self):
        out, _ = self.draw("random", 200)
        self.assertGreater(len(out), len(set(out)))
        self.assertEqual(set(out), set(self.RELS))
        out, _ = self.draw("random", 60, avoid={"i00.png", "i01.png"})
        self.assertFalse({"i00.png", "i01.png"} & set(out))

    def test_avoid_is_ignored_when_it_is_the_only_choice(self):
        for mode in IP.MODES:
            rel, _ = IP.pick(None, ["only.png"], mode, random.Random(1), avoid={"only.png"})
            self.assertEqual(rel, "only.png")

    def test_errors(self):
        with self.assertRaises(StageError) as cm:
            IP.pick(None, [], "shuffle", random.Random(1))
        self.assertEqual(cm.exception.code, "IMAGE_POOL_EMPTY")
        with self.assertRaises(StageError) as cm:
            IP.pick(None, ["a.png"], "bogus", random.Random(1))
        self.assertEqual(cm.exception.code, "BAD_SELECTION_MODE")


class PoolCase(RootCase):
    """Root tạm + thư mục pool + kênh dùng pool + Orchestrator (adapter giả)."""

    def setUp(self):
        super().setUp()
        self.pool = Path(tempfile.mkdtemp(prefix="cf-pool-"))
        self.addCleanup(shutil.rmtree, self.pool, True)

    def setup_pool(self, n=6, mode="shuffle", kind="png", channel="kenh_p"):
        fill(self.pool, n, kind)
        write_config(self.root, image_pools={"anime": {"folder": str(self.pool), "selection_mode": mode}})
        write_channel(self.root, channel, {"name": "Kênh P", "publishing": {"made_for_kids": False}, "thumbnail": {"image_pool": "anime", "selection_mode": mode}})
        return self.orc()

    def src(self, orc, jid) -> dict:
        return orc.store.get_job(jid)["params"]["thumbnail_source"]

    def thumb_text(self, orc, jid) -> str:
        a = next(a for a in orc.store.artifacts(jid) if a["kind"] == "thumbnail")
        return (self.job_dir(jid) / a["path"]).read_text(encoding="utf-8")


class StoreTest(PoolCase):
    def test_concurrent_draws_never_hand_out_the_same_image_twice_per_cycle(self):
        orc = self.setup_pool(40)
        got, lock = [], threading.Lock()

        def worker():
            mine = [orc.image_pools.draw("anime") for _ in range(10)]
            with lock:
                got.extend(mine)
        ts = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual((len(got), len(set(got))), (40, 40))                                    # BEGIN IMMEDIATE: nguyên tử giữa luồng/job

    def test_bag_state_persists_across_restart(self):
        orc = self.setup_pool(10)
        first = [orc.image_pools.draw("anime") for _ in range(4)]
        orc2 = self.orc()                                                                        # "khởi động lại": object mới, cùng DB
        rest = [orc2.image_pools.draw("anime") for _ in range(6)]
        self.assertEqual(sorted(first + rest), sorted(IP.valid_rels(IP.scan(self.pool))))

    def test_v6_migration_adds_table_and_is_idempotent(self):
        db = self.root / "v5.db"
        st = JobStore(db)
        jid = st.create_job({"channel": "k", "input": {"kind": "youtube_url", "value": "u"}})
        c = sqlite3.connect(db)
        c.execute("DROP TABLE image_pool_state")
        c.execute("PRAGMA user_version=5")
        c.commit()
        c.close()
        st2 = JobStore(db)
        self.assertEqual(st2.schema_version(), SCHEMA_VERSION)
        self.assertEqual(st2.image_pool_update("p", lambda s: ("ok", {"n": 1})), "ok")
        self.assertEqual(st2.image_pool_update("p", lambda s: (s, s)), {"n": 1})
        self.assertIsNotNone(st2.get_job(jid))
        self.assertEqual(JobStore(db).schema_version(), SCHEMA_VERSION)


class MaterializeTest(PoolCase):
    def test_copies_into_job_with_sha_and_rejects_escapes(self):
        fill(self.pool, 2)
        dest = self.root / "job" / "inputs" / "thumbnail"
        snap = IP.materialize(self.pool, "img_000.png", dest)
        f = self.root / "job" / snap["file"]
        self.assertTrue(f.is_file())
        self.assertEqual(snap["sha256"], IP.sha256_file(f))
        self.assertEqual(snap["file"], f"inputs/thumbnail/thumbnail_{snap['sha256'][:12]}.png")
        self.assertEqual(list(dest.glob(".thumb_*")), [])                                        # không để file tạm
        for bad in ("../x.png", "nope.png"):
            with self.assertRaises((ValueError, OSError)):
                IP.materialize(self.pool, bad, dest)
        (self.pool / "text.png").write_text("không phải ảnh " * 10, encoding="utf-8")
        with self.assertRaises(ValueError):
            IP.materialize(self.pool, "text.png", dest)
        self.assertEqual(list(dest.glob(".thumb_*")), [])                                        # lỗi cũng không để lại file tạm

    def test_resolve_source_is_deterministic_when_missing_or_changed(self):
        fill(self.pool, 1)
        jd = self.root / "job"
        snap = IP.materialize(self.pool, "img_000.png", jd / "inputs" / "thumbnail")
        self.assertTrue(IP.resolve_source(jd, snap).is_file())
        (jd / snap["file"]).write_bytes(b"x" * 100)
        with self.assertRaises(StageError) as cm:
            IP.resolve_source(jd, snap)
        self.assertEqual(cm.exception.code, "THUMBNAIL_SOURCE_CHANGED")
        (jd / snap["file"]).unlink()
        with self.assertRaises(StageError) as cm:
            IP.resolve_source(jd, snap)
        self.assertEqual((cm.exception.code, bool(cm.exception.detail.get("hint"))), ("THUMBNAIL_SOURCE_MISSING", True))


class JobTest(PoolCase):
    def test_job_snapshots_one_image_and_retry_uses_the_same_one(self):
        orc = self.setup_pool(6)
        jid = orc.submit(params(channel="kenh_p", fake={"render_youtube": {"fail_until_attempt": 1}}), pipeline=spec("render_youtube"))   # lần dựng đầu lỗi tạm thời -> retry
        s = self.src(orc, jid)
        self.assertEqual((s["pool"], s["selection_mode"], s["rerolls"]), ("anime", "shuffle", 0))
        self.assertTrue((self.job_dir(jid) / s["file"]).is_file())
        self.assertTrue(any(d["what"] == "thumbnail.image" for d in orc.store.get_job(jid)["params"]["auto"]))      # giải thích vì sao ảnh này
        shutil.rmtree(self.pool)                                                                 # thư mục nguồn biến mất SAU khi chốt: job vẫn chạy được
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.BY_NAME["render_youtube"].done_state)
        self.assertEqual(self.src(orc, jid), s)                                                  # retry/restart không chọn lại
        self.assertIn(f"img={s['sha256'][:12]}", self.thumb_text(orc, jid))                      # đúng ảnh đã chốt được dựng vào thumbnail
        self.assertTrue(IP.resolve_source(self.job_dir(jid), s).is_file())

    def test_batch_of_jobs_gets_distinct_images_while_pool_is_large_enough(self):
        orc = self.setup_pool(8)
        shas = [self.src(orc, orc.submit(params(channel="kenh_p"), pipeline=spec("render_youtube")))["sha256"] for _ in range(8)]
        self.assertEqual(len(set(shas)), 8)
        nxt = self.src(orc, orc.submit(params(channel="kenh_p"), pipeline=spec("render_youtube")))["sha256"]
        self.assertIn(nxt, shas)                                                                 # hết vòng thì xáo lại (pool chỉ có 8 ảnh)

    def test_sequential_mode_follows_file_order_across_jobs(self):
        orc = self.setup_pool(4, mode="sequential")
        rels = [self.src(orc, orc.submit(params(channel="kenh_p"), pipeline=spec("render_youtube")))["source_relpath"] for _ in range(6)]
        self.assertEqual(rels, ["img_000.png", "img_001.png", "img_002.png", "img_003.png", "img_000.png", "img_001.png"])

    def test_no_thumbnail_branch_means_no_pool_draw(self):
        orc = self.setup_pool(3)
        jid = orc.submit(params(channel="kenh_p"), pipeline=spec("render_tiktok"))
        self.assertNotIn("thumbnail_source", orc.store.get_job(jid)["params"])
        self.assertEqual(orc.store.image_pool_update("anime", lambda s: (s, s)), None)           # túi chưa bị đụng tới

    def test_channel_without_pool_is_unchanged(self):
        write_channel(self.root, "kenh_plain", {"name": "Plain", "publishing": {"made_for_kids": False}})
        orc = self.orc()
        jid = orc.submit(params(channel="kenh_plain"), pipeline=spec("render_youtube"))
        self.assertNotIn("thumbnail_source", orc.store.get_job(jid)["params"])
        orc.run()
        self.assertNotIn("img=", self.thumb_text(orc, jid))

    def test_unusable_pool_rejects_the_job_cleanly(self):
        orc = self.setup_pool(2)
        shutil.rmtree(self.pool)
        with self.assertRaises(StageError) as cm:
            orc.submit(params(channel="kenh_p"), pipeline=spec("render_youtube"))
        self.assertEqual(cm.exception.code, "IMAGE_POOL_EMPTY")
        self.assertEqual(orc.store.list_jobs(), [])                                              # không để lại job nửa vời
        write_channel(self.root, "kenh_x", {"name": "X", "thumbnail": {"image_pool": "khong_co"}})
        with self.assertRaises(StageError) as cm:
            orc.submit(params(channel="kenh_x"), pipeline=spec("render_youtube"))
        self.assertEqual(cm.exception.code, "IMAGE_POOL_NOT_FOUND")

    def test_invalid_channel_thumbnail_config_is_rejected(self):
        write_channel(self.root, "kenh_bad", {"name": "B", "thumbnail": {"image_pool": 5, "selection_mode": "xyz"}})
        orc = self.orc()
        with self.assertRaises(StageError) as cm:
            orc.submit(params(channel="kenh_bad"), pipeline=spec("render_youtube"))
        self.assertEqual(cm.exception.code, "INVALID_CHANNEL_CONFIG")
        self.assertEqual(len(cm.exception.detail["errors"]), 2)

    def test_extending_target_to_render_picks_the_image_then(self):
        orc = self.setup_pool(3)
        jid = orc.submit(params(channel="kenh_p"), mode="THROUGH_TTS")
        self.assertNotIn("thumbnail_source", orc.store.get_job(jid)["params"])
        orc.run()
        orc.set_target(jid, "render_youtube")
        self.assertIn("thumbnail_source", orc.store.get_job(jid)["params"])
        orc.run()
        self.assertIn("img=", self.thumb_text(orc, jid))

    def test_clone_keeps_the_same_image(self):
        orc = self.setup_pool(5)
        jid = orc.submit(params(channel="kenh_p"), pipeline=spec("render_youtube"))
        orc.run()
        new = orc.clone_job(jid)
        a, b = self.src(orc, jid), self.src(orc, new)
        self.assertEqual((a["sha256"], a["file"]), (b["sha256"], b["file"]))
        self.assertTrue((self.job_dir(new) / b["file"]).is_file())                               # đã chép sang workspace job mới
        orc.run()
        self.assertEqual(orc.store.get_job(new)["state"], orc.store.get_job(jid)["state"])


class RerollTest(PoolCase):
    def paused_after_youtube(self, orc):
        jid = orc.submit(params(channel="kenh_p", fake=sleeper("render_tiktok", 20)), pipeline=spec("render_youtube", "render_tiktok", "output"))
        with Loop(orc):
            wait_until(lambda: orc.store.get_job(jid)["state"] == P.TIKTOK_RENDERING, 40, "render_tiktok started")
            orc.pause_job(jid)
            wait_until(lambda: orc.store.get_job(jid)["state"] == P.BY_NAME["render_tiktok"].queue_state and orc.store.get_job(jid)["lease_owner"] is None, 20, "released")
        return jid

    def test_reroll_reruns_only_youtube_render_and_output(self):
        orc = self.setup_pool(6)
        jid = self.paused_after_youtube(orc)
        old = self.src(orc, jid)
        before = {k: list(v) for k, v in self.runs(orc, jid).items()}
        r = orc.reroll_thumbnail(jid)
        self.assertEqual(r["status"], "applied")                                                 # tạm dừng = điểm an toàn
        new = self.src(orc, jid)
        self.assertNotEqual(new["sha256"], old["sha256"])
        self.assertEqual(new["rerolls"], 1)
        act = {s["id"]: s["action"] for s in r["impact"]["stages"]}
        self.assertEqual((act["source"], act["story"], act["tts"], act["audio"]), ("KEEP", "KEEP", "KEEP", "KEEP"))
        self.assertEqual((act["render_youtube"], act["output"]), ("RERUN", "RUN"))
        self.assertEqual(act["render_tiktok"], "RUN")                                            # chưa chạy xong: vẫn chạy, KHÔNG bị làm lại
        orc.resume(jid)
        with Loop(orc):
            wait_until(lambda: P.is_complete(orc.store.get_job(jid)["state"], orc.store.get_job(jid)["target_idx"]), 60, "job done")
        after = self.runs(orc, jid)
        for s in ("source", "story", "tts", "audio"):
            self.assertEqual(after[s], before[s], f"{s} không được chạy lại")
        self.assertEqual(after["render_youtube"], before["render_youtube"] + ["succeeded"])
        self.assertIn(f"img={new['sha256'][:12]}", self.thumb_text(orc, jid))
        rep = json.loads(next((self.job_dir(jid) / "render" / "youtube").glob("render_report.json")).read_text(encoding="utf-8"))
        self.assertEqual(rep["outputs"]["video"]["state"], "reused")                             # video không dựng lại, chỉ thumbnail
        self.assertEqual(rep["outputs"]["thumbnail"]["state"], "done")
        self.assertTrue((self.job_dir(jid) / old["file"]).is_file())                             # ảnh cũ giữ lại làm dấu vết (không mất dữ liệu)

    def test_reroll_is_blocked_for_finished_jobs_without_drawing_an_image(self):
        orc = self.setup_pool(4)
        jid = orc.submit(params(channel="kenh_p"), pipeline=spec("render_youtube", "output"))
        orc.run()
        state = orc.store.image_pool_update("anime", lambda s: (s, s))
        with self.assertRaises(StageError) as cm:
            orc.reroll_thumbnail(jid)
        self.assertTrue(cm.exception.detail.get("clone_suggested"))
        self.assertEqual(orc.store.image_pool_update("anime", lambda s: (s, s)), state)          # chưa rút ảnh nào
        self.assertEqual(self.src(orc, jid)["rerolls"], 0)

    def test_reroll_needs_a_pool_and_double_click_keeps_one_pending_change(self):
        write_channel(self.root, "kenh_plain", {"name": "Plain", "publishing": {"made_for_kids": False}})
        orc = self.orc()
        jid = orc.submit(params(channel="kenh_plain"), pipeline=spec("render_youtube"))
        with self.assertRaises(StageError):
            orc.reroll_thumbnail(jid)
        orc = self.setup_pool(5)
        jid = orc.submit(params(channel="kenh_p", fake=sleeper("render_youtube", 20)), pipeline=spec("render_youtube"))
        with Loop(orc):
            wait_until(lambda: orc.store.get_job(jid)["state"] == P.YOUTUBE_RENDERING, 40, "render running")
            a = orc.reroll_thumbnail(jid)
            b = orc.reroll_thumbnail(jid)
            self.assertEqual((a["status"], b["status"], b.get("already")), ("pending", "pending", True))
            self.assertEqual(a["thumbnail_source"]["sha256"], b["thumbnail_source"]["sha256"])      # bấm đúp: cùng MỘT thay đổi
            self.assertEqual(len(orc.store.revisions(jid)), 1)


@unittest.skipUnless(HAVE_REAL, "cần CF_TEST_CONTENTFLOW_PYTHON (Python có Pillow), ffmpeg/ffprobe và module ContentFlow có hệ thống template")
class RealRenderTest(RootCase):
    """Ảnh chốt từ pool đi tới renderer THẬT của ContentFlow: đổi ảnh thì thumbnail đổi, cùng ảnh thì thumbnail giống hệt (ổn định khi retry)."""

    def test_pool_image_reaches_the_real_thumbnail_renderer(self):
        from PIL import Image
        cfg = self.root / "config" / "config.json"
        c = json.loads(cfg.read_text(encoding="utf-8"))
        c.update({"adapters": {"render": "contentflow"}, "tools": {"contentflow": {"root": str(REPO / "modules" / "ContentFlow"), "python": REAL_PY,
                                                                                    "base_dir": str(self.root / "cfbase"), "user_root": str(self.root / "cf_user")}}})
        cfg.write_text(json.dumps(c), encoding="utf-8")
        orc = Orchestrator(load_config(self.root))
        pool = self.root / "pool"
        pool.mkdir()
        for name, col in (("a.png", (230, 40, 40)), ("b.png", (40, 60, 230))):
            im = Image.new("RGB", (900, 1100), col)
            for x in range(0, 900, 60):
                im.paste((255, 255, 255), (x, 0, x + 20, 1100))
            im.save(pool / name)
        render = orc.adapters["render"]
        snap = render.templates.resolve(id="thumb_default", expect_type="thumbnail")
        ctx = type("C", (), {"cancel": None, "job_id": "j", "attempt": 1})()
        outs = {}
        for key, rel in (("a1", "a.png"), ("b", "b.png"), ("a2", "a.png")):
            src = IP.materialize(pool, rel, self.root / f"job_{key}" / "inputs" / "thumbnail")
            out = self.root / f"out_{key}" / "thumb.png"
            render.render_thumbnail({"title": "Tiêu đề thử", "channel_name": "Kênh", "output": out, "key": f"k_{key}", "template": snap,
                                     "image": str(self.root / f"job_{key}" / src["file"])}, ctx)
            outs[key] = out.read_bytes()
        self.assertNotEqual(outs["a1"], outs["b"])                                               # đổi ảnh nguồn => thumbnail đổi
        self.assertEqual(outs["a1"], outs["a2"])                                                 # cùng ảnh => cùng thumbnail (retry ổn định)


if __name__ == "__main__":
    unittest.main()
