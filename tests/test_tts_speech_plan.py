"""Regression SPEECH_PLAN_INVALID / NO_SPEECH: planner KHÔNG tạo segment rỗng hoặc chỉ dấu câu/marker; tự dọn/gộp tất định khi tạo plan
(không mất chữ/số, index liên tục), không tự sửa được thì dừng an toàn (POLICY, 0 lần gọi TTS). Validator giữ nguyên độ nghiêm."""
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from contentfactory.adapters.fake import FakeAudio, FakeTTS
from contentfactory.contracts import ErrorClass, StageError
from contentfactory.tts import prosody as PRO
from contentfactory.tts import schema as S
from contentfactory.tts.autotune import make_ctx
from contentfactory.tts.manager import TTSManager
from contentfactory.tts.planner import RuleSegmentPlanner, absorb_unspeakable, drop_unspeakable_paragraphs, has_speech, validate_plan

CAPS = S.normalize_capabilities(FakeTTS().capabilities())
FLAT = S.resolve(None, CAPS, None, "fake")
PRO_CFG = PRO.resolve_prosody({"profile": "natural"})

# đầu vào cố tình xấu: dòng/đoạn/câu chỉ dấu câu, ngoặc rỗng, emoji, ký hiệu
BAD_PARAS = ["…", "— —", "“”", "(...)", "😀😀", "?!", "[…]", "___", "* * *"]
GOOD = "Anh ấy đi về nhà lúc 5 giờ chiều. Trời đã tối rồi."


def words(t: str) -> list[str]:
    """Chuỗi chữ/số (bỏ mọi dấu/ký hiệu/khoảng trắng) — thứ không được phép mất."""
    return re.findall(r"[^\W_]+", t)


def alnum_only(t: str) -> str:
    return "".join(c for c in t if c.isalnum())


class CountingTTS(FakeTTS):
    def __init__(self):
        self.calls = []

    def synthesize(self, segment, profile, out_path, ctx):
        self.calls.append(segment["index"])
        return super().synthesize(segment, profile, out_path, ctx)


def check_segments(tc: unittest.TestCase, segs: list[dict], text: str, flat: dict = FLAT):
    tc.assertTrue(segs)
    tc.assertEqual([s["index"] for s in segs], list(range(1, len(segs) + 1)))
    for s in segs:
        tc.assertTrue(has_speech(s["text"]), s)
    tc.assertEqual(alnum_only("".join(s["text"] for s in segs)), alnum_only(text))      # không mất/lặp chữ-số
    tc.assertEqual(validate_plan(segs, text, flat)["errors"], [])


class RulePlannerTest(unittest.TestCase):
    def test_punctuation_only_paragraphs_never_become_segments(self):
        for bad in BAD_PARAS:
            with self.subTest(bad=bad):
                text = f"{GOOD}\n\n{bad}\n\n{GOOD}"
                segs = RuleSegmentPlanner().plan(text, FLAT)
                cleaned, n = drop_unspeakable_paragraphs(text)
                self.assertEqual(n, 1)
                check_segments(self, segs, cleaned)

    def test_punctuation_only_sentence_is_merged_not_lost(self):
        text = "Tôi không biết. … Rồi anh đi. ?! Cô ấy cười. (…) Hết."
        before = RuleSegmentPlanner().plan(text, {**FLAT, "segment": {**FLAT["segment"], "preferred_chars": 10, "min_chars": 1}})
        check_segments(self, before, text)
        self.assertIn("…", "".join(s["text"] for s in before))                               # dấu câu được gộp, không bị bỏ
        self.assertIn("?!", "".join(s["text"] for s in before))

    def test_leading_fragment_merges_forward_and_too_long_neighbour_borrows_words(self):
        mk = lambda b, t, last: {"text": t}
        self.assertEqual([x["text"] for x in absorb_unspeakable([{"text": "…"}, {"text": "Tôi đi"}], 20, " ", mk)], ["… Tôi đi"])
        # hàng xóm đầy sát max_chars: mượn từ cuối thay vì bỏ ký tự
        out = absorb_unspeakable([{"text": "aaa bbb ccc"}, {"text": "…"}], 11, " ", mk)
        self.assertEqual([x["text"] for x in out], ["aaa bbb", "ccc …"])
        self.assertEqual(alnum_only("".join(x["text"] for x in out)), "aaabbbccc")

    def test_hard_cut_cjk_does_not_leave_punctuation_only_tail(self):
        flat = {**FLAT, "joiner": "", "segment": {**FLAT["segment"], "max_chars": 10, "preferred_chars": 10, "min_chars": 1}}
        text = "一二三四五六七八九十…"           # cắt cứng đúng 10 ký tự sẽ để lại mẩu "…"
        check_segments(self, RuleSegmentPlanner().plan(text, flat), text, flat)

    def test_validator_stays_strict(self):
        segs = [{"index": 1, "text": "... --- !!!", "pause_after_ms": 0}]
        self.assertIn("NO_SPEECH", [e["code"] for e in validate_plan(segs, "... --- !!!", FLAT)["errors"]])
        self.assertIn("NO_SPEECH", [e["code"] for e in validate_plan([{"index": 1, "text": "___", "pause_after_ms": 0}], "___", FLAT)["errors"]])


