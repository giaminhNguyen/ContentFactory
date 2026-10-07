"""Phase 5: tích hợp ContentFlow qua RenderAdapter — profile, Source Sync dùng chung (pool), adapter bọc media_worker, Render Manager
(trạng thái/retry/checkpoint từng part), và logic đồng thời (render bận không chặn Story/TTS).

Dùng media_worker + source_sync GIẢ trong tests/fixtures/fake_contentflow (cùng giao thức JSON-lines) nên chạy mọi nơi. Phần chạy ContentFlow THẬT
(Pillow + ffmpeg) bị bỏ qua nếu thiếu: đặt biến môi trường CF_TEST_CONTENTFLOW_PYTHON = Python có Pillow (và có ffmpeg/ffprobe trên PATH)."""
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace

from contentfactory.adapters.fake import FakeRender
from contentfactory.contracts import CancelToken, ErrorClass, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.runner import Orchestrator
from contentfactory.render import frames, pools as PL
from contentfactory.render import profile as PF
from contentfactory.render.contentflow import ContentFlowRender, ext_job_id, map_worker_error
from contentfactory.render.manager import RenderManager
from contentfactory.tts.autotune import make_ctx
from tests.support import REPO, RootCase, params, wait_until

FAKE_CF = REPO / "tests" / "fixtures" / "fake_contentflow"
REAL_PY = os.environ.get("CF_TEST_CONTENTFLOW_PYTHON")
HAVE_REAL = bool(REAL_PY and Path(REAL_PY).exists() and shutil.which("ffmpeg") and shutil.which("ffprobe") and (REPO / "modules" / "ContentFlow").is_dir())
NO_WAIT = {"retry": {"max_attempts": 2, "backoff_s": [0, 0]}}
TIKTOK = {"speed": 2.0, "target_part_sec": 0.8}          # chia nhỏ để có nhiều part từ audio của fake TTS


def ctx_for(tmp: Path, **kw) -> SimpleNamespace:
    return SimpleNamespace(cancel=CancelToken(), job_id="j1", attempt=1, log=lambda *a, **k: None, **kw)


# ======================================================================== phần thuần
class ProfileTest(unittest.TestCase):
    def test_defaults_for_youtube_and_tiktok(self):
        y, t = PF.resolve("youtube", None, None), PF.resolve("tiktok", None, None)
        self.assertEqual((y["aspect_ratio"], y["width"], y["height"]), ("16:9", 1920, 1080))
        self.assertEqual((t["aspect_ratio"], t["width"], t["height"]), ("9:16", 1080, 1920))
        self.assertNotEqual(y["source_pool"], t["source_pool"])                      # pool khác nhau cho nền ngang/dọc
        self.assertTrue(y["thumbnail"]["enabled"])
        self.assertNotIn("thumbnail", t)

    def test_layers_config_then_params_override_defaults(self):
        cfg = {"profiles": {"youtube": {"fps": 24, "source_pool": "a", "retry": {"max_attempts": 5}}}}
        p = PF.resolve("youtube", cfg, {"youtube": {"fps": 60}})
        self.assertEqual((p["fps"], p["source_pool"], p["retry"]["max_attempts"], p["retry"]["backoff_s"]), (60, "a", 5, [5.0, 30.0]))
        self.assertEqual(PF.DEFAULTS["youtube"]["fps"], 30)                            # không làm bẩn mặc định

    def test_invalid_profiles_are_rejected(self):
        for bad in ({"resolution": "wide"}, {"resolution": "1080x1920"}, {"fps": 0}, {"selection_mode": "x"}, {"source_processing": "x"},
                    {"retry": {"max_attempts": 0}}):
            with self.assertRaises(StageError, msg=str(bad)) as e:
                PF.resolve("youtube", None, {"youtube": bad})
            self.assertEqual(e.exception.code, "INVALID_RENDER_PROFILE")
        with self.assertRaises(StageError):
            PF.resolve("instagram", None, None)

    def test_pool_spec_defaults_follow_the_profile_and_missing_pool_is_an_input_problem(self):
        prof = PF.resolve("tiktok", None, None)
        spec = PF.pool_spec(prof, {"pools": {"gameplay_vertical": {"raw_dir": "x"}}})
        self.assertEqual((spec["sync"]["size"], spec["sync"]["fps"], spec["sync"]["remove_audio"]), ("1080x1920", 30, True))
        with self.assertRaises(StageError) as e:
            PF.pool_spec(prof, {"pools": {}})
        self.assertEqual((e.exception.error_class, e.exception.code, e.exception.resource), (ErrorClass.POLICY, "POOL_NOT_CONFIGURED", "input"))
        self.assertIsNone(PF.pool_spec({**prof, "source_pool": None}, {}))


class FramesTest(unittest.TestCase):
    def test_transparent_png_is_a_valid_rgba_png_of_the_requested_size(self):
        data = frames.transparent_png(64, 36)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", data[16:24]), (64, 36))
        self.assertEqual((data[24], data[25]), (8, 6))                                 # 8-bit, color type 6 = RGBA
        idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
        raw = zlib.decompress(idat)
        self.assertEqual(len(raw), 36 * (1 + 64 * 4))
        self.assertEqual(set(raw[1:1 + 64 * 4]), {0})                                  # hoàn toàn trong suốt

    def test_ensure_frame_is_idempotent(self):
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="cf-fr-"))
        self.addCleanup(shutil.rmtree, d, True)
        p = frames.ensure_frame(d, 320, 180)
        m = p.stat().st_mtime_ns
        self.assertEqual((frames.ensure_frame(d, 320, 180), p.stat().st_mtime_ns), (p, m))
        self.assertEqual(p.name, "frame_320x180.png")


