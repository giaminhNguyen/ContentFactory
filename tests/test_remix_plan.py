"""Story Remix – lập kế hoạch (Phase 4): cô lập nguồn, chọn ý tưởng, autocast, bible/đại cương, cổng originality + dopamine, checkpoint theo bước, chi phí/ngân sách."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from contentfactory.contracts import StageError
from contentfactory.jobs.workspace import job_dir
from contentfactory.orchestrator.service import Service
from contentfactory.orchestrator.remix_universe import UniverseBridge
from contentfactory.story import mode as SM
from contentfactory.story_remix import gates as GT
from contentfactory.story_remix import schemas as SC
from contentfactory.story_remix import similarity as SIM
from contentfactory.story_remix import stages as ST
from contentfactory.story_remix.core import Invalid, Ledger, extract_json
from contentfactory.story_remix.plan import plan_story
from contentfactory.universe import Universe, UniverseDB
from tests.fakes_remix import DNA, SOURCE, RemixFakeLLM, premise
from tests.support import RootCase, params

PROFILE = {"target_chars": 18000, "chapter_chars": 3000}          # 6 chương ⇒ 4–8 cho phép


def cfg(**story):
    return SM.parse({"mode": "story_remix", "story": story})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = UniverseDB(self.dir / "u.db")
        self.addCleanup(self.db.close)
        self.uni = Universe(self.db)
        self.bridge = UniverseBridge(self.uni)

    def plan(self, llm, mode=None, out="plan", **kw):
        return plan_story(llm, self.bridge, SOURCE, "Truyện nguồn", "vi", mode or cfg(), PROFILE, self.dir / out, "7", **kw)

    def code(self, llm, **kw):
        try:
            self.plan(llm, **kw)
        except StageError as e:
            return e
        self.fail("không có lỗi")


class HappyPathTest(Base):
    def test_full_plan_artifacts_isolation_and_cast_consistency(self):
        llm = RemixFakeLLM()
        r = self.plan(llm)
        d = self.dir / "plan"
        for f in ("source_dna.json", "premise_candidates.json", "selection_report.json", "character_cast.json", "story_bible.json", "outline.json", "originality_report.json", "quality_report.json", "cost_report.json"):
            self.assertTrue((d / f).is_file(), f)
        # cô lập nguồn: chỉ DNA + cổng đọc transcript
        saw_source = {c["step"] for c in llm.calls if "Tống Mai" in c["prompt"] or "Hắc Long" in c["prompt"]}
        self.assertEqual(saw_source, {"source_dna", "originality_review"})
        for c in llm.calls:
            if c["step"] in ("premises", "story_bible", "outline"):
                self.assertNotIn("Lý Hoàng", c["prompt"])
        # RM-004: ý tưởng yếu bị loại TRƯỚC khi viết dài
        sel = r["selection"]
        self.assertEqual(sel["selected"], "P1")
        self.assertEqual({x["id"] for x in sel["rejected"]}, {"P2", "P3"})
        self.assertTrue(all("yếu nhất" in x["reason"] for x in sel["rejected"]))
        # nhất quán dàn nhân vật (LU-013)
        ids = {m["character_id"] for m in r["cast"]["members"]}
        self.assertEqual({m["character_id"] for m in r["bible"]["cast"]}, ids)
        self.assertTrue(all(set(c["cast"]) <= ids for c in r["outline"]["chapters"]))
        self.assertEqual(r["cast"]["state"], "staged")
        self.assertEqual(self.uni.list_characters(status="")["total"], 0)                                      # chưa chạm kho chính thức
        self.assertEqual(r["originality"]["decision"], "pass")
        self.assertEqual(r["quality"]["decision"], "pass")
        self.assertIn("KHÔNG phải xác nhận", r["originality"]["legal_note"])
        self.assertEqual(r["steps"]["skipped"], [])

    def test_reports_never_claim_clearance_and_list_uncertainty(self):
        og = self.plan(RemixFakeLLM())["originality"]
        self.assertTrue(og["uncertainty"])
        self.assertNotIn("an toàn bản quyền", og["next_step"])
        self.assertIn("plan_4gram_containment", og["metrics"])


class DnaTest(Base):
    def test_dna_with_source_names_is_rejected_and_retried(self):
        class Leaky(RemixFakeLLM):
            def _source_dna(self, prompt, n):
                return {**DNA, "engagement_engine": "Lý Hoàng trả thù Tống Mai"} if n == 1 else DNA
        llm = Leaky()
        r = self.plan(llm)
        self.assertEqual(llm.counts["source_dna"], 2)
        self.assertIn("tên riêng của nguồn", [c for c in llm.calls if c["step"] == "source_dna"][1]["prompt"])      # lý do từ chối được phản hồi cho LLM
        self.assertNotIn("Tống Mai", json.dumps(r["dna"], ensure_ascii=False))

    def test_dna_copying_source_phrases_is_rejected(self):
        phrase = "tỉnh dậy, nhớ lại bữa tiệc ở biệt thự"

        class Copier(RemixFakeLLM):
            def _source_dna(self, prompt, n):
                return {**DNA, "pacing": phrase} if n == 1 else DNA
        llm = Copier()
        self.plan(llm)
        self.assertEqual(llm.counts["source_dna"], 2)
        self.assertIn("chép lại cụm từ", [c for c in llm.calls if c["step"] == "source_dna"][1]["prompt"])

    def test_invalid_llm_output_exhausts_retries_with_clear_error(self):
        class Bad(RemixFakeLLM):
            def _source_dna(self, prompt, n):
                return {"genre": "x"}
        e = self.code(Bad())
        self.assertEqual(e.code, "REMIX_LLM_INVALID")
        self.assertEqual(e.detail["step"], "source_dna")
        self.assertEqual(e.error_class.name, "TRANSIENT")                                                        # orchestrator tự chạy lại (có giới hạn), không fail ngay


class PremiseTest(Base):
    def test_weak_first_round_regenerates_with_feedback_then_selects(self):
        llm = RemixFakeLLM(premises_by_call=[[premise("P1", weak=True), premise("P2", weak=True)], [premise("P3"), premise("P4", weak=True)]])
        r = self.plan(llm)
        self.assertEqual(r["selection"]["selected"], "P3")
        second = [c for c in llm.calls if c["step"] == "premises"][1]["prompt"]
        self.assertIn("PHẢN HỒI TỪ LẦN TRƯỚC", second)

    def test_all_weak_fails_before_any_expensive_step(self):
        llm = RemixFakeLLM(premises_by_call=[[premise("P1", weak=True), premise("P2", weak=True)]] * 2)
        e = self.code(llm)
        self.assertEqual(e.code, "PREMISE_TOO_WEAK")
        self.assertNotIn("story_bible", llm.counts)                                                            # không tốn chi phí bible/đại cương/viết
        self.assertNotIn("outline", llm.counts)
        self.assertTrue((self.dir / "plan" / "selection_report.json").is_file())

    def test_blocked_themes_are_enforced(self):
        llm = RemixFakeLLM()
        e = self.code(llm, mode=cfg(blocked_themes=["gian lận"]))
        self.assertEqual(e.code, "REMIX_LLM_INVALID")                                                          # mọi ý tưởng đều chứa chủ đề bị cấm

    def test_selection_prefers_reuse_when_universe_has_fit(self):
        self.uni.create_character({"display_name": "Mộc Lan", "core_personality": "kiên trì tỉ mỉ chịu khó", "strengths": ["kiên trì", "tỉ mỉ"], "genre_affinities": ["báo thù"]})
        r = self.plan(RemixFakeLLM())
        hero = next(m for m in r["cast"]["members"] if m["role_code"] == "protagonist")
        self.assertEqual(hero["origin"], "reused")
        self.assertGreater(r["selection"]["scores"]["P1"]["parts"]["continuity"], 0)


class OriginalityTest(Base):
    def test_copying_names_is_blocked_then_regenerated_once(self):
        llm = RemixFakeLLM(premises_by_call=[[premise("P1", copy_names=True), premise("P2", weak=True)], [premise("P3"), premise("P4", weak=True)]])
        # bản đầu dùng tên nguồn trong beat để bị chặn
        orig = llm._outline
        llm._outline = lambda prompt, n, repair=False: (lambda o: (o["chapters"][0]["beats"].__setitem__(0, "Lý Hoàng gặp Tống Mai ở biệt thự Hắc Long") if n == 1 else None, o)[1])(orig(prompt, n, repair))
        r = self.plan(llm)
        self.assertEqual(llm.counts["premises"], 2)
        self.assertEqual(r["selection"]["selected"], "P3")
        self.assertIn("quá giống nguồn", [c for c in llm.calls if c["step"] == "premises"][1]["prompt"])
        self.assertEqual(r["originality"]["decision"], "pass")

    def test_still_blocked_after_retry_stops_before_long_write(self):
        llm = RemixFakeLLM(review="retell", review_overlaps=[{"aspect": "chuỗi sự kiện", "severity": "high", "evidence": "cùng chuỗi phản bội → trả thù"}])
        e = self.code(llm)
        self.assertEqual(e.code, "ORIGINALITY_BLOCKED")
        self.assertTrue((self.dir / "plan" / "originality_report.json").is_file())
        self.assertEqual(llm.counts["premises"], 2)

    def test_medium_overlap_needs_review_unless_rights_declared_or_accepted(self):
        sim = dict(review="similar", review_overlaps=[{"aspect": "bối cảnh", "severity": "medium", "evidence": "cùng bối cảnh phản bội trong công ty"}])
        e = self.code(RemixFakeLLM(**sim))
        self.assertEqual(e.code, "ORIGINALITY_REVIEW_REQUIRED")                                                 # quyền nguồn unknown ⇒ cần người xem
        self.assertIn("evidence", e.detail)
        r = self.plan(RemixFakeLLM(**sim), mode=cfg(review_accepted=True), out="acc")
        self.assertEqual(r["originality"]["decision"], "review")
        r = self.plan(RemixFakeLLM(**sim), mode=cfg(source_rights="licensed"), out="own")
        self.assertEqual(r["originality"]["decision"], "pass_with_note")

    def test_lexical_metrics_detect_copying_and_names(self):
        p = premise("P1")
        bible = {"causal_chain": [{"event": SOURCE[:200], "cause": SOURCE[200:400], "effect": SOURCE[400:600]}] * 3}
        outline = {"chapters": [{"goal": "g", "beats": [SOURCE[i * 150:(i + 1) * 150] for i in range(3)], "payoff": None}]}
        cast = {"members": [{"display_name": "Tống Mai"}]}
        og = GT.originality_gate(p, bible, outline, cast, SOURCE, "unknown", None, Ledger(self.dir / "c.json"), review=False)
        self.assertEqual((og["decision"], og["level"]), ("block", "high"))
        self.assertIn("Tống Mai", og["metrics"]["source_names_reused"])
        self.assertIn("Không có nhận xét mô hình", " ".join(og["uncertainty"]))


class DopamineTest(Base):
    def test_gap_triggers_one_bounded_repair(self):
        llm = RemixFakeLLM(outline_gap=50, outline_repair_gap=2)
        r = self.plan(llm)
        self.assertEqual((r["quality"]["decision"], r["quality"]["repairs"]), ("pass", 1))
        self.assertEqual(llm.counts["outline_repair"], 1)

    def test_repair_budget_zero_fails_clearly(self):
        e = self.code(RemixFakeLLM(outline_gap=50), mode=cfg(quality_repair_max_passes=0))
        self.assertEqual(e.code, "OUTLINE_GATE_FAILED")
        self.assertIn("PAYOFF_GAP", {i["code"] for i in e.detail["issues"]})
        self.assertEqual(json.loads((self.dir / "plan" / "quality_report.json").read_text(encoding="utf-8"))["decision"], "fail")

    def test_gate_can_be_disabled(self):
        r = self.plan(RemixFakeLLM(outline_gap=50), mode=cfg(outline_gate=False))
        self.assertEqual(r["quality"]["decision"], "skipped")

    def test_gate_unit_cases(self):
        o = RemixFakeLLM()._outline("ĐẠI CƯƠNG 6–6\n- ch_aaaaaaaaaaaa | x | vai protagonist\n- ch_bbbbbbbbbbbb | y | vai antagonist\n- ch_cccccccccccc | z | vai ally", 1)
        self.assertEqual(GT.dopamine_gate(o, DNA)["decision"], "pass")
        o["chapters"][0]["hook"] = ""
        self.assertIn("NO_OPENING_HOOK", {i["code"] for i in GT.dopamine_gate(o, DNA)["issues"]})
        for c in o["chapters"]:
            c["cast"] = ["a", "b", "c", "d", "e", "f"]
        self.assertIn("TOO_MANY_VOICES", {i["code"] for i in GT.dopamine_gate(o, DNA, "high")["issues"]})


class ResumeAndCostTest(Base):
    def test_rerun_with_same_inputs_makes_no_llm_calls_except_cheap_gate_reuse(self):
        llm = RemixFakeLLM()
        self.plan(llm)
        n = len(llm.calls)
        r = self.plan(llm)
        self.assertEqual(len(llm.calls), n)                                                                     # RM-008: mọi bước đắt được dùng lại
        self.assertEqual(r["steps"]["ran"], [])
        self.assertIn("outline", r["steps"]["skipped"])

    def test_provenance_and_budget_changes_do_not_invalidate_but_content_changes_do(self):
        llm = RemixFakeLLM()
        self.plan(llm, mode=cfg())
        n = len(llm.calls)
        self.plan(llm, mode=cfg(rights_ack=True, source_provenance="video của tôi", budget_usd=900))
        self.assertEqual(len(llm.calls), n)
        r = self.plan(llm, mode=cfg(tone="u ám"))
        self.assertGreater(len(llm.calls), n)
        self.assertNotIn("dna", r["steps"]["ran"])                                                              # đổi giọng không phân tích lại nguồn

    def test_budget_stops_safely_and_resumes_after_raising(self):
        llm = RemixFakeLLM(cost=0.5)
        e = self.code(llm, mode=cfg(budget_usd=1.2))
        self.assertEqual(e.code, "BUDGET_EXCEEDED")
        spent = llm.counts.copy()
        rep = json.loads((self.dir / "plan" / "cost_report.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(rep["known_cost_usd"], 1.2)
        r = self.plan(llm, mode=cfg(budget_usd=50))                                                             # nâng ngân sách: tiếp tục, DNA không gọi lại
        self.assertEqual(llm.counts["source_dna"], spent["source_dna"])
        self.assertEqual(r["quality"]["decision"], "pass")

    def test_unknown_cost_is_reported_not_invented(self):
        r = self.plan(RemixFakeLLM(report_cost=False), mode=cfg(budget_usd=1))                               # không biết chi phí ⇒ không thể chặn, và nói rõ
        self.assertEqual(r["cost"]["known_cost_usd"], 0)
        self.assertEqual(r["cost"]["calls_with_unknown_cost"], r["cost"]["calls"])
        self.assertIn("không ước đoán", r["cost"]["note"])


class SchemaTest(unittest.TestCase):
    def test_json_extraction(self):
        self.assertEqual(extract_json('xin chào ```json\n{"a": [1, {"b": 2}]}\n``` cảm ơn'), {"a": [1, {"b": 2}]})
        with self.assertRaises(Invalid):
            extract_json("không có json")

    def test_arbitrary_length_and_count_limits_never_fail_or_cut(self):
        # Lỗi thật (job 000016): genre dài > 80 ký tự làm hỏng job sau 3 lượt gọi. Giờ văn bản dài/danh sách nhiều mục được nhận NGUYÊN VẸN.
        long = "Ngôn tình đô thị kết hợp huyền huyễn, thần côn hài hước đối đầu tổng tài lạnh lùng trong gia tộc hào môn " * 3
        many = [f"phần thưởng {i}" for i in range(30)]
        dna = SC.source_dna({**DNA, "genre": long, "reward_types": many, "subgenres": many, "avoid": many})
        self.assertEqual((dna["genre"], dna["reward_types"]), (long.strip(), many))
        p = premise("P1")
        big = SC.premise({**p, "id": "IDEA_NUMBER_ONE_LONG", "twist": long, "payoff_plan": p["payoff_plan"] * 10})
        self.assertEqual((big["twist"], len(big["payoff_plan"])), (long.strip(), len(p["payoff_plan"]) * 10))

    def test_safe_format_slips_are_fixed_in_code_not_by_another_llm_call(self):
        dna = SC.source_dna({**DNA, "genre": ["báo thù", "phản đòn"], "reward_types": "vạch mặt", "payoff_cadence": {"first_payoff_by_pct": "15%", "payoffs_per_10pct": 9}})
        self.assertEqual((dna["genre"], dna["reward_types"]), ("báo thù; phản đòn", ["vạch mặt"]))
        self.assertEqual(dna["payoff_cadence"], {"first_payoff_by_pct": 15, "payoffs_per_10pct": 5})          # lệch thang đo ⇒ kẹp về biên
        p = premise("P1")
        s0 = {**p["slots"][0], "slot_id": " Hero ", "role_code": "Protagonist"}
        s1 = {**p["slots"][1], "relationships": [{"with": "HERO", "type": "kẻ thù"}]}
        fixed = SC.premise({**p, "slots": [s0, s1, *p["slots"][2:]]})
        self.assertEqual((fixed["slots"][0]["slot_id"], fixed["slots"][0]["role_code"], fixed["slots"][1]["relationships"][0]["with"]), ("hero", "protagonist", "hero"))
        long = "bằng chứng rất dài " * 40
        rv = GT._review({"verdict": "Similar", "overlaps": [{"aspect": "tình tiết", "severity": "HIGH", "evidence": long}]})
        self.assertEqual((rv["verdict"], rv["overlaps"][0]["severity"], rv["overlaps"][0]["evidence"]), ("similar", "high", long))   # bằng chứng không bị cắt

    def test_retry_repairs_only_the_bad_json_without_resending_the_source(self):
        class LLM:
            def __init__(self):
                self.prompts = []

            def complete(self, prompt, *, system, step, ctx=None):
                self.prompts.append(prompt)
                bad = {**DNA, "pov": {"x": 1}} if len(self.prompts) == 1 else DNA                              # lượt 1 sai đúng MỘT trường
                return {"text": json.dumps(bad, ensure_ascii=False), "cost_usd": 0.1}
        llm, src = LLM(), SOURCE * 20
        led = Ledger(Path(tempfile.mkdtemp()) / "c.json")
        self.assertEqual(ST.analyze_dna(llm, led, src, "vi")["genre"], DNA["genre"])
        self.assertEqual(len(llm.prompts), 2)
        self.assertIn(SOURCE[:200], llm.prompts[0])
        self.assertNotIn(SOURCE[:200], llm.prompts[1])                                                           # lượt sửa không gửi lại transcript
        self.assertIn("pov: phải là văn bản", llm.prompts[1])
        self.assertLess(len(llm.prompts[1]), len(llm.prompts[0]) / 5)

    def test_integrity_and_quality_constraints_are_kept(self):
        p = premise("P1")
        for bad in ({**p, "slots": [{**p["slots"][0], "role_code": "vua"}, *p["slots"][1:]]},                  # vai không thuộc danh mục
                    {**p, "payoff_plan": p["payoff_plan"][:2]},                                             # quá ít điểm thưởng (cơ chế dopamine)
                    {**p, "slots": [s for s in p["slots"] if s["role_code"] != "protagonist"] + [p["slots"][-1]]}):
            with self.assertRaises(Invalid):
                SC.premise(bad)
        with self.assertRaises(Invalid):
            SC.source_dna({**DNA, "genre": ""})

    def test_premise_validation(self):
        good = premise("P1")
        self.assertEqual(SC.premise(good)["id"], "P1")
        for bad in ({**good, "slots": good["slots"][1:]}, {**good, "payoff_plan": good["payoff_plan"][:2]}, {**good, "twist": ""},
                    {**good, "slots": [{**good["slots"][0], "role_code": "king"}, *good["slots"][1:]]},
                    {**good, "slots": [good["slots"][0], {**good["slots"][1], "relationships": [{"with": "zz", "type": "x"}]}, good["slots"][2]]}):
            with self.assertRaises(Invalid):
                SC.premise(bad)

    def test_outline_requires_frozen_cast_ids_and_protagonist_presence(self):
        ids = {"ch_aaaaaaaaaaaa", "ch_bbbbbbbbbbbb"}
        ch = lambda i, cast: {"n": i, "title": "t", "goal": "g", "beats": ["b"], "cast": cast, "hook": "h", "payoff": None}          # noqa: E731
        ok = {"chapters": [ch(1, ["ch_aaaaaaaaaaaa", "ch_bbbbbbbbbbbb"]), ch(2, ["ch_aaaaaaaaaaaa"]), ch(3, ["ch_aaaaaaaaaaaa"])]}
        self.assertEqual(SC.outline(ok, ids, "ch_aaaaaaaaaaaa", 3, 5)["total_chapters"], 3)
        with self.assertRaises(Invalid):
            SC.outline({"chapters": [ch(1, ["Lan"]), ch(2, ["ch_aaaaaaaaaaaa"]), ch(3, ["ch_bbbbbbbbbbbb"])]}, ids, "ch_aaaaaaaaaaaa", 3, 5)         # tên tự do
        with self.assertRaises(Invalid):
            SC.outline({"chapters": [ch(1, ["ch_bbbbbbbbbbbb"]), ch(2, ["ch_bbbbbbbbbbbb"]), ch(3, ["ch_aaaaaaaaaaaa"])]}, ids, "ch_aaaaaaaaaaaa", 3, 5)   # chính < 60%
        with self.assertRaises(Invalid):
            SC.outline(ok, ids, "ch_aaaaaaaaaaaa", 4, 5)                                                                                         # số chương
        with self.assertRaises(Invalid):
            SC.story_bible({"cast": [{"character_id": "ch_zzzzzzzzzzzz", "arc": "x"}]}, ids)

    def test_asr_transcript_capitalised_sentence_starts_are_not_proper_names(self):
        # Lỗi chỉ lộ khi chạy nguồn thật: ASR viết hoa đầu câu nhưng thiếu dấu chấm ⇒ "Cậu/Không/Lúc" bị coi là tên riêng và trừ điểm novelty oan.
        asr = " ".join(["Cậu ấy nói rằng cậu không biết Lúc đó lúc nào không biết Hạnh gặp Khuê rồi Không ai tin cậu lúc đó"] * 6 + ["Hạnh Khuê"] * 3)
        names = SIM.proper_names(asr)
        self.assertIn("Hạnh", names)
        self.assertIn("Khuê", names)
        for common in ("Cậu", "Không", "Lúc"):
            self.assertNotIn(common, names)

    def test_similarity_helpers(self):
        self.assertEqual(SIM.containment("a b c d e f", "a b c d e f g"), 1.0)
        self.assertEqual(SIM.containment("x y z w", "a b c d"), 0.0)
        self.assertEqual(SIM.reused_names(["Lý Hoàng", "Ai Đó"], "gặp lý hoàng ở đó"), ["Lý Hoàng"])


class ServiceViewTest(RootCase):
    def test_remix_plan_view_reads_artifacts_and_never_leaks_call_log(self):
        with mock.patch.object(SM, "BACKEND_READY", True):
            orc = self.orc()
            jid = orc.submit(params(story_mode={"mode": "story_remix"}), mode="STORY_ONLY")
        svc = Service(orc)
        before = svc.remix_plan(jid)
        self.assertEqual((before["active"], before["ready"], before["dna"], before["premises"]), (True, False, None, None))
        plan_story(RemixFakeLLM(), UniverseBridge(orc.universe), SOURCE, "T", "vi", SM.parse({"mode": "story_remix"}), PROFILE, job_dir(orc.cfg.path("workspace"), jid) / "story" / "remix", jid)
        p = svc.remix_plan(jid)
        self.assertTrue(p["ready"])
        self.assertEqual([x["selected"] for x in p["premises"]].count(True), 1)
        self.assertTrue(all(x["reason"] for x in p["premises"] if not x["selected"]))
        self.assertEqual(p["originality"]["decision"], "pass")
        self.assertEqual(len(p["cast"]["members"]), 3)
        self.assertEqual(len(p["outline"]), p["quality"]["metrics"]["chapters"])
        self.assertNotIn("call_log", p["cost"])
        self.assertEqual(svc.job_detail(jid)["story_mode"]["mode"], "story_remix")
        plain = orc.submit(params(), mode="STORY_ONLY")
        self.assertEqual(svc.remix_plan(plain), {"active": False})


if __name__ == "__main__":
    unittest.main()
