import json
import unittest

from contentfactory.contracts import ArtifactDraft, StageError, StageResult
from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator.runner import Orchestrator
from tests.support import RootCase, params


def fingerprint(job_dir, store, job_id):
    """(path, sha256, mtime_ns) của mọi artifact đã checkpoint: để chứng minh stage trước không bị làm lại."""
    return {a["path"]: (a["sha256"], (job_dir / a["path"]).stat().st_mtime_ns) for a in store.artifacts(job_id)}


class FailureRetryTest(RootCase):
    def test_stage_failure_keeps_previous_artifacts_and_retry_reruns_only_that_stage(self):
        orc = self.orc()
        jid = orc.submit(params(fake={"story": {"fail_until_attempt": 1, "error_class": "POLICY", "code": "BAD_SOURCE"}}))
        orc.run()

        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["failed_stage"], j["last_error"]["code"]), (P.FAILED, "story", "BAD_SOURCE"))
        self.assertEqual(self.runs(orc, jid), {"source": ["succeeded"], "story": ["failed"]})   # POLICY: không auto-retry
        before = fingerprint(self.job_dir(jid), orc.store, jid)
        self.assertEqual({a["kind"] for a in orc.store.artifacts(jid)}, {"subtitle_raw", "transcript_structured", "transcript", "metadata"})
        m = json.loads((self.job_dir(jid) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((m["state"], m["failed_stage"]), (P.FAILED, "story"))

        orc2 = self.orc()
        self.assertEqual(orc2.retry(jid), "story")
        self.assertEqual(orc2.store.get_job(jid)["state"], P.SOURCE_READY)       # về hàng đợi đúng stage lỗi
        orc2.run()

        self.assertEqual(orc2.store.get_job(jid)["state"], P.PUBLISHED)
        runs = self.runs(orc2, jid)
        self.assertEqual(runs["source"], ["succeeded"])                          # KHÔNG chạy lại
        self.assertEqual(runs["story"], ["failed", "succeeded"])
        self.assertTrue(all(v == ["succeeded"] for k, v in runs.items() if k not in ("source", "story")))
        after = fingerprint(self.job_dir(jid), orc2.store, jid)
        for path, fp in before.items():
            self.assertEqual(after[path], fp, f"{path} bị ghi lại")
        calls = (self.job_dir(jid) / "source" / "calls.log").read_text().splitlines()
        self.assertEqual(calls, ["source attempt=1"])

    def test_transient_error_auto_retries_with_backoff(self):
        orc = self.orc()
        jid = orc.submit(params(fake={"render_youtube": {"fail_until_attempt": 2, "error_class": "TRANSIENT"}}))
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        runs = self.runs(orc, jid)
        self.assertEqual(runs["render_youtube"], ["failed", "failed", "succeeded"])
        self.assertTrue(all(v == ["succeeded"] for k, v in runs.items() if k != "render_youtube"))
        notes = [t["note"] for t in orc.store.transitions(jid) if t["note"] and t["note"].startswith("retry in")]
        self.assertEqual(len(notes), 2)

    def test_transient_error_gives_up_after_max_attempts(self):
        orc = self.orc()
        jid = orc.submit(params(fake={"render_youtube": {"fail_until_attempt": 99, "error_class": "TRANSIENT"}}))
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["failed_stage"]), (P.FAILED, "render_youtube"))
        self.assertEqual(self.runs(orc, jid)["render_youtube"], ["failed"] * 3)
        kinds = {a["kind"] for a in orc.store.artifacts(jid)}
        self.assertTrue({"transcript", "story_text", "audio_master", "audio_youtube", "audio_tiktok"} <= kinds)
        self.assertFalse({"video_youtube", "output_package"} & kinds)

    def test_unexpected_exception_is_not_retried_blindly(self):
        orc = self.orc()
        orc.adapters["story"].generate = lambda *a, **k: 1 / 0
        jid = orc.submit(params())
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["last_error"]["code"]), (P.FAILED, "UNEXPECTED"))
        self.assertEqual(self.runs(orc, jid)["story"], ["failed"])

    def test_validator_is_the_last_line_of_defence_against_chapter_headers(self):
        """Assembler chỉ gỡ dòng heading thật; câu văn mở đầu bằng "Chương 2 ..." thì validator phải chặn."""
        orc = self.orc()
        real = orc.adapters["story"].generate

        def bad(bundle, profile, out_dir, ctx):
            r = real(bundle, profile, out_dir, ctx)
            last = r["sections"][-1]
            last.write_text(last.read_text(encoding="utf-8") + "\nChương 2 đã kết thúc trong im lặng.\n", encoding="utf-8")
            return r

        orc.adapters["story"].generate = bad
        jid = orc.submit(params())
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["last_error"]["code"]), (P.FAILED, "STORY_INVALID"))
        self.assertNotIn("story_text", {a["kind"] for a in orc.store.artifacts(jid)})
        self.assertFalse((self.job_dir(jid) / "story" / "story.txt").exists())

    def test_publish_requires_explicit_made_for_kids_rejected_at_job_creation(self):
        orc = self.orc()
        p = params()
        del p["made_for_kids"]
        with self.assertRaises(StageError) as cm:                                # chặn lúc TẠO job: không tốn Story/TTS/render rồi mới biết thiếu khai báo COPPA
            orc.submit(p)
        self.assertEqual(cm.exception.code, "MISSING_MADE_FOR_KIDS")
        self.assertEqual(orc.store.list_jobs(), [])

    def test_handler_cannot_emit_undeclared_artifact(self):
        res = StageResult([ArtifactDraft("story/story.txt", "video_youtube")])
        with self.assertRaises(StageError) as cm:
            Orchestrator._seal(P.BY_NAME["story"], res, self.root)
        self.assertEqual(cm.exception.code, "UNDECLARED_KIND")


if __name__ == "__main__":
    unittest.main()
