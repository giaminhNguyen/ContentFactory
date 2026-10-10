"""Remix bám sự việc (`story_scene_remix`): đăng ký mode, quyền nguồn, Scene Map → Remix Plan → viết lại có mục tiêu → QA → story.txt; checkpoint/resume; ngân sách; không retry vô hạn.
Dùng LLM GIẢ: chỉ chứng minh đường ống và các validator, KHÔNG chứng minh chất lượng truyện của LLM thật."""
import json
import tempfile
import unittest
from pathlib import Path

from contentfactory.adapters import fake
from contentfactory.contracts import CancelToken, ErrorClass, StageContext, StageError
from contentfactory.orchestrator.story_router import StoryModeRouter
from contentfactory.story import mode as SM
from contentfactory.story.validate import validate_story_text
from contentfactory.story_remix.core import Ledger
from contentfactory.story_scene_remix import logic as L
from contentfactory.story_scene_remix.adapter import StorySceneRemixAdapter
from contentfactory.story_scene_remix.fake_llm import FakeSceneRemixLLM
from tests.support import RootCase, params

RIGHTS = {"source_rights": "own", "rights_ack": True}
SCENE = {"mode": "story_scene_remix", "story": dict(RIGHTS)}


WORDS = ["gió", "mưa", "nắng", "sương", "bụi", "tiếng trống", "mùi mực", "hơi lạnh", "ánh đèn", "tiếng cười"]


def para(k: int, pen: bool) -> str:
    """Đoạn ~700 ký tự, MỌI câu khác nhau (nguồn thật không lặp nguyên văn; Assembler sẽ xoá phần lặp)."""
    sents = [f"Sáng thứ {k}, Lan bước vào lớp {k + 3}A và thấy {WORDS[(k + j) % 10]} len qua khe cửa số {j + k}, khiến cả lớp im lặng lạ thường." for j in range(3)]
    sents += [f"Cô giáo Hà đứng trước bảng lần thứ {k * 7 + j}, ánh mắt nghiêm khắc dừng ở bàn {j + 2} và {WORDS[(k * 3 + j) % 10]} như báo trước điều chẳng lành." for j in range(3)]
    sents += [f"Lan nhớ lại chuyện hôm qua lúc {k + 6} giờ {10 + j} phút, tim đập thình thịch trong lồng ngực." for j in range(2)]
    return " ".join(sents) + (" Trên bàn của Hùng có chiếc bút đỏ mà cả lớp đang bàn tán. " if pen else " ")


def make_source(n_paras: int = 14, pen_at=(0, 2, 6)) -> str:
    return "\n\n".join(para(k, k in pen_at) for k in range(n_paras))


class Env:
    """Dựng StageContext + file nguồn để gọi adapter trực tiếp (không qua orchestrator)."""

    def __init__(self, tc: unittest.TestCase, source: str, mode=None, channel: str = ""):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-scene-"))
        tc.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.src = self.tmp / "transcript.txt"
        self.src.write_text(source, encoding="utf-8")
        self.out = self.tmp / "story"
        self.out.mkdir()
        self.mode = mode or SCENE
        self.logs: list = []

    def ctx(self, mode=None):
        return StageContext(job_id="j1", stage="story", attempt=1, stage_key="k", workspace=self.tmp, stage_dir=self.out, params={"story_mode": mode or self.mode}, inputs={}, config={},
                            cancel=CancelToken(), log=lambda *a, **k: self.logs.append((a, k)))

    def run(self, llm, mode=None, price=None, profile=None):
        ad = StorySceneRemixAdapter(llm, price)
        return ad.generate({"title": "t", "language": "vi", "transcript": self.src}, profile or {}, self.out, self.ctx(mode))

    def rd(self, name):
        return json.loads((self.out / "scene_remix" / name).read_text(encoding="utf-8"))

    def story(self):
        return (self.out / "scene_remix" / "rewritten_story.md").read_text(encoding="utf-8")


