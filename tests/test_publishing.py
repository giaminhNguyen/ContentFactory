"""Phase 6: Sequence Manager, Metadata Builder, Channel Config, OutputPublisher (gói cho người dùng, phiên bản), PublishAdapter yt_uploader và
stage publish (retry upload riêng, không render lại).

yt_uploader được thay bằng daemon GIẢ cùng API (tests/fake_yt_uploader.py). Daemon THẬT (Go) chỉ được kiểm khi đặt CF_TEST_YT_UPLOADER_EXE."""
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from contentfactory.contracts import CancelToken, ErrorClass, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.jobs.db import JobStore, SCHEMA, SCHEMA_VERSION
from contentfactory.jobs.sequences import SequenceManager
from contentfactory.orchestrator import channels as CH
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.runner import Orchestrator
from contentfactory.orchestrator.stages import StageContract
from contentfactory.output import metadata as MD
from contentfactory.output.publisher import BuiltinOutputPublisher
from contentfactory.publish.yt_uploader import YtUploaderPublish, map_job_error, next_quota_reset
from contentfactory.tts.autotune import make_ctx
from tests.fake_yt_uploader import FakeUploader
from tests.support import RootCase, params

YT_EXE = os.environ.get("CF_TEST_YT_UPLOADER_EXE")
TIKTOK = {"speed": 2.0, "target_part_sec": 0.8}                      # nhiều part từ audio của fake TTS
PROJECT = {"title": "Tôi Trùng Sinh Quyết Tâm Làm Hại Nữ Chính"}


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# =============================================================================== Sequence Manager
class SequenceManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-seq-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = JobStore(self.tmp / "x.db")
        self.seq = SequenceManager(self.store)

    def test_reserve_is_idempotent_and_continues_after_last_used(self):
        self.assertEqual(self.seq.reserve("kenh", "p1", last_used=26), 27)                  # nối tiếp số đã đăng thủ công
        self.assertEqual([self.seq.reserve("kenh", "p1", last_used=26) for _ in range(3)], [27, 27, 27])
        self.assertEqual(self.seq.reserve("kenh", "p2", last_used=26), 28)
        self.assertEqual(self.seq.reserve("khac", "p3"), 1)                                  # mỗi channel một dãy riêng
        self.assertEqual((self.seq.get("p1"), self.seq.get("none")), (27, None))

    def test_numbers_are_never_reissued_even_after_release(self):
        a, b = self.seq.reserve("k", "p1"), self.seq.reserve("k", "p2")
        self.assertTrue(self.seq.release("p2"))
        self.assertIsNone(self.seq.get("p2"))
        self.assertEqual(self.seq.reserve("k", "p3"), b + 1)                                 # khoảng trống được chấp nhận
        self.assertEqual(self.seq.reserve("k", "p2"), b + 2)                                 # cùng project reserve lại sau release: số mới
        self.assertFalse(self.seq.release("khong-co"))
        self.assertEqual(a, 1)

    def test_published_sequence_cannot_be_released(self):
        n = self.seq.reserve("k", "p1")
        self.seq.mark_published("p1")
        with self.assertRaises(ValueError):
            self.seq.release("p1")
        self.assertEqual((self.seq.get("p1"), self.seq.reserve("k", "p1")), (n, n))
        self.assertEqual({r["status"] for r in self.seq.list("k")}, {"published"})

    def test_concurrent_reservations_get_distinct_numbers(self):
        out = []
        ts = [threading.Thread(target=lambda i=i: out.append(self.seq.reserve("race", f"r{i}"))) for i in range(16)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(out), list(range(1, 17)))

    def test_v1_database_is_migrated_to_v2_keeping_data(self):
        db = self.tmp / "old.db"
        c = sqlite3.connect(db)
        c.executescript(SCHEMA)
        c.execute("INSERT INTO jobs(id,seq,created_at,updated_at,state,params) VALUES('000001',1,1,1,'NEW','{}')")
        c.commit()
        c.close()
        st = JobStore(db)
        self.assertEqual(st.schema_version(), SCHEMA_VERSION)
        self.assertEqual(st.get_job("000001")["state"], "NEW")
        self.assertEqual(SequenceManager(st).reserve("k", "p"), 1)
        self.assertEqual(JobStore(db).schema_version(), SCHEMA_VERSION)


# =============================================================================== Metadata Builder + Channel Config
class MetadataBuilderTest(unittest.TestCase):
    P = {"id": "j1", "title": "Tôi Trùng Sinh", "title_source": "user", "channel_id": "kenh", "language": "vi"}

    def channel(self, **kw):
        return MD.normalize_channel({"name": "Kênh Truyện", **kw}, "kenh")

    def test_default_title_and_description_follow_the_templates(self):
        m = MD.build(self.P, self.channel(), 27)
        self.assertEqual(m["youtube_title"], "[Full Audio][Kênh Truyện số 27] | Tôi Trùng Sinh")
        self.assertEqual(m["description"], "Tôi Trùng Sinh\n\nKênh Truyện")
        self.assertEqual((m["sequence"], m["warnings"]), (27, []))
        m2 = MD.build(self.P, self.channel(description_template="Nghe full: {project_title} (tập {sequence}) - {channel_name} {{ok}}"), 3)
        self.assertEqual(m2["description"], "Nghe full: Tôi Trùng Sinh (tập 3) - Kênh Truyện {ok}")      # {{ }} là dấu ngoặc

    def test_templates_are_strict(self):
        for bad in ("{unknown}", "{sequence:03d}", "{project_title!r}", "{", "{}"):
            with self.assertRaises(StageError, msg=bad) as e:
                MD.check_template(bad)
            self.assertEqual(e.exception.code, "INVALID_TEMPLATE")
        with self.assertRaises(StageError) as e:
            MD.normalize_channel({"title_template": "{x}"}, "k")                                       # lỗi lộ ra ngay lúc nạp Channel Config
        self.assertEqual((e.exception.code, e.exception.resource), ("INVALID_CHANNEL_CONFIG", "input"))

    def test_youtube_limits_are_errors_not_silent_truncation(self):
        long_title = {**self.P, "title": "T" * 120}
        with self.assertRaises(StageError) as e:
            MD.build(long_title, self.channel(), 1)
        self.assertEqual((e.exception.error_class, e.exception.code), (ErrorClass.POLICY, "TITLE_TOO_LONG"))
        self.assertEqual(long_title["title"], "T" * 120)                                               # project.title không bị đổi
        edge = {**self.P, "title": "T" * (100 - len("[Full Audio][Kênh Truyện số 1] | "))}
        self.assertEqual(len(MD.build(edge, self.channel(), 1)["youtube_title"]), 100)                 # đúng 100: hợp lệ
        with self.assertRaises(StageError) as e:
            MD.build(self.P, self.channel(description_template="é" * 2600), 1)                         # 2600 ký tự = 5200 byte
        self.assertEqual(e.exception.code, "DESCRIPTION_TOO_LONG")
        with self.assertRaises(StageError):
            MD.build(self.P, self.channel(title_template="a\n{project_title}"), 1)

    def test_source_default_title_is_flagged(self):
        m = MD.build({**self.P, "title_source": "source_default"}, self.channel(), 1)
        self.assertIn("project.title chưa được đặt", m["warnings"][0])

    def test_channel_config_validation(self):
        c = MD.normalize_channel(None, "x")
        self.assertEqual((c["name"], c["title_template"], c["sequence"]["last_used"]), ("x", MD.DEFAULT_TITLE_TEMPLATE, 0))
        for bad in ({"name": ""}, {"publishing": {"privacy": "secret"}}, {"publishing": {"tags": "a"}}, {"sequence": {"last_used": -1}},
                    {"thumbnail": "x"}, {"sequence": {"last_used": True}}):
            with self.assertRaises(StageError, msg=str(bad)):
                MD.normalize_channel(bad, "k")


