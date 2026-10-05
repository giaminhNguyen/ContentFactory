"""Dữ liệu mẫu cho người chưa có truyện/video: sinh đúng, an toàn khi chạy lại, không ghi đè cấu hình, và THỰC SỰ chạy được qua pipeline/UI."""
import contextlib
import io
import json
import shutil
import subprocess
import unittest
import wave
from pathlib import Path

from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator import samples as SM
from contentfactory.orchestrator.cli import main
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.service import Service
from contentfactory.orchestrator.service_admin import AdminService
from contentfactory.story.validate import validate_story_text
from tests.test_automode import LENIENT_AUDIO, write_channel, write_config
from tests.test_ui import UiCase

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class SamplesTest(UiCase):
    def test_files_are_valid_inputs(self):
        r = SM.make_samples(load_config(self.root))
        d = Path(r["dir"])
        self.assertEqual(d, (self.root / "samples").resolve())
        text = Path(r["story"]).read_text(encoding="utf-8")
        self.assertEqual(validate_story_text(text), [])                       # qua đúng bộ kiểm story của hệ thống (không heading/marker)
        self.assertGreater(len(text.split("\n\n")), 5)
        with wave.open(r["audio"]) as w:
            self.assertGreaterEqual(w.getnframes() / w.getframerate(), 40)
        self.assertIn("-->", Path(r["subtitle"]).read_text(encoding="utf-8"))
        det = {k: self.svc.detect_input(r[k]) for k in ("story", "subtitle", "audio")}
        self.assertEqual({k: v["kind"] for k, v in det.items()}, {"story": "story_text", "subtitle": "transcript_file", "audio": "audio"})
        self.assertTrue(all(v["ok"] and v["modes"] for v in det.values()))

    @unittest.skipUnless(HAVE_FFMPEG, "cần ffmpeg")
    def test_videos_pools_and_thumbnail_are_created_and_registered_without_overriding(self):
        cfg = load_config(self.root)
        r = SM.make_samples(cfg)
        for sub, (w, h) in (("video_ngang", (1280, 720)), ("video_doc", (720, 1280))):
            clips = sorted((Path(r["dir"]) / sub).glob("*.mp4"))
            self.assertEqual(len(clips), 3)
            info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(clips[0])], capture_output=True, text=True, check=True).stdout)
            v = next(s for s in info["streams"] if s["codec_type"] == "video")
            self.assertEqual((v["width"], v["height"]), (w, h))
        local = json.loads((self.root / "config" / "config.local.json").read_text(encoding="utf-8"))
        self.assertEqual({k: v["orientation"] for k, v in local["render"]["pools"].items()}, {"gameplay": "landscape", "gameplay_vertical": "portrait"})
        self.assertEqual(len(r["registered"]) >= 2, True)
        self.assertEqual(cfg.data["render"]["pools"]["gameplay"]["orientation"], "landscape")           # cấu hình đang chạy cũng được cập nhật
        again = SM.make_samples(load_config(self.root))
        self.assertEqual((again["created"], again["registered"]), ([], []))                              # chạy lại: không làm gì thừa
        self.assertGreaterEqual(len(again["skipped"]), 9)
        # người dùng đã có pool riêng: không bao giờ bị đè
        mine = self.root / "my_videos"
        mine.mkdir()
        local["render"]["pools"] = {"gameplay": {"raw_dir": str(mine)}}
        (self.root / "config" / "config.local.json").write_text(json.dumps(local), encoding="utf-8")
        other = self.root / "other_samples"
        r2 = SM.make_samples(load_config(self.root), other)
        self.assertEqual([x for x in r2["registered"] if "pool" in x], [])
        self.assertEqual(json.loads((self.root / "config" / "config.local.json").read_text(encoding="utf-8"))["render"]["pools"]["gameplay"]["raw_dir"], str(mine))

    def test_missing_ffmpeg_degrades_with_a_clear_message(self):
        write_config(self.root, tools={"ffmpeg": str(self.root / "khong-co-ffmpeg")})
        orig = shutil.which
        shutil.which = lambda name, *a, **k: None if "ffmpeg" in name else orig(name, *a, **k)
        self.addCleanup(setattr, shutil, "which", orig)
        r = SM.make_samples(load_config(self.root))
        self.assertTrue(any("ffmpeg" in w for w in r["warnings"]))
        self.assertTrue(Path(r["story"]).is_file() and Path(r["audio"]).is_file())                        # phần không cần ffmpeg vẫn có
        self.assertEqual(r["registered"], [])

    def test_force_recreates(self):
        r = SM.make_samples(load_config(self.root), register=False)
        Path(r["story"]).write_text("hỏng", encoding="utf-8")
        SM.make_samples(load_config(self.root), register=False)
        self.assertEqual(Path(r["story"]).read_text(encoding="utf-8"), "hỏng")                           # không tự đè file người dùng đã sửa
        SM.make_samples(load_config(self.root), register=False, force=True)
        self.assertNotEqual(Path(r["story"]).read_text(encoding="utf-8"), "hỏng")

    def test_cli_and_api(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--root", str(self.root), "samples", "--no-register"])
        self.assertEqual(rc, 0)
        self.assertIn("truyen_mau.txt", buf.getvalue())
        r = AdminService(self.o).make_samples()
        self.assertEqual(r["skipped"].count("truyen_mau.txt"), 1)


class SamplesRunTest(UiCase):
    """Dùng chính dữ liệu mẫu chạy qua facade như người dùng bấm RUN trong UI."""

    def test_sample_story_and_audio_run_through_the_pipeline(self):
        r = SM.make_samples(load_config(self.root), register=False)
        a = self.svc.create_run({"input": {"value": r["story"]}, "channel": "kenh", "run": "story_full", "title": "Ngôi nhà cuối ngõ", "kids": False})
        b = self.svc.create_run({"input": {"value": r["audio"]}, "channel": "kenh", "run": "audio_package", "title": "Audio mẫu"})
        self.o.run()
        for j in (a, b):
            d = self.svc.job_detail(j["job_id"])
            self.assertEqual(d["status"], "completed", d["diagnosis"]["human"])
            self.assertTrue(Path(d["output"]["project_dir"]).is_dir())

    @unittest.skipUnless(HAVE_FFMPEG, "cần ffmpeg")
    def test_sample_audio_passes_the_real_audio_pipeline(self):
        write_config(self.root, adapters={"audio": "ffmpeg"})
        o = self.orc()
        svc = Service(o)
        r = SM.make_samples(load_config(self.root), register=False)
        j = svc.create_run({"input": {"value": r["audio"]}, "channel": "kenh", "run": "audio_video", "title": "Audio mẫu"})
        o.run()
        d = svc.job_detail(j["job_id"])
        self.assertEqual(d["status"], "completed", d["diagnosis"]["human"])                               # QA audio thật (silence/loudness/clipping) chấp nhận audio mẫu


if __name__ == "__main__":
    unittest.main()
