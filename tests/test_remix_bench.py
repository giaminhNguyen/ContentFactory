"""Benchmark Story Remix đa thể loại (Phase 7): cơ chế thưởng theo từng thể loại được giữ, không lẫn thể loại, báo cáo trung thực (unknown khi thiếu dữ liệu)."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from contentfactory.adapters.fake_remix import FakeRemixLLM

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("remix_bench", REPO / "scripts" / "remix_bench.py")
rb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rb)


class BenchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.rep = rb.run(FakeRemixLLM(cost=0.0, report_cost=False), Path(cls.tmp.name) / "run", llm_name="fake")
        cls.rows = {r["id"]: r for r in cls.rep["results"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_five_heterogeneous_genres_all_pass_gates(self):
        s = self.rep["summary"]
        self.assertEqual((s["benchmarks"], s["distinct_genres"], s["all_gates_pass"]), (5, 5, True))                # RM-005: ≥ 3 thể loại, ở đây 5
        self.assertIn("ĐẠT", s["heterogeneous_requirement"])
        self.assertEqual({r["dna"]["genre"] for r in self.rows.values()}, {"kinh dị", "trinh thám", "tình cảm", "báo thù, phản đòn", "hài hước"})

    def test_each_genre_keeps_its_own_reward_mechanics_without_leaking_others(self):
        keep = {"horror": "sợ hãi", "mystery": "manh mối", "romance": "tỏ tình", "comedy": "nhầm lẫn", "revenge": "phản đòn"}
        for bid, kw in keep.items():
            types = " ".join(self.rows[bid]["premise_payoff_types"])
            self.assertIn(kw, types, bid)                                                                           # cơ chế thưởng của thể loại đó
            for other, okw in keep.items():
                if other != bid:
                    self.assertNotIn(okw, types, f"{bid} lẫn cơ chế của {other}")
            self.assertEqual(self.rows[bid]["reward_types_covered_in_outline"].split("/")[0], "3", bid)             # đại cương phủ đủ 3/3 loại thưởng của DNA
            self.assertEqual(self.rows[bid]["dopamine"], "pass")

    def test_originality_and_cast_are_per_benchmark(self):
        for r in self.rows.values():
            self.assertEqual(r["originality"]["decision"], "pass")
            self.assertEqual(r["originality"]["source_names_reused"], [])
            self.assertEqual(r["cast"]["reused"], 0)                                                                # kho riêng từng benchmark: không lẫn nhân vật giữa thể loại
            self.assertEqual(r["cast"]["new"], 3)

    def test_report_is_honest_about_missing_data(self):
        for r in self.rows.values():
            self.assertEqual(r["cost"]["known_usd"], "unknown")                                                     # LLM giả không báo chi phí ⇒ unknown, không bịa
            self.assertTrue(r["baseline"].startswith("unknown"))
        self.assertIn("LLM GIẢ", self.rep["summary"]["note"])
        out = Path(self.tmp.name) / "run"
        self.assertTrue((out / "report.md").is_file())
        md = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("không phải chất lượng văn bản", md)
        self.assertEqual(json.loads((out / "report.json").read_text(encoding="utf-8"))["summary"]["benchmarks"], 5)

    def test_baseline_is_reported_only_when_provided_and_real_run_needs_confirmation(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t) / "base"
            base.mkdir()
            (base / "horror.json").write_text(json.dumps({"cost_usd": 47.56, "seconds": 7373}), encoding="utf-8")
            rep = rb.run(FakeRemixLLM(cost=0.0, report_cost=False), Path(t) / "r", only=["horror", "comedy"], baseline_dir=base)
        rows = {r["id"]: r for r in rep["results"]}
        self.assertEqual(rows["horror"]["baseline"], {"cost_usd": 47.56, "seconds": 7373})
        self.assertTrue(rows["comedy"]["baseline"].startswith("unknown"))
        self.assertEqual(rb.main(["run", "--llm", "claude"]), 2)                                                    # không tự tốn tiền

    def test_writing_mode_runs_through_chapters(self):
        with tempfile.TemporaryDirectory() as t:
            rep = rb.run(FakeRemixLLM(cost=0.0, report_cost=False), Path(t) / "r", only=["mystery"], write=True)
        ch = rep["results"][0]["chapters"]
        self.assertGreater(ch["written"], 3)


if __name__ == "__main__":
    unittest.main()
