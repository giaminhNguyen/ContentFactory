"""Story Remix end-to-end (Phase 5): job → router → kế hoạch → viết chương → Assembler/Validator HIỆN CÓ → story.txt → QA cuối; tương thích TTS; resume; chi phí."""
import json
import unittest

from contentfactory.adapters import fake
from contentfactory.contracts import StageError
from contentfactory.adapters.fake_remix import FakeRemixLLM
from contentfactory.contracts import CancelToken
from contentfactory.orchestrator.remix_universe import UniverseBridge
from contentfactory.orchestrator.story_router import StoryModeRouter
from contentfactory.story.validate import validate_story_text
from contentfactory.story_remix.adapter import StoryRemixAdapter
from contentfactory.universe import Universe, UniverseDB
from tests.support import RootCase, params

REMIX = {"mode": "story_remix", "story": {"quality_repair_max_passes": 1}}


class E2E(RootCase):
    def setUp(self):
        super().setUp()
        self.orc_ = self.orc()
        self.uni = self.orc_.universe

    def install(self, llm):
        bridge = UniverseBridge(self.uni)
        self.orc_.adapters["story"] = StoryModeRouter(fake.FakeStory(), lambda: StoryRemixAdapter(llm, lambda: bridge))

    def run_job(self, story_mode=REMIX, mode="STORY_ONLY", **kw):
        jid = self.orc_.submit(params(story_mode=story_mode, **kw), mode=mode)
        self.orc_.run()
        return jid

    def stage(self, jid, name="story"):
        return [r for r in self.orc_.store.stage_runs(jid) if r["stage"] == name][-1]

    def sdir(self, jid):
        return self.job_dir(jid) / "story"

    def test_default_registry_remix_job_produces_valid_story_and_artifacts(self):
        jid = self.run_job()
        self.assertEqual(self.orc_.store.get_job(jid)["state"], "STORY_READY")
        text = (self.sdir(jid) / "story.txt").read_text(encoding="utf-8")
        self.assertEqual(validate_story_text(text), [])                                                         # đúng hợp đồng story.txt hiện có
        self.assertGreater(len(text), 5000)
        d = json.loads(self.stage(jid)["data"])
        self.assertEqual((d["mode"], d["originality"], d["final_qa_accepted"]), ("story_remix", "pass", True))
        r = self.sdir(jid) / "remix"
        for f in ("source_dna.json", "selection_report.json", "character_cast.json", "story_bible.json", "outline.json", "originality_report.json", "quality_report.json",
                  "story_memory.json", "writer_report.json", "final_qa.json", "cost_report.json"):
            self.assertTrue((r / f).is_file(), f)
        cast = json.loads((r / "character_cast.json").read_text(encoding="utf-8"))
        for m in cast["members"]:
            self.assertIn(m["display_name"].split()[-1], text)                                                  # mọi nhân vật trong dàn có mặt trong truyện
        self.assertEqual(self.uni.list_characters(status="")["total"], 0)                                       # publish thuộc Phase 6; staged chưa vào kho

    def test_old_story_mode_is_untouched_by_the_router(self):
        jid = self.orc_.submit(params(), mode="STORY_ONLY")
        self.orc_.run()
        self.assertEqual(self.orc_.store.get_job(jid)["state"], "STORY_READY")
        self.assertFalse((self.sdir(jid) / "remix").exists())
        self.assertNotIn("mode", json.loads(self.stage(jid)["data"]))

    def test_remix_story_flows_through_tts_interface_unchanged(self):
        jid = self.run_job(mode="THROUGH_TTS")
        self.assertEqual(self.orc_.store.get_job(jid)["state"], "AUDIO_READY")
        kinds = {a["kind"] for a in self.orc_.store.artifacts(jid)}
        self.assertTrue({"story_text", "story_report", "audio_chunks"} <= kinds or {"story_text", "story_report"} <= kinds)

    def test_chapter_checkpoints_make_rerun_free_and_budget_resumable(self):
        llm = FakeRemixLLM(cost=0.5)
        self.install(llm)
        jid = self.run_job()
        n = len(llm.calls)
        self.assertGreater(n, 8)
        from contentfactory.story_remix.core import Ledger
        led = json.loads((self.sdir(jid) / "remix" / "cost_report.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(led["known_cost_usd"], 0.5 * n, places=3)
        # chạy lại stage story với cùng đầu vào: adapter dùng lại mọi chương/bước (không gọi LLM)
        sdir = self.sdir(jid)
        (sdir / "story.txt").unlink()
        (sdir / "assembly_report.json").unlink()
        adapter = self.orc_.adapters["story"].remix
        from contentfactory.contracts import StageContext
        ctx = StageContext(job_id=jid, stage="story", attempt=1, stage_key="k", workspace=self.job_dir(jid), stage_dir=sdir, params=self.orc_.store.get_job(jid)["params"], inputs={}, config={}, cancel=CancelToken(),
                           log=lambda *a, **k: None)
        res = adapter.generate({"title": "t", "language": "vi", "source_language": "vi", "transcript": self.job_dir(jid) / next(a["path"] for a in self.orc_.store.artifacts(jid) if a["kind"] == "transcript")}, {}, sdir, ctx)
        self.assertEqual([c['step'] for c in llm.calls[n:]], [])
        self.assertEqual(res["stats"]["chapters_written"], 0)
        self.assertEqual(res["stats"]["chapters_reused"], res["stats"]["chapters"])
        self.assertEqual(Ledger(sdir / "remix" / "cost_report.json").known_cost(), led["known_cost_usd"])

    def test_budget_exceeded_fails_clearly_and_keeps_finished_work(self):
        llm = FakeRemixLLM(cost=0.7)
        self.install(llm)
        jid = self.run_job(story_mode={**REMIX, "story": {"budget_usd": 1.5}})
        self.assertEqual(self.orc_.store.get_job(jid)["state"], "FAILED")
        self.assertIn("BUDGET_EXCEEDED", json.dumps(self.stage(jid), default=str))
        self.assertTrue((self.sdir(jid) / "remix" / "source_dna.json").is_file())
        self.assertFalse((self.sdir(jid) / "story.txt").exists())

    def test_chapter_qa_repairs_then_fails_when_repairs_disabled(self):
        short = lambda attempt, names, target: ("Quá ngắn. " * 20, {"new_facts": [], "state_changes": [], "opened": [], "resolved": [], "new_named_persons": []}) if attempt == 0 else None   # noqa: E731
        llm = FakeRemixLLM(chapter_behavior={2: short})
        self.install(llm)
        ok = self.run_job()
        self.assertEqual(self.orc_.store.get_job(ok)["state"], "STORY_READY")
        rep = json.loads((self.sdir(ok) / "remix" / "writer_report.json").read_text(encoding="utf-8"))
        self.assertEqual(next(c for c in rep["chapters"] if c["n"] == 2)["repairs"], 1)
        self.assertEqual(llm.counts["chapter_2_repair"], 1)                                                     # sửa có mục tiêu, chỉ đúng chương lỗi
        llm2 = FakeRemixLLM(chapter_behavior={2: short})
        self.install(llm2)
        bad = self.run_job(story_mode={**REMIX, "story": {"quality_repair_max_passes": 0}}, title="Truyện khác")
        self.assertEqual(self.orc_.store.get_job(bad)["state"], "FAILED")
        self.assertIn("CHAPTER_QA_FAILED", json.dumps(self.stage(bad), default=str))
        self.assertTrue((self.sdir(bad) / "remix" / "chapters" / "ch_001.md").is_file())                       # chương trước được giữ

    def test_unapproved_main_character_in_chapter_is_blocked_and_repaired(self):
        def sneaky(attempt, names, target):
            if attempt == 0:
                text = " ".join(f"{names[0]} đi qua con phố vắng và nói chuyện với {names[1]} về chuyện cũ {i}." for i in range(60))
                return text, {"new_facts": ["x"], "state_changes": [], "opened": [], "resolved": [], "new_named_persons": [{"name": "Kẻ Lạ Mặt", "minor": False}]}
            return None
        llm = FakeRemixLLM(chapter_behavior={3: sneaky})
        self.install(llm)
        jid = self.run_job()
        self.assertEqual(self.orc_.store.get_job(jid)["state"], "STORY_READY")
        self.assertEqual(llm.counts.get("chapter_3_repair"), 1)
        mem = json.loads((self.sdir(jid) / "remix" / "story_memory.json").read_text(encoding="utf-8"))
        self.assertNotIn("Kẻ Lạ Mặt", [p["name"] for p in mem["minor_persons"]])

    def test_originality_block_stops_before_any_chapter_is_written(self):
        llm = FakeRemixLLM(review="retell", review_overlaps=[{"aspect": "chuỗi sự kiện", "severity": "high", "evidence": "kể lại cùng chuỗi"}])
        self.install(llm)
        jid = self.run_job()
        self.assertEqual(self.orc_.store.get_job(jid)["state"], "FAILED")
        self.assertIn("ORIGINALITY_BLOCKED", json.dumps(self.stage(jid), default=str))
        self.assertFalse(any(c["step"].startswith("chapter_") for c in llm.calls))                              # không tốn chi phí viết dài
        self.assertFalse((self.sdir(jid) / "remix" / "chapters").exists())

    def test_final_qa_accepted_but_publish_not_configured_leaves_universe_untouched(self):
        llm = FakeRemixLLM()
        self.install(llm)
        jid = self.run_job()
        fq = json.loads((self.sdir(jid) / "remix" / "final_qa.json").read_text(encoding="utf-8"))
        self.assertTrue(fq["accepted"])
        self.assertEqual(fq["universe_publish"]["status"], "skipped")                                            # Phase 6 nối publisher; ở đây không ghi gì vào kho


class Controls(E2E):
    def svc(self):
        from contentfactory.orchestrator.service import Service
        return Service(self.orc_)

    def test_review_required_then_accept_resumes_without_rework(self):
        llm = FakeRemixLLM(review="similar", review_overlaps=[{"aspect": "bối cảnh", "severity": "medium", "evidence": "cùng bối cảnh"}])
        self.install(llm)
        jid = self.run_job()
        self.assertEqual(self.orc_.store.get_job(jid)["state"], "FAILED")
        plan = self.svc().remix_plan(jid)
        self.assertEqual(plan["stop"]["code"], "ORIGINALITY_REVIEW_REQUIRED")
        self.assertTrue(plan["stop"]["hint"])
        self.assertEqual(plan["chapters_done"], 0)
        out = self.svc().update_story_mode(jid, {"story": {"review_accepted": True}})
        self.assertTrue(out["story_mode"]["story"]["review_accepted"])
        self.orc_.run()
        self.assertEqual(self.orc_.store.get_job(jid)["state"], "STORY_READY")
        self.assertEqual(llm.counts["source_dna"], 1)                                                           # không phân tích lại nguồn
        self.assertEqual(llm.counts["story_bible"], 1)
        self.assertIsNone(self.svc().remix_plan(jid)["stop"])
        self.assertTrue(self.svc().remix_plan(jid)["final_qa"]["accepted"])

    def test_budget_raise_resumes_from_where_it_stopped(self):
        llm = FakeRemixLLM(cost=0.7)
        self.install(llm)
        jid = self.run_job(story_mode={**REMIX, "story": {"budget_usd": 3}})
        self.assertEqual(self.svc().remix_plan(jid)["stop"]["code"], "BUDGET_EXCEEDED")
        before = dict(llm.counts)
        self.svc().update_story_mode(jid, {"story": {"budget_usd": 100}})
        self.orc_.run()
        self.assertEqual(self.orc_.store.get_job(jid)["state"], "STORY_READY")
        self.assertEqual(llm.counts["source_dna"], before["source_dna"])
        cost = self.svc().remix_plan(jid)["cost"]
        self.assertGreater(cost["known_cost_usd"], 3)

    def test_only_safe_fields_are_editable_and_only_for_remix_jobs(self):
        self.install(FakeRemixLLM())
        jid = self.run_job()
        for bad in ({"story": {"tone": "đổi"}}, {"story": {"budget_usd": 0}}, {"story": "x"}):
            with self.assertRaises(StageError):
                self.svc().update_story_mode(jid, bad)
        plain = self.orc_.submit(params(), mode="STORY_ONLY")
        with self.assertRaises(StageError):
            self.svc().update_story_mode(plain, {"story": {"budget_usd": 5}})


if __name__ == "__main__":
    unittest.main()
