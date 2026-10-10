"""Constraint-first cho Story Remix + Story/Assembler: output contract nằm TRONG prompt, bị kiểm bằng code ngay sau khi sinh, lỗi tất định tự sửa, chỉ sửa đúng phần lỗi, hết lượt thì dừng an toàn.
Hồi quy cho: PREMISE_TOO_WEAK, ORIGINALITY_BLOCKED, OUTLINE_GATE_FAILED, CHAPTER_QA_FAILED, REMIX_LLM_INVALID, BUDGET_EXCEEDED, STORY_INVALID, ASSEMBLER_REMOVED_TOO_MUCH, EMPTY_STORY."""
import json
import unittest

from contentfactory.adapters import fake
from contentfactory.contracts import ErrorClass, StageError
from contentfactory.orchestrator.remix_universe import UniverseBridge
from contentfactory.orchestrator.story_router import StoryModeRouter
from contentfactory.story.assembler import assemble
from contentfactory.story.validate import sanitize_prose, validate_story_text
from contentfactory.story_remix.adapter import StoryRemixAdapter
from contentfactory.story_remix.core import Ledger, extract_json
from tests.fakes_remix import RemixFakeLLM, premise
from tests.support import RootCase, params
from tests.test_remix_plan import Base, cfg

MEM = {"new_facts": [], "state_changes": [], "opened": [], "resolved": [], "new_named_persons": []}


class PremiseRubricTest(Base):
    def test_rubric_is_in_the_prompt_and_failure_names_the_weak_criteria(self):
        llm = RemixFakeLLM(premises_by_call=[[premise("P1", weak=True), premise("P2", weak=True)]] * 2)
        e = self.code(llm)
        self.assertEqual(e.code, "PREMISE_TOO_WEAK")
        first, second = [c["prompt"] for c in llm.calls if c["step"] == "premises"]
        self.assertIn("TIÊU CHÍ CHẤM", first)
        self.assertIn("≥ 60 ký tự", first)                                                          # tiêu chí quality có số đo cụ thể
        self.assertIn("Ý TƯỞNG TỐT NHẤT LẦN TRƯỚC", second)                                         # lượt 2 SỬA ý tưởng tốt nhất, không sinh lại từ đầu
        self.assertIn("chỉ sửa đúng các tiêu chí yếu", second)
        crit = [c["criterion"] for c in e.detail["weak_criteria"]]
        self.assertTrue(crit)
        self.assertIn(crit[0], e.message)                                                           # lỗi nói rõ tiêu chí nào yếu


class OutlineConstraintTest(Base):
    def test_outline_prompt_states_every_gate_rule_before_generation(self):
        llm = RemixFakeLLM()
        self.plan(llm, mode=cfg(audio_readability="high"))
        prompt = next(c["prompt"] for c in llm.calls if c["step"] == "outline")
        for needle in ("RÀNG BUỘC", "hook", "Phần ba cuối", "loại thưởng", "TỐI ĐA 5"):
            self.assertIn(needle, prompt)

    def test_gate_failure_stops_before_writer_and_repair_sends_previous_outline(self):
        llm = RemixFakeLLM(outline_gap=50, outline_repair_gap=2)
        r = self.plan(llm)
        rp = next(c["prompt"] for c in llm.calls if c["step"] == "outline_repair")
        self.assertIn("BẢN TRƯỚC", rp)                                                              # chỉ sửa phần lỗi: có bản trước + yêu cầu giữ chương không lỗi
        self.assertIn("GIỮ NGUYÊN chương không lỗi", rp)
        self.assertEqual(r["quality"]["decision"], "pass")
        e = self.code(RemixFakeLLM(outline_gap=50), mode=cfg(quality_repair_max_passes=0), out="x")
        self.assertEqual(e.code, "OUTLINE_GATE_FAILED")
        self.assertFalse((self.dir / "x" / "chapters").exists())                                    # outline xấu không vào Writer

    def test_outline_without_hook_is_rejected_even_when_the_gate_is_off(self):
        class NoHook(RemixFakeLLM):
            def _outline(self, prompt, n, repair=False):
                if "ĐẠI CƯƠNG" not in prompt:                                                       # lượt sửa JSON gọn: fake không tự sửa nội dung, trả lại bản cũ
                    return json.loads(prompt.split("JSON HIỆN TẠI:", 1)[1])
                o = super()._outline(prompt, n, repair)
                o["chapters"][0]["hook"] = ""
                return o
        e = self.code(NoHook(), mode=cfg(outline_gate=False))
        self.assertEqual(e.code, "REMIX_LLM_INVALID")
        self.assertEqual(e.detail["kind"], "content")