class ChannelLoaderTest(RootCase):
    def cfg(self):
        return load_config(self.root)

    def write(self, cid="kenh", body=None, name="channel.json"):
        d = self.root / "channels" / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        return d

    def test_load_defaults_and_files(self):
        c = CH.load_channel(self.cfg(), "chua-co-file")
        self.assertEqual((c["name"], c["loaded_from"]), ("chua-co-file", None))
        d = self.write(body={"name": "Kênh A", "sequence": {"last_used": 26}, "watermark": "watermark.wav", "publishing": {"privacy": "unlisted"}})
        c = CH.load_channel(self.cfg(), "kenh")
        self.assertEqual((c["name"], c["sequence"]["last_used"], c["publishing"]["privacy"]), ("Kênh A", 26, "unlisted"))
        self.assertEqual(Path(c["watermark"]), d / "watermark.wav")                                      # tương đối theo thư mục kênh (HANDOFF §10)
        self.assertTrue(c["loaded_from"].endswith("channel.json"))

    def test_invalid_files_and_ids_are_rejected(self):
        self.write("hong", "{không phải json")
        with self.assertRaises(StageError) as e:
            CH.load_channel(self.cfg(), "hong")
        self.assertEqual(e.exception.code, "INVALID_CHANNEL_CONFIG")
        self.write("sai", {"description_template": "{bad}"})
        with self.assertRaises(StageError):
            CH.load_channel(self.cfg(), "sai")
        self.write("mang", "[1, 2]")
        with self.assertRaises(StageError):
            CH.load_channel(self.cfg(), "mang")
        for cid in ("../x", "a/b", "", ".."):
            with self.assertRaises(StageError) as e:
                CH.load_channel(self.cfg(), cid)
            self.assertEqual(e.exception.code, "INVALID_CHANNEL_ID")

    def test_bad_channel_rejects_the_job_at_submit(self):
        self.write("hong", "{x")
        orc = self.orc()
        with self.assertRaises(StageError):
            orc.submit(params(channel="hong"))
        self.assertEqual(orc.store.list_jobs(), [])                                                      # không tạo job nửa vời

    def test_channel_watermark_is_used_when_the_job_has_none(self):
        wm = self.root / "channels" / "kenh" / "watermark.wav"
        self.write(body={"name": "K", "watermark": "watermark.wav"})
        wm.write_bytes(b"RIFFfake")
        orc = self.orc()
        j = orc.store.get_job(orc.submit(params(channel="kenh")))
        self.assertEqual(Path(j["params"]["watermark"]), wm)
        j2 = orc.store.get_job(orc.submit(params(channel="kenh", watermark="/khac.wav")))
        self.assertEqual(j2["params"]["watermark"], "/khac.wav")                                         # job tự chỉ định thì ưu tiên
        wm.unlink()
        j3 = orc.store.get_job(orc.submit(params(channel="kenh")))
        self.assertNotIn("watermark", j3["params"])                                                      # file không tồn tại: không đặt


