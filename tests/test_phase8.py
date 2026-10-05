"""Phase 8 — kiểm chứng production: video-only không cần story, chẩn đoán lỗi, bí mật không rò rỉ, upload không chặn render,
và kill/restart với ffmpeg + ContentFlow THẬT (cần CF_TEST_CONTENTFLOW_PYTHON)."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
import wave
from pathlib import Path

from contentfactory.contracts import ErrorClass, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator import diagnose as DG
from contentfactory.orchestrator import ops
from contentfactory.orchestrator.cli import main
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.runner import Orchestrator
from tests.fake_yt_uploader import FakeUploader
from tests.support import REPO, RootCase, params, wait_until
from tests.test_publishing import PROJECT, TIKTOK
from tests.test_render import Loop

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
REAL_PY = os.environ.get("CF_TEST_CONTENTFLOW_PYTHON")
HAVE_REAL = bool(REAL_PY and Path(REAL_PY).exists() and HAVE_FFMPEG and (REPO / "modules" / "ContentFlow").is_dir())
FONT = Path(r"C:\Windows\Fonts\arial.ttf")


def edit_config(root: Path, **extra) -> None:
    f = root / "config" / "config.json"
    c = json.loads(f.read_text(encoding="utf-8"))
    for k, v in extra.items():
        c[k] = {**c[k], **v} if isinstance(v, dict) and isinstance(c.get(k), dict) else v
    f.write_text(json.dumps(c), encoding="utf-8")


def silent_wav(path: Path, seconds: float = 4.0) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


# ========================================================================================== video-only không có truyện
class VideoOnlyPackageTest(RootCase):
    def test_audio_only_job_can_be_extended_to_a_full_output_package_without_a_story(self):
        orc = self.orc()
        wav = silent_wav(self.root / "master.wav")
        jid = orc.submit(params(project=PROJECT, tiktok=TIKTOK), mode="VIDEO_ONLY", inputs={"audio_master": str(wav), "metadata": {"title": "Video có sẵn audio"}})
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.OUTPUT_READY)
        self.assertEqual(set(self.runs(orc, jid)), {"audio", "render_youtube", "render_tiktok"})
        orc.set_target(jid, "publish")                                     # trước đây lỗi "thiếu story_text" ở stage output
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        runs = self.runs(orc, jid)
        self.assertTrue(all(v == ["succeeded"] for v in runs.values()), runs)          # nới target không chạy lại stage đã xong
        res = ops.summary(orc, jid, echo=lambda *_: None)
        d = Path(res["output_dir"])
        files = {p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()}
        self.assertNotIn("story.txt", files)
        self.assertTrue({"README.txt", "project.json", "youtube/video.mp4", "youtube/thumbnail.jpg", "youtube/title.txt", "tiktok/part_01.mp4"} <= files)
        pj = json.loads((d / "project.json").read_text(encoding="utf-8"))
        self.assertIsNone(pj["story"])
        self.assertNotIn("story.txt", (d / "README.txt").read_text(encoding="utf-8"))

    def test_package_with_story_is_unchanged(self):
        orc = self.orc()
        jid = orc.submit(params(project=PROJECT, tiktok=TIKTOK))
        orc.run()
        d = Path(ops.summary(orc, jid, echo=lambda *_: None)["output_dir"])
        self.assertTrue((d / "story.txt").is_file())
        self.assertEqual(json.loads((d / "project.json").read_text(encoding="utf-8"))["story"]["file"], "story.txt")


# ========================================================================================== ghi file dùng chung đồng thời
class SharedWriteRaceTest(RootCase):
    def test_concurrent_atomic_writes_to_one_file_never_fail_or_corrupt(self):
        from contentfactory.fsutil import atomic_write_json
        target = self.root / "shared" / "meta.json"
        errors: list[BaseException] = []

        def worker(n: int) -> None:
            try:
                for i in range(60):
                    atomic_write_json(target, {"writer": n, "i": i, "pad": "x" * 2000})
            except BaseException as e:                       # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["pad"], "x" * 2000)           # luôn là một bản hoàn chỉnh
        self.assertEqual([p.name for p in target.parent.iterdir()], ["meta.json"])                     # không còn file tạm

    def test_two_identical_jobs_running_together_both_publish(self):
        """Cùng URL/cùng nội dung chạy song song (rất hay gặp): cùng khóa cache TTS, cùng tên thư mục output."""
        for _ in range(3):
            root_orc = self.orc()
            a, b = root_orc.submit(params(), auto_resume=False), root_orc.submit(params(), auto_resume=False)
            root_orc.run()
            self.assertEqual([root_orc.store.get_job(x)["state"] for x in (a, b)], [P.PUBLISHED, P.PUBLISHED])
            shutil.rmtree(self.root / "output", ignore_errors=True)
            shutil.rmtree(self.root / "workspace", ignore_errors=True)
            (self.root / "workspace").mkdir()


# ========================================================================================== chẩn đoán
class DiagnosticsTest(RootCase):
    def test_failed_job_names_stage_provider_reason_attempts_checkpoint_and_the_way_back(self):
        orc = self.orc()
        jid = orc.submit(params(fake={"tts_chunk_3": {"error_class": "POLICY", "fail_until_attempt": 99}}), auto_resume=False)
        orc.run()
        d = DG.explain(orc, jid)
        self.assertEqual((d["status"], d["stage"], d["provider"], d["reason_code"], d["error_class"]), ("failed", "tts", "fake", "FAKE_FAILURE", "POLICY"))
        self.assertEqual((d["attempts"], d["failed_attempts"]), (1, 1))
        self.assertEqual((d["checkpoint"]["done"], d["checkpoint"]["total"]), (1, 6))
        self.assertEqual(d["inputs_needed"], ["story_text"])
        self.assertEqual(d["inputs_present"], ["story_text"])
        self.assertEqual(d["resume"]["actions"], ["retry"])
        self.assertEqual(d["resume"]["cli"], f"retry {jid}")
        self.assertTrue(Path(d["log_file"]).is_file())
        text = "\n".join(DG.format_lines(d))
        self.assertIn("Giọng đọc (TTS) thất bại", text)
        self.assertIn(f"retry {jid}", text)
        self.assertNotIn("Traceback", text)

    def test_unexpected_exception_is_reported_without_a_stack_trace(self):
        orc = self.orc()

        def boom(*a, **k):
            raise ZeroDivisionError("lỗi bất ngờ trong adapter")
        orc.adapters["story"].run = boom
        orc.adapters["story"].write = boom
        jid = orc.submit(params(fake={"story": {"error_class": "POLICY", "fail_until_attempt": 99}}), auto_resume=False)
        orc.run()
        d = DG.explain(orc, jid)
        self.assertEqual(d["status"], "failed")
        self.assertEqual(d["stage"], "story")
        self.assertNotIn("Traceback", d["human"])
        self.assertTrue(d["reason_code"])

    def test_held_jobs_explain_whether_and_how_they_resume(self):
        orc = self.orc()
        net = {"story": {"error_class": "RESOURCE", "resource": "network", "fail_until_attempt": 99}}
        orc.monitor.probes["network"] = type("Down", (), {"resource": "network", "check": lambda s: (False, "mất mạng (giả lập)")})()
        off = orc.submit(params(fake=net), auto_resume=False)
        on = orc.submit(params(fake=net), auto_resume=True)
        cred = orc.submit(params(fake={"story": {"error_class": "AUTH", "fail_until_attempt": 99}}), auto_resume=True)
        orc.run()
        d_off, d_on, d_cred = (DG.explain(orc, x) for x in (off, on, cred))
        self.assertEqual((d_off["status"], d_off["hold"]["reason"], d_off["resume"]["mode"]), ("waiting", P.PAUSED_NETWORK, "manual"))
        self.assertEqual(d_off["resume"]["actions"], ["resume", "enable_auto_resume"])
        self.assertEqual((d_on["status"], d_on["resume"]["mode"]), ("waiting", "auto"))
        self.assertEqual(d_on["resume"]["actions"], ["resume_now", "disable_auto_resume"])
        self.assertEqual((d_cred["status"], d_cred["hold"]["reason"], d_cred["resume"]["mode"]), ("attention", P.PAUSED_CREDENTIAL, "manual"))
        self.assertEqual(d_off["stage"], "story")
        self.assertIn("Đang chờ mạng", d_off["human"])
        for d in (d_off, d_on, d_cred):
            self.assertTrue(d["hold"]["todo"])
            self.assertEqual(d["retry_budget"], 3)

    def test_ui_status_groups_every_backend_state(self):
        base = {"hold_reason": None, "needs_user": False, "target_idx": 7}
        cases = [({"state": P.NEW}, "queued"), ({"state": P.TTS_RUNNING}, "running"), ({"state": P.AUDIO_READY}, "queued"),
                 ({"state": P.PUBLISHED}, "completed"), ({"state": P.FAILED}, "failed"),
                 ({"state": P.OUTPUT_READY, "target_idx": 5}, "completed"),                 # đạt đích sớm (VIDEO_ONLY) cũng là xong
                 ({"state": P.SOURCE_READY, "hold_reason": P.PAUSED_QUOTA}, "waiting"),
                 ({"state": P.SOURCE_READY, "hold_reason": P.PAUSED_QUOTA, "needs_user": True}, "attention"),
                 ({"state": P.SOURCE_READY, "hold_reason": P.PAUSED_MISSING_INPUT}, "attention")]
        for over, want in cases:
            self.assertEqual(DG.ui_status({**base, **over}), want, over)

    def test_cli_status_prints_the_diagnosis_for_a_failed_job(self):
        orc = self.orc()
        jid = orc.submit(params(fake={"tts_chunk_3": {"error_class": "POLICY", "fail_until_attempt": 99}}), auto_resume=False)
        orc.run()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["--root", str(self.root), "status", jid])
        out = buf.getvalue()
        self.assertIn("provider=fake", out)
        self.assertIn(f"retry {jid}", out)
        self.assertIn("checkpoint: 1/6", out)


# ========================================================================================== bí mật không rò rỉ
class SecretsTest(RootCase):
    TOKEN = "tok-SECRET-b7f3a9"
    API_KEY = "AIza-SECRET-KEY-4c1d"

    def setUp(self):
        super().setUp()
        self.srv = FakeUploader()
        self.srv.TOKEN = self.TOKEN
        self.addCleanup(self.srv.stop)
        edit_config(self.root, adapters={"publish": "yt_uploader"},
                    tools={"yt_uploader": {"url": self.srv.url, "token": self.TOKEN, "poll_s": 0.01, "max_wait_s": 30}})
        os.environ["YOUTUBE_API_KEY"] = self.API_KEY
        self.addCleanup(os.environ.pop, "YOUTUBE_API_KEY", None)
        d = self.root / "channels" / "k"
        d.mkdir(parents=True)
        (d / "channel.json").write_text(json.dumps({"name": "K", "publishing": {"made_for_kids": False}}), encoding="utf-8")

    def everything(self) -> list[Path]:
        return [p for p in self.root.rglob("*") if p.is_file() and (self.root / "config") not in p.parents]     # config/ = nơi người dùng tự đặt bí mật

    def test_tokens_and_api_keys_never_land_in_logs_db_workspace_or_output(self):
        orc = self.orc()
        out: list[str] = []
        res = ops.go(orc, "https://youtu.be/x", "k", title="Bí Mật", kids=False, echo=out.append)
        self.assertTrue(res["ok"], out)
        self.assertEqual(len([r for r in self.srv.requests if r[0] == "POST"]), 1)
        leaks = [str(p.relative_to(self.root)) for p in self.everything()
                 if any(s.encode() in p.read_bytes() for s in (self.TOKEN, self.API_KEY))]
        self.assertEqual(leaks, [], "bí mật xuất hiện trong file")
        for s in (self.TOKEN, self.API_KEY):
            self.assertNotIn(s, "\n".join(out))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["--root", str(self.root), "status"])
            main(["--root", str(self.root), "doctor", "--json"])
        self.assertNotIn(self.TOKEN, buf.getvalue())
        self.assertNotIn(self.API_KEY, buf.getvalue())

    def test_auth_failure_message_does_not_echo_the_token(self):
        edit_config(self.root, tools={"yt_uploader": {"url": self.srv.url, "token": "sai-token-xyz", "poll_s": 0.01, "max_wait_s": 30}}, auto={"hold_wait_s": 2})
        orc = self.orc()
        res = ops.go(orc, "https://youtu.be/x", "k", title="Bí Mật", kids=False, echo=lambda *_: None)
        self.assertFalse(res["ok"])
        for p in self.everything():
            self.assertNotIn(b"sai-token-xyz", p.read_bytes(), p.name)
        d = DG.explain(orc, res["job_id"])
        self.assertEqual(d["hold"]["reason"], P.PAUSED_CREDENTIAL)         # token sai: cần người xử lý, không phải lỗi vĩnh viễn
        self.assertEqual(d["status"], "attention")
        self.assertTrue(d["hold"]["todo"])
        self.assertNotIn("sai-token-xyz", json.dumps(d, ensure_ascii=False))

    def test_secret_files_are_gitignored(self):
        gi = (REPO / ".gitignore").read_text(encoding="utf-8")
        for pat in ("config.local.json", "secrets.local.env", "/tools/"):
            self.assertIn(pat, gi)


# ========================================================================================== upload chậm không chặn render
class UploadDoesNotBlockTest(RootCase):
    def test_slow_upload_of_one_job_does_not_stop_the_next_job_from_rendering(self):
        srv = FakeUploader()
        self.addCleanup(srv.stop)
        edit_config(self.root, adapters={"publish": "yt_uploader"}, tools={"yt_uploader": {"url": srv.url, "token": FakeUploader.TOKEN, "poll_s": 0.01, "max_wait_s": 60}})
        orc = self.orc()
        srv.mode = "slow"
        a = orc.submit(params(tiktok=TIKTOK), auto_resume=False)
        state = lambda j: orc.store.get_job(j)["state"]          # noqa: E731
        with Loop(orc):
            wait_until(lambda: state(a) == P.UPLOADING and len(srv.jobs) == 1, timeout=30, what="A đang upload")
            srv.mode = "ok"
            b = orc.submit(params(tiktok=TIKTOK), auto_resume=False)
            wait_until(lambda: state(b) in (P.UPLOAD_READY, P.UPLOADING, P.PUBLISHED), timeout=60, what="B render xong dù A còn đang upload")
            self.assertEqual(state(a), P.UPLOADING)                # A vẫn đang upload chậm
            self.assertFalse(any(x["status"] == "running" for x in orc.store.stage_runs(b) if x["stage"] in ("render_youtube", "render_tiktok")))
            srv.release()
            wait_until(lambda: all(state(x) == P.PUBLISHED for x in (a, b)), timeout=60, what="cả hai xong")


# ========================================================================================== kill/restart với ffmpeg + ContentFlow THẬT
@unittest.skipUnless(HAVE_REAL, "cần CF_TEST_CONTENTFLOW_PYTHON (Python có Pillow) + ffmpeg/ffprobe + modules/ContentFlow")
class RealKillRestartTest(RootCase):
    def setUp(self):
        super().setUp()

        def ff(*a):
            subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *a], check=True)
        self.raw = {"h": self.root / "raw_h", "v": self.root / "raw_v"}
        for k, (d, size) in {"h": (self.raw["h"], "640x360"), "v": (self.raw["v"], "360x640")}.items():
            d.mkdir()
            for i in (1, 2):
                ff("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration=4", "-pix_fmt", "yuv420p", str(d / f"c{i}.mp4"))
        ff("-f", "lavfi", "-i", "color=c=0x203040:s=1648x928", "-frames:v", "1", str(self.root / "template.png"))
        chunk = self.root / "chunk.wav"
        ff("-f", "lavfi", "-i", "sine=frequency=300:duration=3:sample_rate=48000", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.7",
           "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]", "-map", "[a]", "-c:a", "pcm_s16le", str(chunk))
        self.wav = self.root / "long.wav"
        ff("-stream_loop", "15", "-i", str(chunk), "-c:a", "pcm_s16le", str(self.wav))                 # ~59 s, có nhịp nghỉ để cắt part
        thumb = {"config_overrides": {"template": {"file": str(self.root / "template.png")}, "title": {"font": str(FONT)}, "channel": {"font": str(FONT)}}}
        edit_config(self.root, adapters={"audio": "ffmpeg", "render": "contentflow"},
                    tools={"contentflow": {"root": str(REPO / "modules" / "ContentFlow"), "python": REAL_PY, "base_dir": str(self.root / "cfbase")}},
                    render={"pools_dir": str(self.root / "pools"), "pool_sync_interval_s": 3600,
                            "pools": {"gameplay": {"raw_dir": str(self.raw["h"]), "sync": {"size": "640x360", "fps": 25, "quality": "fast", "encoder": "cpu"}},
                                      "gameplay_vertical": {"raw_dir": str(self.raw["v"]), "sync": {"size": "360x640", "fps": 25, "quality": "fast", "encoder": "cpu"}}},
                            "profiles": {"youtube": {"resolution": "640x360", "encoder": "libx264", "thumbnail": thumb},
                                         "tiktok": {"resolution": "360x640", "encoder": "libx264"}}})

    def probe(self, p: Path) -> dict:
        d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(p)],
                                      capture_output=True, text=True, check=True).stdout)
        v = next(s for s in d["streams"] if s["codec_type"] == "video")
        return {"w": v["width"], "h": v["height"], "dur": float(d["format"]["duration"]), "audio": any(s["codec_type"] == "audio" for s in d["streams"])}

    def test_killing_the_runner_during_the_real_render_resumes_without_redoing_audio(self):
        orc = self.orc()
        audio = {"qa": {"silence": {"max_ratio": 0.8, "max_gap_s": 8.0}}}
        jid = orc.submit(params(audio=audio, tiktok={"speed": 2.0, "target_part_sec": 12}, project=PROJECT), mode="VIDEO_ONLY",
                         inputs={"audio_master": str(self.wav), "metadata": {"title": "Kill thật"}}, auto_resume=False)
        env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
        proc = subprocess.Popen([sys.executable, "-m", "contentfactory", "--root", str(self.root), "run"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_until(lambda: orc.store.get_job(jid)["state"] == P.YOUTUBE_RENDERING, timeout=60, what="đang render YouTube thật")
            time.sleep(0.5)
        finally:
            proc.kill()
            proc.wait()
        self.assertEqual(orc.store.get_job(jid)["state"], P.YOUTUBE_RENDERING)           # DB còn nguyên trạng thái lúc chết
        t0 = time.time()
        orc2 = self.orc()
        orc2.run()                                                                       # lease hết hạn -> nhận lại -> render xong
        j = orc2.store.get_job(jid)
        self.assertEqual(j["state"], P.OUTPUT_READY, j.get("last_error"))
        runs = self.runs(orc2, jid)
        self.assertEqual(runs["audio"], ["succeeded"])                                    # không xử lý lại audio
        self.assertEqual(runs["render_youtube"], ["interrupted", "succeeded"])
        self.assertEqual(runs["render_tiktok"], ["succeeded"])
        arts = {a["kind"]: a for a in orc2.store.artifacts(jid)}
        yv = self.probe(self.job_dir(jid) / arts["video_youtube"]["path"])
        self.assertEqual((yv["w"], yv["h"], yv["audio"]), (640, 360, True))
        self.assertAlmostEqual(yv["dur"], 59.2, delta=2.0)
        parts = [a for a in orc2.store.artifacts(jid) if a["kind"] == "video_tiktok"]
        self.assertGreaterEqual(len(parts), 2)
        total = 0.0
        for a in sorted(parts, key=lambda x: json.loads(x["meta"])["index"] if isinstance(x["meta"], str) else x["meta"]["index"]):
            v = self.probe(self.job_dir(jid) / a["path"])
            self.assertEqual((v["w"], v["h"], v["audio"]), (360, 640, True))
            total += v["dur"]
        self.assertAlmostEqual(total * 2, yv["dur"], delta=3.0)                           # các part TikTok x2 khớp tổng audio YouTube
        stray = [p.name for p in self.job_dir(jid).rglob("*") if p.suffix in (".part", ".tmp") or p.name.startswith(".tmp")]
        self.assertEqual(stray, [], "file tạm bị bỏ lại trong workspace sau khi resume")
        self.assertLess(time.time() - t0, 120)


if __name__ == "__main__":
    unittest.main()