class OriginalityScopeTest(Base):
    def _copying_outline(self, llm):
        orig = llm._outline
        llm._outline = lambda prompt, n, repair=False: (lambda o: (o["chapters"][0]["beats"].__setitem__(0, "Lý Hoàng gặp Tống Mai ở biệt thự Hắc Long") if n == 1 and not repair else None, o)[1])(orig(prompt, n, repair))
        return llm

    def test_violation_only_in_outline_repairs_only_the_outline(self):
        llm = self._copying_outline(RemixFakeLLM())
        r = self.plan(llm)
        self.assertEqual(llm.counts["premises"], 1)                                                 # KHÔNG lập lại ý tưởng
        self.assertEqual(llm.counts.get("story_bible", 0), 1)                                       # bible giữ nguyên
        self.assertEqual(llm.counts["outline_repair"], 1)                                           # chỉ đại cương được lập lại
        self.assertEqual(r["originality"]["decision"], "pass")
        blocked = json.loads((self.dir / "plan" / "originality_report.blocked_1.json").read_text(encoding="utf-8"))
        self.assertEqual(blocked["repair_scope"], "outline")
        cond = blocked["failed_conditions"][0]
        self.assertEqual(cond["condition"], "SOURCE_NAMES_REUSED")                                  # nói rõ điều kiện thực sự gây chặn
        self.assertTrue(any(loc.startswith("outline.chapters[1]") for loc in cond["locations"]))    # và vị trí
        fb = next(c["prompt"] for c in llm.calls if c["step"] == "outline_repair")
        self.assertIn("SOURCE_NAMES_REUSED", fb)                                                    # lượt sửa nhận đúng điều kiện vi phạm

    def test_evidence_carries_condition_value_threshold_and_location(self):
        from contentfactory.story_remix import gates as GT
        from tests.fakes_remix import SOURCE
        p = premise("P1")
        bible = {"causal_chain": [{"event": "a", "cause": "b", "effect": "c"}] * 3}
        outline = {"chapters": [{"goal": "g", "beats": [SOURCE[i * 150:(i + 1) * 150] for i in range(3)], "payoff": None}]}
        og = GT.originality_gate(p, bible, outline, {"members": [{"display_name": "Nam"}]}, SOURCE, "own", None, Ledger(self.dir / "c.json"), review=False)
        ev = {e["condition"]: e for e in og["evidence"]}
        self.assertIn("PLAN_4GRAM_CONTAINMENT", ev)
        self.assertEqual(ev["PLAN_4GRAM_CONTAINMENT"]["threshold"], GT.CONTAIN_HIGH)
        self.assertEqual(ev["PLAN_4GRAM_CONTAINMENT"]["components"], ["outline"])
        self.assertEqual(og["repair_scope"], "outline")

    def test_still_blocked_reports_the_failing_conditions(self):
        llm = RemixFakeLLM(review="retell", review_overlaps=[{"aspect": "chuỗi sự kiện", "severity": "high", "evidence": "cùng chuỗi phản bội → trả thù"}])
        e = self.code(llm)
        self.assertEqual(e.code, "ORIGINALITY_BLOCKED")
        self.assertEqual(e.detail["conditions"][0]["condition"], "MODEL_RETELL")
        self.assertIn("MODEL_RETELL", e.message)