# =============================================================================== OutputPublisher
class OutputPublisherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-out-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws, self.out = self.tmp / "ws", self.tmp / "out"
        self.ws.mkdir()
        self.pub = BuiltinOutputPublisher({"name_template": "{date}_{slug}"})
        self.ctx = make_ctx(self.ws)

    def entry(self, rel: str, body: bytes, **kw) -> dict:
        p = self.ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        return {"path": p, "source": rel, "sha256": hashlib.sha256(body).hexdigest(), **kw}

    def req(self, title="Truyện Ma", video=b"VIDEO-1", parts=3, job="j1", **kw):
        r = {"job_id": job, "project": {"id": job, "title": title, "title_source": "user", "channel_id": "kenh", "channel_name": "Kênh A",
                                         "language": "vi", "sequence": 5},
             "youtube_title": f"[Full Audio 5] | {title}", "description": f"Mô tả {title}", "output_root": self.out,
             "story": self.entry("story/story.txt", "Đây là truyện.".encode()), "youtube_video": self.entry("render/youtube/video.mp4", video),
             "youtube_thumbnail": self.entry("render/youtube/thumbnail.jpg", b"JPG"),
             "tiktok_parts": [self.entry(f"render/tiktok/part_{i:02d}.mp4", f"PART-{i}".encode(), index=i, duration_sec=60.0 * i) for i in range(1, parts + 1)],
             "warnings": []}
        r.update(kw)
        return r

    @staticmethod
    def files(d: Path) -> list[str]:
        return sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())

    def test_layout_is_exactly_what_the_user_expects_and_nothing_else(self):
        res = self.pub.publish(self.req(parts=3), self.ctx)
        d = Path(res["project_dir"])
        self.assertEqual(self.files(d), ["README.txt", "project.json", "story.txt", "tiktok/part_01.mp4", "tiktok/part_02.mp4", "tiktok/part_03.mp4",
                                         "youtube/description.txt", "youtube/thumbnail.jpg", "youtube/title.txt", "youtube/video.mp4"])
        self.assertEqual((res["version"], res["reused"], res["supersedes"]), (1, False, None))
        self.assertEqual([p.name for p in self.out.iterdir()], [d.name])                                 # không còn .tmp-*, cache, chunk
        self.assertRegex(d.name, r"^\d{8}_truyen-ma$")
        self.assertEqual((d / "youtube" / "title.txt").read_text(encoding="utf-8"), "[Full Audio 5] | Truyện Ma\n")
        self.assertEqual((d / "story.txt").read_text(encoding="utf-8"), "Đây là truyện.")

    def test_project_json_points_at_the_right_artifacts(self):
        req = self.req(parts=4)
        d = Path(self.pub.publish(req, self.ctx)["project_dir"])
        pj = json.loads((d / "project.json").read_text(encoding="utf-8"))
        self.assertEqual((pj["job_id"], pj["version"], pj["project"]["title"], pj["project"]["sequence"]), ("j1", 1, "Truyện Ma", 5))
        self.assertEqual((pj["youtube"]["video"], pj["youtube"]["thumbnail"], pj["youtube"]["title"]), ("youtube/video.mp4", "youtube/thumbnail.jpg", "[Full Audio 5] | Truyện Ma"))
        by = {f["path"]: f for f in pj["files"]}
        for rel, f in by.items():
            self.assertTrue((d / rel).is_file(), rel)
            if "sha256" in f:
                self.assertEqual(f["sha256"], sha(d / rel), rel)                                         # sha256 ghi trong project.json = sha file thật trong gói
            if f.get("source"):
                src = self.ws / f["source"]["workspace_path"]
                self.assertEqual(sha(src), f["sha256"], rel)                                              # ...và = sha artifact nguồn trong workspace
        self.assertEqual([(x["index"], x["file"]) for x in pj["tiktok"]["parts"]], [(i, f"tiktok/part_{i:02d}.mp4") for i in range(1, 5)])
        self.assertEqual([x["duration_sec"] for x in pj["tiktok"]["parts"]], [60.0, 120.0, 180.0, 240.0])
        self.assertEqual(pj["story"]["sha256"], sha(d / "story.txt"))

    def test_readme_explains_the_folder_and_lists_parts_in_order(self):
        d = Path(self.pub.publish(self.req(parts=3, warnings=["project.title chưa được đặt"]), self.ctx)["project_dir"])
        r = (d / "README.txt").read_text(encoding="utf-8")
        for needle in ("Truyện Ma", "Kênh A", "Full Audio 5", "Phiên bản : 1", "youtube/video.mp4", "tiktok/part_*.mp4", "THỨ TỰ", "[Full Audio 5] | Truyện Ma",
                       "LƯU Ý", "project.title chưa được đặt", "thuộc về bạn"):
            self.assertIn(needle, r)
        listed = [ln.split()[0] for ln in r.splitlines() if ln.strip().startswith("part_")]
        self.assertEqual(listed, ["part_01.mp4", "part_02.mp4", "part_03.mp4"])
        self.assertIn("2:00", r)                                                                           # độ dài part 2 (120 s)

    def test_parts_are_ordered_and_zero_padded_even_with_many_parts(self):
        d = Path(self.pub.publish(self.req(parts=105), self.ctx)["project_dir"])
        names = sorted(p.name for p in (d / "tiktok").iterdir())
        self.assertEqual((len(names), names[0], names[-1]), (105, "part_001.mp4", "part_105.mp4"))         # sắp theo tên == sắp theo thứ tự
        self.assertEqual((d / "tiktok" / "part_007.mp4").read_bytes(), b"PART-7")
        idx = [x["index"] for x in json.loads((d / "project.json").read_text(encoding="utf-8"))["tiktok"]["parts"]]
        self.assertEqual(idx, list(range(1, 106)))

    def test_parts_given_out_of_order_are_still_numbered_by_index(self):
        r = self.req(parts=3)
        r["tiktok_parts"] = list(reversed(r["tiktok_parts"]))
        d = Path(self.pub.publish(r, self.ctx)["project_dir"])
        self.assertEqual([(d / "tiktok" / f"part_0{i}.mp4").read_bytes() for i in (1, 2, 3)], [b"PART-1", b"PART-2", b"PART-3"])

    def test_same_content_again_leaves_the_users_package_untouched(self):
        a = self.pub.publish(self.req(), self.ctx)
        d = Path(a["project_dir"])
        (d / "README.txt").write_text("người dùng đã sửa README này", encoding="utf-8")                 # người dùng sửa gói của mình
        marks = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in d.rglob("*") if p.is_file()}
        b = self.pub.publish(self.req(), self.ctx)
        self.assertEqual((b["project_dir"], b["version"], b["reused"]), (a["project_dir"], 1, True))
        self.assertEqual(marks, {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in d.rglob("*") if p.is_file()})     # không file nào bị đụng
        self.assertEqual((d / "README.txt").read_text(encoding="utf-8"), "người dùng đã sửa README này")
        self.assertEqual(len(list(self.out.iterdir())), 1)

    def test_rerender_creates_a_new_explicit_version_and_keeps_the_old_one(self):
        d1 = Path(self.pub.publish(self.req(video=b"VIDEO-1"), self.ctx)["project_dir"])
        before = {p: p.read_bytes() for p in d1.rglob("*") if p.is_file()}
        r2 = self.pub.publish(self.req(video=b"VIDEO-2-rerendered"), self.ctx)
        d2 = Path(r2["project_dir"])
        self.assertEqual((r2["version"], r2["reused"], r2["supersedes"]), (2, False, d1.name))
        self.assertEqual(d2.name, f"{d1.name}-v2")
        self.assertEqual({p: p.read_bytes() for p in d1.rglob("*") if p.is_file()}, before)                 # bản cũ nguyên vẹn từng byte
        self.assertEqual((d2 / "youtube" / "video.mp4").read_bytes(), b"VIDEO-2-rerendered")
        pj = json.loads((d2 / "project.json").read_text(encoding="utf-8"))
        self.assertEqual((pj["version"], pj["supersedes"]), (2, d1.name))
        self.assertIn("Phiên bản : 2", (d2 / "README.txt").read_text(encoding="utf-8"))
        self.assertIn(d1.name, (d2 / "README.txt").read_text(encoding="utf-8"))
        r3 = self.pub.publish(self.req(video=b"VIDEO-2-rerendered"), self.ctx)                              # chạy lại đúng bản 2: không sinh v3
        self.assertEqual((r3["version"], r3["reused"], r3["project_dir"]), (2, True, str(d2)))
        r4 = self.pub.publish(self.req(video=b"VIDEO-3"), self.ctx)
        self.assertEqual((r4["version"], Path(r4["project_dir"]).name), (3, f"{d1.name}-v3"))

    def test_changed_title_or_thumbnail_also_versions(self):
        d1 = Path(self.pub.publish(self.req(), self.ctx)["project_dir"])
        self.assertEqual(self.pub.publish(self.req(title="Tên Khác"), self.ctx)["version"], 2)
        r = self.req()
        r["youtube_thumbnail"] = self.entry("render/youtube/thumbnail.jpg", b"NEW-JPG")
        self.assertEqual(self.pub.publish(r, self.ctx)["version"], 3)
        self.assertEqual((d1 / "youtube" / "thumbnail.jpg").read_bytes(), b"JPG")

    def test_renamed_or_moved_packages_never_break_or_get_overwritten(self):
        d1 = Path(self.pub.publish(self.req(), self.ctx)["project_dir"])
        renamed = d1.with_name("Truyện Ma - bản đẹp")
        d1.rename(renamed)                                                                                 # người dùng đổi tên thư mục
        r = self.pub.publish(self.req(), self.ctx)
        self.assertEqual((r["reused"], r["project_dir"]), (True, str(renamed)))                           # vẫn nhận ra qua project.json.job_id
        moved = self.tmp / "ngoai"
        shutil.move(str(renamed), str(moved))                                                              # chuyển hẳn ra ngoài output/
        r2 = self.pub.publish(self.req(), self.ctx)
        self.assertEqual((r2["version"], r2["reused"]), (1, False))                                        # không crash: dựng gói mới
        self.assertTrue((moved / "project.json").is_file())                                                # bản người dùng đã chuyển đi không bị đụng
        shutil.rmtree(r2["project_dir"])
        self.assertEqual(self.pub.publish(self.req(), self.ctx)["version"], 1)                              # xóa hết: dựng lại, không lỗi

    def test_name_collision_with_a_foreign_folder_gets_a_suffix(self):
        first = Path(self.pub.publish(self.req(job="a"), self.ctx)["project_dir"])
        second = Path(self.pub.publish(self.req(job="b"), self.ctx)["project_dir"])                         # job khác, cùng tiêu đề, cùng ngày
        self.assertNotEqual(first, second)
        self.assertEqual(second.name, first.name + "-2")

    def test_copy_is_verified_against_the_artifact_hash(self):
        r = self.req()
        r["youtube_video"]["sha256"] = "0" * 64                                                            # artifact nguồn đã đổi so với lúc niêm phong
        with self.assertRaises(StageError) as e:
            self.pub.publish(r, self.ctx)
        self.assertEqual((e.exception.error_class, e.exception.code), (ErrorClass.TRANSIENT, "COPY_VERIFY_FAILED"))
        self.assertEqual(list(self.out.iterdir()), [])                                                      # không để lại gói nửa vời hay .tmp

    def test_leftover_temp_dir_from_a_crash_is_cleaned(self):
        (self.out / ".tmp-j1" / "youtube").mkdir(parents=True)
        (self.out / ".tmp-j1" / "youtube" / "video.mp4").write_bytes(b"CUT")
        d = Path(self.pub.publish(self.req(), self.ctx)["project_dir"])
        self.assertFalse((self.out / ".tmp-j1").exists())
        self.assertEqual((d / "youtube" / "video.mp4").read_bytes(), b"VIDEO-1")


