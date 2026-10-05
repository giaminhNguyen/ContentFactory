import unittest

from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator.config import DEFAULTS
from contentfactory.orchestrator.handlers import HANDLERS

REQUIRED_STATES = """NEW SOURCE_PROCESSING SOURCE_READY STORY_RUNNING STORY_READY TTS_RUNNING AUDIO_READY
YOUTUBE_RENDER_READY YOUTUBE_RENDERING TIKTOK_RENDER_READY TIKTOK_RENDERING OUTPUT_READY UPLOAD_READY
PUBLISHED FAILED""".split()


class StateMachineTest(unittest.TestCase):
    def test_contains_required_states_once(self):
        self.assertTrue(set(REQUIRED_STATES) <= set(P.ALL_STATES))
        self.assertEqual(len(P.ALL_STATES), len(set(P.ALL_STATES)))

    def test_required_states_keep_their_order(self):
        order = [s for s in P.ALL_STATES if s in REQUIRED_STATES and s != "FAILED"]
        self.assertEqual(order, [s for s in REQUIRED_STATES if s != "FAILED"])

    def test_transition_rules(self):
        self.assertTrue(P.allowed("NEW", "SOURCE_PROCESSING"))
        self.assertFalse(P.allowed("NEW", "STORY_RUNNING"))            # không nhảy cóc stage
        self.assertFalse(P.allowed("NEW", "SOURCE_READY"))             # phải qua running
        self.assertTrue(P.allowed("STORY_RUNNING", "FAILED"))
        self.assertTrue(P.allowed("STORY_RUNNING", "SOURCE_READY"))    # retry/ngắt: về hàng đợi của chính stage
        self.assertFalse(P.allowed("STORY_RUNNING", "AUDIO_READY"))
        self.assertTrue(P.allowed("FAILED", "SOURCE_READY"))           # retry thủ công
        self.assertFalse(P.allowed("PUBLISHED", "NEW"))
        self.assertFalse(P.allowed("PUBLISHED", "FAILED"))

    def test_artifact_dataflow_is_closed(self):
        """Mỗi stage chỉ đòi artifact đã được stage đứng trước sản sinh (module nói chuyện bằng artifact)."""
        made: set[str] = set()
        for s in P.STAGES:
            self.assertTrue(set(s.requires) <= made, f"{s.name} đòi {set(s.requires) - made}")
            made |= set(s.produces)

    def test_every_stage_is_wired(self):
        for s in P.STAGES:
            self.assertIn(s.name, HANDLERS)
            for a in s.adapters:
                self.assertIn(a, DEFAULTS["adapters"])


if __name__ == "__main__":
    unittest.main()