class ChapterContractTest(RootCase):
    def setUp(self):
        super().setUp()
        self.o = self.orc()

    def run_story(self, llm, passes=1):
        bridge = UniverseBridge(self.o.universe)
        self.o.adapters["story"] = StoryModeRouter(fake.FakeStory(), lambda: StoryRemixAdapter(llm, lambda: bridge))
        jid = self.o.submit(params(story_mode={"mode": "story_remix", "story": {"quality_repair_max_passes": passes}}), mode="STORY_ONLY")
        self.o.run()
        return jid

    @staticmethod
    def body(names, target=3000):
        return "\n\n".join(f"{names[k % len(names)]} nhìn {names[(k + 1) % len(names)]} ở bàn số {k} và nói điều thứ {k * 11} với giọng khẽ khàng, ánh đèn hắt xuống sàn nhà lạnh." for k in range(1, target // 100))

    def sroot(self, jid):
        return self.job_dir(jid) / "story"

    def test_headings_markers_and_recap_are_cleaned_by_code_without_repair_call(self):
        def dirty(attempt, names, target):
            text = "## Chương 2\n\nỞ chương trước, mọi chuyện đã xảy ra như thế.\n\n<!-- ghi chú -->" + self.body(names, target) + "\n\n**Hết chương**"
            return text, MEM
        llm = RemixFakeLLM(chapter_behavior={2: dirty})
        jid = self.run_story(llm)
        self.assertEqual(self.o.store.get_job(jid)["state"], "STORY_READY")
        self.assertNotIn("chapter_2_repair", llm.counts)                                            # sửa bằng code, không tốn lượt AI
        self.assertEqual(validate_story_text((self.sroot(jid) / "story.txt").read_text(encoding="utf-8")), [])
        saved = (self.sroot(jid) / "remix" / "chapters" / "ch_002.md").read_text(encoding="utf-8")
        for bad in ("Chương 2", "<!--", "Ở chương trước", "**"):
            self.assertNotIn(bad, saved)

    def test_repeated_paragraphs_are_blocked_repaired_once_then_fail_when_no_repair_allowed(self):
        def repeated(attempt, names, target):
            if attempt:
                return None
            para = f"{names[0]} kể cho {names[1]} nghe một chuyện rất dài về buổi tối hôm đó, từng chi tiết một, không bỏ sót điều gì cả."
            return "\n\n".join([para] * 10 + [self.body(names, target)]), MEM
        llm = RemixFakeLLM(chapter_behavior={2: repeated})
        jid = self.run_story(llm)
        self.assertEqual(self.o.store.get_job(jid)["state"], "STORY_READY")
        self.assertEqual(llm.counts["chapter_2_repair"], 1)
        rep = json.loads((self.sroot(jid) / "remix" / "writer_report.json").read_text(encoding="utf-8"))["chapters"]
        self.assertEqual(next(c for c in rep if c["n"] == 2)["repairs"], 1)
        bad = self.run_story(RemixFakeLLM(chapter_behavior={2: repeated}), passes=0)
        self.assertEqual(self.o.store.get_job(bad)["state"], "FAILED")
        self.assertIn("CHAPTER_QA_FAILED", json.dumps(self.o.store.stage_runs(bad), default=str))

    def test_prompt_carries_the_output_contract(self):
        llm = RemixFakeLLM()
        self.run_story(llm)
        p = next(c["prompt"] for c in llm.calls if c["step"] == "chapter_1")
        for needle in ("HỢP ĐỒNG ĐẦU RA", "KHÔNG tiêu đề", "markdown", "KHÔNG tóm tắt hay nhắc lại chương trước", "tên kênh", "chức danh"):
            self.assertIn(needle, p)


class SharedContractTest(unittest.TestCase):
    def test_sanitize_prose_only_drops_non_story_lines(self):
        raw = "Chương 3: Gặp lại\n---\nTôi đã nói rồi, chương trình này hay lắm.\n[[TODO]]\n# Tiêu đề\nCô ấy cười."
        text, removed = sanitize_prose(raw)
        self.assertEqual(" ".join(text.split()), "Tôi đã nói rồi, chương trình này hay lắm. Cô ấy cười.")
        self.assertGreaterEqual(len(removed), 3)
        self.assertEqual(validate_story_text(text), [])

    def test_story_invalid_is_detected_for_each_forbidden_marker(self):
        for bad, code in (("Chương 1: mở đầu\n\nNội dung.", "CHAPTER_HEADER"), ("Nội dung <!-- x -->", "TECHNICAL_MARKER"), ("Nội dung {{x}}", "TECHNICAL_MARKER"), ("# Tiêu đề\n\nNội dung", "TECHNICAL_MARKER"),
                          ("TODO viết tiếp", "TECHNICAL_MARKER"), ("  \n ", "EMPTY"), ("\n\n".join(["Một đoạn lặp lại y hệt nhau."] * 5), "REPEATED_PARAGRAPHS")):
            self.assertIn(code, validate_story_text(bad), bad)

    def test_assembler_names_the_section_that_causes_excess_removal_and_never_raises_the_limit(self):
        good = "\n\n".join(f"Đoạn số {k} kể về một sự kiện khác hẳn với các đoạn còn lại trong chương này, có chi tiết riêng {k * 31}." for k in range(8))
        with self.assertRaises(StageError) as cm:
            assemble([good, good + "\n\nChương 9\n\n---"], 0.35)                                   # section 2 chép lại section 1 ⇒ bị xoá gần hết
        self.assertEqual(cm.exception.code, "ASSEMBLER_REMOVED_TOO_MUCH")
        self.assertEqual(cm.exception.detail["culprits"][0]["section"], 1)                          # sửa tại nguồn: section nào gây ra
        self.assertIn("không nâng ngưỡng", cm.exception.detail["hint"])

    def test_empty_story_names_every_emptied_section(self):
        with self.assertRaises(StageError) as cm:
            assemble(["Chương 1\n---", "## Phần 2\n[[x]]"], 0.35)
        self.assertEqual(cm.exception.code, "EMPTY_STORY")
        self.assertEqual([x["chars_out"] for x in cm.exception.detail["by_section"]], [0, 0])


class StageEmptyStoryTest(RootCase):
    def test_stage_never_leaves_a_story_txt_for_tts_when_all_content_vanishes(self):
        class Junk:
            def generate(self, bundle, profile, out_dir, ctx):
                out_dir.mkdir(parents=True, exist_ok=True)
                f = out_dir / "s1.md"
                f.write_text("Chương 1\n---\n<!-- x -->\n", encoding="utf-8")
                return {"sections": [f]}
        o = self.orc()
        o.adapters["story"] = Junk()
        jid = o.submit(params(), mode="THROUGH_TTS")
        o.run()
        self.assertEqual(o.store.get_job(jid)["state"], "FAILED")
        self.assertIn("EMPTY_STORY", json.dumps(o.store.stage_runs(jid), default=str))
        self.assertFalse((self.job_dir(jid) / "story" / "story.txt").exists())
        self.assertFalse(any(r["stage"] == "tts" for r in o.store.stage_runs(jid)))                 # không có gì đi tiếp sang TTS


class LlmFormatAndBudgetTest(Base):
    def test_json_errors_are_classified_and_bounded(self):
        class Bad(RemixFakeLLM):
            def _source_dna(self, prompt, n):
                return {"genre": "x"}
        llm = Bad()
        e = self.code(llm)
        self.assertEqual((e.code, e.detail["kind"]), ("REMIX_LLM_INVALID", "content"))
        self.assertEqual(llm.counts["source_dna"], 3)                                               # 1 + 2 lần sửa: không vòng lặp vô hạn

    def test_code_repairs_harmless_json_defects_without_calling_the_llm_again(self):
        self.assertEqual(extract_json('{"a": [1, 2,], "b": "dòng 1\ndòng 2",}'), {"a": [1, 2], "b": "dòng 1\ndòng 2"})
        self.assertEqual(extract_json('Đây là kết quả:\n```json\n{"x": 1}\n```'), {"x": 1})

    def test_budget_preflight_stops_early_but_only_with_real_cost_data(self):
        led = Ledger(self.dir / "l.json", 5.0)
        led.preflight(10, "x")                                                                      # chưa có chi phí thật ⇒ không ước được, không chặn bừa
        for i in range(3):
            led.record(f"s{i}", {"cost_usd": 1.0, "tokens_in": 1, "tokens_out": 1, "seconds": 0.0})
        led.preflight(1, "ok")                                                                      # 3 + 1×1 = 4 ≤ 5
        with self.assertRaises(StageError) as cm:
            led.preflight(5, "viết chương")                                                         # 3 + 5×1 = 8 > 5 ⇒ dừng SỚM, trước khi tốn thêm
        self.assertEqual(cm.exception.code, "BUDGET_EXCEEDED")
        self.assertTrue(cm.exception.detail["estimate"])
        self.assertEqual(cm.exception.error_class, ErrorClass.POLICY)


if __name__ == "__main__":
    unittest.main()
