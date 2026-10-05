import threading
import time
import unittest
from pathlib import Path
import shutil
import tempfile

from contentfactory.contracts import ErrorClass, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.jobs.db import JobStore

SRC = P.BY_NAME["source"]


class StoreTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="cf-store-")
        self.addCleanup(shutil.rmtree, d, True)
        self.s = JobStore(Path(d) / "db.sqlite")

    def test_concurrent_claims_never_duplicate(self):
        ids = {self.s.create_job({}) for _ in range(8)}
        got, lock = [], threading.Lock()

        def worker(n):
            for c in self.s.claim(SRC, 8, 99, f"owner{n}", 30):
                with lock:
                    got.append(c.job_id)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(got), sorted(ids))     # mỗi job đúng một lần

    def test_resource_limit(self):
        for _ in range(5):
            self.s.create_job({})
        self.assertEqual(len(self.s.claim(SRC, 5, 2, "o", 30)), 2)
        self.assertEqual(self.s.claim(SRC, 5, 2, "o", 30), [])      # đã đủ 2 job đang chạy

    def test_only_lease_owner_can_commit(self):
        self.s.create_job({})
        [c] = self.s.claim(SRC, 1, 9, "A", 30)
        art = [{"path": "a", "kind": "transcript", "sha256": "x", "bytes": 1, "meta": {}}]
        self.assertFalse(self.s.succeed(c, "B", art, {}))
        self.assertEqual(self.s.get_job(c.job_id)["state"], P.SOURCE_PROCESSING)
        self.assertEqual(self.s.artifacts(c.job_id), [])
        self.assertTrue(self.s.succeed(c, "A", art, {}))
        self.assertEqual(self.s.get_job(c.job_id)["state"], P.SOURCE_READY)

    def test_illegal_transition_rejected(self):
        jid = self.s.create_job({})
        with self.s._tx() as c:
            with self.assertRaises(ValueError):
                self.s._move(c, jid, P.NEW, P.STORY_RUNNING, time.time())

    def test_backoff_respected(self):
        self.s.create_job({})
        [c] = self.s.claim(SRC, 1, 9, "A", 30, now=1000)
        err = StageError(ErrorClass.TRANSIENT, "X")
        self.assertEqual(self.s.fail(c, "A", err, 3, [100], now=1000), "retry")
        self.assertEqual(self.s.claim(SRC, 1, 9, "A", 30, now=1050), [])     # chưa tới not_before
        self.assertEqual(len(self.s.claim(SRC, 1, 9, "A", 30, now=1101)), 1)

    def test_non_transient_goes_failed_with_stage(self):
        self.s.create_job({})
        [c] = self.s.claim(SRC, 1, 9, "A", 30)
        self.assertEqual(self.s.fail(c, "A", StageError(ErrorClass.POLICY, "BAD"), 3, [1]), "failed")
        j = self.s.get_job(c.job_id)
        self.assertEqual((j["state"], j["failed_stage"], j["last_error"]["code"]), (P.FAILED, "source", "BAD"))
        self.assertEqual(self.s.retry_failed(c.job_id), "source")
        self.assertEqual(self.s.get_job(c.job_id)["state"], P.NEW)

    def test_repeated_interruption_eventually_fails(self):
        self.s.create_job({})
        for i in range(2):
            self.s.claim(SRC, 1, 9, "A", 1, now=100)
            acted = self.s.recover_expired(2, now=200)
        self.assertEqual(acted[0][1], "failed")
        self.assertEqual(self.s.get_job("000001")["last_error"]["code"], "INTERRUPTED_REPEATEDLY")


if __name__ == "__main__":
    unittest.main()
