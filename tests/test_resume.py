"""Kill tiến trình thật giữa chừng rồi chạy lại: job phải resume đúng stage, không làm lại stage đã xong."""
import threading
import time
import unittest

from contentfactory.jobs import pipeline as P
from tests.support import RootCase, params, start_runner, wait_until


class ResumeTest(RootCase):
    def kill_when(self, jid: str, cond, what: str) -> None:
        proc = start_runner(self.root)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        store = self.orc().store
        wait_until(lambda: cond(store, jid), what=what)
        proc.kill()                       # TerminateProcess: không có cơ hội dọn dẹp
        proc.wait()

    def test_kill_mid_stage_then_resume_at_same_stage(self):
        jid = self.orc().submit(params(fake={"story": {"sleep_s": 60, "attempt": 1}}))
        self.kill_when(jid, lambda s, j: s.get_job(j)["state"] == P.STORY_RUNNING, "STORY_RUNNING")

        orc = self.orc()
        j = orc.store.get_job(jid)
        self.assertEqual(j["state"], P.STORY_RUNNING)                  # DB còn nguyên trạng thái lúc chết
        self.assertIsNotNone(j["lease_owner"])
        self.assertEqual({a["kind"] for a in orc.store.artifacts(jid)},
                         {"subtitle_raw", "transcript_structured", "transcript", "metadata"})   # stage trước còn
        t0 = time.time()
        orc.run()                                                       # lease hết hạn (~1s) => nhận lại
        self.assertLess(time.time() - t0, 15)

        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        runs = self.runs(orc, jid)
        self.assertEqual(runs["story"], ["interrupted", "succeeded"])
        self.assertEqual(runs["source"], ["succeeded"])                 # không làm lại source
        self.assertTrue(all(v == ["succeeded"] for k, v in runs.items() if k not in ("story",)))
        self.assertIn("lease_expired", [t["note"] for t in orc.store.transitions(jid)])

    def test_kill_mid_tts_reuses_finished_chunks(self):
        jid = self.orc().submit(params(fake={"tts_chunk_3": {"sleep_s": 60, "attempt": 1}}))
        chunks = self.job_dir(jid) / "tts" / "chunks"
        self.kill_when(jid, lambda s, j: s.get_job(j)["state"] == P.TTS_RUNNING and
                       (chunks / "000002.json").exists(), "chunk 2 done")   # sidecar ghi SAU file wav: mới là dấu hiệu chunk hoàn tất

        orc = self.orc()
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        calls = (self.job_dir(jid) / "tts" / "calls.log").read_text().splitlines()
        self.assertEqual(calls[:2], ["tts_chunk_1 attempt=1", "tts_chunk_2 attempt=1"])
        self.assertEqual(sorted(c.split()[0] for c in calls), [f"tts_chunk_{i}" for i in range(1, 7)])   # mỗi chunk đúng 1 lần
        m = orc.store.stage_runs(jid)
        tts = [r for r in m if r["stage"] == "tts"]
        self.assertEqual([r["status"] for r in tts], ["interrupted", "succeeded"])
        self.assertIn('"reused_chunks": 2', tts[-1]["data"])

    def test_graceful_stop_releases_job_without_waiting_for_lease(self):
        orc = self.orc()
        jid = orc.submit(params(fake={"story": {"sleep_s": 60, "attempt": 1}}))
        stop = threading.Event()
        t = threading.Thread(target=orc.run, kwargs={"until_idle": False, "stop": stop})
        t.start()
        wait_until(lambda: orc.store.get_job(jid)["state"] == P.STORY_RUNNING, what="STORY_RUNNING")
        stop.set()
        t.join(10)
        self.assertFalse(t.is_alive())

        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["lease_owner"], j["retry_used"]), (P.SOURCE_READY, None, 0))
        self.assertEqual(self.runs(orc, jid)["story"], ["cancelled"])
        t0 = time.time()
        orc2 = self.orc()
        orc2.run()                                                      # tiếp tục ngay, không chờ lease
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(orc2.store.get_job(jid)["state"], P.PUBLISHED)
        self.assertEqual(self.runs(orc2, jid)["story"], ["cancelled", "succeeded"])

    def test_two_orchestrators_do_not_double_run_a_job(self):
        a, b = self.orc(), self.orc()
        jid = a.submit(params())
        ta = threading.Thread(target=a.run)
        tb = threading.Thread(target=b.run)
        ta.start(); tb.start(); ta.join(120); tb.join(120)            # lease 1 s: dưới tải nặng job có thể bị nhận lại vài lần trước khi xong
        self.assertEqual(a.store.get_job(jid)["state"], P.PUBLISHED)
        self.assertEqual(self.runs(a, jid), {s.name: ["succeeded"] for s in P.STAGES})


if __name__ == "__main__":
    unittest.main()