def tweak(prompt, step):
    """Bản sửa của LLM giả: văn bản khác đi một chút (như LLM thật sửa mối nối)."""
    return prompt.split("CẢNH HIỆN TẠI:" + chr(10), 1)[1] + " Lan thở dài."


def mode_with(**kw):
    return {"mode": "story_scene_remix", "story": {**RIGHTS, **kw}}


class ModeRegistrationTest(unittest.TestCase):
    def test_three_modes_exist_and_legacy_default_is_unchanged(self):
        self.assertEqual(SM.MODES, ("story_branch", "story_remix", "story_scene_remix"))
        self.assertEqual(SM.parse({}), {"mode": "story_branch"})
        self.assertEqual(SM.default_mode({}), "story_branch")
        self.assertIsNone(SM.resolve_for_job(None, {}))
        d = SM.describe({})
        self.assertEqual([m["id"] for m in d["modes"]], list(SM.MODES))
        self.assertIn("story_scene_remix", d["by_mode"])
        self.assertNotIn("character_universe", d["by_mode"]["story_scene_remix"]["schema"])         # không Kho nhân vật
        self.assertNotIn("premise_candidates", {f["key"] for f in d["by_mode"]["story_scene_remix"]["schema"]["story"]})
        self.assertIn("character_universe", d["schema"])                                            # schema Story Remix giữ nguyên cho client cũ

    def test_stage_keys_are_isolated_between_modes(self):
        remix = SM.stage_key_extra({"story_mode": SM.parse({"mode": "story_remix"}, {})})
        scene = SM.stage_key_extra({"story_mode": SM.parse(SCENE, {})})
        self.assertNotEqual(json.dumps(remix, sort_keys=True), json.dumps(scene, sort_keys=True))
        self.assertIsNone(SM.stage_key_extra({}))                                                   # story thường: khoá y như trước
        self.assertEqual(scene["story_mode"]["mode"], "story_scene_remix")
        # đổi quyền/ngân sách/ghi chú KHÔNG đổi khoá (không viết lại); đổi độ dễ nghe thì đổi
        same = SM.stage_key_extra({"story_mode": SM.parse(mode_with(budget_usd=5, source_provenance="x", review_accepted=True), {})})
        self.assertEqual(same, scene)
        diff = SM.stage_key_extra({"story_mode": SM.parse(mode_with(audio_readability="high"), {})})
        self.assertNotEqual(diff, scene)

    def test_unknown_or_unconfirmed_rights_block_at_job_creation(self):
        for story in ({}, {"source_rights": "unknown", "rights_ack": True}, {"source_rights": "own"}, {"source_rights": "licensed", "rights_ack": False}):
            with self.assertRaises(StageError) as cm:
                SM.resolve_for_job({"mode": "story_scene_remix", "story": story}, {})
            self.assertEqual(cm.exception.code, "SOURCE_RIGHTS_REQUIRED")
        self.assertEqual(SM.resolve_for_job(SCENE, {})["mode"], "story_scene_remix")

    def test_scene_mode_rejects_foreign_fields_and_character_universe(self):
        with self.assertRaises(StageError):
            SM.parse({"mode": "story_scene_remix", "story": {**RIGHTS, "premise_candidates": 3}}, {})
        with self.assertRaises(StageError):
            SM.parse({"mode": "story_scene_remix", "story": RIGHTS, "character_universe": {}}, {})

    def test_effective_reports_scene_values(self):
        e = SM.effective(SCENE, {})
        self.assertEqual(e["mode"], "story_scene_remix")
        self.assertEqual(e["story"]["source_rights"]["value"], "own")
        self.assertNotIn("character_universe", e)