# =============================================================================== yt_uploader adapter (daemon giả)
class UploaderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-yt-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.srv = FakeUploader()
        self.addCleanup(self.srv.stop)
        self.video = self.tmp / "ws" / "render" / "youtube" / "video.mp4"
        self.video.parent.mkdir(parents=True)
        self.video.write_bytes(b"V" * 5000)
        self.thumb = self.video.with_name("thumbnail.jpg")
        self.thumb.write_bytes(b"J" * 500)
        self.ctx = make_ctx(self.tmp / "ws")
        self.ctx.cancel = CancelToken()

    def up(self, **kw) -> YtUploaderPublish:
        return YtUploaderPublish({"url": self.srv.url, "token": FakeUploader.TOKEN, "poll_s": 0.01, "max_wait_s": 30, "http_timeout_s": 5, **kw})

    def req(self, key="key-1", **kw) -> dict:
        return {"platform": "youtube", "video": self.video, "thumbnail": self.thumb, "title": "[Full Audio 5] | Truyện Ma", "description": "Mô tả",
                "tags": ["truyen"], "privacy": "unlisted", "made_for_kids": False, "account_id": None, "idempotency_key": key, "category": None,
                "playlists": [], **kw}


class YtUploaderAdapterTest(UploaderCase):
    def test_publish_sends_the_final_video_thumbnail_and_metadata(self):
        res = self.up().publish(self.req(), self.ctx)
        self.assertEqual((res["state"], res["remote_id"], res["remote_url"]), ("completed", "vid1", "https://www.youtube.com/watch?v=vid1"))
        [post] = [b for m, p, b in self.srv.requests if m == "POST" and p.endswith("/jobs")]
        self.assertEqual(Path(post["file_path"]), self.video.resolve())
        self.assertEqual(Path(post["thumbnail_path"]), self.thumb.resolve())
        self.assertEqual((post["title"], post["description"], post["privacy"], post["made_for_kids"], post["idempotency_key"]),
                         ("[Full Audio 5] | Truyện Ma", "Mô tả", "unlisted", False, "key-1"))
        self.assertTrue(Path(post["file_path"]).is_absolute())                                           # daemon chạy ở máy khác cwd: luôn đường dẫn tuyệt đối

    def test_same_key_never_creates_a_second_video(self):
        a = self.up()
        r1 = a.publish(self.req(), self.ctx)
        r2 = self.up().publish(self.req(), self.ctx)                                                      # tiến trình mới, cùng key (sau crash/retry)
        self.assertEqual((r1["remote_id"], r2["remote_id"]), ("vid1", "vid1"))
        self.assertEqual((len(self.srv.jobs), self.srv.creates()), (1, 1))
        self.assertEqual(self.srv.count("POST", "retry"), 0)
        self.assertEqual(a.find("key-1")["remote_id"], "vid1")
        self.assertIsNone(a.find("khong-co"))
        self.up().publish(self.req(key="key-2"), self.ctx)
        self.assertEqual(len(self.srv.jobs), 2)                                                           # key khác (nội dung khác) => job khác

    def test_completed_with_post_step_warning_is_still_success(self):
        self.srv.mode = "warn"
        res = self.up().publish(self.req(), self.ctx)
        self.assertEqual(res["state"], "completed")
        self.assertIn("invalid_metadata", res["warnings"][0])

    def test_failures_map_to_typed_errors(self):
        cases = [("quota_exceeded", ErrorClass.RESOURCE, "quota"), ("auth_revoked", ErrorClass.AUTH, "credential"),
                 ("auth_required", ErrorClass.AUTH, "credential"), ("network_error", ErrorClass.TRANSIENT, "network"),
                 ("rate_limited", ErrorClass.TRANSIENT, "provider"), ("invalid_metadata", ErrorClass.POLICY, None),
                 ("youtube_rejected", ErrorClass.POLICY, None), ("invalid_file", ErrorClass.POLICY, "input"),
                 ("database_error", ErrorClass.RESOURCE, "runtime"), ("AMBIGUOUS_UPLOAD", ErrorClass.AMBIGUOUS, None),
                 ("internal_error", ErrorClass.TRANSIENT, None)]
        for i, (code, cls, res) in enumerate(cases):
            self.srv.mode = f"fail:{code}"
            with self.assertRaises(StageError, msg=code) as e:
                self.up().publish(self.req(key=f"k{i}"), self.ctx)
            self.assertEqual((e.exception.error_class, e.exception.resource), (cls, res), code)

    def test_quota_hold_ends_at_the_next_daily_reset(self):
        self.srv.mode = "fail:quota_exceeded"
        with self.assertRaises(StageError) as e:
            self.up().publish(self.req(), self.ctx)
        t = e.exception.resume_after
        self.assertTrue(time.time() < t <= time.time() + 25 * 3600)
        d = datetime.fromtimestamp(next_quota_reset(datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)), timezone.utc)
        self.assertEqual((d.day, d.hour), (1, 8))
        d2 = datetime.fromtimestamp(next_quota_reset(datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)), timezone.utc)
        self.assertEqual(d2.day, 2)

    def test_retry_resumes_the_existing_job_instead_of_uploading_again(self):
        self.srv.mode = "fail:network_error"
        with self.assertRaises(StageError):
            self.up().publish(self.req(), self.ctx)
        self.srv.retry_mode = "ok"
        res = self.up().publish(self.req(), self.ctx)                                                     # lần chạy sau: tìm thấy job lỗi => retry (probe + resume)
        self.assertEqual(res["remote_id"], "vid1")
        self.assertEqual((len(self.srv.jobs), self.srv.creates(), self.srv.count("POST", "retry")), (1, 1, 1))

    def test_permanent_failures_are_not_retried_blindly(self):
        for code in ("invalid_metadata", "youtube_rejected", "AMBIGUOUS_UPLOAD", "invalid_file"):
            self.srv.mode = f"fail:{code}"
            with self.assertRaises(StageError):
                self.up().publish(self.req(key=f"p-{code}"), self.ctx)
            with self.assertRaises(StageError) as e:
                self.up().publish(self.req(key=f"p-{code}"), self.ctx)
            self.assertIn(code.upper() if code != "AMBIGUOUS_UPLOAD" else "AMBIGUOUS_UPLOAD", e.exception.code.upper())
        self.assertEqual(self.srv.count("POST", "retry"), 0)

    def test_paused_job_after_daemon_restart_is_resumed(self):
        self.srv.mode = "paused"
        self.srv.retry_mode = "ok"
        res = self.up().publish(self.req(), self.ctx)
        self.assertEqual(res["state"], "completed")
        self.assertEqual(self.srv.count("POST", "retry"), 1)

    def test_cancel_stops_waiting_but_leaves_the_remote_job_to_be_found_again(self):
        self.srv.mode = "slow"
        threading.Timer(0.3, self.ctx.cancel.set).start()
        with self.assertRaises(StageError) as e:
            self.up().publish(self.req(), self.ctx)
        self.assertEqual(e.exception.error_class, ErrorClass.CANCELLED)
        self.ctx.cancel = CancelToken()
        self.srv.release()
        res = self.up().publish(self.req(), self.ctx)                                                     # shutdown/restart: tìm lại đúng job, không tạo mới
        self.assertEqual((res["state"], len(self.srv.jobs), self.srv.creates()), ("completed", 1, 1))

    def test_daemon_down_bad_token_and_missing_features(self):
        port = self.srv.httpd.server_address[1]
        self.srv.stop()
        with self.assertRaises(StageError) as e:
            YtUploaderPublish({"url": f"http://127.0.0.1:{port}", "token": "t", "http_timeout_s": 2}).publish(self.req(), self.ctx)
        self.assertEqual((e.exception.error_class, e.exception.code, e.exception.resource), (ErrorClass.RESOURCE, "UPLOADER_UNREACHABLE", "runtime"))
        self.assertFalse(YtUploaderPublish({"url": f"http://127.0.0.1:{port}", "token": "t", "http_timeout_s": 2}).health()["ok"])
        srv = FakeUploader()
        self.addCleanup(srv.stop)
        with self.assertRaises(StageError) as e:
            YtUploaderPublish({"url": srv.url, "token": "sai-token"}).publish(self.req(), self.ctx)
        self.assertEqual((e.exception.error_class, e.exception.code, e.exception.resource), (ErrorClass.AUTH, "UPLOADER_UNAUTHORIZED", "credential"))
        self.assertTrue(YtUploaderPublish({"url": srv.url, "token": FakeUploader.TOKEN}).health()["ok"])
        srv.features = ["idempotency_key"]                                                                  # daemon cũ thiếu resume_probe
        h = YtUploaderPublish({"url": srv.url, "token": FakeUploader.TOKEN}).health()
        self.assertEqual((h["ok"], h["missing_features"]), (False, ["resume_probe"]))

    def test_token_is_read_from_the_daemon_data_dir(self):
        (self.tmp / "dd").mkdir()
        (self.tmp / "dd" / "api_token").write_text(FakeUploader.TOKEN + "\n", encoding="utf-8")
        a = YtUploaderPublish({"url": self.srv.url, "data_dir": str(self.tmp / "dd"), "poll_s": 0.01})
        self.assertEqual(a.publish(self.req(), self.ctx)["state"], "completed")
        with self.assertRaises(StageError) as e:
            YtUploaderPublish({"url": self.srv.url, "data_dir": str(self.tmp / "khong-co")}).publish(self.req(), self.ctx)
        self.assertEqual((e.exception.error_class, e.exception.code), (ErrorClass.AUTH, "UPLOADER_TOKEN_MISSING"))

    def test_daemon_validation_errors_are_mapped(self):
        with self.assertRaises(StageError) as e:
            self.up().publish(self.req(video=self.tmp / "khong-co.mp4"), self.ctx)
        self.assertEqual((e.exception.error_class, e.exception.code, e.exception.resource), (ErrorClass.POLICY, "UPLOAD_INVALID_FILE", "input"))
        with self.assertRaises(StageError) as e:
            self.up().publish(self.req(title="T" * 150, key="long"), self.ctx)
        self.assertEqual((e.exception.error_class, e.exception.code), (ErrorClass.POLICY, "INVALID_METADATA"))

    def test_job_error_mapping_function_is_total(self):
        self.assertEqual(map_job_error({"last_error": {"code": "weird", "message": "?"}}).error_class, ErrorClass.TRANSIENT)
        self.assertEqual(map_job_error({"error_class": "AMBIGUOUS_PUBLISH", "last_error": {"code": "x"}}).error_class, ErrorClass.AMBIGUOUS)
        self.assertEqual(map_job_error({}).error_class, ErrorClass.TRANSIENT)

    @unittest.skipUnless(shutil.which("ffmpeg"), "cần ffmpeg để nén thumbnail")
    def test_oversized_thumbnail_is_compressed_to_a_temp_copy_and_final_is_untouched(self):
        big = self.tmp / "ws" / "render" / "youtube" / "big.jpg"
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", "nullsrc=s=2600x2600,geq=random(1)*255:128:128", "-frames:v", "1",
                        "-q:v", "1", str(big)], check=True)
        self.assertGreater(big.stat().st_size, 2 * 1024 * 1024)
        before = sha(big)
        self.up().publish(self.req(thumbnail=big), self.ctx)
        [post] = [b for m, p, b in self.srv.requests if m == "POST" and p.endswith("/jobs")]
        sent = Path(post["thumbnail_path"])
        self.assertEqual(sent.name, "thumbnail_upload.jpg")
        self.assertLessEqual(sent.stat().st_size, 2 * 1024 * 1024)
        self.assertEqual(sha(big), before)                                                                  # thumbnail final không bị sửa

    def test_oversized_thumbnail_without_ffmpeg_is_a_resource_problem(self):
        big = self.video.with_name("big.jpg")
        big.write_bytes(b"x" * (2 * 1024 * 1024 + 10))
        with self.assertRaises(StageError) as e:
            self.up(ffmpeg=str(self.tmp / "khong-co-ffmpeg")).publish(self.req(thumbnail=big), self.ctx)
        self.assertEqual((e.exception.code, e.exception.resource), ("THUMBNAIL_TOO_LARGE", "runtime"))
        self.assertEqual(len(self.srv.jobs), 0)                                                             # chưa tạo job rác trên daemon


