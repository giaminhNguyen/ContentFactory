"""Watermark tạo bằng TTS: dùng CHÍNH TTSManager/profile/adapter của narration, fingerprint/cache, sửa → revision mới, lỗi không làm hỏng bản đang dùng."""
import json
import wave
from pathlib import Path

from contentfactory.adapters.fake import FakeTTS
from contentfactory.contracts import ErrorClass, StageError
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.registry import build_adapters
from contentfactory.orchestrator.runner import Orchestrator
from contentfactory.orchestrator.service_watermarks import WatermarkService
from tests.support import RootCase, params
from tests.test_automode import tts_profile, write_channel


class CountingTTS(FakeTTS):
    """TTS giả ghi lại mọi lời gọi (đúng văn bản, đúng profile) và có thể bị ép lỗi."""
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail: StageError | None = None

    def synthesize(self, segment, profile, out_path, ctx):
        if self.fail:
            raise self.fail
        self.calls.append((segment["text"], profile))
        return super().synthesize(segment, profile, out_path, ctx)


class TtsCase(RootCase):
    def setUp(self):
        super().setUp()
        self.d = write_channel(self.root, "kenh", {"name": "Kênh"})
        tts_profile("giong_a", self.root, tuned=True)
        tts_profile("giong_b", self.root)
        f = self.root / "tts_profiles" / "giong_b.json"                                                      # hai profile phải KHÁC giọng thì mới khác âm thanh (tên profile không tính)
        prof = json.loads(f.read_text(encoding="utf-8"))
        prof["voice"] = {"value": "giong-b", "source": "user", "confidence": "high", "evidence": []}
        f.write_text(json.dumps(prof), encoding="utf-8")
        cfg = load_config(self.root)
        self.tts = CountingTTS()
        self.orc_ = Orchestrator(cfg, adapters={**build_adapters(cfg), "tts": self.tts})
        self.svc = WatermarkService(self.orc_)
        self.lib = self.orc_.watermarks

    def code(self, fn, *a, **kw):
        with self.assertRaises(StageError) as cm:
            fn(*a, **kw)
        return cm.exception.code


class CreateTest(TtsCase):
    def test_text_goes_through_the_narration_tts_path_and_is_persisted(self):
        r = self.svc.create_tts("kenh", "Intro truyện đêm", "  Bạn đang nghe truyện tại kênh ABC.  ", "giong_a")
        item = r["item"]
        self.assertEqual((r["result"], item["source"], item["current_revision"], item["valid"]), ("created", "tts", 1, True))
        self.assertGreater(item["duration_sec"], 0)
        self.assertEqual([t for t, _ in self.tts.calls], ["Bạn đang nghe truyện tại kênh ABC."])        # đúng văn bản đã trim, không thêm câu nào
        meta = self.lib.revision("kenh", item["id"], 1)
        self.assertEqual(meta["tts"]["source_text"], "Bạn đang nghe truyện tại kênh ABC.")
        self.assertEqual((meta["tts"]["profile"], meta["tts"]["engine"], meta["tts"]["selection"]), ("giong_a", "fake", "giong_a"))
        self.assertEqual(len(meta["fingerprint"]), 64)
        self.assertEqual(Path(self.lib.revision_path("kenh", item["id"], meta)).read_bytes()[:4], b"RIFF")   # WAV hợp lệ
        again = Orchestrator(load_config(self.root)).watermarks.get("kenh", item["id"])                      # khởi động lại vẫn còn
        self.assertEqual((again["name"], again["text"]), ("Intro truyện đêm", "Bạn đang nghe truyện tại kênh ABC."))

    def test_auto_uses_the_same_resolver_as_narration_and_reports_the_choice(self):
        r = self.svc.create_tts("kenh", "Auto", "Xin chào các bạn.", "auto")
        tts = self.lib.revision("kenh", r["item"]["id"], 1)["tts"]
        self.assertEqual((tts["selection"], tts["profile"]), ("auto", "giong_a"))                            # ready + đã Auto Tune thắng giong_b
        self.assertIn("tự chọn", tts["profile_why"])
        opts = self.svc.tts_options("kenh")
        self.assertEqual((opts["available"], opts["auto"]["profile"], [p["name"] for p in opts["profiles"]]), (True, "giong_a", ["giong_a", "giong_b"]))
        write_channel(self.root, "kenh2", {"name": "K2", "preset": {"tts_profile": "giong_b"}})
        r2 = self.svc.create_tts("kenh2", "Theo preset", "Xin chào.", "auto")
        self.assertEqual(self.lib.revision("kenh2", r2["item"]["id"], 1)["tts"]["profile"], "giong_b")        # preset của kênh thắng Auto

    def test_validation_errors_are_clear_and_nothing_is_created(self):
        self.assertEqual(self.code(self.svc.create_tts, "kenh", "A", "   "), "WATERMARK_TEXT_EMPTY")
        self.assertEqual(self.code(self.svc.create_tts, "kenh", " ", "Xin chào"), "WATERMARK_NAME_EMPTY")
        self.assertEqual(self.code(self.svc.create_tts, "kenh", "A", "x" * 1001), "WATERMARK_TEXT_TOO_LONG")
        self.assertEqual(self.code(self.svc.create_tts, "kenh", "A", "Xin chào", "khong_co"), "TTS_PROFILE_NOT_FOUND")
        self.assertEqual((self.tts.calls, self.lib.list("kenh")["items"]), ([], []))

    def test_long_text_reuses_the_segmenter_and_still_produces_one_valid_wav(self):
        text = " ".join(f"Đây là câu số {i} của một lời giới thiệu khá dài." for i in range(1, 20))
        r = self.svc.create_tts("kenh", "Dài", text, "giong_a")
        self.assertGreater(len(self.tts.calls), 1)                                                          # vượt max_chars của profile ⇒ planner chia đoạn
        self.assertTrue(r["item"]["valid"])

    def test_request_id_makes_double_submit_idempotent(self):
        a = self.svc.create_tts("kenh", "A", "Xin chào.", "giong_a", request_id="rid-1")["item"]
        b = self.svc.create_tts("kenh", "A", "Xin chào.", "giong_a", request_id="rid-1")["item"]
        self.assertEqual((a["id"], len(self.lib.list("kenh")["items"])), (b["id"], 1))


