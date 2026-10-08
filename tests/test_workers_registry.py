"""W1.3: store sqlite riêng + discovery/registry + vòng đời worker."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from contentfactory.workers.drivers.fake import FakeDriver
from contentfactory.workers.errors import WorkerInUse
from contentfactory.workers.models import (
    Attempt,
    AttemptState,
    PoolStrategy,
    WorkerPool,
    WorkerStatus,
)
from contentfactory.workers.registry import WorkerRegistry
from contentfactory.workers.store import WorkerStore

# id của các driver thật: test tiêm driver giả để KHÔNG quét PATH thật của máy.
_REAL_IDS = ("claude_cli", "codex_cli", "gemini_cli", "opencode_cli")


def _drivers(fake_cfg: dict | None = None, real_cfg: dict | None = None) -> dict:
    d = {"fake": FakeDriver(fake_cfg or {})}
    for rid in _REAL_IDS:
        d[rid] = FakeDriver(real_cfg or {"detected": []})
    return d


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "workers.db"
        self.store = WorkerStore(self.db)

    def registry(self, fake_cfg: dict | None = None) -> WorkerRegistry:
        return WorkerRegistry(self.store, drivers=_drivers(fake_cfg))


class StoreTests(Base):
    def test_worker_roundtrip(self):
        reg = self.registry()
        w = reg.add("fake1", "fake", "cf-fake-worker")
        got = self.store.worker(w.id)
        self.assertEqual(got.id, w.id)
        self.assertEqual(got.status, WorkerStatus.READY)
        self.assertEqual([m.id for m in got.models], ["fake-fast", "fake-high"])
        self.assertEqual(got.meta.get("version"), "0.0.0-fake")

    def test_pool_and_routing_roundtrip(self):
        self.store.save_pool(WorkerPool(name="story", display_name="Story", strategy=PoolStrategy.LEAST_BUSY,
                                        members=["w1", "w2"]))
        self.store.save_routing("story.write", {"pool": "story", "model_profile": "fast",
                                                "policy": {"max_retries": 3}})
        pool = self.store.pool("story")
        self.assertEqual(pool.members, ["w1", "w2"])
        self.assertEqual(pool.strategy, PoolStrategy.LEAST_BUSY)
        cfg = self.store.routing()["story.write"]
        self.assertEqual(cfg["pool"], "story")
        self.assertEqual(cfg["policy"]["max_retries"], 3)

    def test_attempt_history_kept_per_job(self):
        a = Attempt(attempt_id="a1", work_type="story.write", worker_id="w1", job_id="j1",
                    state=AttemptState.FAILED, started_at=1.0, ended_at=2.0,
                    error_kind="QUOTA", error_message="hết quota")
        self.store.add_attempt(a)
        rows = self.store.attempts(job_id="j1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].error_kind, "QUOTA")
        self.assertEqual(rows[0].duration_s, 1.0)


class RegistryTests(Base):
    def test_add_and_duplicate_rejected(self):
        reg = self.registry()
        w = reg.add("fake1", "fake", "cf-fake-worker")
        with self.assertRaises(ValueError):
            reg.add("fake2", "fake", "cf-fake-worker")
        with self.assertRaises(ValueError):
            reg.add("x", "nope", "whatever")
        with self.assertRaises(ValueError):
            reg.add("x", "fake", "   ")
        self.assertEqual(len(reg.list()), 1)
        self.assertEqual(w.name, "fake1")

    def test_probe_updates_status_models_and_keeps_disabled(self):
        reg = self.registry()
        w = reg.add("fake1", "fake", "cf-fake-worker")
        self.assertEqual(w.status, WorkerStatus.READY)
        reg.set_enabled(w.id, False)
        w = reg.probe(w.id)
        self.assertEqual(w.status, WorkerStatus.DISABLED)
        reg.set_enabled(w.id, True)
        w = reg.probe(w.id)
        self.assertEqual(w.status, WorkerStatus.READY)
        self.assertFalse(w.in_cooldown())

    def test_scan_finds_new_and_does_not_duplicate(self):
        reg = self.registry({"detected": ["/opt/bin/cf-fake-worker"]})
        first = reg.scan()
        self.assertEqual(len(first["added"]), 1)
        self.assertNotIn("fake", first["missing"])
        self.assertIn("claude_cli", first["missing"])
        second = reg.scan()
        self.assertEqual(second["added"], [])
        self.assertEqual(len(self.store.workers()), 1)
        self.assertEqual(second["existing"].count("fake"), 1)

    def test_removed_cli_goes_not_found_but_history_keeps(self):
        reg = self.registry({"probe_status": "missing"})
        w = reg.add("fake1", "fake", "cf-fake-worker")
        self.assertEqual(w.status, WorkerStatus.NOT_FOUND)
        self.assertEqual(len(reg.list()), 1)             # không bị xoá
        self.store.add_attempt(Attempt(attempt_id="a1", work_type="story.write", worker_id=w.id,
                                       job_id="j1", state=AttemptState.SUCCESS, started_at=1.0,
                                       ended_at=2.0))
        self.assertEqual(self.store.attempts(job_id="j1")[0].worker_id, w.id)

    def test_remove_requires_pool_ref_or_force(self):
        reg = self.registry()
        w = reg.add("fake1", "fake", "cf-fake-worker")
        self.store.save_pool(WorkerPool(name="story", members=[w.id]))
        with self.assertRaises(WorkerInUse) as ctx:
            reg.remove(w.id)
        self.assertEqual(ctx.exception.pools, ["story"])
        pools = reg.remove(w.id, force=True)
        self.assertEqual(pools, ["story"])
        self.assertEqual(self.store.pool("story").members, [])
        self.assertIsNone(self.store.worker(w.id))

    def test_update_rejects_unknown_field(self):
        reg = self.registry()
        w = reg.add("fake1", "fake", "cf-fake-worker")
        with self.assertRaises(ValueError):
            reg.update(w.id, failure_streak=9)
        got = reg.update(w.id, name="renamed", timeout_s=60)
        self.assertEqual(got.name, "renamed")
        self.assertEqual(got.timeout_s, 60)
        self.assertEqual(got.id, w.id)

    def test_driver_ids_are_vendor_agnostic(self):
        reg = self.registry()
        self.assertEqual(reg.driver_ids(), ["claude_cli", "codex_cli", "fake", "gemini_cli", "opencode_cli"])


if __name__ == "__main__":
    unittest.main()
