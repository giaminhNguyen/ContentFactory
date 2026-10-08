"""Harness benchmark Story (Dopamine rollout Phase 0): số liệu tất định + fixture BENCHMARK-001 nguyên vẹn."""
import hashlib
import importlib.util
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("story_bench", REPO / "scripts" / "story_bench.py")
sb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sb)


class StoryBenchTest(unittest.TestCase):
    def test_metrics_hook_and_quiet_span(self):
        m = sb.metrics("Một hai ba. Bốn năm sáu bảy. Ai đó? Tám chín. Mười một hai ba bốn.")
        self.assertEqual(m["hook_latency_words(first ?/!/quote)"], 7)
        self.assertEqual(m["longest_quiet_span_words"], 7)

    def test_benchmark_fixture_matches_recorded_hashes(self):
        d = REPO / "benchmarks" / "BENCHMARK-001"
        meta = json.loads((d / "benchmark.json").read_text(encoding="utf-8"))
        sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        self.assertEqual(sha(d / "source.txt"), meta["source_sha256"])
        self.assertEqual(sha(d / "approved" / "story.txt"), meta["approved_story_sha256"])


if __name__ == "__main__":
    unittest.main()
