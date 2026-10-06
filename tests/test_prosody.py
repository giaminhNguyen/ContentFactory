"""Agent Plan Phase 3: Prosody Engine — tách câu tiếng Việt, speech plan tất định, nhóm tổng hợp, khoảng nghỉ chính xác, QC, tùy chọn ngữ nghĩa (không LLM mặc định)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from contentfactory.adapters.fake import FakeAudio, FakeTTS
from contentfactory.contracts import ErrorClass, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator.stages import StageContract
from contentfactory.tts import prosody as PRO
from contentfactory.tts import schema as S
from contentfactory.tts.autotune import make_ctx
from contentfactory.tts.manager import TTSManager
from contentfactory.tts.planner import validate_plan
from contentfactory.tts.prosody.profiles import PROFILES
from tests.support import RootCase, params
from tests.test_audio import burst, needs_ffmpeg, silent_runs

FLAT = S.resolve({}, {"max_chars": 600, "languages": ["vi"]}, "vi")


def sents(text: str) -> list[str]:
    return [s["text"] for p in PRO.paragraphs(text) for s in PRO.split_sentences(p["text"])]


def plan_of(text: str, profile: str = "natural", caps: dict | None = None, **pro) -> dict:
    pr = PRO.resolve_prosody({"profile": profile, **pro})
    norm = PRO.mark_scenes(text)
    return PRO.build_speech_plan(norm, FLAT, caps or {}, pr)


class SegmenterTest(unittest.TestCase):
    def test_normal_vietnamese_prose(self):
        self.assertEqual(sents("Cô mở cửa. Căn phòng hoàn toàn trống rỗng! Ai đã ở đây? Không ai biết."),
                         ["Cô mở cửa.", "Căn phòng hoàn toàn trống rỗng!", "Ai đã ở đây?", "Không ai biết."])
        kinds = [s["end"] for s in PRO.split_sentences("Cô mở cửa. Căn phòng trống rỗng! Ai đã ở đây? Không ai biết")]
        self.assertEqual(kinds, ["sentence", "exclamation", "question", "none"])

    def test_decimals_abbreviations_dates_and_domains_do_not_split(self):
        text = "Giá 1.5 triệu đồng. Anh ở TP.HCM từ 12.10.2026, xem example.com nhé. Ông GS. Nguyễn V. An nói v.v. Rồi đi."
        self.assertEqual(sents(text), ["Giá 1.5 triệu đồng.", "Anh ở TP.HCM từ 12.10.2026, xem example.com nhé.", "Ông GS. Nguyễn V. An nói v.v.", "Rồi đi."])
        self.assertEqual(sents("Tôi gặp TP. Hồ Chí Minh nhiều lần. Rồi thôi."), ["Tôi gặp TP. Hồ Chí Minh nhiều lần.", "Rồi thôi."])
        self.assertEqual(sents("Họ mua nhà, xe cộ v.v. và đi."), ["Họ mua nhà, xe cộ v.v. và đi."])        # v.v. + chữ thường: chưa hết câu

    def test_dialogue_tag_is_one_sentence_but_new_sentence_after_quote_is_two(self):
        self.assertEqual(sents("“Anh đi đâu?” cô hỏi."), ["“Anh đi đâu?” cô hỏi."])
        self.assertEqual(sents("“Anh đi đâu?” Cô đứng dậy."), ["“Anh đi đâu?”", "Cô đứng dậy."])
        self.assertEqual(sents("“Đi đi,” cô nói, “trước khi quá muộn.”"), ["“Đi đi,” cô nói, “trước khi quá muộn.”"])
        self.assertEqual(sents("— Anh đi đâu? — cô hỏi."), ["— Anh đi đâu? — cô hỏi."])

    def test_ellipsis_hesitation_vs_end_of_paragraph(self):
        s = PRO.split_sentences("Tôi… tôi không biết. Rồi anh im lặng… Cô nhìn anh.")
        self.assertEqual([x["text"] for x in s], ["Tôi… tôi không biết.", "Rồi anh im lặng…", "Cô nhìn anh."])
        self.assertEqual([m["kind"] for m in s[0]["micro"]], ["ellipsis_hesitation"])
        self.assertEqual(s[1]["end"], "ellipsis")

    def test_micro_boundaries_skip_decimal_commas_and_times(self):
        s = PRO.split_sentences("Lúc 10:30, giá 1,5 triệu; anh bảo: đi.")[0]
        self.assertEqual([m["kind"] for m in s["micro"]], ["comma", "semicolon", "colon"])

    def test_scene_separators_and_newlines(self):
        text = "Đoạn một.\nVẫn đoạn một.\n\nĐoạn hai.\n\n***\n\nĐoạn ba.\n\n\n\nĐoạn bốn."
        pars = PRO.paragraphs(PRO.mark_scenes(text))
        self.assertEqual([p["text"] for p in pars], ["Đoạn một.\nVẫn đoạn một.", "Đoạn hai.", "Đoạn ba.", "Đoạn bốn."])
        self.assertEqual([p["scene_after"] for p in pars], [False, True, True, False])          # xuống dòng đơn KHÔNG phải phân cảnh

    def test_long_sentence_splits_at_linguistic_boundary_never_mid_word_or_inside_phrase(self):
        body = ("bởi vì, " + "ngôi nhà cũ nằm cuối con hẻm vắng, ") * 14 + "cô đã bỏ đi."
        pieces = PRO.split_long(body, 200)
        self.assertTrue(all(len(p["text"]) <= 200 for p in pieces))
        self.assertEqual(" ".join(p["text"] for p in pieces), body.strip())
        for p in pieces[:-1]:
            self.assertNotRegex(p["text"].rstrip(",; ").lower(), r"(bởi vì|bởi|vì|rằng|của|với|để)$")       # không cắt vỡ cụm
            self.assertEqual(p["cut"], "comma")
        for a, b in zip(pieces, pieces[1:]):
            self.assertFalse(a["text"][-1].isalpha() and b["text"][0].isalpha() and a["text"][-1] + b["text"][0] in ("ơn", "ac"))
        words = set(body.replace(",", "").replace(".", "").split())
        self.assertTrue(all(w in words for p in pieces for w in p["text"].replace(",", "").replace(".", "").split()))          # không có từ bị cắt đôi
        hard = PRO.split_long("x" * 500, 200)                                    # không có ranh giới nào: cắt cứng, vẫn phủ hết
        self.assertEqual("".join(p["text"] for p in hard), "x" * 500)


class ProfileTest(unittest.TestCase):
    def test_profiles_are_ordered_and_scale_and_custom_apply(self):
        f, n, d = (PRO.resolve_prosody({"profile": p})["pauses"] for p in ("fast", "natural", "dramatic"))
        for k in ("sentence", "paragraph", "scene", "ellipsis", "dialogue"):
            self.assertLess(f[k], n[k], k)
            self.assertLess(n[k], d[k], k)
        r = PRO.resolve_prosody({"profile": "custom", "custom": {"paragraph": 1000}, "scale": 0.5})
        self.assertEqual((r["pauses"]["paragraph"], r["pauses"]["sentence"]), (500, round(PROFILES["natural"]["sentence"] * 0.5)))

    def test_invalid_prosody_is_rejected_with_a_clear_error(self):
        for bad in ({"profile": "wild"}, {"custom": {"nope": 5}}, {"custom": {"sentence": -1}}, {"scale": 9}, {"overrides": {"k": {"pause_ms": "x"}}}):
            with self.assertRaises(StageError) as cm:
                PRO.resolve_prosody(bad)
            self.assertEqual(cm.exception.code, "INVALID_PROSODY")


class SpeechPlanTest(unittest.TestCase):
    TEXT = ("Cô mở cửa. Nhưng căn phòng hoàn toàn trống rỗng, không một bóng người, không một tiếng động: chỉ có gió.\n\n"
            "“Anh đi đâu vậy?” cô hỏi.\n\n“Tôi… tôi không biết,” anh đáp.\n\nCô đứng nhìn rất lâu…\n\n***\n\nSáng hôm sau, chuông điện thoại reo lên!")

    def test_plan_is_deterministic(self):
        a, b = plan_of(self.TEXT), plan_of(self.TEXT)
        self.assertEqual(json.dumps(a, sort_keys=True, ensure_ascii=False), json.dumps(b, sort_keys=True, ensure_ascii=False))
        self.assertEqual(a["rules_version"], PRO.RULES_VERSION)

    def test_boundary_kinds_and_exact_pauses_follow_the_profile(self):
        p = plan_of(self.TEXT)
        n = PROFILES["natural"]
        by_text = {s["text"]: s for s in p["segments"]}
        self.assertEqual(by_text["“Anh đi đâu vậy?” cô hỏi."]["boundary_after"], "dialogue")            # hai đoạn thoại liền nhau
        self.assertEqual(by_text["“Anh đi đâu vậy?” cô hỏi."]["pause_after_ms"], n["dialogue"])
        self.assertEqual(by_text["“Tôi… tôi không biết,” anh đáp."]["boundary_after"], "paragraph")      # thoại -> kể
        self.assertEqual(by_text["Cô đứng nhìn rất lâu…"]["boundary_after"], "scene")                   # đoạn kết bằng … trước dấu phân cảnh
        self.assertEqual(by_text["Cô đứng nhìn rất lâu…"]["pause_after_ms"], n["scene"])
        self.assertEqual(by_text["Cô mở cửa."]["boundary_after"], "sentence")
        self.assertEqual(by_text["Cô mở cửa."]["pause_after_ms"], n["sentence"])
        self.assertEqual(p["segments"][-1]["boundary_after"], "end")
        self.assertEqual(p["segments"][-1]["pause_after_ms"], 0)
        fast = plan_of(self.TEXT, "fast")
        self.assertLess(fast["groups"][0]["pause_after_ms"], p["groups"][0]["pause_after_ms"])

    def test_paragraph_ellipsis_is_stronger_than_a_plain_paragraph_pause(self):
        p = plan_of("Cô đứng nhìn rất lâu…\n\nRồi cô đi.")
        s = p["segments"][0]
        self.assertEqual((s["boundary_after"], s["source"]), ("paragraph", "auto:paragraph+ellipsis_end"))
        self.assertEqual(s["pause_after_ms"], max(PROFILES["natural"]["paragraph"], PROFILES["natural"]["ellipsis"]))

    def test_commas_do_not_fragment_synthesis_calls(self):
        text = "Anh đi, rồi dừng lại, quay đầu, nhìn cô; sau đó anh bảo: ở lại đây, đợi anh, được không?"
        p = plan_of(text)
        self.assertEqual((len(p["segments"]), len(p["groups"])), (1, 1))
        self.assertEqual(len(p["segments"][0]["micro"]), 7)
        many = " ".join(f"Câu số {i} nói chuyện, rồi kể tiếp, và thế là hết." for i in range(1, 40))
        pm = plan_of(many)
        self.assertLess(len(pm["groups"]), len(pm["segments"]))                       # nhiều câu một nhóm
        self.assertTrue(all(g["chars"] <= FLAT["segment"]["preferred_chars"] for g in pm["groups"]))
        self.assertGreater(len(pm["groups"]), 3)

    def test_groups_never_cross_paragraphs_and_cover_the_text(self):
        p = plan_of(self.TEXT)
        for g in p["groups"]:
            pids = {s["paragraph_id"] for s in p["segments"] if s["synthesis_group"] == g["id"]}
            self.assertEqual(len(pids), 1)
        norm = PRO.strip_marks(PRO.mark_scenes(self.TEXT))
        self.assertEqual(validate_plan(PRO.segments_of(p), norm, FLAT)["errors"], [])

    def test_provider_limit_is_respected_without_semantic_damage(self):
        small = S.resolve({"segment": {"preferred_chars": 120, "max_chars": 150, "min_chars": 20}}, {"max_chars": 150}, "vi")
        long_sentence = ("Anh ấy đã đi rất xa, qua nhiều con phố đông đúc, bởi vì anh muốn quên đi tất cả, " * 3 + "và cuối cùng anh đã trở về.").strip()
        p = PRO.build_speech_plan(long_sentence, small, {}, PRO.resolve_prosody({"profile": "natural"}))
        self.assertTrue(all(len(s["text"]) <= 150 for s in p["segments"]))
        self.assertEqual(validate_plan(PRO.segments_of(p), long_sentence, small)["errors"], [])
        self.assertTrue(p["split_sentences"])
        self.assertTrue(any(w["code"] == "SENTENCE_SPLIT_FOR_LIMIT" for w in p["qc"]["warnings"]))
        for s in p["segments"][:-1]:
            self.assertNotRegex(s["text"].rstrip(",; ").lower(), r"(bởi vì|bởi|vì|rằng|của|với|để)$")

    def test_manual_override_is_exact_locked_and_survives_replanning(self):
        base = plan_of(self.TEXT)
        target = next(s for s in base["segments"] if s["text"] == "Cô mở cửa.")
        self.assertEqual(target["realized"], "engine")                                 # nằm trong nhóm: engine tự xử lý
        p = plan_of(self.TEXT, overrides={target["key"]: {"pause_ms": 1234}})
        t2 = next(s for s in p["segments"] if s["key"] == target["key"])
        self.assertEqual((t2["pause_after_ms"], t2["source"], t2["locked"], t2["manual_override"], t2["realized"]), (1234, "manual", True, True, "external"))
        self.assertEqual(len(p["groups"]), len(base["groups"]) + 1)                    # override ép đóng nhóm để khoảng nghỉ chính xác được chèn
        again = plan_of(self.TEXT, overrides={target["key"]: {"pause_ms": 1234}})
        self.assertEqual(p, again)
        gone = plan_of(self.TEXT.replace("Cô mở cửa.", "Cô khép cửa."), overrides={target["key"]: {"pause_ms": 1234}})
        self.assertTrue(any("override" in w for w in gone["warnings"]))                # câu đổi => override mất hiệu lực, không áp nhầm

    def test_native_break_strategy_keeps_groups_and_passes_exact_breaks(self):
        p = plan_of(self.TEXT, caps={"supports_exact_break_ms": True})
        self.assertEqual(p["strategy"], "native_breaks")
        g = next(g for g in p["groups"] if g.get("breaks"))
        self.assertTrue(all(b["ms"] > 0 for b in g["breaks"]))
        self.assertEqual(PRO.segments_of(p)[g["segments"] and p["groups"].index(g)]["breaks"], g["breaks"])

    def test_qc_flags_suspicious_patterns_but_never_fails(self):
        tiny = plan_of("\n\n".join(["Ừ.", "Vâng.", "Dạ.", "Thôi.", "Ờ.", "Được.", "Xong."]))
        codes = {w["code"] for w in tiny["qc"]["warnings"]}
        self.assertIn("TOO_MANY_TINY_GROUPS", codes)
        loud = plan_of("A một.\n\nB hai.\n\nC ba.", custom={"paragraph": 6000}, profile="custom")
        self.assertIn("PAUSE_ABOVE_MAX", {w["code"] for w in loud["qc"]["warnings"]})
        scenes = plan_of("A.\n\n***\n\nB.\n\n***\n\nC.\n\n***\n\nD.\n\n***\n\nE.\n\n***\n\nF.\n\n***\n\nG.")
        self.assertIn("TOO_MANY_SCENE_BREAKS", {w["code"] for w in scenes["qc"]["warnings"]})
        q = plan_of(self.TEXT)["qc"]
        self.assertGreater(q["groups"], 2)
        self.assertLessEqual(q["pause_ms"]["p50"], q["pause_ms"]["p95"])
        self.assertLessEqual(q["pause_ms"]["p95"], q["pause_ms"]["max"])
        self.assertEqual(PRO.analyze_audio([100, 400, 0], {"timeline": [{"gap_after_sec": 0.1}, {"gap_after_sec": 0.7}, {}]}, None, None)["warnings"][0]["code"], "PAUSE_DRIFT")
        clamped = PRO.analyze_audio([100, 5000, 0], {"pauses": [{"requested": 100}, {"requested": 3000}, {"requested": 300}]}, None, None)
        self.assertEqual([w["code"] for w in clamped["warnings"]], ["PAUSE_DRIFT"])


FIXTURES = Path(__file__).parent / "fixtures" / "prosody"


class GoldenTest(unittest.TestCase):
    """Bộ mẫu nhỏ nội bộ (tổng hợp, không bản quyền) đại diện: kể thường, hội thoại, đoạn ngắn kịch tính, câu dài, ngập ngừng, chuyển cảnh.
    Kiểm cấu trúc nhịp tự động (không thay được nghe thử thật với engine thật — xem docs/PRODUCTION_CHECKLIST.md)."""

    def plan(self, name: str, profile: str = "natural", flat: dict | None = None) -> tuple[dict, str]:
        from contentfactory.tts.normalize import normalize_text
        flat = flat or FLAT
        text, _ = normalize_text(PRO.mark_scenes((FIXTURES / f"{name}.txt").read_text(encoding="utf-8")), flat["normalize"])
        return PRO.build_speech_plan(text, flat, {}, PRO.resolve_prosody({"profile": profile})), text

    def test_every_fixture_is_deterministic_covers_text_and_respects_limits(self):
        for f in sorted(FIXTURES.glob("*.txt")):
            a, text = self.plan(f.stem)
            b, _ = self.plan(f.stem)
            self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True), f.name)
            self.assertEqual(validate_plan(PRO.segments_of(a), PRO.strip_marks(text), FLAT)["errors"], [], f.name)
            self.assertTrue(all(len(g["text"]) <= FLAT["segment"]["max_chars"] for g in a["groups"]), f.name)
            self.assertNotIn("§§", json.dumps(a, ensure_ascii=False), f.name)
            self.assertEqual(a["segments"][-1]["boundary_after"], "end")

    def test_neutral_narration_is_not_over_chunked(self):
        p, _ = self.plan("neutral")
        self.assertEqual(len(p["segments"]), 9)
        self.assertLessEqual(len(p["groups"]), 4)                                      # ~3 câu một lần gọi TTS, không phải một lần mỗi câu
        self.assertEqual(p["qc"]["warnings"], [])
        self.assertEqual({s["boundary_after"] for s in p["segments"]}, {"sentence", "paragraph", "end"})

    def test_dialogue_fixture_distinguishes_speaker_turns_from_narration_and_keeps_tags(self):
        p, _ = self.plan("dialogue")
        kinds = [s["boundary_after"] for s in p["segments"]]
        self.assertEqual(kinds.count("dialogue"), 4)
        self.assertIn("“Anh đi đâu vậy?” cô hỏi.", [s["text"] for s in p["segments"]])            # lời dẫn dính câu thoại
        self.assertIn("“Tôi biết.”", [s["text"] for s in p["segments"]])
        self.assertTrue(all(s["pause_after_ms"] == PROFILES["natural"]["dialogue"] for s in p["segments"] if s["boundary_after"] == "dialogue"))
        self.assertTrue(p["segments"][0]["dialogue"])

    def test_dramatic_short_paragraphs_get_paragraph_pauses_not_scene_pauses(self):
        p, _ = self.plan("dramatic")
        self.assertNotIn("scene", {s["boundary_after"] for s in p["segments"]})
        self.assertEqual(sum(1 for s in p["segments"] if s["boundary_after"] == "paragraph"), 5)
        slow, _ = self.plan("dramatic", "dramatic")
        self.assertGreater(sum(g["pause_after_ms"] for g in slow["groups"]), sum(g["pause_after_ms"] for g in p["groups"]))

    def test_ellipsis_fixture_separates_hesitation_from_paragraph_ending_ellipsis(self):
        p, _ = self.plan("ellipsis")
        texts = [s["text"] for s in p["segments"]]
        self.assertEqual(texts[0], "Tôi… tôi không biết nữa.")
        self.assertEqual(texts[1], "Có lẽ… có lẽ anh nói đúng.")
        end = next(s for s in p["segments"] if s["text"].startswith("Cô nhìn anh"))
        self.assertEqual((end["boundary_after"], end["source"]), ("paragraph", "auto:paragraph+ellipsis_end"))
        self.assertGreaterEqual(end["pause_after_ms"], PROFILES["natural"]["ellipsis"])

    def test_scene_fixture_uses_only_explicit_separators(self):
        p, _ = self.plan("scenes")
        self.assertEqual([s["boundary_after"] for s in p["segments"] if s["boundary_after"] == "scene"], ["scene"] * 3)
        self.assertTrue(all(s["pause_after_ms"] == PROFILES["natural"]["scene"] for s in p["segments"] if s["boundary_after"] == "scene"))
        self.assertEqual(p["segments"][-1]["text"], "Mùa đông năm đó rất lạnh.")
        no_marks, _ = self.plan("neutral")
        self.assertNotIn("scene", {s["boundary_after"] for s in no_marks["segments"]})      # xuống dòng thường không bao giờ là cảnh mới

    def test_long_sentences_split_at_phrase_boundaries_under_a_small_provider_limit(self):
        small = S.resolve({"segment": {"preferred_chars": 140, "max_chars": 160, "min_chars": 30}}, {"max_chars": 160}, "vi")
        p, text = self.plan("long_sentences", flat=small)
        self.assertTrue(all(len(s["text"]) <= 160 for s in p["segments"]))
        self.assertGreater(len(p["segments"]), 3)
        self.assertEqual(validate_plan(PRO.segments_of(p), PRO.strip_marks(text), small)["errors"], [])
        for s in p["segments"][:-1]:
            if s["boundary_after"] in ("comma", "semicolon", "colon", "micro"):
                self.assertNotRegex(s["text"].rstrip(",; ").lower(), r"(bởi vì|bởi|vì|rằng|của|với|để|và|mặc dù|mặc)$", s["text"])
        self.assertEqual(p["warnings"], [])                                                # có ranh giới ngôn ngữ nên không phải cắt cứng


class ManagerTest(unittest.TestCase):
    """TTSManager với adapter giả: speech plan snapshot, reuse khi retry, 0 lần gọi LLM mặc định, ngữ nghĩa tùy chọn có cache."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-prosody-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ctx = make_ctx(self.tmp)
        self.ctx.params = {"prosody": {"profile": "natural"}}
        self.text = SpeechPlanTest.TEXT

    def mgr(self, planner=None):
        return TTSManager(FakeTTS(), FakeAudio(), planner)

    def synth_calls(self) -> int:
        f = self.tmp / "calls.log"
        return len(f.read_text(encoding="utf-8").splitlines()) if f.is_file() else 0

    def test_run_writes_snapshot_speech_plan_and_manifest_qc(self):
        r = self.mgr().run(self.ctx, self.text, None)
        kinds = {a.kind for a in r.artifacts}
        self.assertEqual(kinds, {"audio_master", "tts_manifest", "audio_timeline", "speech_plan"})
        plan = json.loads((self.tmp / "speech_plan.json").read_text(encoding="utf-8"))
        self.assertEqual((plan["mode"], plan["profile"]), ("prosody", "natural"))
        mf = json.loads((self.tmp / "tts_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(mf["prosody"]["plan_key"], plan["plan_key"])
        self.assertIn("audio", plan["qc"])
        self.assertEqual(r.data["llm_calls"], 0)
        self.assertEqual(len(mf["segments"]), len(plan["groups"]))
        self.assertNotIn("§§", (self.tmp / "tts_manifest.json").read_text(encoding="utf-8"))          # dấu phân cảnh không lọt ra văn bản đọc
        for seg in mf["segments"][:-1]:
            self.assertIn(seg["pause_after_ms"], set(PROFILES["natural"].values()))                   # pause lấy từ profile, không phải số ngẫu nhiên

    def test_retry_reuses_the_speech_plan_and_completed_chunks(self):
        m = self.mgr()
        m.run(self.ctx, self.text, None)
        first_calls = self.synth_calls()
        plan1 = (self.tmp / "speech_plan.json").read_text(encoding="utf-8")
        r2 = m.run(self.ctx, self.text, None)
        self.assertEqual(self.synth_calls(), first_calls)                              # chunk đã xong không tổng hợp lại
        self.assertEqual(json.loads((self.tmp / "tts_manifest.json").read_text(encoding="utf-8"))["planner"]["reused_plan"], True)
        self.assertEqual(json.loads((self.tmp / "speech_plan.json").read_text(encoding="utf-8"))["segments"], json.loads(plan1)["segments"])
        self.assertEqual(r2.data["llm_calls"], 0)

    def test_changing_prosody_changes_plan_key_story_unchanged(self):
        self.mgr().run(self.ctx, self.text, None)
        k1 = json.loads((self.tmp / "speech_plan.json").read_text(encoding="utf-8"))["plan_key"]
        self.ctx.params = {"prosody": {"profile": "dramatic"}}
        self.mgr().run(self.ctx, self.text, None)
        plan = json.loads((self.tmp / "speech_plan.json").read_text(encoding="utf-8"))
        self.assertNotEqual(plan["plan_key"], k1)
        self.assertEqual(plan["profile"], "dramatic")

    def test_default_path_makes_zero_llm_calls_even_with_a_semantic_labeler_present(self):
        calls = []

        class Planner:
            name = "rule"

            def __init__(self):
                self.semantic_labeler = type("L", (), {"name": "x", "version": "1", "labels": lambda s, sents: calls.append(1) or {}})()

            def plan(self, text, profile, feedback=None):
                raise AssertionError("planner cũ không được dùng trong đường prosody")

        self.mgr(Planner()).run(self.ctx, self.text, None)
        self.assertEqual(calls, [])

    def test_optional_semantic_labels_are_used_cached_and_only_change_the_boundary_kind(self):
        calls = []

        class Labeler:
            name, version = "fake-llm", "1"

            def labels(self, sentences):
                calls.append(len(sentences))
                i = next(n for n, s in enumerate(sentences, 1) if s.startswith("Nhưng"))
                return {str(i): "dramatic_reveal", "99999": "emphasis"}

        class Planner:
            name = "rule"
            semantic_labeler = Labeler()

        self.ctx.params = {"prosody": {"profile": "natural", "semantic_llm": True}}
        self.mgr(Planner()).run(self.ctx, self.text, None)
        plan = json.loads((self.tmp / "speech_plan.json").read_text(encoding="utf-8"))
        seg = next(s for s in plan["segments"] if s["text"].startswith("Nhưng"))
        self.assertEqual(seg["pause_after_ms"], max(PROFILES["natural"]["paragraph"], PROFILES["natural"]["reveal"]))      # ms vẫn lấy từ bảng profile
        self.assertIn("semantic:dramatic_reveal", seg["source"])
        self.mgr(Planner()).run(self.ctx, self.text, None)                              # retry/rerender: dùng cache, không gọi LLM lần nữa
        self.assertEqual(len(calls), 1)
        self.ctx.params = {"prosody": {"profile": "natural"}}
        self.mgr(Planner()).run(self.ctx, self.text, None)                              # tắt tùy chọn vẫn đủ plan
        self.assertEqual(len(calls), 1)

    def test_semantic_requested_without_a_labeler_degrades_with_a_warning(self):
        self.ctx.params = {"prosody": {"profile": "natural", "semantic_llm": True}}
        r = self.mgr().run(self.ctx, self.text, None)
        plan = json.loads((self.tmp / "speech_plan.json").read_text(encoding="utf-8"))
        self.assertTrue(any("semantic_labeler" in w for w in plan["warnings"]))
        self.assertEqual(r.data["llm_calls"], 0)

    def test_legacy_jobs_without_prosody_keep_the_old_planner_and_still_get_a_plan(self):
        self.ctx.params = {}
        r = self.mgr().run(self.ctx, self.text, None)
        plan = json.loads((self.tmp / "speech_plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["mode"], "legacy")
        self.assertTrue((self.tmp / "segments.json").is_file())
        self.assertIn("speech_plan", {a.kind for a in r.artifacts})

    def test_story_text_is_never_modified_by_planning(self):
        story = self.tmp / "story.txt"
        story.write_text(self.text, encoding="utf-8")
        before = story.read_bytes()
        self.mgr().run(self.ctx, story.read_text(encoding="utf-8"), None)
        self.assertEqual(story.read_bytes(), before)
        plan = (self.tmp / "speech_plan.json").read_text(encoding="utf-8")
        self.assertNotIn("§§SCENE§§", plan)

    def test_empty_or_only_scene_marks_text_is_rejected(self):
        with self.assertRaises(StageError):
            self.mgr().run(self.ctx, "***\n\n***", None)


class InvalidationTest(unittest.TestCase):
    def test_tts_stage_key_depends_on_prosody_but_not_on_thumbnail_or_templates(self):
        c = StageContract(P.BY_NAME["tts"])
        ins = {"story_text": [{"path": "a", "kind": "story_text", "sha256": "x", "bytes": 1, "meta": {}}]}
        base = {"tts": {"voice": "a"}, "prosody": {"profile": "natural"}}
        k = c.stage_key(base, None, ins)
        self.assertNotEqual(k, c.stage_key({**base, "prosody": {"profile": "dramatic"}}, None, ins))
        self.assertNotEqual(k, c.stage_key({**base, "prosody": {"profile": "natural", "overrides": {"a.1": {"pause_ms": 5}}}}, None, ins))
        self.assertEqual(k, c.stage_key({**base, "templates": {"thumbnail": {"id": "x"}}, "render": {"youtube": {}}, "project": {"title": "T"}}, None, ins))


class JobFlowTest(RootCase):
    """Job thật (fake adapter): prosody mặc định theo cấu hình máy, speech plan là artifact, đổi prosody bằng revision chỉ chạy lại TTS trở đi."""

    def test_default_config_gives_new_jobs_natural_prosody_and_a_speech_plan(self):
        from contentfactory.orchestrator.config import load_config
        from contentfactory.orchestrator.runner import Orchestrator
        cfgf = self.root / "config" / "config.json"
        c = json.loads(cfgf.read_text(encoding="utf-8"))
        c["prosody"] = {"default_profile": "natural"}
        cfgf.write_text(json.dumps(c), encoding="utf-8")
        orc = Orchestrator(load_config(self.root))
        jid = orc.submit(params(), mode="THROUGH_TTS")
        j = orc.store.get_job(jid)
        self.assertEqual(j["params"]["prosody"], {"profile": "natural"})
        self.assertTrue(any(d["what"] == "prosody" for d in j["params"]["auto"]))
        orc.run()
        kinds = {a["kind"] for a in orc.store.artifacts(jid)}
        self.assertIn("speech_plan", kinds)
        story = next(a for a in orc.store.artifacts(jid) if a["kind"] == "story_text")
        self.assertNotIn("§§", (self.job_dir(jid) / story["path"]).read_text(encoding="utf-8"))

    def test_prosody_change_on_a_live_job_reruns_tts_and_downstream_only(self):
        orc = self.orc()
        jid = orc.submit(params(prosody={"profile": "natural"}), mode="THROUGH_TTS")
        orc.pause_job(jid)
        imp = orc.preview_update(jid, params_patch={"prosody": {"profile": "dramatic"}})
        act = {s["id"]: s["action"] for s in imp["stages"]}
        self.assertEqual((act["source"], act["story"]), ("RUN", "RUN"))              # chưa chạy gì: tất cả sẽ chạy
        orc.resume(jid)
        orc.run()
        first = self.runs(orc, jid)
        done = orc.preview_update(jid, params_patch={"prosody": {"profile": "dramatic"}})        # job đã đạt target (THROUGH_TTS): cập nhật tại chỗ bị chặn
        self.assertFalse(done["ok"])
        self.assertTrue(done["clone_suggested"])
        new = orc.clone_job(jid, rerun_from="tts", params_patch={"prosody": {"profile": "dramatic"}})
        orc.run()
        second = self.runs(orc, new)
        self.assertNotIn("source", second)                                           # clone dùng lại source/story
        self.assertNotIn("story", second)
        self.assertEqual(second["tts"], ["succeeded"])
        plan = json.loads((self.job_dir(new) / next(a for a in orc.store.artifacts(new) if a["kind"] == "speech_plan")["path"]).read_text(encoding="utf-8"))
        self.assertEqual(plan["profile"], "dramatic")
        self.assertEqual(first["tts"], ["succeeded"])


@needs_ffmpeg
class StitchTest(unittest.TestCase):
    """Khoảng nghỉ chính xác sau cắt biên: im lặng cuối của engine không cộng dồn với khoảng nghỉ đã lập kế hoạch; không trôi theo số segment."""

    def setUp(self):
        from contentfactory.audio.processor import FfmpegAudio
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-stitch-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ctx = make_ctx(self.tmp)
        self.audio = FfmpegAudio()

    def test_provider_trailing_silence_does_not_double_the_planned_pause(self):
        pauses = [380, 720, 560, 1200, 380, 0]
        chunks = [burst(self.tmp / f"c{i}.wav", lead=0.15, tone=0.9, trail=0.22) for i in range(len(pauses))]       # engine để lại 220 ms im lặng cuối
        self.ctx.params = {"audio": {"join": {"pause": {"tail_ms": 0}}}}
        out = self.tmp / "raw.wav"
        self.audio.assemble(chunks, pauses, out, self.ctx)
        gaps = [d * 1000 for _, d in silent_runs(out, thresh=0.002, min_s=0.2)]
        for want in pauses[:-1]:
            self.assertTrue(any(abs(g - want) <= 30 for g in gaps), (want, gaps))
        self.assertFalse(any(abs(g - (want + 220)) <= 30 for want in pauses[:-1] for g in gaps), gaps)             # không có "kế hoạch + đuôi engine"

    def test_no_duration_drift_over_many_segments(self):
        n = 40
        pauses = [380 if i % 4 else 720 for i in range(n - 1)] + [0]
        chunks = [burst(self.tmp / f"d{i}.wav", lead=0.1, tone=0.5, trail=0.15) for i in range(n)]
        self.ctx.params = {"audio": {"join": {"pause": {"tail_ms": 0}}}}
        r = self.audio.assemble(chunks, pauses, self.tmp / "many.wav", self.ctx)
        expected = n * 0.5 + sum(pauses) / 1000
        self.assertAlmostEqual(r["duration_sec"], expected, delta=0.5 + 0.004 * n)
        self.assertEqual(PRO.analyze_audio(pauses, r, expected, r["duration_sec"])["warnings"], [])


if __name__ == "__main__":
    unittest.main()