class RevisionTest(TtsCase):
    def make(self, text="Xin chào.", sel="giong_a"):
        item = self.svc.create_tts("kenh", "Intro", text, sel)["item"]
        self.lib.activate("kenh", item["id"])
        return item

    def test_same_semantics_means_no_new_revision_and_no_provider_call(self):
        item = self.make()
        n = len(self.tts.calls)
        r = self.svc.regenerate("kenh", item["id"])
        self.assertEqual((r["result"], r["item"]["current_revision"]), ("unchanged", 1))
        r = self.svc.update_tts("kenh", item["id"], text="  Xin chào.  ", selection="giong_a")             # cùng nội dung sau trim: vẫn không gọi engine
        self.assertEqual((r["result"], len(self.tts.calls)), ("unchanged", n))

    def test_changing_text_or_voice_creates_a_new_revision_and_follows_active(self):
        item = self.make()
        old = self.lib.revision("kenh", item["id"], 1)
        old_bytes = self.lib.revision_path("kenh", item["id"], old).read_bytes()
        r = self.svc.update_tts("kenh", item["id"], text="Chào bạn nhé.", name="Intro 2")
        self.assertEqual((r["result"], r["item"]["current_revision"], r["item"]["name"], r["item"]["active_revision"]), ("revised", 2, "Intro 2", 2))
        self.assertEqual(self.lib.revision_path("kenh", item["id"], old).read_bytes(), old_bytes)           # bản 1 bất biến
        fp = {m["revision"]: m["fingerprint"] for m in self.lib.revisions("kenh", item["id"])}
        r = self.svc.update_tts("kenh", item["id"], selection="giong_b")                                    # đổi giọng ⇒ fingerprint khác ⇒ bản 3
        self.assertEqual(r["item"]["current_revision"], 3)
        fp[3] = self.lib.revision("kenh", item["id"], 3)["fingerprint"]
        self.assertEqual(len(set(fp.values())), 3)
        self.assertEqual(self.lib.revision("kenh", item["id"], 3)["tts"]["profile"], "giong_b")
        self.assertEqual(self.lib.active_ref("kenh"), {"id": item["id"], "revision": 3})

    def test_rename_only_is_metadata(self):
        item = self.make()
        n = len(self.tts.calls)
        r = self.svc.update_tts("kenh", item["id"], name="Tên khác")
        self.assertEqual((r["result"], r["item"]["name"], r["item"]["current_revision"], len(self.tts.calls)), ("unchanged", "Tên khác", 1, n))

    def test_provider_failure_keeps_the_active_revision_and_leaves_nothing_behind(self):
        item = self.make()
        self.tts.fail = StageError(ErrorClass.TRANSIENT, "TTS_TIMEOUT", "hết thời gian chờ")
        self.assertEqual(self.code(self.svc.update_tts, "kenh", item["id"], text="Nội dung mới hoàn toàn."), "TTS_GENERATION_FAILED")
        self.tts.fail = StageError(ErrorClass.AUTH, "BAD_KEY", "thiếu khóa", resource="credential")
        self.assertEqual(self.code(self.svc.update_tts, "kenh", item["id"], text="Nội dung mới hoàn toàn."), "TTS_UNAVAILABLE")
        self.assertEqual((self.lib.active_ref("kenh"), self.lib.get("kenh", item["id"])["current_revision"]), ({"id": item["id"], "revision": 1}, 1))
        self.assertEqual([m["revision"] for m in self.lib.revisions("kenh", item["id"])], [1])
        wm_dir = self.d / "watermarks" / item["id"]
        self.assertEqual(sorted(p.name for p in wm_dir.iterdir()), ["rev_0001.json", "rev_0001.wav"])        # không .part, không file nửa vời
        self.tts.fail = None                                                                                 # hết lỗi: tạo bình thường (không kẹt trạng thái bận)
        self.assertEqual(self.svc.update_tts("kenh", item["id"], text="Nội dung mới hoàn toàn.")["item"]["current_revision"], 2)

    def test_failed_create_leaves_no_watermark(self):
        self.tts.fail = StageError(ErrorClass.POLICY, "ENGINE_DOWN", "engine hỏng")
        self.assertEqual(self.code(self.svc.create_tts, "kenh", "A", "Xin chào."), "TTS_GENERATION_FAILED")
        self.assertEqual(self.lib.list("kenh", include_archived=True)["items"], [])

    def test_provider_cache_prevents_a_second_synthesis_for_identical_chunks(self):
        a = self.make("Một câu duy nhất.")
        n = len(self.tts.calls)
        b = self.svc.create_tts("kenh", "Bản sao", "Một câu duy nhất.", "giong_a")                          # cùng văn bản + giọng, watermark mới: chunk lấy từ cache chung
        self.assertEqual((len(self.tts.calls), b["reused_cache"]), (n, True))
        self.assertNotEqual(a["id"], b["item"]["id"])


