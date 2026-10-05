import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from contentfactory.contracts import ErrorClass, StageError
from contentfactory.source.processor import YouTubeSourceProcessor
from contentfactory.source.youtube import YtDlp
from tests.fakes import FIXTURES, URL, FakeYtDlp, make_ctx

HERE = Path(__file__).resolve().parent


class SourceProcessorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-src-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.yt = FakeYtDlp()
        self.proc = YouTubeSourceProcessor({}, self.tmp / "cache", self.yt)

    def run_job(self, job_id="000001", proc=None, **params):
        ctx = make_ctx(self.tmp, "source", params, job_id=job_id)
        res = (proc or self.proc).process({"kind": "youtube_url", "value": URL}, ctx.stage_dir, ctx)
        return res, ctx

    def stamp(self, res):
        return {k: res[k].stat().st_mtime_ns for k in ("subtitle_raw", "structured", "transcript", "metadata")}

    def test_first_run_keeps_raw_structured_and_clean(self):
        res, _ = self.run_job()
        self.assertEqual((self.yt.info_calls, self.yt.download_calls), (1, 1))
        # phụ đề gốc: nguyên byte
        self.assertEqual(res["subtitle_raw"].read_bytes(), (FIXTURES / "manual_split.srt").read_bytes())
        s = json.loads(res["structured"].read_text(encoding="utf-8"))
        self.assertEqual(s["provenance"]["kind"], "manual")
        self.assertTrue(all({"start", "end", "text", "gap_before"} <= set(c) for c in s["cues"]))   # timestamp còn nguyên
        clean = res["transcript"].read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\d{1,2}:\d{2}|-->", clean))
        self.assertTrue(clean.startswith("Hôm qua tôi đi chợ và gặp một người bạn cũ ở cổng. Ông ấy hỏi thăm"))
        meta = json.loads(res["metadata"].read_text(encoding="utf-8"))
        self.assertEqual((meta["title"], meta["language"]), ("Chuyện ma ở nhà cũ", "vi"))
        self.assertNotIn("description", meta)             # không chép mô tả của video gốc thành mô tả của ta

    def test_rerun_in_same_workspace_downloads_and_rewrites_nothing(self):
        res, _ = self.run_job()
        before = self.stamp(res)
        res2, ctx = self.run_job()
        self.assertEqual((self.yt.info_calls, self.yt.download_calls), (1, 1))
        self.assertEqual(self.stamp(res2), before)        # không file nào bị ghi lại
        self.assertTrue(res2["stats"]["reused_parse"])
        self.assertIn("source_raw_reused", [e for e, _ in ctx.events])

    def test_new_job_for_same_video_uses_cache_instead_of_downloading(self):
        self.run_job("000001")
        res2, _ = self.run_job("000002")
        self.assertEqual((self.yt.info_calls, self.yt.download_calls), (1, 1))
        self.assertEqual(res2["stats"]["raw_origin"], "cache")
        self.assertEqual(res2["subtitle_raw"].read_bytes(), (FIXTURES / "manual_split.srt").read_bytes())

    def test_corrupted_raw_is_restored_without_network_and_reparsed(self):
        res, _ = self.run_job()
        res["subtitle_raw"].write_bytes(b"corrupted")
        res2, _ = self.run_job()
        self.assertEqual((self.yt.info_calls, self.yt.download_calls), (1, 1))
        self.assertEqual(res2["subtitle_raw"].read_bytes(), (FIXTURES / "manual_split.srt").read_bytes())
        self.assertEqual(res2["transcript"].read_text(encoding="utf-8"), res["transcript"].read_text(encoding="utf-8"))

    def test_refresh_forces_a_new_download(self):
        self.run_job()
        self.run_job(refresh_source=True)
        self.assertEqual(self.yt.download_calls, 2)

    def test_changing_reconstruction_config_reparses_but_does_not_download(self):
        res, _ = self.run_job()
        other = YouTubeSourceProcessor({"reconstruct": {"sentence_gap": 0.1}}, self.tmp / "cache", self.yt)
        res2, _ = self.run_job(proc=other)
        self.assertEqual(self.yt.download_calls, 1)
        self.assertFalse(res2["stats"]["reused_parse"])
        self.assertEqual(json.loads(res2["structured"].read_text(encoding="utf-8"))["provenance"]["config"]["sentence_gap"], 0.1)

    def test_auto_caption_is_used_only_when_no_manual_track(self):
        yt = FakeYtDlp("auto_rolling.vtt", manual=False)
        res, _ = self.run_job(proc=YouTubeSourceProcessor({}, self.tmp / "cache", yt))
        self.assertEqual(res["stats"]["subtitle_kind"], "auto")
        self.assertEqual(res["stats"]["subtitle_lang"], "vi")

    def test_errors_are_classified(self):
        class NoSubs(FakeYtDlp):
            def info(self, url):
                return {"id": "abcdefghijk", "title": "x", "subtitles": {}, "automatic_captions": {}}
        with self.assertRaises(StageError) as cm:
            self.run_job(proc=YouTubeSourceProcessor({}, self.tmp / "c2", NoSubs()))
        self.assertEqual((cm.exception.error_class, cm.exception.code), (ErrorClass.POLICY, "NO_SUBTITLES"))
        with self.assertRaises(StageError) as cm:
            ctx = make_ctx(self.tmp, "source")
            self.proc.process({"kind": "youtube_url", "value": "https://vimeo.com/123"}, ctx.stage_dir, ctx)
        self.assertEqual(cm.exception.code, "NOT_YOUTUBE_URL")