# =============================================================================== pipeline đầy đủ
class PublishingPipelineTest(RootCase):
    def setUp(self):
        super().setUp()
        self.srv = FakeUploader()
        self.addCleanup(self.srv.stop)
        cfg = self.root / "config" / "config.json"
        c = json.loads(cfg.read_text(encoding="utf-8"))
        c.update({"adapters": {"publish": "yt_uploader"},
                  "tools": {"yt_uploader": {"url": self.srv.url, "token": FakeUploader.TOKEN, "poll_s": 0.01, "max_wait_s": 30}}})
        cfg.write_text(json.dumps(c), encoding="utf-8")
        self.channel("kenh_a", {"name": "Kênh Truyện A", "sequence": {"last_used": 26}, "publishing": {"privacy": "unlisted", "tags": ["truyen", "audio"]}})

    def channel(self, cid: str, body: dict) -> None:
        d = self.root / "channels" / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / "channel.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    def submit(self, orc, **extra):
        kw = {"auto_resume": False}
        return orc.submit(params(channel="kenh_a", project=PROJECT, tiktok=TIKTOK, **extra), **kw)

    def pkg(self) -> Path:
        [d] = [d for d in (self.root / "output").iterdir() if not d.name.startswith(".")]
        return d

    def posts(self) -> list[dict]:
        return [b for m, p, b in self.srv.requests if m == "POST" and p.endswith("/jobs")]

    def render_calls(self, jid: str) -> dict[str, int]:
        out = {}
        for sub, name in (("youtube", "render_youtube"), ("tiktok", "render_tiktok")):
            f = self.job_dir(jid) / "render" / sub / "calls.log"
            out[sub] = len(f.read_text(encoding="utf-8").splitlines()) if f.exists() else 0
        return out

    def test_full_job_delivers_one_youtube_video_and_the_tiktok_parts(self):
        orc = self.orc()
        jid = self.submit(orc)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual(j["state"], P.PUBLISHED, j["last_error"])
        d = self.pkg()
        title = (d / "youtube" / "title.txt").read_text(encoding="utf-8").strip()
        self.assertEqual(title, f"[Full Audio][Kênh Truyện A số 27] | {PROJECT['title']}")                                 # last_used 26 => 27
        [post] = self.posts()
        self.assertEqual((post["title"], post["description"].strip()), (title, (d / "youtube" / "description.txt").read_text(encoding="utf-8").strip()))
        self.assertEqual((post["privacy"], post["tags"], post["made_for_kids"]), ("unlisted", ["truyen", "audio"], False))   # mặc định đăng từ Channel Config
        arts = {a["kind"]: a for a in orc.store.artifacts(jid) if a["kind"] in ("video_youtube", "thumbnail")}
        self.assertEqual(Path(post["file_path"]), (self.job_dir(jid) / arts["video_youtube"]["path"]).resolve())        # upload từ WORKSPACE, không phải output/
        self.assertEqual(Path(post["thumbnail_path"]), (self.job_dir(jid) / arts["thumbnail"]["path"]).resolve())
        self.assertEqual(sha(d / "youtube" / "video.mp4"), arts["video_youtube"]["sha256"])
        res = json.loads((self.job_dir(jid) / "publish" / "publish_result.json").read_text(encoding="utf-8"))
        self.assertEqual((res["remote_id"], res["sequence"], res["title"]), ("vid1", 27, title))
        self.assertEqual(orc.sequence.list("kenh_a")[0]["status"], "published")
        self.assertGreaterEqual(len(list((d / "tiktok").iterdir())), 3)

    def test_project_json_matches_the_artifacts_in_the_job_manifest(self):
        orc = self.orc()
        jid = self.submit(orc)
        orc.run()
        d = self.pkg()
        pj = json.loads((d / "project.json").read_text(encoding="utf-8"))
        parts = [a for a in orc.store.artifacts(jid) if a["kind"] == "video_tiktok"]
        parts.sort(key=lambda a: json.loads(a["meta"])["index"])
        self.assertEqual([x["file"] for x in pj["tiktok"]["parts"]], [f"tiktok/part_{i:02d}.mp4" for i in range(1, len(parts) + 1)])
        by = {f["path"]: f for f in pj["files"]}
        for i, a in enumerate(parts, 1):
            f = by[f"tiktok/part_{i:02d}.mp4"]
            self.assertEqual((f["sha256"], f["source"]["workspace_path"], f["index"]), (a["sha256"], a["path"], i))
            self.assertEqual(sha(d / f["path"]), a["sha256"])
        for kind, rel in (("video_youtube", "youtube/video.mp4"), ("thumbnail", "youtube/thumbnail.jpg"), ("story_text", "story.txt")):
            a = next(a for a in orc.store.artifacts(jid) if a["kind"] == kind)
            self.assertEqual((by[rel]["sha256"], by[rel]["source"]["workspace_path"]), (a["sha256"], a["path"]))
            self.assertTrue((self.job_dir(jid) / by[rel]["source"]["workspace_path"]).is_file())
        pm = json.loads((self.job_dir(jid) / "output" / "publish_metadata.json").read_text(encoding="utf-8"))
        self.assertEqual((pj["youtube"]["title"], pj["project"]["sequence"]), (pm["youtube_title"], pm["sequence"]))
        m = json.loads((self.job_dir(jid) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["stages"]["output"]["data"]["project_dir"], str(d))

    def test_upload_failure_keeps_the_rendered_video_and_retry_does_not_rerender(self):
        orc = self.orc()
        self.srv.mode = self.srv.retry_mode = "fail:network_error"
        jid = self.submit(orc)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["hold_reason"]), (P.UPLOAD_READY, P.PAUSED_NETWORK))               # giữ job, không FAILED, không mất gì
        self.assertEqual(self.runs(orc, jid)["publish"], ["failed", "failed", "held"])
        d = self.pkg()
        kept = {a["kind"]: self.job_dir(jid) / a["path"] for a in orc.store.artifacts(jid) if a["kind"] in ("video_youtube", "thumbnail")}
        self.assertTrue(all(p.is_file() and p.stat().st_size > 0 for p in kept.values()))                   # video + thumbnail còn nguyên trong workspace
        self.assertTrue((d / "youtube" / "video.mp4").is_file())                                            # và gói output đã giao
        renders, before = self.render_calls(jid), {k: sha(p) for k, p in kept.items()}
        self.assertEqual((len(self.srv.jobs), len(self.posts())), (1, 1))                                   # lần thử lại dùng job cũ, không tạo thêm video
        self.srv.retry_mode = "ok"
        orc.store.release_hold(jid)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        self.assertEqual(self.render_calls(jid), renders)                                                   # KHÔNG render lại
        self.assertEqual({k: sha(p) for k, p in kept.items()}, before)
        runs = self.runs(orc, jid)
        self.assertEqual((runs["render_youtube"], runs["render_tiktok"], runs["output"]), (["succeeded"], ["succeeded"], ["succeeded"]))
        self.assertEqual((len(self.srv.jobs), len(self.posts()), self.srv.jobs["job1"]["state"]), (1, 1, "completed"))
        self.assertEqual(len(list(self.pkg().iterdir())) >= 5, True)

    def test_manual_retry_of_a_failed_upload_runs_only_the_upload_stage(self):
        orc = self.orc()
        self.srv.mode = "fail:youtube_rejected"
        jid = self.submit(orc)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["failed_stage"], j["last_error"]["code"]), (P.FAILED, "publish", "YOUTUBE_REJECTED"))
        self.assertTrue((self.pkg() / "youtube" / "video.mp4").is_file())                                   # lỗi upload không làm mất gói đã giao
        orc.retry(jid)
        orc.run()
        self.assertEqual(self.runs(orc, jid)["render_youtube"], ["succeeded"])
        self.assertEqual(self.runs(orc, jid)["publish"][-1], "failed")                                      # cùng key + lỗi vĩnh viễn: không tự đăng lại mù quáng
        self.assertEqual(self.srv.count("POST", "retry"), 0)

    def test_quota_credential_and_daemon_down_hold_the_job(self):
        for mode, hold in (("fail:quota_exceeded", P.PAUSED_QUOTA), ("fail:auth_revoked", P.PAUSED_CREDENTIAL)):
            self.srv.mode = self.srv.retry_mode = mode
            orc = self.orc()
            jid = self.submit(orc)
            orc.run()
            j = orc.store.get_job(jid)
            self.assertEqual((j["hold_reason"], j["state"]), (hold, P.UPLOAD_READY), mode)
            if hold == P.PAUSED_QUOTA:
                self.assertGreater(j["resume_after"], time.time())                                           # chờ tới lúc reset quota, không đốt retry
            shutil.rmtree(self.root / "output")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cfg = self.root / "config" / "config.json"
        c = json.loads(cfg.read_text(encoding="utf-8"))
        c["tools"]["yt_uploader"]["url"] = f"http://127.0.0.1:{port}"
        cfg.write_text(json.dumps(c), encoding="utf-8")
        orc = self.orc()
        jid = self.submit(orc)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual((j["hold_reason"], j["state"]), (P.PAUSED_RESOURCE, P.UPLOAD_READY))
        late = FakeUploader(port)                                                                           # daemon lên sau
        self.addCleanup(late.stop)
        orc.store.release_hold(jid)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        self.assertEqual(len(late.jobs), 1)

    def test_output_folder_can_be_moved_or_deleted_without_breaking_the_pipeline(self):
        orc = self.orc()
        self.srv.mode = self.srv.retry_mode = "fail:network_error"
        jid = self.submit(orc)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["hold_reason"], P.PAUSED_NETWORK)
        elsewhere = self.root / "da-chep-di" / "ban-cua-toi"
        elsewhere.parent.mkdir()
        shutil.move(str(self.pkg()), str(elsewhere))                                                         # người dùng chuyển gói đi nơi khác
        self.assertEqual(list((self.root / "output").iterdir()), [])
        self.srv.retry_mode = "ok"
        orc.store.release_hold(jid)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)                                      # upload không phụ thuộc output/
        self.assertEqual(orc.store.get_job(jid)["last_error"], None)
        self.assertTrue((elsewhere / "project.json").is_file())
        self.assertEqual(list((self.root / "output").iterdir()), [])                                        # pipeline không tự dựng lại gói ở output/
        mf = json.loads((self.job_dir(jid) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(mf["state"], P.PUBLISHED)

    def test_rerunning_output_after_the_user_moved_the_package_does_not_crash(self):
        orc = self.orc()
        jid = self.submit(orc)
        orc.run()
        first = self.pkg()
        shutil.move(str(first), str(self.root / "luu-tru"))
        jid2 = orc.submit(params(channel="kenh_a", project=PROJECT, tiktok=TIKTOK), start_stage="output", target_stage="output",
                          from_job={"job_id": jid, "kinds": ["story_text", "metadata", "video_youtube", "thumbnail", "video_tiktok"]}, auto_resume=False)
        orc.run()
        self.assertEqual(orc.store.get_job(jid2)["state"], P.UPLOAD_READY)
        self.assertEqual(len(list((self.root / "output").iterdir())), 1)
        self.assertTrue((self.root / "luu-tru" / "project.json").is_file())

    def test_sequence_is_reserved_once_per_project_and_per_channel(self):
        orc = self.orc()
        self.channel("kenh_b", {"name": "Kênh B"})
        a, b = self.submit(orc), orc.submit(params(channel="kenh_b", project={"title": "Truyện B"}, tiktok=TIKTOK), auto_resume=False)
        c = self.submit(orc)
        orc.run()
        seqs = {s["project_id"]: (s["channel_id"], s["sequence"]) for s in orc.sequence.list()}
        self.assertEqual(seqs[a][0], "kenh_a")
        self.assertEqual(sorted([seqs[a][1], seqs[c][1]]), [27, 28])                                         # nối tiếp last_used = 26, hai job song song không trùng
        self.assertEqual(seqs[b], ("kenh_b", 1))
        titles = sorted(p["title"] for p in self.posts())
        self.assertEqual(len(set(titles)), 3)

    def test_retry_never_changes_the_sequence(self):
        orc = self.orc()
        self.srv.mode = self.srv.retry_mode = "fail:network_error"
        jid = self.submit(orc)
        orc.run()
        t1 = self.posts()[0]["title"]
        self.srv.retry_mode = "ok"
        orc.store.release_hold(jid)
        orc.run()
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual((orc.sequence.get(jid), t1), (27, f"[Full Audio][Kênh Truyện A số 27] | {PROJECT['title']}"))

    def test_source_default_title_warns_but_still_works(self):
        orc = self.orc()
        jid = orc.submit(params(channel="kenh_a", tiktok=TIKTOK), auto_resume=False)                        # không đặt project.title
        orc.run()
        pm = json.loads((self.job_dir(jid) / "output" / "publish_metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(pm["title_source"], "source_default")
        self.assertIn("project.title chưa được đặt", pm["warnings"][0])
        self.assertIn("project.title chưa được đặt", (self.pkg() / "README.txt").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((self.pkg() / "project.json").read_text(encoding="utf-8"))["project"]["title_source"], "source_default")

    def test_title_policy_require_blocks_before_anything_is_published(self):
        cfg = self.root / "config" / "config.json"
        c = json.loads(cfg.read_text(encoding="utf-8"))
        c["publishing"] = {"title_policy": "require"}
        cfg.write_text(json.dumps(c), encoding="utf-8")
        orc = self.orc()
        jid = orc.submit(params(channel="kenh_a", tiktok=TIKTOK), auto_resume=False)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual(j["hold_reason"], P.PAUSED_MISSING_INPUT)
        self.assertEqual((len(self.posts()), self.pkg_count()), (0, 0))

    def pkg_count(self) -> int:
        d = self.root / "output"
        return len(list(d.iterdir())) if d.exists() else 0

    def test_channel_config_is_snapshotted_per_job(self):
        orc = self.orc()
        jid = self.submit(orc)
        self.channel("kenh_a", {"name": "TÊN MỚI", "title_template": "MỚI {sequence} {project_title}", "sequence": {"last_used": 500}})   # sửa SAU khi tạo job
        orc.run()
        self.assertEqual(self.posts()[0]["title"], f"[Full Audio][Kênh Truyện A số 27] | {PROJECT['title']}")                  # job giữ nguyên cấu hình lúc bắt đầu
        j2 = orc.submit(params(channel="kenh_a", project={"title": "Truyện Mới"}, tiktok=TIKTOK), auto_resume=False)
        orc.run()
        self.assertEqual(orc.sequence.get(j2), 501)
        self.assertTrue(self.posts()[1]["title"].startswith("MỚI 501"))
        self.assertEqual(orc.store.get_job(jid)["config_snapshot"]["semantic"]["channel_config"]["name"], "Kênh Truyện A")

    def test_project_title_change_reruns_render_and_output_but_never_tts_or_audio(self):
        base = params(channel="kenh_a", project={"title": "A"})
        other = params(channel="kenh_a", project={"title": "B"})
        ins = {"audio_master": [{"sha256": "x", "path": "p", "kind": "k", "bytes": 1, "meta": {}}]}
        key = lambda stage, pr: StageContract(P.BY_NAME[stage]).stage_key(pr, None, ins)               # noqa: E731
        for stage in ("source", "story", "tts", "audio", "render_tiktok"):
            self.assertEqual(key(stage, base), key(stage, other), stage)
        for stage in ("render_youtube", "output"):
            self.assertNotEqual(key(stage, base), key(stage, other), stage)

    def test_thumbnail_uses_channel_name_and_project_title(self):
        orc = self.orc()
        jid = self.submit(orc)
        orc.run()
        thumb = (self.job_dir(jid) / "render" / "youtube" / "thumbnail.jpg").read_text(encoding="utf-8")
        self.assertIn("[Kênh Truyện A số 27]", thumb)                                                        # [tên kênh số tập], không phải id "kenh_a"
        self.assertIn(PROJECT["title"], thumb)

    def test_blank_title_uses_the_ai_story_title_never_the_source_title(self):
        orc = self.orc()
        seen = []
        orc.adapters["story"]._titler = lambda text, bundle, ctx, prefix, budget: seen.append((bundle["title"], prefix, budget)) or "Đêm Mưa Ở Làng"
        jid = orc.submit(params(channel="kenh_a", tiktok=TIKTOK), auto_resume=False)                       # không đặt tên truyện
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED, orc.store.get_job(jid)["last_error"])
        self.assertEqual(len(seen), 1)                                                                      # một lượt đặt tên, nhận tên gốc để tránh trùng
        self.assertEqual(seen[0][1:], ("[Full Audio][Kênh Truyện A số 999] | ", 100 - len("[Full Audio][Kênh Truyện A số 999] | ")))   # ngân sách theo mẫu kênh, dư 1 chữ số
        self.assertEqual(self.posts()[0]["title"], "[Full Audio][Kênh Truyện A số 27] | Đêm Mưa Ở Làng")
        thumb = (self.job_dir(jid) / "render" / "youtube" / "thumbnail.jpg").read_text(encoding="utf-8")
        self.assertIn("Đêm Mưa Ở Làng", thumb)
        pm = json.loads((self.pkg() / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(pm["project"]["title_source"], "story")
        orc2 = self.orc()
        orc2.adapters["story"]._titler = lambda *a: self.fail("đã đặt tên thì không gọi AI")
        j2 = orc2.submit(params(channel="kenh_a", project=PROJECT, tiktok=TIKTOK), auto_resume=False)
        orc2.run()
        self.assertTrue(self.posts()[-1]["title"].endswith(PROJECT["title"]))                             # tên người dùng đặt luôn thắng

    def test_status_cli_lists_upload_link(self):
        from contentfactory.orchestrator.cli import _print_links, _print_status
        import contextlib
        import io
        orc = self.orc()
        jid = self.submit(orc)
        orc.run()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_status(orc, jid)
            _print_links(orc, jid)
        self.assertIn("https://www.youtube.com/watch?v=vid1", buf.getvalue())


# =============================================================================== daemon THẬT (Go)
@unittest.skipUnless(YT_EXE and Path(YT_EXE).exists(), "cần CF_TEST_YT_UPLOADER_EXE (yt-uploader.exe đã build)")
class RealYtUploaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-realyt-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        exe = self.tmp / "yt-uploader.exe"
        shutil.copyfile(YT_EXE, exe)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.proc = subprocess.Popen([str(exe), "serve", "--headless", "--portable", "--no-open", "--port", str(self.port)], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop)
        a = YtUploaderPublish({"url": f"http://127.0.0.1:{self.port}", "data_dir": str(self.tmp / "data"), "poll_s": 0.05})
        for _ in range(100):
            if a.health().get("ok"):
                break
            time.sleep(0.1)
        self.a = a

    def _stop(self):
        self.proc.kill()
        self.proc.wait()

    def test_real_daemon_health_idempotency_lookup_and_auth_mapping(self):
        h = self.a.health()
        self.assertTrue(h["ok"], h)
        self.assertEqual(sorted(h["features"]), ["idempotency_key", "resume_probe"])
        self.assertIsNone(self.a.find("chua-ton-tai"))
        v = self.tmp / "v.mp4"
        v.write_bytes(b"x" * 2048)
        ctx = make_ctx(self.tmp)
        req = {"platform": "youtube", "video": v, "thumbnail": None, "title": "T", "description": "D", "tags": [], "privacy": "private", "made_for_kids": False,
               "account_id": None, "idempotency_key": "k", "category": None, "playlists": []}
        with self.assertRaises(StageError) as e:                                                              # chưa đăng nhập Google => AUTH, giữ job chờ người dùng
            self.a.publish(req, ctx)
        self.assertEqual((e.exception.error_class, e.exception.resource), (ErrorClass.AUTH, "credential"))
        bad = YtUploaderPublish({"url": f"http://127.0.0.1:{self.port}", "token": "sai"})
        with self.assertRaises(StageError) as e:
            bad.publish(req, ctx)
        self.assertEqual(e.exception.code, "UPLOADER_UNAUTHORIZED")


if __name__ == "__main__":
    unittest.main()