class UploadTest(TtsCase):
    def wav_bytes(self, byte=1, seconds=0.4) -> bytes:
        p = self.root / f"u{byte}.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(bytes([byte, 0]) * int(8000 * seconds))
        return p.read_bytes()

    def test_upload_creates_library_entry_and_replace_makes_a_new_revision(self):
        r = self.svc.create_upload("kenh", "Intro tải lên", "../Intro tôi!.wav", self.wav_bytes(1), activate=True)
        item = r["item"]
        self.assertEqual((item["source"], item["active"], item["name"]), ("upload", True, "Intro tải lên"))
        self.assertEqual(self.lib.revision("kenh", item["id"], 1)["upload"]["filename"], "Intro_tôi_.wav")      # tên file đã làm sạch, không thoát khỏi thư mục
        r2 = self.svc.replace_upload("kenh", item["id"], "v2.wav", self.wav_bytes(5))
        self.assertEqual((r2["item"]["current_revision"], r2["item"]["active_revision"]), (2, 2))
        self.assertEqual(self.code(self.svc.update_tts, "kenh", item["id"], text="x"), "WATERMARK_NOT_TTS")
        tts = self.svc.create_tts("kenh", "TTS", "Xin chào.", "giong_a")["item"]
        self.assertEqual(self.code(self.svc.replace_upload, "kenh", tts["id"], "x.wav", self.wav_bytes(2)), "WATERMARK_NOT_UPLOAD")

    def test_invalid_uploads_rejected(self):
        self.assertEqual(self.code(self.svc.create_upload, "kenh", "A", "x.exe", b"MZ"), "WATERMARK_AUDIO_INVALID")
        self.assertEqual(self.code(self.svc.create_upload, "kenh", "A", "x.wav", b"garbage"), "WATERMARK_AUDIO_INVALID")
        self.assertEqual(self.code(self.svc.create_upload, "kenh", " ", "x.wav", self.wav_bytes()), "WATERMARK_NAME_EMPTY")
        self.assertEqual(self.lib.list("kenh", include_archived=True)["items"], [])
        self.assertEqual(self.code(self.svc.audio, "kenh", "legacy"), "WATERMARK_NOT_FOUND")

    def test_preview_audio_only_serves_managed_assets(self):
        item = self.svc.create_upload("kenh", "Intro", "i.wav", self.wav_bytes())["item"]
        raw = self.svc.audio("kenh", item["id"])
        self.assertEqual((raw.ctype, raw.body[:4]), ("audio/wav", b"RIFF"))
        for bad in ("../../channel.json", "wm_00000000", "..%2f"):
            self.assertEqual(self.code(self.svc.audio, "kenh", bad), "WATERMARK_NOT_FOUND")
        self.assertEqual(self.code(self.svc.audio, "kenh", item["id"], 7), "WATERMARK_REVISION_NOT_FOUND")