class PoolAssessTest(unittest.TestCase):
    SYNC = {"size": "1920x1080", "fps": 30, "quality": "balanced", "remove_audio": True, "encoder": "auto"}

    def setUp(self):
        import tempfile
        self.d = Path(tempfile.mkdtemp(prefix="cf-pool-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.raw = {"a.mov": [100, 1], "b.mp4": [200, 2]}
        self.synced = self.d / "synced"
        self.synced.mkdir()
        for n, size in (("a.mp4", 10), ("b.mp4", 20)):
            (self.synced / n).write_bytes(b"x" * size)
        self.state = {"options": PL.options_sig(self.SYNC, "v1"), "sources": self.raw, "dests": {"a.mp4": 10, "b.mp4": 20}, "failed": {}}

    def a(self, raw=None, sync=None, version="v1", state="default"):
        return PL.assess(raw or self.raw, sync or self.SYNC, version, self.state if state == "default" else state, self.synced)

    def test_ready_only_when_every_source_has_a_trusted_destination(self):
        r = self.a()
        self.assertEqual((r["ready"], r["todo"], r["untrusted"]), (True, [], []))
        self.assertFalse(self.a(state=None)["ready"])                                     # chưa có trạng thái: không tin file nào
        self.assertEqual(self.a(state=None)["untrusted"], ["a.mp4", "b.mp4"])             # file đích sót lại (có thể cụt do crash) bị loại

    def test_changed_added_removed_or_truncated_files(self):
        r = self.a(raw={**self.raw, "a.mov": [100, 99]})                                  # nguồn đổi mtime
        self.assertEqual((r["ready"], r["todo"], r["untrusted"]), (False, ["a.mov"], ["a.mp4"]))
        self.assertEqual(self.a(raw={**self.raw, "c.mkv": [5, 5]})["todo"], ["c.mkv"])    # nguồn mới: chỉ làm file đó
        self.assertTrue(self.a(raw={"b.mp4": [200, 2]})["ready"])                         # nguồn bị xóa: phần còn lại vẫn đúng
        (self.synced / "b.mp4").write_bytes(b"x" * 7)                                    # file đích bị cụt
        r = self.a()
        self.assertEqual((r["ready"], r["todo"], r["untrusted"]), (False, ["b.mp4"], ["b.mp4"]))

    def test_option_or_version_change_redoes_everything(self):
        for r in (self.a(sync={**self.SYNC, "fps": 60}), self.a(version="v2")):
            self.assertEqual((r["ready"], sorted(r["todo"]), r["untrusted"]), (False, ["a.mov", "b.mp4"], ["a.mp4", "b.mp4"]))
        self.assertNotEqual(PL.fingerprint(self.raw, self.SYNC, "v1"), PL.fingerprint(self.raw, self.SYNC, "v2"))

    def test_source_known_to_be_bad_is_not_retried_until_it_changes(self):
        (self.synced / "b.mp4").unlink()
        st = {**self.state, "dests": {"a.mp4": 10}, "sources": {"a.mov": [100, 1]}, "failed": {"b.mp4": [200, 2, "hỏng"]}}
        self.assertTrue(self.a(state=st)["ready"])
        self.assertEqual(self.a(raw={**self.raw, "b.mp4": [200, 3]}, state=st)["todo"], ["b.mp4"])      # file đổi: thử lại
        self.assertFalse(PL.assess({}, self.SYNC, "v1", st, self.synced)["ready"])                       # pool rỗng không bao giờ ready

    def test_scan_raw_is_non_recursive_and_extension_filtered(self):
        (self.d / "raw").mkdir()
        for n in ("x.mp4", "y.MOV", "z.txt"):
            (self.d / "raw" / n).write_bytes(b"1")
        (self.d / "raw" / "sub").mkdir()
        (self.d / "raw" / "sub" / "deep.mp4").write_bytes(b"1")
        self.assertEqual(sorted(PL.scan_raw(self.d / "raw")), ["x.mp4", "y.MOV"])


class PoolLockTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = Path(tempfile.mkdtemp(prefix="cf-lock-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_exclusive_and_released(self):
        a, b = PL.PoolLock(self.d), PL.PoolLock(self.d)
        self.assertTrue(a.try_acquire())
        self.assertFalse(b.try_acquire())
        a.release()
        self.assertTrue(b.try_acquire())
        b.release()
        self.assertFalse((self.d / PL.LOCK).exists())

    def test_orphaned_lock_of_a_dead_process_is_taken_over(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        (self.d / PL.LOCK).write_text(json.dumps({"pid": p.pid, "ts": time.time()}), encoding="utf-8")
        self.assertTrue(PL.PoolLock(self.d).try_acquire())

    def test_lock_of_a_live_owner_is_respected_until_ttl_without_heartbeat(self):
        (self.d / PL.LOCK).write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
        self.assertFalse(PL.PoolLock(self.d, ttl=60).try_acquire())
        old = time.time() - 500
        os.utime(self.d / PL.LOCK, (old, old))
        self.assertTrue(PL.PoolLock(self.d, ttl=60).try_acquire())                        # quá TTL không heartbeat: coi như treo


class WorkerErrorMappingTest(unittest.TestCase):
    def test_table(self):
        m = lambda c, k: map_worker_error({"code": c, "class": k, "message": "m"})       # noqa: E731
        for code, res in (("FFMPEG_MISSING", "runtime"), ("GPU_UNAVAILABLE", "runtime"), ("DISK_FULL", "disk")):
            e = m(code, "RESOURCE")
            self.assertEqual((e.error_class, e.resource), (ErrorClass.RESOURCE, res))
        e = m("MISSING_INPUT", "POLICY")
        self.assertEqual((e.error_class, e.resource), (ErrorClass.POLICY, "input"))     # người dùng cung cấp lại => giữ job, không FAILED
        self.assertEqual((m("INVALID_CONFIG", "POLICY").error_class, m("INVALID_CONFIG", "POLICY").resource), (ErrorClass.POLICY, None))
        for code in ("FFMPEG_FAILED", "TIMEOUT", "INTERNAL_ERROR"):
            self.assertEqual(m(code, "TRANSIENT").error_class, ErrorClass.TRANSIENT)
        self.assertEqual(m("CANCELLED", "CANCELLED").error_class, ErrorClass.CANCELLED)
        self.assertEqual(ext_job_id("k1"), ext_job_id("k1"))                              # xác định từ idempotency_key (giống worker thật)
        self.assertTrue(ext_job_id("k1").startswith("mw-"))


# ======================================================================== adapter + media_worker giả
class FakeCFCase(RootCase):
    """Root tạm + thư mục ContentFlow giả + hai pool nguồn; cấu hình trỏ orchestrator vào adapter `contentflow`."""

    def setUp(self):
        super().setUp()
        self.cfbase = self.root / "cfbase"
        self.cfbase.mkdir()
        self.raw = {"gameplay": self.root / "raw_h", "gameplay_vertical": self.root / "raw_v"}
        for d in self.raw.values():
            d.mkdir()
            for n in ("clip1.mp4", "clip2.mp4"):
                (d / n).write_bytes((d.name + n).encode() * 50)
        cfg = self.root / "config" / "config.json"
        c = json.loads(cfg.read_text(encoding="utf-8"))
        c.update({"adapters": {"render": "contentflow"},
                  "tools": {"contentflow": {"root": str(FAKE_CF), "python": sys.executable, "base_dir": str(self.cfbase), "verify_output": False}},
                  "render": {"pools": {k: {"raw_dir": str(v)} for k, v in self.raw.items()}, "pool_sync_interval_s": 3600}})
        cfg.write_text(json.dumps(c), encoding="utf-8")

    # -- điều khiển worker giả
    def ctl(self, **kw):
        (self.cfbase / "fake_cf_control.json").write_text(json.dumps(kw), encoding="utf-8")

    def calls(self, output: str | None = None) -> list[dict]:
        f = self.cfbase / "fake_cf_calls.log"
        rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []
        return [r for r in rows if output is None or r["output"] == output]

    def sync_calls(self, pool: str = "gameplay") -> list[dict]:
        f = self.root / "runtime" / "pools" / pool / "fake_sync_calls.log"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []

    def adapter(self) -> ContentFlowRender:
        return ContentFlowRender({"root": FAKE_CF, "python": sys.executable, "base_dir": self.cfbase, "pools_dir": self.root / "runtime" / "pools",
                                  "verify_output": False})

    def pool(self, name="gameplay", **sync) -> dict:
        return {"name": name, "raw_dir": str(self.raw[name]),
                "sync": {"size": "1920x1080", "fps": 30, "quality": "balanced", "remove_audio": True, "encoder": "auto", **sync}}

    def audio(self, name="a.wav", body=b"RIFF....audio") -> Path:
        p = self.root / name
        p.write_bytes(body)
        return p


class ContentFlowAdapterTest(FakeCFCase):
    def req(self, key="k1", out="video.mp4", audio=None, **kw):
        return {"audio": audio or self.audio(), "profile": PF.resolve("youtube", None, None), "output": self.root / "out" / out, "key": key, **kw}

    def test_health_version_and_missing_python(self):
        a = self.adapter()
        h = a.health()
        self.assertEqual((h["ok"], h["worker_version"]), (True, "0.1.0-fake"))
        self.assertIn("+w0.1.0-fake", a.version())
        bad = ContentFlowRender({"root": FAKE_CF, "python": str(self.root / "không-có-python"), "base_dir": self.cfbase})
        self.assertFalse(bad.health()["ok"])
        with self.assertRaises(StageError) as e:
            bad.render_video(self.req(), ctx_for(self.root))
        self.assertEqual((e.exception.error_class, e.exception.code, e.exception.resource), (ErrorClass.RESOURCE, "CONTENTFLOW_MISSING", "runtime"))

    def test_render_worker_sees_the_user_asset_root(self):
        a = ContentFlowRender({"root": FAKE_CF, "base_dir": self.cfbase, "user_root": self.root / "u"})
        self.assertEqual(a._env()["CONTENTFLOW_USER_ROOT"], str(self.root / "u"))

    def test_render_reports_progress_and_replays_a_completed_key_without_rendering_again(self):
        a, ctx, seen = self.adapter(), ctx_for(self.root), []
        r = a.render_video(self.req(on_progress=seen.append), ctx)
        self.assertEqual((r["replayed"], seen), (False, [0.25, 0.5, 0.75]))
        out = self.root / "out" / "video.mp4"
        self.assertTrue(out.read_bytes().startswith(b"FAKE-RENDER|video.mp4"))
        self.assertEqual(len(self.calls()), 1)
        r2 = a.render_video(self.req(), ctx)
        self.assertEqual((r2["replayed"], len(self.calls())), (True, 1))                  # idempotency: worker không render lại
        out.unlink()                                                                       # output mất => không replay
        self.assertFalse(a.render_video(self.req(), ctx)["replayed"])
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(sorted(p.name for p in (self.root / "out").iterdir() if not p.name.startswith(".")), ["video.mp4"])
        self.assertEqual(a.status("k1", self.root / "out")["state"], "completed")
        self.assertEqual(a.status("never", self.root / "out")["state"], "unknown")

    def test_worker_failures_map_to_typed_errors(self):
        a, ctx = self.adapter(), ctx_for(self.root)
        cases = [("FFMPEG_MISSING", "RESOURCE", ErrorClass.RESOURCE, "runtime"), ("DISK_FULL", "RESOURCE", ErrorClass.RESOURCE, "disk"),
                 ("MISSING_INPUT", "POLICY", ErrorClass.POLICY, "input"), ("INVALID_CONFIG", "POLICY", ErrorClass.POLICY, None),
                 ("FFMPEG_FAILED", "TRANSIENT", ErrorClass.TRANSIENT, None)]
        for i, (code, klass, cls, res) in enumerate(cases):
            name = f"v{i}.mp4"
            self.ctl(fail={name: {"code": code, "class": klass, "times": 1}})
            with self.assertRaises(StageError, msg=code) as e:
                a.render_video(self.req(key=f"f{i}", out=name), ctx)
            self.assertEqual((e.exception.code, e.exception.error_class, e.exception.resource), (code, cls, res))
        self.ctl(crash=["die.mp4"])
        with self.assertRaises(StageError) as e:                                              # worker chết không có sự kiện kết thúc
            a.render_video(self.req(key="c1", out="die.mp4"), ctx)
        self.assertEqual((e.exception.error_class, e.exception.code), (ErrorClass.TRANSIENT, "RENDER_WORKER_DIED"))

    def test_cancel_stops_the_worker(self):
        gate = self.root / "gate"
        gate.write_text("x")
        self.ctl(gate=str(gate))
        a, ctx = self.adapter(), ctx_for(self.root)
        threading.Timer(0.6, ctx.cancel.set).start()
        t0 = time.time()
        with self.assertRaises(StageError) as e:
            a.render_video(self.req(), ctx)
        self.assertEqual(e.exception.error_class, ErrorClass.CANCELLED)
        self.assertLess(time.time() - t0, 20)
        self.assertFalse(list((self.root / "out").glob("cancel.*")))                          # không để file hủy lại

    def test_thumbnail_goes_through_the_worker(self):
        p = self.adapter().render_thumbnail({"title": "Tiêu đề", "channel_name": "Kênh", "output": self.root / "out" / "t.jpg", "key": "t"},
                                            ctx_for(self.root))
        self.assertTrue(p.read_bytes().startswith(b"FAKE-THUMBNAIL"))


class PoolSyncTest(FakeCFCase):
    def test_sync_once_then_reuse_until_the_source_changes(self):
        a, ctx, pool = self.adapter(), ctx_for(self.root), self.pool()
        r1 = a.prepare_pool(pool, ctx)
        self.assertEqual((r1["reused"], r1["files"]), (False, 2))
        self.assertEqual(self.sync_calls()[0]["synced"], ["clip1.mp4", "clip2.mp4"])
        self.assertEqual(self.sync_calls()[0]["size"], "1920x1080")
        for _ in range(3):
            self.assertTrue(a.prepare_pool(pool, ctx)["reused"])
        self.assertEqual(len(self.sync_calls()), 1)                                           # nguồn không đổi: KHÔNG đồng bộ lại
        (self.raw["gameplay"] / "clip3.mp4").write_bytes(b"new" * 40)                         # thêm nguồn: chỉ file mới
        r2 = a.prepare_pool(pool, ctx)
        self.assertEqual((r2["reused"], r2["fingerprint"] != r1["fingerprint"]), (False, True))
        self.assertEqual(self.sync_calls()[1]["synced"], ["clip3.mp4"])
        st = a.pool_status(pool)
        self.assertEqual((st["ready"], st["todo"], st["raw_files"]), (True, 0, 3))

    def test_changed_option_resyncs_everything_with_the_new_size(self):
        a, ctx = self.adapter(), ctx_for(self.root)
        a.prepare_pool(self.pool(), ctx)
        a.prepare_pool(self.pool(size="1280x720"), ctx)
        self.assertEqual(self.sync_calls()[1]["synced"], ["clip1.mp4", "clip2.mp4"])
        self.assertEqual(self.sync_calls()[1]["size"], "1280x720")

    def test_destination_left_by_a_crash_is_not_trusted(self):
        """ContentFlow ghi thẳng vào đích: crash để lại file cụt mà nó sẽ bỏ qua vì size > 0. Ta không có trạng thái => xóa và làm lại."""
        a, ctx, pool = self.adapter(), ctx_for(self.root), self.pool()
        synced = self.root / "runtime" / "pools" / "gameplay" / "synced"
        synced.mkdir(parents=True)
        (synced / "clip1.mp4").write_bytes(b"CUT")
        a.prepare_pool(pool, ctx)
        self.assertIn("clip1.mp4", self.sync_calls()[0]["synced"])
        self.assertNotEqual((synced / "clip1.mp4").read_bytes(), b"CUT")
        (synced / "clip2.mp4").write_bytes(b"CUT")                                            # hỏng sau khi đã đồng bộ: kích thước lệch trạng thái
        a.prepare_pool(pool, ctx)
        self.assertEqual(self.sync_calls()[1]["synced"], ["clip2.mp4"])

    def test_concurrent_callers_trigger_exactly_one_sync(self):
        self.addCleanup(os.environ.pop, "FAKE_SYNC_DELAY", None)
        os.environ["FAKE_SYNC_DELAY"] = "0.3"
        pool, results, errs = self.pool(), [], []

        def go():
            try:
                results.append(self.adapter().prepare_pool(pool, ctx_for(self.root)))
            except Exception as e:                                                             # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(len(results), 4)
        self.assertEqual(len(self.sync_calls()), 1)
        self.assertEqual(sorted(r["reused"] for r in results), [False, True, True, True])      # một người đồng bộ, ba người chờ rồi dùng lại

    def test_waiting_for_another_sync_is_cancellable(self):
        lock = PL.PoolLock(self.root / "runtime" / "pools" / "gameplay")
        self.assertTrue(lock.try_acquire())
        self.addCleanup(lock.release)
        ctx = ctx_for(self.root)
        threading.Timer(0.5, ctx.cancel.set).start()
        with self.assertRaises(StageError) as e:
            self.adapter().prepare_pool(self.pool(), ctx)
        self.assertEqual(e.exception.error_class, ErrorClass.CANCELLED)

    def test_problems_with_the_pool_are_typed(self):
        a, ctx = self.adapter(), ctx_for(self.root)
        with self.assertRaises(StageError) as e:
            a.prepare_pool({**self.pool(), "raw_dir": str(self.root / "không-có")}, ctx)
        self.assertEqual((e.exception.code, e.exception.resource), ("MISSING_INPUT", "input"))
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(StageError) as e:
            a.prepare_pool({**self.pool(), "raw_dir": str(empty)}, ctx)
        self.assertEqual(e.exception.code, "MISSING_INPUT")
        for f in self.raw["gameplay"].iterdir():
            f.rename(f.with_name("bad_" + f.name))
        with self.assertRaises(StageError) as e:
            a.prepare_pool(self.pool(), ctx)
        self.assertEqual((e.exception.error_class, e.exception.code), (ErrorClass.POLICY, "POOL_SYNC_FAILED"))

    def test_one_corrupt_source_does_not_poison_the_pool_and_is_not_retried(self):
        (self.raw["gameplay"] / "bad_clip.mp4").write_bytes(b"x" * 30)
        a, ctx, pool = self.adapter(), ctx_for(self.root), self.pool()
        r = a.prepare_pool(pool, ctx)
        self.assertEqual((r["files"], r["failed"]), (2, ["bad_clip.mp4"]))
        self.assertTrue(a.prepare_pool(pool, ctx)["reused"])                                   # file hỏng đã biết: không thử lại mỗi lần
        self.assertEqual(len(self.sync_calls()), 1)


# ======================================================================== stage render với adapter contentflow (worker giả)
class RenderPipelineTest(FakeCFCase):
    def submit(self, orc, **extra):
        return orc.submit(params(tiktok=TIKTOK, render={"youtube": NO_WAIT, "tiktok": NO_WAIT}, **extra), auto_resume=False)

    def parts(self, orc, jid) -> list[dict]:
        return [a for a in orc.store.artifacts(jid) if a["kind"] == "video_tiktok"]

    def test_full_job_renders_youtube_and_every_tiktok_part_with_shared_pools(self):
        orc = self.orc()
        jid = self.submit(orc)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual(j["state"], P.PUBLISHED, j["last_error"])
        n = len(self.parts(orc, jid))
        self.assertGreaterEqual(n, 3)
        kinds = {a["kind"] for a in orc.store.artifacts(jid)}
        self.assertTrue({"video_youtube", "thumbnail", "video_tiktok", "youtube_render_report", "tiktok_render_report"} <= kinds)
        self.assertEqual(len(self.calls("video.mp4")), 1)
        self.assertEqual([c["output"] for c in self.calls() if c["output"].startswith("part_")], [f"part_{i:02d}.mp4" for i in range(1, n + 1)])
        self.assertEqual((len(self.sync_calls("gameplay")), len(self.sync_calls("gameplay_vertical"))), (1, 1))
        rep = json.loads((self.job_dir(jid) / "render" / "tiktok" / "render_report.json").read_text(encoding="utf-8"))
        self.assertEqual({v["state"] for v in rep["parts"].values()}, {"done"})
        self.assertEqual(rep["pool"]["name"], "gameplay_vertical")
        yrep = json.loads((self.job_dir(jid) / "render" / "youtube" / "render_report.json").read_text(encoding="utf-8"))
        self.assertEqual({k: v["state"] for k, v in yrep["outputs"].items()}, {"video": "done", "thumbnail": "done"})
        self.assertIn("pool=synced", (self.job_dir(jid) / "render" / "tiktok" / "part_01.mp4").read_text())   # render đọc pool đã đồng bộ

    def test_second_job_reuses_the_synced_pools(self):
        orc = self.orc()
        self.submit(orc)
        self.submit(orc)
        orc.run()
        self.assertEqual((len(self.sync_calls("gameplay")), len(self.sync_calls("gameplay_vertical"))), (1, 1))   # không đồng bộ lại cho job thứ hai

    def test_part_03_failure_retries_only_part_03(self):
        orc = self.orc()
        self.ctl(fail={"part_03.mp4": {"code": "FFMPEG_FAILED", "class": "TRANSIENT", "times": 1000}})
        jid = self.submit(orc)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["failed_stage"], j["last_error"]["code"]), (P.FAILED, "render_tiktok", "RENDER_PARTS_FAILED"))
        self.assertEqual(j["last_error"]["detail"]["failed_parts"], [3])
        n = max(int(c["output"][5:7]) for c in self.calls() if c["output"].startswith("part_"))
        # các part khác vẫn render hết (không bị part 3 chặn) và mỗi part chỉ render ĐÚNG MỘT lần, kể cả khi job retry cả stage
        for i in range(1, n + 1):
            if i != 3:
                self.assertEqual(len(self.calls(f"part_{i:02d}.mp4")), 1, f"part {i}")
        self.assertEqual(len(self.calls("part_03.mp4")), 2 * 3)                                # 2 lần thử trong stage × 3 lần job retry
        states = j["checkpoint"]["render_tiktok"]["parts"]
        self.assertEqual(states["3"]["state"], "failed")
        self.assertEqual(states["3"]["error"], "FFMPEG_FAILED")
        self.assertEqual({v["state"] for k, v in states.items() if k != "3"}, {"reused"})     # lần chạy cuối dùng lại các part đã xong
        self.assertEqual(self.runs(orc, jid)["render_tiktok"], ["failed"] * 3)
        before = len(self.calls())
        self.ctl()                                                                             # sửa nguyên nhân
        orc.retry(jid)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        new = self.calls()[before:]
        self.assertEqual([c["output"] for c in new], ["part_03.mp4"])                         # chỉ part 03 được render lại
        self.assertEqual(self.runs(orc, jid)["render_youtube"], ["succeeded"])                # YouTube không bị đụng tới

    def test_transient_part_failure_recovers_inside_the_stage_without_touching_other_parts(self):
        orc = self.orc()
        self.ctl(fail={"part_02.mp4": {"code": "FFMPEG_FAILED", "class": "TRANSIENT", "times": 1}})
        jid = self.submit(orc)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        self.assertEqual(len(self.calls("part_02.mp4")), 2)                                    # thử lại đúng part 02 trong cùng lần chạy
        self.assertEqual(self.runs(orc, jid)["render_tiktok"], ["succeeded"])
        rep = json.loads((self.job_dir(jid) / "render" / "tiktok" / "render_report.json").read_text(encoding="utf-8"))
        self.assertEqual((rep["parts"]["2"]["attempts"], rep["parts"]["2"]["retry_errors"]), (2, ["FFMPEG_FAILED"]))
        self.assertEqual(rep["parts"]["1"]["attempts"], 1)

    def test_rerender_one_valid_part_on_demand(self):
        orc = self.orc()
        self.ctl(fail={"part_03.mp4": {"code": "FFMPEG_FAILED", "class": "TRANSIENT", "times": 1000}})
        jid = self.submit(orc)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["failed_stage"], "render_tiktok")
        before = len(self.calls())
        self.ctl()
        self.assertEqual(orc.rerender_part(jid, 2), P.TIKTOK_RENDER_READY)                    # part 2 đang hợp lệ nhưng người dùng muốn làm lại
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        self.assertEqual(sorted(c["output"] for c in self.calls()[before:]), ["part_02.mp4", "part_03.mp4"])
        with self.assertRaises(ValueError):
            orc.rerender_part(jid, 1)                                                          # job đã qua stage render_tiktok: chưa rewind được

    def test_resource_problem_holds_the_job_instead_of_failing_it(self):
        orc = self.orc()
        self.ctl(fail={"video.mp4": {"code": "DISK_FULL", "class": "RESOURCE", "times": 1}})
        jid = self.submit(orc)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual((j["hold_reason"], j["state"]), (P.PAUSED_DISK, P.YOUTUBE_RENDER_READY))
        self.assertNotEqual(j["state"], P.FAILED)
        self.ctl()
        self.assertEqual(orc.store.release_hold(jid), "released")
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)

    def test_missing_thumbnail_template_pauses_for_missing_input(self):
        orc = self.orc()
        self.ctl(fail={"thumbnail.jpg": {"code": "MISSING_INPUT", "class": "POLICY", "times": 1}})
        jid = self.submit(orc)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual(j["hold_reason"], P.PAUSED_MISSING_INPUT)
        self.assertEqual(len(self.calls("video.mp4")), 1)                                      # video đã xong, không bị render lại khi tiếp tục
        self.ctl()
        orc.store.release_hold(jid)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        self.assertEqual(len(self.calls("video.mp4")), 1)

    def test_unconfigured_pool_is_an_input_problem(self):
        cfg = self.root / "config" / "config.json"
        c = json.loads(cfg.read_text(encoding="utf-8"))
        c["render"]["pools"] = {}
        cfg.write_text(json.dumps(c), encoding="utf-8")
        orc = self.orc()
        jid = self.submit(orc)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual((j["hold_reason"], j["state"]), (P.PAUSED_MISSING_INPUT, P.YOUTUBE_RENDER_READY))
        self.assertIn("POOL_NOT_CONFIGURED", j["hold_detail"])

    def test_cli_status_shows_part_states(self):
        from contentfactory.orchestrator.cli import _print_status
        import contextlib
        import io
        orc = self.orc()
        self.ctl(fail={"part_03.mp4": {"code": "FFMPEG_FAILED", "class": "TRANSIENT", "times": 1000}})
        jid = self.submit(orc)
        orc.run()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_status(orc, jid)
        self.assertIn("3=failed(FFMPEG_FAILED)", buf.getvalue())
        self.assertIn("1=reused", buf.getvalue())


class RenderManagerCacheTest(FakeCFCase):
    """Render Manager dùng trực tiếp: khóa nội dung quyết định render lại hay dùng lại."""

    def setUp(self):
        super().setUp()
        self.stage_dir = self.root / "ws" / "render" / "tiktok"
        self.stage_dir.mkdir(parents=True)
        self.parts = []
        for i in (1, 2, 3):
            p = self.root / "ws" / f"audio_{i}.wav"
            p.write_bytes(f"audio-{i}".encode())
            self.parts.append(p)

    def ctx(self, shas=None, render_cfg=None):
        shas = shas or ["s1", "s2", "s3"]
        refs = [{"path": f"audio_{i}.wav", "kind": "audio_tiktok", "sha256": shas[i - 1], "bytes": 7, "meta": {"index": i}} for i in (1, 2, 3)]
        c = make_ctx(self.root / "ws")
        c.stage_dir, c.workspace = self.stage_dir, self.root / "ws"
        c.inputs = {"audio_tiktok": refs}
        c.params = {"render": {"tiktok": NO_WAIT}}
        c.config = {"render": render_cfg or {"pools": {k: {"raw_dir": str(v)} for k, v in self.raw.items()}}}
        return c

    def run_tiktok(self, **kw) -> dict:
        r = RenderManager(self.adapter()).tiktok(self.ctx(**kw))
        return r.data

    def test_valid_outputs_are_never_rendered_again(self):
        self.assertEqual(self.run_tiktok()["rendered"], 3)
        self.assertEqual(len(self.calls()), 3)
        d = self.run_tiktok()
        self.assertEqual((d["rendered"], d["reused"], len(self.calls())), (0, 3, 3))

    def test_changed_audio_rerenders_only_that_part(self):
        self.run_tiktok()
        d = self.run_tiktok(shas=["s1", "CHANGED", "s3"])
        self.assertEqual((d["rendered"], d["reused"]), (1, 2))
        self.assertEqual([c["output"] for c in self.calls()[3:]], ["part_02.mp4"])

    def test_corrupted_or_truncated_output_is_not_trusted(self):
        self.run_tiktok()
        (self.stage_dir / "part_01.mp4").write_bytes(b"x")                                    # kích thước lệch sidecar
        (self.stage_dir / "part_03.mp4.key.json").unlink()                                    # mất khóa
        d = self.run_tiktok()
        self.assertEqual((d["rendered"], d["reused"]), (2, 1))

    def test_pool_or_profile_change_rerenders_everything(self):
        self.run_tiktok()
        (self.raw["gameplay_vertical"] / "clip3.mp4").write_bytes(b"more" * 20)               # pool đổi => dấu vân tay đổi
        self.assertEqual(self.run_tiktok()["rendered"], 3)
        cfg = {"pools": {k: {"raw_dir": str(v)} for k, v in self.raw.items()}, "profiles": {"tiktok": {"fps": 24}}}
        self.assertEqual(self.run_tiktok(render_cfg=cfg)["rendered"], 3)                      # profile đổi

    def test_cancel_between_parts_keeps_finished_ones(self):
        c = self.ctx()
        orig = self.adapter().render_video
        adapter = self.adapter()
        seen = []

        def spy(req, ctx):
            seen.append(req["part"])
            r = orig(req, ctx)
            if req["part"] == 2:
                c.cancel.set()
            return r
        adapter.render_video = spy
        with self.assertRaises(StageError) as e:
            RenderManager(adapter).tiktok(c)
        self.assertEqual((e.exception.error_class, seen), (ErrorClass.CANCELLED, [1, 2]))
        self.assertTrue((self.stage_dir / "part_02.mp4").is_file())
        c2 = self.ctx()
        d = RenderManager(self.adapter()).tiktok(c2).data
        self.assertEqual((d["rendered"], d["reused"]), (1, 2))


# ======================================================================== đồng thời: render bận không chặn Story/TTS
class GatedRender(FakeRender):
    """FakeRender có cổng: render_video của (job, profile, part) chờ cho tới khi test mở cổng."""

    def __init__(self):
        self.gates: dict[tuple, threading.Event] = {}
        self.entered: list[tuple] = []
        self.lock = threading.Lock()

    def gate(self, job_id: str, pid: str, part=None) -> threading.Event:
        return self.gates.setdefault((job_id, pid, part), threading.Event())

    def render_video(self, req, ctx):
        k = (ctx.job_id, req["profile"]["id"], req.get("part"))
        with self.lock:
            self.entered.append(k)
        g = self.gates.get(k)
        if g is not None:
            while not g.is_set():
                ctx.cancel.wait(0.02)
        return super().render_video(req, ctx)


class Loop:
    def __init__(self, orc):
        self.orc, self.stop = orc, threading.Event()
        self.t = threading.Thread(target=orc.run, kwargs={"until_idle": False, "stop": self.stop}, daemon=True)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join(30)


class RenderConcurrencyTest(RootCase):
    def setUp(self):
        super().setUp()
        self.render = GatedRender()

    def orc2(self):
        o = self.orc()
        o.adapters["render"] = self.render
        return o

    def state(self, orc, jid):
        return orc.store.get_job(jid)["state"]

    def test_story_and_tts_continue_while_the_renderer_is_busy(self):
        orc = self.orc2()
        a = orc.submit(params(tiktok=TIKTOK), auto_resume=False)
        self.render.gate(a, "youtube")                                                         # job A kẹt ở render YouTube
        with Loop(orc):
            wait_until(lambda: self.state(orc, a) == P.YOUTUBE_RENDERING, what="A đang render")
            b = orc.submit(params(tiktok=TIKTOK), auto_resume=False)
            c = orc.submit(params(tiktok=TIKTOK), auto_resume=False)
            # B và C đi tiếp qua source -> story -> tts -> audio trong lúc A chiếm renderer (lane gpu = 1)
            wait_until(lambda: all(self.state(orc, x) == P.YOUTUBE_RENDER_READY for x in (b, c)), what="B, C xếp hàng chờ render")
            self.assertEqual(self.state(orc, a), P.YOUTUBE_RENDERING)
            for x in (b, c):
                done = {r["stage"] for r in orc.store.stage_runs(x) if r["status"] == "succeeded"}
                self.assertEqual(done, {"source", "story", "tts", "audio"})
            running_render = [x for x in (a, b, c) if self.state(orc, x) in (P.YOUTUBE_RENDERING, P.TIKTOK_RENDERING)]
            self.assertEqual(running_render, [a])                                              # mỗi lúc chỉ một job render
            self.assertEqual({k[0] for k in self.render.entered}, {a})
            self.render.gate(a, "youtube").set()
            wait_until(lambda: all(self.state(orc, x) == P.PUBLISHED for x in (a, b, c)), timeout=60, what="tất cả xong")

    def test_new_story_work_proceeds_even_with_the_render_queue_full(self):
        orc = self.orc2()
        a = orc.submit(params(), auto_resume=False)
        self.render.gate(a, "youtube")
        with Loop(orc):
            wait_until(lambda: self.state(orc, a) == P.YOUTUBE_RENDERING)
            ids = [orc.submit(params(), auto_resume=False) for _ in range(4)]
            wait_until(lambda: all(self.state(orc, x) == P.YOUTUBE_RENDER_READY for x in ids), what="4 job xếp hàng ở render")
            late = orc.submit(params(), mode="STORY_ONLY", auto_resume=False)                 # job story-only mới vẫn chạy ngay
            wait_until(lambda: self.state(orc, late) == P.STORY_READY, what="story của job mới")
            self.assertEqual(self.state(orc, a), P.YOUTUBE_RENDERING)
            self.render.gate(a, "youtube").set()
            wait_until(lambda: all(self.state(orc, x) == P.PUBLISHED for x in [a, *ids]), timeout=90)

    def test_youtube_and_tiktok_part_states_are_explicit_while_rendering(self):
        orc = self.orc2()
        jid = orc.submit(params(tiktok=TIKTOK), auto_resume=False)
        self.render.gate(jid, "tiktok", 2)                                                     # kẹt ở part 2
        with Loop(orc):
            wait_until(lambda: self.state(orc, jid) == P.TIKTOK_RENDERING, what="đang render TikTok")
            wait_until(lambda: (orc.store.get_job(jid)["checkpoint"].get("render_tiktok") or {}).get("parts", {}).get("2", {}).get("state") == "rendering",
                       what="part 2 rendering")
            j = orc.store.get_job(jid)
            parts = j["checkpoint"]["render_tiktok"]["parts"]
            self.assertEqual((parts["1"]["state"], parts["2"]["state"], parts["3"]["state"]), ("done", "rendering", "pending"))
            self.assertEqual(j["checkpoint"]["render_tiktok"]["done"], 1)
            self.assertIn("parts", j["progress"])
            yt = orc.store.stage_runs(jid)
            self.assertEqual([r["status"] for r in yt if r["stage"] == "render_youtube"], ["succeeded"])      # YouTube đã xong, tách bạch
            self.render.gate(jid, "tiktok", 2).set()
            wait_until(lambda: self.state(orc, jid) == P.PUBLISHED, timeout=60)
        parts = orc.store.get_job(jid)["checkpoint"]["render_tiktok"]["parts"]
        self.assertEqual({v["state"] for v in parts.values()}, {"done"})


class BackgroundPoolSyncTest(FakeCFCase):
    def test_background_sync_runs_without_blocking_story_and_tts(self):
        self.addCleanup(os.environ.pop, "FAKE_SYNC_DELAY", None)
        os.environ["FAKE_SYNC_DELAY"] = "0.6"                                                 # mỗi file đồng bộ mất 0.6s => pool mất ~1.2s
        orc = self.orc()
        jid = orc.submit(params(tiktok=TIKTOK), mode="THROUGH_TTS", auto_resume=False)       # job không cần render
        with Loop(orc):
            wait_until(lambda: orc.store.get_job(jid)["state"] == P.AUDIO_READY, timeout=30, what="story+tts xong")
            st = self.adapter().pool_status(orc.pool_sync.specs()["gameplay"])
            self.assertTrue(st["syncing"] or not st["ready"] or self.sync_calls("gameplay"))  # sync nền đang/đã chạy cùng lúc, không chặn job
            wait_until(lambda: (orc.store.get_resource_status("pool:gameplay") or {}).get("ok") is True, timeout=30, what="pool gameplay sẵn sàng")
            wait_until(lambda: (orc.store.get_resource_status("pool:gameplay_vertical") or {}).get("ok") is True, timeout=30)
        self.assertEqual((len(self.sync_calls("gameplay")), len(self.sync_calls("gameplay_vertical"))), (1, 1))
        job2 = orc.submit(params(tiktok=TIKTOK), auto_resume=False)                           # job render sau đó dùng pool đã sẵn sàng, không đồng bộ lại
        orc.run()
        self.assertEqual(orc.store.get_job(job2)["state"], P.PUBLISHED)
        self.assertEqual((len(self.sync_calls("gameplay")), len(self.sync_calls("gameplay_vertical"))), (1, 1))

    def test_pool_failure_is_reported_not_fatal(self):
        shutil.rmtree(self.raw["gameplay"])
        orc = self.orc()
        res = orc.pool_sync.sync()
        self.assertIn("error", res["gameplay"])
        st = orc.store.get_resource_status("pool:gameplay")
        self.assertFalse(st["ok"])
        self.assertIn("MISSING_INPUT", st["detail"])
        self.assertTrue(orc.store.get_resource_status("pool:gameplay_vertical")["ok"])


# ======================================================================== ContentFlow THẬT (Pillow + ffmpeg)
def make_png(path: Path, w: int, h: int) -> None:
    path.write_bytes(frames.transparent_png(w, h))


@unittest.skipUnless(HAVE_REAL, "cần CF_TEST_CONTENTFLOW_PYTHON (Python có Pillow) + ffmpeg/ffprobe + modules/ContentFlow")
class RealContentFlowTest(RootCase):
    def setUp(self):
        super().setUp()
        self.base = self.root / "cfbase"
        self.base.mkdir()
        self.raw = self.root / "raw"
        self.raw.mkdir()

        def ff(*a):
            subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *a], check=True)
        ff("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25", "-t", "3", "-pix_fmt", "yuv420p", str(self.raw / "c1.mp4"))
        ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-t", "3", "-pix_fmt", "yuv420p", str(self.raw / "c2.mp4"))
        ff("-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-ar", "48000", "-c:a", "pcm_s24le", str(self.root / "a.wav"))
        self.a = ContentFlowRender({"root": REPO / "modules" / "ContentFlow", "python": REAL_PY, "base_dir": self.base, "pools_dir": self.root / "pools"})
        self.pool = {"name": "p", "raw_dir": str(self.raw), "sync": {"size": "640x360", "fps": 25, "quality": "fast", "remove_audio": True, "encoder": "cpu"}}

    def probe(self, p: Path) -> dict:
        d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(p)],
                                      capture_output=True, text=True, check=True).stdout)
        v = next(s for s in d["streams"] if s["codec_type"] == "video")
        return {"w": v["width"], "h": v["height"], "dur": float(d["format"]["duration"]), "codec": v["codec_name"]}

    def test_source_sync_is_shared_and_real_renders_have_the_profile_resolution(self):
        ctx = ctx_for(self.root)
        self.assertTrue(self.a.health()["ok"])
        r = self.a.prepare_pool(self.pool, ctx)
        self.assertEqual((r["reused"], r["files"]), (False, 2))
        synced = Path(r["dir"])
        info = self.probe(synced / "c1.mp4")
        self.assertEqual((info["w"], info["h"], info["codec"]), (640, 360, "h264"))             # Source Sync thật chuẩn hóa về đúng kích thước
        self.assertTrue(self.a.prepare_pool(self.pool, ctx)["reused"])
        yt = PF.resolve("youtube", {"profiles": {"youtube": {"resolution": "640x360", "encoder": "libx264"}}}, None)
        out = self.root / "o" / "video.mp4"
        res = self.a.render_video({"audio": self.root / "a.wav", "profile": yt, "output": out, "pool": r, "key": "k-yt"}, ctx)
        self.assertTrue(res["verified"])
        v = self.probe(out)
        self.assertEqual((v["w"], v["h"]), (640, 360))
        self.assertAlmostEqual(v["dur"], 4.0, delta=0.2)                                        # video dài đúng bằng audio
        tk = PF.resolve("tiktok", {"profiles": {"tiktok": {"resolution": "360x640", "encoder": "libx264"}}}, None)
        out2 = self.root / "o" / "part_01.mp4"
        self.a.render_video({"audio": self.root / "a.wav", "profile": tk, "output": out2, "pool": r, "key": "k-tk", "part": 1}, ctx)
        v2 = self.probe(out2)
        self.assertEqual((v2["w"], v2["h"]), (360, 640))                                        # 9:16
        again = self.a.render_video({"audio": self.root / "a.wav", "profile": tk, "output": out2, "pool": r, "key": "k-tk", "part": 1}, ctx)
        self.assertTrue(again["replayed"])                                                      # worker thật replay theo idempotency_key

    def test_wrong_size_output_is_rejected_and_removed(self):
        ctx = ctx_for(self.root)
        r = self.a.prepare_pool(self.pool, ctx)
        prof = PF.resolve("youtube", {"profiles": {"youtube": {"resolution": "640x360", "encoder": "libx264"}}}, None)
        prof["width"], prof["height"] = 1280, 720                                                # kỳ vọng sai kích thước so với frame đã dùng
        out = self.root / "o" / "bad.mp4"
        frame = frames.ensure_frame(self.a.frames_dir, 640, 360)
        prof["frame_path"] = str(frame)
        with self.assertRaises(StageError) as e:
            self.a.render_video({"audio": self.root / "a.wav", "profile": prof, "output": out, "pool": r, "key": "k-bad"}, ctx)
        self.assertEqual((e.exception.error_class, e.exception.code), (ErrorClass.TRANSIENT, "RENDER_OUTPUT_INVALID"))
        self.assertFalse(out.exists())                                                           # xóa để worker không replay output hỏng

    def test_thumbnail_missing_template_is_input_problem_and_works_with_assets(self):
        ctx = ctx_for(self.root)
        req = {"title": "Tiêu đề", "channel_name": "Kênh", "output": self.root / "o" / "t.jpg", "key": "t1"}
        with self.assertRaises(StageError) as e:
            self.a.render_thumbnail(req, ctx)
        self.assertEqual((e.exception.error_class, e.exception.code, e.exception.resource), (ErrorClass.POLICY, "MISSING_INPUT", "input"))
        font = Path(r"C:\Windows\Fonts\arial.ttf")
        if not font.exists():
            self.skipTest("không có font hệ thống để thử thumbnail có asset")
        make_png(self.root / "template.png", 1648, 928)
        cfg = {"template": {"file": str(self.root / "template.png")}, "title": {"font": str(font)}, "channel": {"font": str(font)}}
        p = self.a.render_thumbnail({**req, "key": "t2", "config_overrides": cfg}, ctx)
        self.assertGreater(p.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