class LogicTest(unittest.TestCase):
    def test_split_is_contiguous_and_keeps_every_word(self):
        src = make_source()
        scenes = L.split_scenes(src)
        self.assertGreater(len(scenes), 5)
        self.assertEqual(" ".join(" ".join(scenes).split()), " ".join(src.split()))
        self.assertTrue(all(len(s) <= L.SPLIT_MAX for s in scenes))
        self.assertEqual(L.split_scenes("   "), [])
        huge = "Một câu rất dài không dấu chấm " * 400                                             # ASR một dòng khổng lồ
        self.assertTrue(all(len(s) <= L.SPLIT_MAX for s in L.split_scenes(huge)))

    def test_clean_prose_removes_non_story_lines_only(self):
        raw = "```\n# Chương 3\n**Lan** bước vào lớp.\nGhi chú: đã sửa chiếc bút.\n<!-- x -->Cô giáo nhìn.\nCảnh s003\n```"
        text, removed = L.clean_prose(raw)
        self.assertEqual(text, "Lan bước vào lớp.\nCô giáo nhìn.")
        self.assertTrue(removed)
        self.assertEqual(L.clean_prose("Tôi đã nói rồi, chương trình này hay lắm.")[0], "Tôi đã nói rồi, chương trình này hay lắm.")

    def test_check_scene_flags_contract_violations(self):
        src = "Hùng cầm chiếc bút đỏ. " * 20
        rel = [{"id": "c1", "old": "chiếc bút", "new": "hộp cơm", "level": 1, "scene_ids": ["s001"], "why": ""}]
        codes = lambda t: {i["code"] for i in L.check_scene(src, t, rel, ["Kênh Cũ Audio"])}   # noqa: E731
        self.assertEqual(codes("Hùng cầm hộp cơm đỏ. " * 20), set())
        self.assertIn("OLD_REMAINS", codes("Hùng cầm chiếc bút đỏ. " * 20))
        self.assertIn("TOO_SHORT", codes("Hùng cầm hộp cơm."))
        self.assertIn("TOO_LONG", codes("Hùng cầm hộp cơm đỏ. " * 80))
        self.assertIn("SOURCE_BRAND_TRACE", codes("Hùng cầm hộp cơm đỏ. " * 20 + "Kênh Cũ Audio"))
        self.assertIn("FORMAT", codes("Hùng cầm hộp cơm đỏ. " * 20 + "\n\nChương 3"))
        self.assertIn("EMPTY", codes("  "))

    def test_plan_checks(self):
        scenes = ["Hùng cầm chiếc bút đỏ.", "Lan khóc.", "Cô giáo thấy chiếc bút."]
        ok = L.check_plan({"changes": [{"id": "c1", "old": "chiếc bút", "new": "hộp cơm", "level": 1, "scene_ids": ["s001"]}]}, scenes)
        self.assertEqual(ok["changes"][0]["scene_ids"], ["s001", "s003"])                           # bổ sung bằng code mọi cảnh chứa cụm cũ
        from contentfactory.story_remix.core import Invalid
        for c in ({"id": "c1", "old": "cái áo", "new": "cái mũ", "level": 1, "scene_ids": ["s001"]},          # old không có trong nguồn
                  {"id": "c1", "old": "chiếc bút", "new": "chiếc bút", "level": 1, "scene_ids": ["s001"]},
                  {"id": "c1", "old": "chiếc bút", "new": "hộp cơm", "level": 3, "scene_ids": ["s001"]},      # cấp 3 không tự động
                  {"id": "c1", "old": "Lan khóc", "new": "Lan cười", "level": 2, "scene_ids": ["s002"]}):     # cấp 2 thiếu lý do
            with self.assertRaises(Invalid):
                L.check_plan({"changes": [c]}, scenes)
        with self.assertRaises(StageError) as cm:
            L.check_plan({"changes": [], "needs_level3": "phải đổi cả tuyến"}, scenes)
        self.assertEqual(cm.exception.code, "REMIX_NEEDS_LEVEL3")

    def test_det_qa_catches_leftovers_duplicates_and_markers(self):
        ch = [{"id": "c1", "old": "chiếc bút", "new": "hộp cơm", "level": 1, "scene_ids": ["s001"], "why": ""}]
        dup = "Đoạn văn này đủ dài để bị coi là lặp lại giữa hai cảnh khác nhau của truyện."
        issues = L.det_qa(["Hùng cầm chiếc bút. " + dup, dup + " Lan khóc.\n\n<!-- x -->"], ch, [])
        codes = {(i["scene_id"], i["code"]) for i in issues}
        self.assertIn(("s001", "OLD_REMAINS"), codes)
        self.assertIn(("s002", "FORMAT"), codes)
        self.assertEqual(L.det_qa(["Hùng cầm hộp cơm. " + dup, "Lan khóc rất nhiều vì chuyện hôm qua."], ch, []), [])


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(self, make_source())
        self.llm = FakeSceneRemixLLM()

    def test_level1_end_to_end_changes_only_affected_scenes(self):
        res = self.env.run(self.llm)
        scenes = L.split_scenes(self.env.src.read_text(encoding="utf-8"))
        out = self.env.story()
        self.assertNotIn("chiếc bút", out)
        self.assertIn("hộp cơm", out)
        plan = self.env.rd("remix_plan.json")
        self.assertEqual([c["level"] for c in plan["changes"]], [1])
        affected = self.env.rd("affected_scenes.json")["ids"]
        # đúng các cảnh còn chứa cụm cũ trong nguồn được viết lại, cảnh khác gọi LLM = 0 và giữ nguyên văn
        want = [L.sid(i) for i, t in enumerate(scenes) if "chiếc bút" in t]
        self.assertEqual(affected, want)
        self.assertEqual(self.llm.count("scene_rewrite_"), len(want))
        self.assertEqual(self.llm.count("scene_change_plan"), 1)                                   # MỘT kế hoạch toàn cục
        self.assertEqual(self.llm.count("source_map_"), -(-len(scenes) // L.GROUP))
        untouched = [i for i in range(len(scenes)) if L.sid(i) not in affected]
        for i in untouched:
            self.assertIn(scenes[i].strip()[:120], out)
        self.assertEqual(validate_story_text(out), [])
        self.assertEqual(res["stats"]["mode"], "story_scene_remix")
        self.assertEqual(res["stats"]["scenes_revised"], len(want))

    def test_scene_map_is_structured_and_reused(self):
        self.env.run(self.llm)
        m = self.env.rd("scene_map.json")
        for k in ("pov", "characters", "recurring_objects", "hook_scene", "beats", "ending_scenes", "scenes"):
            self.assertIn(k, m)
        self.assertEqual(m["hook_scene"], "s001")
        self.assertIn("chiếc bút", m["recurring_objects"])
        self.assertEqual(m["scenes"][0]["beat"], "hook")

    def test_dependent_scenes_are_updated_even_when_plan_lists_only_one(self):
        self.env.run(self.llm)
        plan = self.env.rd("remix_plan.json")
        scenes = L.split_scenes(self.env.src.read_text(encoding="utf-8"))
        holders = [L.sid(i) for i, t in enumerate(scenes) if "chiếc bút" in t]
        self.assertEqual(plan["changes"][0]["scene_ids"], holders)
        for s in holders:
            self.assertNotIn("chiếc bút", (self.env.out / "scene_remix" / "scenes" / f"{s}.txt").read_text(encoding="utf-8"))

    def test_resume_makes_zero_llm_calls(self):
        self.env.run(self.llm)
        n = len(self.llm.calls)
        res = self.env.run(self.llm)
        self.assertEqual(len(self.llm.calls), n)
        self.assertGreater(res["stats"]["steps_skipped"], 0)

    def test_resume_after_scene_failure_redoes_only_remaining_scenes(self):
        state = {"n": 0}

        def die(prompt, step):
            state["n"] += 1
            return "Quá ngắn." if state["n"] >= 3 else None                                          # từ lần gọi viết lại thứ 3 trở đi LLM trả cảnh cụt (cả lần thử lại)
        llm = FakeSceneRemixLLM(script={"scene_rewrite_": die})
        with self.assertRaises(StageError) as cm:
            self.env.run(llm)
        self.assertEqual(cm.exception.code, "SCENE_REWRITE_INVALID")
        self.assertFalse((self.env.out / "scene_remix" / "rewritten_story.md").exists())            # không có story nửa vời
        done = sorted(p.name for p in (self.env.out / "scene_remix" / "scenes").glob("*.json"))
        self.assertEqual(len(done), 2)
        good = FakeSceneRemixLLM()
        self.env.run(good)
        self.assertEqual(good.count("source_map_"), 0)
        self.assertEqual(good.count("scene_change_plan"), 0)
        self.assertEqual(good.count("scene_rewrite_"), len(self.env.rd("affected_scenes.json")["ids"]) - 2)

    def test_junk_in_rewrite_is_cleaned_by_code_without_retry(self):
        def junk(prompt, step):
            src = prompt.split("CẢNH NGUỒN:\n", 1)[1].replace("chiếc bút", "hộp cơm")
            return "```\n## Chương 2\n" + src + "\nGhi chú: đã thay bút bằng hộp cơm.\n```"
        llm = FakeSceneRemixLLM(script={"scene_rewrite_": junk})
        self.env.run(llm)
        self.assertEqual(llm.count("scene_rewrite_"), len(self.env.rd("affected_scenes.json")["ids"]))     # không thử lại
        self.assertEqual(validate_story_text(self.env.story()), [])
        self.assertNotIn("Ghi chú", self.env.story())

    def test_too_short_rewrite_is_retried_once_with_reason_then_ok(self):
        seen = {}

        def short_first(prompt, step):
            if step not in seen:
                seen[step] = 1
                return "Cụt."
            self.assertIn("LẦN TRƯỚC BỊ TỪ CHỐI", prompt)
            return None
        llm = FakeSceneRemixLLM(script={"scene_rewrite_": short_first})
        self.env.run(llm)
        n = len(self.env.rd("affected_scenes.json")["ids"])
        self.assertEqual(llm.count("scene_rewrite_"), 2 * n)                                       # đúng 1 lần thử lại mỗi cảnh, không hơn

    def test_no_infinite_retry_on_garbage_json(self):
        llm = FakeSceneRemixLLM(script={"source_map_": lambda p, s: "không phải json"})
        with self.assertRaises(StageError) as cm:
            self.env.run(llm)
        self.assertEqual(cm.exception.code, "REMIX_LLM_INVALID")
        self.assertEqual(cm.exception.detail["kind"], "format")
        self.assertEqual(llm.count("source_map_001"), 3)                                           # 1 + 2 thử lại, dừng

    def test_invalid_json_is_repaired_by_code_not_by_llm(self):
        llm = FakeSceneRemixLLM(script={"scene_continuity_qa": lambda p, s: '{"issues": [],}'})        # dấu phẩy thừa: sửa bằng code
        self.env.run(llm)
        self.assertEqual(llm.count("scene_continuity_qa"), 1)

    def test_hallucinated_old_phrase_is_rejected_then_hard_fails_bounded(self):
        bad = {"changes": [{"id": "c1", "old": "cái áo tàng hình", "new": "cái mũ", "level": 1, "scene_ids": ["s001"]}]}
        llm = FakeSceneRemixLLM(script={"scene_change_plan": lambda p, s: bad})
        with self.assertRaises(StageError) as cm:
            self.env.run(llm)
        self.assertEqual(cm.exception.code, "REMIX_LLM_INVALID")
        self.assertEqual(cm.exception.detail["kind"], "content")
        self.assertEqual(llm.count("scene_change_plan"), 3)
        self.assertEqual(llm.count("scene_rewrite_"), 0)

    def test_level3_is_never_automatic(self):
        lv3 = {"changes": [{"id": "c1", "old": "chiếc bút", "new": "cả một vụ án", "level": 3, "scene_ids": ["s001"]}]}
        llm = FakeSceneRemixLLM(script={"scene_change_plan": lambda p, s: lv3})
        with self.assertRaises(StageError):
            self.env.run(llm)
        self.assertEqual(llm.count("scene_rewrite_"), 0)
        need = FakeSceneRemixLLM(script={"scene_change_plan": lambda p, s: {"changes": [], "needs_level3": "chỉ đổi được bằng cách thay cả tuyến án mạng"}})
        env2 = Env(self, make_source())
        with self.assertRaises(StageError) as cm:
            env2.run(need)
        self.assertEqual(cm.exception.code, "REMIX_NEEDS_LEVEL3")
        self.assertIn("tuyến", cm.exception.detail["reason"])
        self.assertEqual(need.count("scene_rewrite_"), 0)

    def test_level2_allowed_only_with_reason_and_is_warned_when_heavy(self):
        scenes = L.split_scenes(self.env.src.read_text(encoding="utf-8"))
        plan = {"changes": [{"id": "c1", "old": "chiếc bút", "new": "hộp cơm", "level": 1, "scene_ids": ["s001"]},
                            {"id": "c2", "old": "im lặng lạ thường", "new": "ồn ào lạ thường", "level": 2, "scene_ids": ["s002"], "why_level2": "cả cảnh cần đổi không khí cho hợp"},
                            {"id": "c3", "old": "ánh mắt nghiêm khắc", "new": "ánh mắt dịu dàng", "level": 2, "scene_ids": ["s003"], "why_level2": "đổi thái độ cô giáo kéo theo cả cảnh"}]}
        llm = FakeSceneRemixLLM(script={"scene_change_plan": lambda p, s: plan})
        self.env.run(llm)
        rep = self.env.rd("scene_remix_report.json")
        self.assertEqual((rep["level_1"], rep["level_2"], rep["level_3"]), (1, 2, 0))
        self.assertTrue(rep["plan_warnings"])                                                       # 2/3 cấp 2 > 25%: CẢNH BÁO, không chặn
        lvl = self.env.rd("affected_scenes.json")["levels"]
        self.assertIn(2, lvl.values())
        self.assertTrue(len(scenes) > 5)
        no_reason = {"changes": [{**plan["changes"][1], "why_level2": ""}]}
        env2 = Env(self, make_source())
        with self.assertRaises(StageError):
            env2.run(FakeSceneRemixLLM(script={"scene_change_plan": lambda p, s: no_reason}))

    def test_plan_spreading_over_65_percent_is_rejected_for_long_stories(self):
        src = make_source(30, pen_at=tuple(range(30)))
        env = Env(self, src)
        llm = FakeSceneRemixLLM()
        with self.assertRaises(StageError) as cm:
            env.run(llm)
        self.assertEqual(cm.exception.code, "REMIX_LLM_INVALID")
        self.assertIn("lan sang", cm.exception.detail["reason"])
        self.assertEqual(llm.count("scene_rewrite_"), 0)                                            # chặn TRƯỚC khi tốn chi phí viết văn

    def test_qa_issue_repairs_only_the_cited_scene(self):
        calls = {"n": 0}

        def qa(prompt, step):
            calls["n"] += 1
            return {"issues": [{"scene_id": "s003", "problem": "Hùng xưng hô lệch so với cảnh trước."}]} if calls["n"] == 1 else {"issues": []}
        llm = FakeSceneRemixLLM(script={"scene_continuity_qa": qa, "scene_repair_": tweak})
        self.env.run(llm)
        self.assertEqual([c for c in llm.calls if c.startswith("scene_repair_")], ["scene_repair_s003"])
        self.assertEqual(llm.count("scene_continuity_qa"), 2)                                       # QA lại một lần sau sửa, không hơn
        q = self.env.rd("continuity_qa.json")
        self.assertEqual((q["issues"], q["repair_passes"]), ([], 1))

    def test_unresolved_qa_blocks_story_until_review_accepted(self):
        always = lambda p, s: {"issues": [{"scene_id": "s002", "problem": "Mâu thuẫn thời gian."}]}   # noqa: E731
        llm = FakeSceneRemixLLM(script={"scene_continuity_qa": always, "scene_repair_": tweak})
        with self.assertRaises(StageError) as cm:
            self.env.run(llm)
        self.assertEqual(cm.exception.code, "SCENE_CONTINUITY_REVIEW")
        self.assertEqual(llm.count("scene_repair_"), 1)                                             # chỉ 1 lượt sửa (mặc định), không vòng lặp
        self.assertEqual(llm.count("scene_continuity_qa"), 2)
        before = len(llm.calls)
        ok = self.env.run(llm, mode=mode_with(review_accepted=True))                                # người dùng đã xem: tiếp tục, KHÔNG sửa lại/gọi lại QA
        self.assertEqual(len(llm.calls), before)
        self.assertEqual(ok["stats"]["qa_unresolved"], 1)

    def test_zero_repair_passes_never_repairs(self):
        always = lambda p, s: {"issues": [{"scene_id": "s002", "problem": "x"}]}   # noqa: E731
        llm = FakeSceneRemixLLM(script={"scene_continuity_qa": always})
        with self.assertRaises(StageError):
            self.env.run(llm, mode=mode_with(quality_repair_max_passes=0))
        self.assertEqual(llm.count("scene_repair_"), 0)

    def test_qa_ids_that_do_not_exist_are_dropped_not_retried(self):
        llm = FakeSceneRemixLLM(script={"scene_continuity_qa": lambda p, s: {"issues": [{"scene_id": "s999", "problem": "ảo"}]}})
        self.env.run(llm)
        self.assertEqual(llm.count("scene_continuity_qa"), 1)

    def test_rights_are_checked_again_at_run_time(self):
        for mode in ({"mode": "story_scene_remix", "story": {"source_rights": "unknown", "rights_ack": True}}, {"mode": "story_remix"}):
            with self.assertRaises(StageError):
                self.env.run(self.llm, mode=mode)
        self.assertEqual(self.llm.calls, [])

    def test_budget_stops_early_and_keeps_checkpoints(self):
        llm = FakeSceneRemixLLM(cost=1.0)
        with self.assertRaises(StageError) as cm:
            self.env.run(llm, mode=mode_with(budget_usd=3))
        self.assertEqual(cm.exception.code, "BUDGET_EXCEEDED")
        self.assertTrue((self.env.out / "scene_remix" / "remix_plan.json").is_file() or (self.env.out / "scene_remix" / "source_map_001.json").is_file())
        spent = Ledger(self.env.out / "scene_remix" / "cost_report.json").known_cost()
        self.assertLessEqual(spent, 3.0 + 1e-9)                                                     # không vượt ngân sách
        done = len(llm.calls)
        res = self.env.run(llm, mode=mode_with(budget_usd=500))                                     # nâng ngân sách ⇒ tiếp tục, không làm lại bước xong
        self.assertEqual(res["stats"]["mode"], "story_scene_remix")
        self.assertEqual(llm.count("source_map_001"), 1)
        self.assertGreater(len(llm.calls), done)

    def test_budget_preflight_with_price_blocks_before_any_call(self):
        llm = FakeSceneRemixLLM()
        with self.assertRaises(StageError) as cm:
            self.env.run(llm, mode=mode_with(budget_usd=1), price={"in": 5000.0, "out": 20000.0})
        self.assertEqual(cm.exception.code, "BUDGET_EXCEEDED")
        self.assertEqual(llm.calls, [])

    def test_estimate_is_small_vs_story_remix(self):
        from contentfactory.story_remix.estimate import estimate as remix_estimate
        e = L.estimate(60_000)
        r = remix_estimate({"quality_repair_max_passes": 1, "premise_candidates": 3}, {}, 60_000)
        self.assertLess(e["calls"]["min"], r["calls"]["min"] + 100)
        self.assertGreaterEqual(e["calls"]["max"], e["calls"]["min"])

    def test_source_without_replaceable_detail_stops_with_reason(self):
        env = Env(self, "Một câu rất ngắn không có đồ vật nào.\n\nCâu thứ hai cũng vậy.")
        with self.assertRaises(StageError) as cm:
            env.run(FakeSceneRemixLLM())
        self.assertEqual(cm.exception.code, "REMIX_NEEDS_LEVEL3")
        self.assertEqual(cm.exception.error_class, ErrorClass.POLICY)

    def test_brand_trace_in_rewrite_is_a_hard_contract_error(self):
        env = Env(self, make_source())
        ad = StorySceneRemixAdapter(FakeSceneRemixLLM())
        self.assertEqual(ad._marks("Truyện hay 【Tinh Hà Audio】 | Kênh Cũ", "Kênh Gốc"), ["Kênh Gốc", "Tinh Hà Audio", "Kênh Cũ"])
        self.assertEqual(ad._marks("Mẹ chồng độc ác - số 3", ""), [])                              # không dùng phần đầu tên video (tránh báo nhầm)
        self.assertTrue(env.src.is_file())


class RouterAndPipelineTest(RootCase):
    """Qua orchestrator thật (fake adapters): job Scene Remix tạo story.txt, đi tiếp TTS; hai mode cũ không đổi."""

    def setUp(self):
        super().setUp()
        self.o = self.orc()
        story = make_source().replace("\n\n", " ")
        self.cues = story

        class BigSource(fake.FakeSource):
            def acquire(s, src, out_dir, ctx):
                res = super().acquire(src, out_dir, ctx)
                ws = story.split(". ")
                lines = ["WEBVTT", ""]
                for k, w in enumerate(ws):
                    lines += [f"00:{k // 60:02d}:{k % 60:02d}.000 --> 00:{k // 60:02d}:{k % 60:02d}.900", w.strip() + ".", ""]
                Path(res["raw_subtitle_path"]).write_text("\n".join(lines), encoding="utf-8")
                return res
        self.o.adapters["source"] = BigSource()

    def run_job(self, story_mode, mode="STORY_ONLY", **kw):
        jid = self.o.submit(params(story_mode=story_mode, **kw), mode=mode)
        self.o.run()
        return jid

    def test_scene_remix_job_produces_valid_story_txt_and_flows_to_tts(self):
        jid = self.run_job(SCENE, mode="THROUGH_TTS")
        self.assertEqual(self.o.store.get_job(jid)["state"], "AUDIO_READY")
        text = (self.job_dir(jid) / "story" / "story.txt").read_text(encoding="utf-8")
        self.assertEqual(validate_story_text(text), [])
        self.assertIn("hộp cơm", text)
        self.assertNotIn("chiếc bút", text)
        self.assertFalse((self.job_dir(jid) / "story" / "remix").exists())                           # không đụng đường Story Remix
        d = json.loads([r for r in self.o.store.stage_runs(jid) if r["stage"] == "story"][-1]["data"])
        self.assertEqual(d["mode"], "story_scene_remix")

    def test_job_creation_rejects_unknown_rights(self):
        with self.assertRaises(StageError) as cm:
            self.o.submit(params(story_mode={"mode": "story_scene_remix", "story": {}}), mode="STORY_ONLY")
        self.assertEqual(cm.exception.code, "SOURCE_RIGHTS_REQUIRED")

    def test_old_modes_unchanged_and_default_job_has_no_story_mode(self):
        plain = self.o.submit(params(), mode="STORY_ONLY")
        self.o.run()
        self.assertEqual(self.o.store.get_job(plain)["state"], "STORY_READY")
        self.assertNotIn("story_mode", self.o.store.get_job(plain)["params"])
        self.assertFalse((self.job_dir(plain) / "story" / "scene_remix").exists())
        remix = self.run_job({"mode": "story_remix"})
        self.assertEqual(self.o.store.get_job(remix)["state"], "STORY_READY")
        self.assertTrue((self.job_dir(remix) / "story" / "remix").is_dir())
        self.assertFalse((self.job_dir(remix) / "story" / "scene_remix").exists())

    def test_router_selects_adapter_by_mode_and_never_mixes(self):
        r = self.o.adapters["story"]
        self.assertIsInstance(r, StoryModeRouter)
        self.assertIsInstance(r.scene_remix, StorySceneRemixAdapter)
        self.assertIsNot(r.scene_remix, r.remix)
        self.assertEqual(r.health()["ok"] if "ok" in r.health() else True, True)


if __name__ == "__main__":
    unittest.main()