class SpeechPlanTest(unittest.TestCase):
    def build(self, text: str, flat=FLAT):
        t = PRO.mark_scenes(text)
        return PRO.build_speech_plan(t, flat, CAPS, PRO_CFG), t

    def test_bad_paragraphs_and_marks_do_not_produce_empty_groups(self):
        for bad in BAD_PARAS:
            with self.subTest(bad=bad):
                text = f"{GOOD}\n\n{bad}\n\n{GOOD}"
                plan, t = self.build(text)
                segs = PRO.segments_of(plan)
                check_segments(self, segs, drop_unspeakable_paragraphs(PRO.strip_marks(t), (PRO.SCENE_MARK,))[0])
                self.assertEqual(PRO.check_integrity(plan, FLAT["joiner"]), [])

    def test_inline_fragments_merge_into_neighbour_sentence(self):
        text = "… Tôi không biết. … Rồi anh đi! ?! Cô ấy cười. (…)\n\nHết."
        plan, t = self.build(text)
        self.assertEqual(PRO.check_integrity(plan, FLAT["joiner"]), [])
        self.assertEqual(alnum_only(" ".join(s["text"] for s in plan["segments"])), alnum_only(text))
        for s in plan["segments"]:
            self.assertTrue(has_speech(s["text"]), s)
        check_segments(self, PRO.segments_of(plan), PRO.strip_marks(t))

    def test_integrity_detects_corrupt_plans(self):
        plan, _ = self.build(GOOD)
        bad = {**plan, "groups": [{**plan["groups"][0], "text": "…"}]}
        self.assertTrue(PRO.check_integrity(bad, FLAT["joiner"]))
        self.assertTrue(PRO.check_integrity({**plan, "groups": []}, FLAT["joiner"]))
        self.assertTrue(PRO.check_integrity({**plan, "groups": plan["groups"] + plan["groups"]}, FLAT["joiner"]))


class ManagerStopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-speechplan-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ctx = make_ctx(self.tmp)
        self.tts = CountingTTS()

    def run_mgr(self, text: str, prosody: bool):
        self.ctx.params = {"prosody": {"profile": "natural"}} if prosody else {}
        return TTSManager(self.tts, FakeAudio()).run(self.ctx, text, None)

    def test_story_with_bad_paragraphs_still_synthesizes_all_readable_text(self):
        for pro in (True, False):
            for bad in BAD_PARAS:
                with self.subTest(prosody=pro, bad=bad):
                    self.tts.calls.clear()
                    shutil.rmtree(self.tmp, True)
                    self.tmp.mkdir()
                    text = f"{GOOD}\n\n{bad}\n\n{GOOD}\n\n…"
                    r = self.run_mgr(text, pro)
                    self.assertTrue(self.tts.calls)
                    self.assertEqual(self.tts.calls, list(range(1, len(self.tts.calls) + 1)))
                    self.assertEqual(r.data["segments"], len(self.tts.calls))

    def stops(self, text: str, code: str, prosody: bool):
        with self.assertRaises(StageError) as cm:
            self.run_mgr(text, prosody)
        self.assertEqual((cm.exception.error_class, cm.exception.code), (ErrorClass.POLICY, code))
        self.assertEqual(self.tts.calls, [])                                          # dừng TRƯỚC khi gọi TTS

    def test_empty_story_stops_early_with_clear_error(self):
        for pro in (True, False):
            for text in ("", "   \n\n  ", "***"):
                with self.subTest(prosody=pro, text=text):
                    self.stops(text, "EMPTY_STORY", pro)

    def test_nothing_readable_is_a_hard_stop_not_an_empty_plan(self):
        for pro in (True, False):
            for text in ("…", "— —\n\n“”\n\n😀", "(...)\n\n?!"):
                with self.subTest(prosody=pro, text=text):
                    self.stops(text, "NO_SPEECH_CONTENT", pro)
        self.assertFalse((self.tmp / "speech_plan.json").exists())
        self.assertFalse((self.tmp / "segments.json").exists())

    def test_unfixable_plan_stops_with_clear_code_and_no_tts_call(self):
        # planner (nguồn sinh) tự hỏng: trả segment chỉ dấu câu => validator giữ nguyên, Manager dừng PLAN_INVALID, 0 lần gọi TTS
        class Broken:
            name = "rule"                                                               # không có đường dự phòng nào khác

            def plan(self, text, profile, feedback=None):
                return [{"index": 1, "text": "…", "pause_after_ms": 0}]

        self.ctx.params = {}
        with self.assertRaises(StageError) as cm:
            TTSManager(self.tts, FakeAudio(), Broken()).run(self.ctx, GOOD, None)
        self.assertEqual((cm.exception.error_class, cm.exception.code), (ErrorClass.POLICY, "PLAN_INVALID"))
        self.assertEqual(self.tts.calls, [])

    def test_broken_non_rule_planner_falls_back_to_rule_and_tts_gets_readable_text(self):
        class Broken:
            name = "ai"

            def plan(self, text, profile, feedback=None):
                return [{"index": 1, "text": "…", "pause_after_ms": 0}]

        self.ctx.params = {}
        TTSManager(self.tts, FakeAudio(), Broken()).run(self.ctx, GOOD, None)
        self.assertTrue(self.tts.calls)


if __name__ == "__main__":
    unittest.main()