class YtDlpWrapperTest(unittest.TestCase):
    """Chạy lớp YtDlp thật với một tiến trình giả lập yt-dlp (subprocess thật)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-ytd-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["FAKE_FIXTURE"] = str(FIXTURES / "gaps.vtt")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_FIXTURE", "FAKE_YTDLP_MODE")])
        self.yt = YtDlp([sys.executable, str(HERE / "fake_ytdlp.py")])

    def test_info_and_subtitle_download_through_a_real_subprocess(self):
        info = self.yt.info(URL)
        self.assertEqual(info["title"], "Chuyện ma ở nhà cũ")
        got = self.yt.download_subtitle(URL, "vi", False, self.tmp / "dl")
        self.assertEqual(got.read_bytes(), (FIXTURES / "gaps.vtt").read_bytes())

    def test_full_processor_with_real_wrapper(self):
        proc = YouTubeSourceProcessor({}, self.tmp / "cache", self.yt)
        ctx = make_ctx(self.tmp, "source")
        res = proc.process({"kind": "youtube_url", "value": URL}, ctx.stage_dir, ctx)
        self.assertEqual(res["stats"]["subtitle_kind"], "manual")
        self.assertEqual(res["stats"]["paragraphs"], 2)

    def test_missing_binary_is_a_resource_error(self):
        with self.assertRaises(StageError) as cm:
            YtDlp(["khong-co-yt-dlp-nao-ca"]).info(URL)
        self.assertEqual((cm.exception.error_class, cm.exception.code), (ErrorClass.RESOURCE, "YTDLP_MISSING"))

    def test_private_video_is_policy_and_rate_limit_is_transient(self):
        os.environ["FAKE_YTDLP_MODE"] = "private"
        with self.assertRaises(StageError) as cm:
            self.yt.info(URL)
        self.assertEqual((cm.exception.error_class, cm.exception.code), (ErrorClass.POLICY, "VIDEO_UNAVAILABLE"))
        os.environ["FAKE_YTDLP_MODE"] = "rate"
        with self.assertRaises(StageError) as cm:
            self.yt.info(URL)
        self.assertEqual(cm.exception.error_class, ErrorClass.TRANSIENT)


if __name__ == "__main__":
    unittest.main()
