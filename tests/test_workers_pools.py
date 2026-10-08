"""W1.5–W1.7: model config, worker pool, work routing + router pick()."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from contentfactory.workers import policy as policy_mod
from contentfactory.workers.drivers.fake import FakeDriver
from contentfactory.workers.errors import PoolInUse
from contentfactory.workers.models import Attempt, AttemptState, PoolStrategy, WorkerStatus
from contentfactory.workers.registry import WorkerRegistry
from contentfactory.workers.store import WorkerStore

_REAL_IDS = ("claude_cli", "codex_cli", "gemini_cli", "opencode_cli")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WorkerStore(Path(self.tmp.name) / "workers.db")
        self.reg = WorkerRegistry(
            self.store,
            drivers={"fake": FakeDriver({}), **{rid: FakeDriver({"detected": []}) for rid in _REAL_IDS}},
        )

    def worker(self, name: str) -> str:
        return self.reg.add(name, "fake", f"cf-{name}").id


class ModelConfigTests(Base):
    def test_default_and_disable(self):
        wid = self.worker("w1")
        w = self.reg.set_models(wid, [{"id": "a", "default": True}, {"id": "b"},
                                       {"id": "c", "enabled": False}])
        self.assertEqual(w.default_model, "a")
        self.assertFalse([m for m in w.models if m.id == "c"][0].enabled)
        self.assertEqual(w.model_for("high"), "a")           # profile chưa set -> default
        self.assertEqual([m.id for m in w.models if m.default], ["a"])

    def test_profile_requires_known_model(self):
        wid = self.worker("w1")
        self.reg.set_models(wid, ["a", "b"])
        with self.assertRaises(ValueError):
            self.reg.set_profiles(wid, {"fast": "khong-ton-tai"})
        with self.assertRaises(ValueError):
            self.reg.set_profiles(wid, {"turbo": "a"})
        w = self.reg.set_profiles(wid, {"fast": "a", "high": "b"})
        self.assertEqual(w.model_for("fast"), "a")
        self.assertEqual(w.model_for("high"), "b")
        self.assertEqual(w.model_for("balanced"), "a")       # chưa set -> default

    def test_dropping_model_clears_profile_and_validates(self):
        wid = self.worker("w1")
        self.reg.set_models(wid, ["a", "b"])
        self.reg.set_profiles(wid, {"fast": "b"})
        w = self.reg.set_models(wid, ["a"])
        self.assertEqual(w.profiles, {})                    # profile trỏ model đã xoá -> bỏ hẳn khoá
        self.assertEqual([m.id for m in w.models], ["a"])

    def test_duplicate_and_empty_model_rejected(self):
        wid = self.worker("w1")
        with self.assertRaises(ValueError):
            self.reg.set_models(wid, ["a", "a"])
        with self.assertRaises(ValueError):
            self.reg.set_models(wid, [{"id": "  "}])


class PoolTests(Base):
    def test_crud_rename_duplicate(self):
        a, b = self.worker("a"), self.worker("b")
        p = self.reg.create_pool("story", members=[a])
        self.assertEqual(p.members, [a])
        with self.assertRaises(ValueError):
            self.reg.create_pool("story")
        p = self.reg.update_pool("story", new_name="story2", display_name="Truyện", members=[a, b])
        self.assertEqual(p.name, "story2")
        self.assertIsNone(self.store.pool("story"))
        self.assertEqual(self.store.pool("story2").members, [a, b])
        p2 = self.reg.update_pool("story2", strategy="least_busy", enabled=False)
        self.assertEqual(p2.strategy, PoolStrategy.LEAST_BUSY)
        self.assertFalse(p2.enabled)

    def test_member_validation(self):
        with self.assertRaises(ValueError):
            self.reg.create_pool("p", members=["wkhongton-tai"])
        wid = self.worker("a")
        with self.assertRaises(ValueError):
            self.reg.create_pool("p2", members=[wid, wid])
        self.reg.create_pool("p", members=[wid])
        with self.assertRaises(ValueError):
            self.reg.update_pool("p", members=["x"])

    def test_reorder_persists(self):
        a, b, c = self.worker("a"), self.worker("b"), self.worker("c")
        self.reg.create_pool("p", members=[a, b, c])
        self.reg.update_pool("p", members=[c, a, b])
        self.assertEqual(self.store.pool("p").members, [c, a, b])

    def test_delete_pool_blocked_by_routing(self):
        wid = self.worker("a")
        self.reg.create_pool("p", members=[wid])
        self.reg.set_routing("story.write", pool="p")
        with self.assertRaises(PoolInUse) as ctx:
            self.reg.delete_pool("p")
        self.assertEqual(ctx.exception.work_types, ["story.write"])
        used = self.reg.delete_pool("p", force=True)
        self.assertEqual(used, ["story.write"])
        self.assertIsNone(self.store.pool("p"))
        self.assertEqual(self.reg.routing(), {})


class RoutingTests(Base):
    def test_set_routing_validates(self):
        with self.assertRaises(ValueError):
            self.reg.set_routing("story.write", pool="khong-co")
        wid = self.worker("a")
        self.reg.create_pool("p", members=[wid])
        with self.assertRaises(ValueError):
            self.reg.set_routing("story.write", pool="p", model_profile="lau")
        with self.assertRaises(ValueError):
            self.reg.set_routing("", pool="p")
        with self.assertRaises(ValueError):
            self.reg.set_routing("story.write", pool="p", policy={"max_tries": 3})

    def test_change_pool_and_profile_without_code(self):
        a, b = self.worker("a"), self.worker("b")
        self.reg.create_pool("p1", members=[a])
        self.reg.create_pool("p2", members=[b])
        self.reg.set_models(a, ["a-fast", "a-high"])
        self.reg.set_profiles(a, {"high": "a-high"})
        self.reg.set_routing("story.write", pool="p1", model_profile="high")
        target, _ = self.reg.pick("story.write")
        self.assertEqual(target.worker.id, a)
        self.assertEqual(target.model, "a-high")
        self.assertEqual(target.pool, "p1")
        self.reg.set_routing("story.write", pool="p2")
        target, _ = self.reg.pick("story.write")
        self.assertEqual(target.worker.id, b)
        self.assertEqual(target.pool, "p2")

    def test_policy_defaults_and_merge(self):
        pol = policy_mod.validate({"max_total_attempts": 8})
        self.assertEqual(pol["max_total_attempts"], 8)
        self.assertEqual(pol["max_distinct_workers"], 3)          # mặc định còn giữ
        self.assertEqual(policy_mod.retries_for(pol, "TEMPORARY"), 2)
        self.assertEqual(policy_mod.retries_for(pol, "QUOTA"), 0)
        pol = policy_mod.validate({"retry_on": {"TEMPORARY": 4}})
        self.assertEqual(policy_mod.retries_for(pol, "TEMPORARY"), 4)
        self.assertEqual(policy_mod.retries_for(pol, "UNKNOWN"), 1)
        with self.assertRaises(ValueError):
            policy_mod.validate({"retry_on": {"LALA": 1}})
        with self.assertRaises(ValueError):
            policy_mod.validate({"max_total_attempts": 0})


class PickTests(Base):
    def test_no_routing_reasons(self):
        target, why = self.reg.pick("story.write")
        self.assertIsNone(target)
        self.assertIn("chưa cấu hình", why)
        wid = self.worker("a")
        self.reg.create_pool("empty")
        self.reg.set_routing("story.write", pool="empty")
        target, why = self.reg.pick("story.write")
        self.assertIsNone(target)
        self.assertIn("rỗng", why)
        self.reg.create_pool("p", members=[wid])
        self.reg.set_routing("story.write", pool="p")
        self.store.save_pool(self.store.pool("p").with_updates(members=["ghost"]))   # ghi đè thẳng: member ma
        target, why = self.reg.pick("story.write")
        self.assertIsNone(target)
        self.assertIn("dangling", why)

    def test_priority_skips_unroutable_and_excluded(self):
        a, b = self.worker("a"), self.worker("b")
        self.reg.create_pool("p", members=[a, b])
        self.reg.set_routing("story.write", pool="p")
        self.reg.set_enabled(a, False)
        target, why = self.reg.pick("story.write")
        self.assertEqual(target.worker.id, b)
        self.assertIn("b", why)
        self.reg.set_enabled(a, True)
        target, why = self.reg.pick("story.write", exclude=(a,))
        self.assertEqual(target.worker.id, b)
        self.assertIn("đã lỗi", why)

    def test_cooldown_worker_not_picked(self):
        a, b = self.worker("a"), self.worker("b")
        w = self.reg.get(a)
        self.store.save_worker(w.with_updates(cooldown_until=time.time() + 60))
        self.reg.create_pool("p", members=[a, b])
        self.reg.set_routing("story.write", pool="p")
        target, why = self.reg.pick("story.write")
        self.assertEqual(target.worker.id, b)
        self.assertIn("cooldown", why)

    def test_least_busy_picks_fewest_running(self):
        a, b = self.worker("a"), self.worker("b")
        self.reg.create_pool("p", members=[a, b], strategy="least_busy")
        self.reg.set_routing("story.write", pool="p")
        self.store.add_attempt(Attempt(attempt_id="r1", work_type="story.write", worker_id=a,
                                       state=AttemptState.RUNNING, started_at=1.0))
        target, why = self.reg.pick("story.write")
        self.assertEqual(target.worker.id, b)
        self.assertIn("least_busy", why)


if __name__ == "__main__":
    unittest.main()
