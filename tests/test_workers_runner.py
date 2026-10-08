"""Cầu nối AgentRunner <-> WorkerManager (C6): story.write đi qua Worker Runtime."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from contentfactory.contracts import CancelToken, ErrorClass, StageError
from contentfactory.workers.drivers.fake import FakeDriver
from contentfactory.workers.manager import WorkerManager
from contentfactory.workers.models import AttemptState
from contentfactory.workers.registry import WorkerRegistry
from contentfactory.workers.runner import DriverRunner, WorkerRunner
from contentfactory.workers.store import WorkerStore
from tests.fakes import make_ctx


class CancelledDriver(FakeDriver):
    def execute(self, req):
        raise StageError(ErrorClass.CANCELLED, "CANCELLED", "huỷ giữa lượt")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.reg = WorkerRegistry(WorkerStore(root / "workers.db"))
        self.mgr = WorkerManager(self.reg, root / "workspace")
        self.runner = WorkerRunner(self.mgr)

    def worker(self, name: str, driver: FakeDriver) -> str:
        self.reg.register_driver(f"drv_{name}", driver)
        return self.reg.add(name, f"drv_{name}", f"cf-{name}").id

    def pool(self, members: list[str], policy: dict | None = None) -> None:
        self.reg.create_pool("p", members=members)
        self.reg.set_routing("story.write", pool="p", policy=policy or {})

    def turn(self, session: str | None = "SID0", ctx=None):
        ctx = ctx or make_ctx(Path(self.tmp.name))
        return self.runner.run("viết chương 1", Path(self.tmp.name) / "oh-story", session, ctx)


class WorkerRunnerTests(Base):
    def test_returns_agent_turn_and_passes_session_to_driver(self):
        drv = FakeDriver({"script": ["ok"]})
        self.pool([self.worker("a", drv)])
        t = self.turn()
        self.assertIn("chương thử", t["text"])
        self.assertEqual((t["session_id"], t["is_error"]), (None, False))
        self.assertEqual(drv.calls[0]["session"], "SID0")             # resume session đi xuyên qua manager
        self.assertEqual(self.reg.store.attempts(work_type="story.write")[0].state, AttemptState.SUCCESS)

    def test_raises_stage_error_when_every_worker_fails(self):
        self.pool([self.worker("a", FakeDriver({"script": ["auth"]})),
                   self.worker("b", FakeDriver({"script": ["auth"]}))])
        with self.assertRaises(StageError) as cm:
            self.turn()
        self.assertEqual(cm.exception.error_class, ErrorClass.AUTH)
        self.assertEqual(len(self.reg.store.attempts(work_type="story.write")), 2)

    def test_text_without_output_file_is_invalid(self):
        self.pool([self.worker("a", FakeDriver({"script": ["no_output"]}))],
                  policy={"retry_on": {"INVALID_OUTPUT": 0}})
        with self.assertRaises(StageError) as cm:
            self.turn()
        self.assertEqual((cm.exception.error_class, cm.exception.code), (ErrorClass.POLICY, "OUTPUT_INVALID"))
        self.assertEqual(self.reg.store.attempts(work_type="story.write")[0].state, AttemptState.INVALID)

    def test_cancel_propagates_and_is_not_a_worker_failure(self):
        self.pool([self.worker("a", CancelledDriver({}))])
        with self.assertRaises(StageError) as cm:
            self.turn()                                    # driver huỷ giữa lượt (token set trong driver)
        self.assertEqual(cm.exception.error_class, ErrorClass.CANCELLED)
        attempts = self.reg.store.attempts(work_type="story.write")
        self.assertEqual(len(attempts), 1)                            # không retry sau huỷ
        self.assertEqual(attempts[0].error_code, "CANCELLED")
        self.assertEqual(self.reg.get(attempts[0].worker_id).failure_streak, 0)

    def test_cancel_before_run_reports_cancelled_not_worker_error(self):
        self.pool([self.worker("a", FakeDriver({"script": ["ok"]}))])
        token = CancelToken()
        token.set()
        with self.assertRaises(StageError) as cm:
            self.turn(ctx=make_ctx(Path(self.tmp.name), cancel=token))
        self.assertEqual(cm.exception.error_class, ErrorClass.CANCELLED)
        self.assertEqual(self.reg.store.attempts(), [])


class DriverRunnerTests(Base):
    def test_runs_one_driver_without_routing(self):
        drv = FakeDriver({"script": ["ok"]})
        r = DriverRunner(drv)
        t = r.run("viết", Path(self.tmp.name), None, make_ctx(Path(self.tmp.name)))
        self.assertIn("chương thử", t["text"])
        self.assertEqual(len(drv.calls), 1)                           # không retry/fallback
        self.assertEqual(self.reg.store.attempts(), [])               # không ghi attempt

    def test_failed_run_raises_stage_error(self):
        r = DriverRunner(FakeDriver({"script": ["quota"]}))
        with self.assertRaises(StageError) as cm:
            r.run("viết", Path(self.tmp.name), None, make_ctx(Path(self.tmp.name)))
        self.assertEqual(cm.exception.error_class, ErrorClass.RESOURCE)


if __name__ == "__main__":
    unittest.main()
