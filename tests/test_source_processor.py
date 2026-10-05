import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from contentfactory.contracts import ErrorClass, StageError
from contentfactory.source.chain import ProviderChain
from contentfactory.source.providers import YtDlpProvider
from contentfactory.source.youtube import YtDlp
from tests.fakes import FIXTURES, URL, FakeYtDlp, make_ctx

HERE = Path(__file__).resolve().parent


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

    def test_full_chain_with_real_wrapper(self):
        chain = ProviderChain([YtDlpProvider(ytdlp=self.yt)], self.tmp / "cache")
        ctx = make_ctx(self.tmp, "source")
        res = chain.acquire({"kind": "youtube_url", "value": URL}, ctx.stage_dir, ctx)
        self.assertEqual((res["provider"], res["subtitle_kind"], res["subtitle_format"]), ("ytdlp", "manual", "vtt"))
        self.assertEqual(res["raw_subtitle_path"].read_bytes(), (FIXTURES / "gaps.vtt").read_bytes())

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
