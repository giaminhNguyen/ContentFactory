"""W2: Production Hardening — circuit breaker, rich health, orphan recovery,
no-progress protection, timeout refinement, metrics (W2.1–W2.7).
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from contentfactory.workers.drivers.base import ExecResult, kill_tree
from contentfactory.workers.drivers.fake import FakeDriver
from contentfactory.workers.manager import WorkerManager
from contentfactory.workers.models import (
    AttemptState,
    CircuitState,
    HealthState,
    WorkerStatus,
)
from contentfactory.workers.registry import WorkerRegistry
from contentfactory.workers.store import WorkerStore

_REAL_IDS = ("claude_cli", "codex_cli", "gemini_cli", "opencode_cli")


class Clock:
    """Đồng hồ giả để chạy hết thời gian cooldown mà không phải sleep thật."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.clock = Clock()
        self.store = WorkerStore(root / "workers.db")
        self.drivers = {rid: FakeDriver({"detected": []}) for rid in _REAL_IDS}
        self.reg = WorkerRegistry(self.store, drivers=self.drivers)
        self.mgr = WorkerManager(self.reg, root / "workspace", now=self.clock.now)

    def worker(self, name: str, script: list[str]) -> str:
        did = f"drv_{name}"
        self.reg.register_driver(did, FakeDriver({"script": script}))
        return self.reg.add(name, did, f"cf-{name}").id

    def pool(self, members: list[str], policy: dict | None = None) -> None:
        self.reg.create_pool("p", members=members)
        self.reg.set_routing("story.write", pool="p", policy=policy or {})

    def go(self, **kw) -> object:
        kw.setdefault("job_id", "j1")
        return self.mgr.run("story.write", prompt="viết", **kw)


class CircuitBreakerTests(Base):
    def test_failure_storm_opens_circuit_and_next_worker_handles(self):
        """OPEN: không gọi provider lỗi nữa — mọi task rơi xuống worker khoẻ."""
        a = self.worker("a", ["temporary", "temporary", "temporary", "temporary"])
        b = self.worker("b", ["ok"])
        self.pool([a, b], policy={"cooldown_after": 2, "cooldown_s": 60})
        r = self.go()
        self.assertTrue(r.ok, r.reason)
        wa = self.reg.get(a)
        self.assertEqual(wa.circuit(self.clock.now()), CircuitState.OPEN)
        self.assertFalse(wa.routable(self.clock.now())[0])
        # task kế cũng không quay lại a (circuit vẫn mở)
        self.reg.set_routing("story.write", pool="p")
        r2 = self.go(job_id="j2")
        self.assertTrue(r2.ok, r2.reason)
        self.assertNotIn(a, [x.worker_id for x in r2.attempts])

    def test_half_open_allows_single_probe_and_success_closes(self):
        a = self.worker("a", ["temporary", "temporary", "ok"])
        b = self.worker("b", ["ok"])
        self.pool([a, b], policy={"cooldown_after": 2, "cooldown_s": 60, "retry_on": {"TEMPORARY": 1}})
        r = self.go()
        self.assertTrue(r.ok, r.reason)
        wa = self.reg.get(a)
        self.assertEqual(wa.circuit(self.clock.now()), CircuitState.OPEN)
        # hết cooldown -> HALF_OPEN, cho đúng 1 probe chạy song song
        self.clock.advance(61)
        self.assertEqual(self.reg.get(a).circuit(self.clock.now()), CircuitState.HALF_OPEN)
        self.assertTrue(self.reg.get(a).routable(self.clock.now(), running=0)[0])
        self.assertFalse(self.reg.get(a).routable(self.clock.now(), running=1)[0])
        # probe thành công -> CLOSED, xoá sạch dấu vết lỗi
        r = self.go(job_id="j2")
        self.assertTrue(r.ok, r.reason)
        wa = self.reg.get(a)
        self.assertEqual(wa.circuit(self.clock.now()), CircuitState.CLOSED)
        self.assertEqual(wa.failure_streak, 0)
        self.assertEqual(wa.last_error_code, "")
        self.assertTrue(wa.routable(self.clock.now())[0])

    def test_half_open_probe_failure_reopens_circuit(self):
        a = self.worker("a", ["temporary"] * 4)
        b = self.worker("b", ["ok"])
        self.pool([a, b], policy={"cooldown_after": 2, "cooldown_s": 60, "retry_on": {"TEMPORARY": 99},
                                   "max_no_progress": 8, "max_total_attempts": 8})
        r = self.go()
        self.assertTrue(r.ok, r.reason)
        wa = self.reg.get(a)
        self.assertEqual(wa.circuit(self.clock.now()), CircuitState.OPEN)
        self.clock.advance(61)                       # cooldown hết -> probe a lần nữa
        r = self.go(job_id="j2")
        self.assertTrue(r.ok, r.reason)
        refreshed = self.reg.get(a)
        self.assertEqual(refreshed.circuit(self.clock.now()), CircuitState.OPEN)      # probe hỏng -> mở lại circuit
        self.assertGreater(refreshed.cooldown_until, self.clock.now())
        self.assertGreater(refreshed.circuit_opened_at, 0)

    def test_pick_blocks_probe_while_half_open_probe_running(self):
        """Concurrency storm tránh: HALF_OPEN mà worker đang có attempt chạy -> loại khỏi pick."""
        a = self.worker("a", ["temporary", "temporary"])
        b = self.worker("b", ["ok"])
        self.pool([a, b], policy={"cooldown_after": 1, "cooldown_s": 60, "retry_on": {"TEMPORARY": 0}})
        r = self.go()
        self.assertTrue(r.ok, r.reason)
        self.clock.advance(61)
        # chèn một attempt RUNNING giả để mô phỏng probe đang chạy trên worker a
        self.store.add_attempt(type("A", (), {
            "attempt_id": "att_probe", "work_type": "story.write", "worker_id": a, "worker_name": "a",
            "driver_id": "drv_a", "model_id": None, "pool": "p", "job_id": "j2", "stage": "",
            "state": AttemptState.RUNNING, "started_at": self.clock.now(), "ended_at": None,
            "error_kind": "", "error_code": "", "error_message": "", "workspace": "", "validation": [],
            "promoted": False, "session_id": None, "cost_usd": 0.0, "meta": {},
        })())
        target, why = self.reg.pick("story.write", now=self.clock.now())
        self.assertEqual(target.worker.id, b)            # a đang probe -> b được chọn
        self.assertIn("half-open", why)


class HealthStateTests(Base):
    def test_health_mapping(self):
        self.assertEqual(self.reg.get(self.worker("ok", ["ok"])).health(self.clock.now())[0], HealthState.HEALTHY)
        dis = self.worker("off", ["ok"])
        self.reg.set_enabled(dis, False)
        self.assertEqual(self.reg.get(dis).health(self.clock.now())[0], HealthState.DISABLED)

    def test_quota_cooldown_gives_quota_blocked_not_generic_cooldown(self):
        a = self.worker("a", ["quota"])
        self.pool([a], policy={"cooldown_s": 60})
        self.go()
        self.assertEqual(self.reg.get(a).health(self.clock.now())[0], HealthState.QUOTA_BLOCKED)

    def test_auth_gives_auth_blocked(self):
        a = self.worker("a", ["auth"])
        self.pool([a])
        self.go()
        self.assertEqual(self.reg.get(a).health(self.clock.now())[0], HealthState.AUTH_BLOCKED)
        self.assertEqual(self.reg.get(a).status, WorkerStatus.AUTH_REQUIRED)

    def test_health_transition_history_recorded(self):
        a = self.worker("a", ["temporary", "temporary", "ok"])
        self.pool([a], policy={"cooldown_after": 2, "cooldown_s": 60, "retry_on": {"TEMPORARY": 1}})
        self.clock.advance(61)
        self.go()
        hist = [h["state"] for h in self.store.health_history(a)]
        self.assertIn(HealthState.COOLDOWN.value, hist)     # lỗi dồn -> cooldown/circuit
        self.assertIn(HealthState.HEALTHY.value, hist)       # probe thành công -> khoẻ lại


class ReconcilerTests(Base):
    def test_orphan_running_attempt_reconciled_after_restart(self):
        a = self.worker("a", ["ok"])
        self.store.add_attempt(type("A", (), {
            "attempt_id": "att_orphan", "work_type": "story.write", "worker_id": a, "worker_name": "a",
            "driver_id": "drv_a", "model_id": None, "pool": "p", "job_id": "j9", "stage": "",
            "state": AttemptState.RUNNING, "started_at": self.clock.now() - 10, "ended_at": None,
            "error_kind": "", "error_code": "", "error_message": "", "workspace": "", "validation": [],
            "promoted": False, "session_id": None, "cost_usd": 0.0, "meta": {},
        })())
        self.assertEqual(len(self.store.running_attempts()), 1)
        orphans = self.mgr.reconcile_orphans()
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0].state, AttemptState.FAILED)
        self.assertEqual(orphans[0].error_code, "ORPHANED")
        self.assertEqual(orphans[0].error_kind, "UNKNOWN")
        self.assertEqual(self.store.running_attempts(), [])     # không để RUNNING vĩnh viễn
        persisted = self.store.attempt("att_orphan")
        self.assertEqual(persisted.state, AttemptState.FAILED)

    def test_restart_does_not_touch_promoted_canonical(self):
        """Canonical chỉ được ghi atomic sau validation — orphan attempt không corrupt canonical."""
        out = Path(self.tmp.name) / "canonical.md"
        out.write_text("nội dung đã commit", encoding="utf-8")
        a = self.worker("a", ["ok"])
        self.store.add_attempt(type("A", (), {
            "attempt_id": "att_orphan2", "work_type": "story.write", "worker_id": a, "worker_name": "a",
            "driver_id": "drv_a", "model_id": None, "pool": "p", "job_id": "j9", "stage": "",
            "state": AttemptState.RUNNING, "started_at": self.clock.now() - 10, "ended_at": None,
            "error_kind": "", "error_code": "", "error_message": "", "workspace": "", "validation": [],
            "promoted": False, "session_id": None, "cost_usd": 0.0, "meta": {},
        })())
        self.mgr.reconcile_orphans()
        self.assertEqual(out.read_text(encoding="utf-8"), "nội dung đã commit")


class NoProgressTests(Base):
    def test_poison_task_stops_automation_early(self):
        """Cùng lỗi lặp lại trên task không tiến triển -> dừng, không đốt hết budget."""
        a = self.worker("a", ["temporary"] * 9)
        self.pool([a], policy={"retry_on": {"TEMPORARY": 99}, "max_total_attempts": 9,
                               "max_no_progress": 4, "cooldown_after": 99})
        r = self.go()
        self.assertFalse(r.ok)
        self.assertEqual(len(r.attempts), 4)                   # dừng ở 4, không tới 9
        self.assertIn("poison task", r.reason)

    def test_fallback_to_healthy_worker_rescues_before_poison(self):
        a = self.worker("a", ["temporary", "temporary", "temporary"])
        b = self.worker("b", ["ok"])
        self.pool([a, b], policy={"retry_on": {"TEMPORARY": 2}, "max_no_progress": 4,
                                   "cooldown_after": 99})
        r = self.go()
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.attempts[-1].worker_id, b)          # fallback cứu task

    def test_success_resets_poison_counter(self):
        a = self.worker("a", ["temporary", "ok"])
        self.pool([a], policy={"retry_on": {"TEMPORARY": 99}, "max_no_progress": 4,
                               "cooldown_after": 99})
        r = self.go()
        self.assertTrue(r.ok, r.reason)                        # 1 lỗi rồi thành công: không phải poison
        self.assertEqual(len(r.attempts), 2)


class TimeoutTests(Base):
    def test_exec_request_defaults_are_sane(self):
        from contentfactory.workers.drivers.base import ExecRequest
        req = ExecRequest(work_type="x", prompt="p", cwd=Path("."))
        self.assertGreater(req.startup_timeout_s, 0)
        self.assertGreater(req.idle_timeout_s, 0)
        self.assertGreaterEqual(req.hard_timeout_s, req.idle_timeout_s)

    def test_manager_passes_worker_timeouts_to_driver(self):
        class CapDriver(FakeDriver):
            def __init__(self):
                super().__init__({"script": ["ok"]})
                self.last_req = None

            def execute(self, req):
                self.last_req = req
                return ExecResult(ok=True, text="ok", exit_code=0)

        drv = CapDriver()
        self.reg.register_driver("cap", drv)
        w = self.reg.add("cap", "cap", "cf-cap").id
        self.reg.create_pool("p", members=[w])
        self.reg.set_routing("story.write", pool="p")
        self.mgr.run("story.write", prompt="viết")
        self.assertEqual(drv.last_req.idle_timeout_s, self.reg.get(w).idle_timeout_s)
        self.assertEqual(drv.last_req.hard_timeout_s, self.reg.get(w).hard_timeout_s)

    def test_kill_tree_is_noop_on_finished_process(self):
        import subprocess
        p = subprocess.Popen(["python", "-c", "pass"])
        p.wait()
        kill_tree(p)                                             # không được ném


class MetricsTests(Base):
    def test_worker_stats_and_attempts_per_task(self):
        a = self.worker("a", ["quota"])
        b = self.worker("b", ["ok"])
        self.pool([a, b])
        r = self.go()
        self.assertTrue(r.ok, r.reason)
        stats = self.store.worker_stats()
        self.assertIn(a, stats)
        self.assertEqual(stats[a]["attempts"], 1)
        self.assertEqual(stats[a]["failures"]["QUOTA"], 1)
        self.assertEqual(stats[b]["success"], 1)
        self.assertEqual(stats[b]["success_rate"], 100.0)
        tasks = self.store.attempts_per_task()
        row = next(t for t in tasks if t["job_id"] == "j1")
        self.assertEqual(row["attempts"], 2)
        self.assertTrue(row["fallback"])                         # dùng 2 worker khác nhau

    def test_worker_stats_answers_who_is_failing(self):
        a = self.worker("a", ["temporary", "temporary"])
        b = self.worker("b", ["ok"])
        self.pool([a, b], policy={"retry_on": {"TEMPORARY": 1}, "cooldown_after": 99})
        self.go()
        stats = self.store.worker_stats()[a]
        self.assertNotIn("TEMPORARY", stats["failures"])      # TEMPORARY không phải số đếm riêng (chỉ QUOTA/AUTH/TIMEOUT/INVALID_OUTPUT)
        self.assertGreaterEqual(stats["attempts"], 2)
        self.assertEqual(stats["success_rate"], 0.0)


class UiEndpointsTests(Base):
    """W2.UI backend contract: summary / impact / pool_impact / worker_detail / attempts."""

    def setUp(self) -> None:
        super().setUp()
        import types
        from contentfactory.orchestrator.worker_admin import WorkerService
        cfg = types.SimpleNamespace(path=lambda key: Path("ignored"))   # registry được truyền thẳng
        self.svc = WorkerService(cfg, registry=self.reg)

    def fake_running(self, worker_id: str) -> None:
        self.store.add_attempt(type("A", (), {
            "attempt_id": "att_x", "work_type": "story.write", "worker_id": worker_id, "worker_name": "x",
            "driver_id": "drv_x", "model_id": None, "pool": "p", "job_id": "jx", "stage": "",
            "state": AttemptState.RUNNING, "started_at": self.clock.now(), "ended_at": None,
            "error_kind": "", "error_code": "", "error_message": "", "workspace": "", "validation": [],
            "promoted": False, "session_id": None, "cost_usd": 0.0, "meta": {},
        })())

    def test_summary_counts_blocks_and_circuits(self):
        a = self.worker("a", ["quota"])
        b = self.worker("b", ["ok"])
        self.pool([a, b], policy={"cooldown_after": 1, "cooldown_s": 60})
        self.mgr.now = time.time                    # summary() đọc health theo real clock
        self.go()
        s = self.svc.summary()
        self.assertEqual(s["total"], 2)
        self.assertGreaterEqual(s["counts"]["QUOTA_BLOCKED"], 1)
        self.assertIn(a, [c["worker_id"] for c in s["circuits"]])

    def test_impact_lists_pools_work_types_and_running(self):
        a = self.worker("a", ["ok"])
        b = self.worker("b", ["ok"])
        self.pool([a, b])
        self.fake_running(a)
        imp = self.svc.impact(a)
        self.assertEqual(imp["pools"], ["p"])
        self.assertIn("story.write", imp["work_types"])
        self.assertEqual(imp["running"], 1)

    def test_pool_impact(self):
        a = self.worker("a", ["ok"])
        self.pool([a])
        self.fake_running(a)
        imp = self.svc.pool_impact("p")
        self.assertEqual(imp["work_types"], ["story.write"])
        self.assertEqual(imp["running"], 1)

    def test_worker_detail_includes_history_stats_and_recent_attempts(self):
        a = self.worker("a", ["temporary", "temporary", "ok"])
        b = self.worker("b", ["ok"])
        self.pool([a, b], policy={"cooldown_after": 2, "cooldown_s": 60, "retry_on": {"TEMPORARY": 1}})
        r = self.go()
        self.assertTrue(r.ok, r.reason)
        self.clock.advance(61)                       # a hết cooldown -> probe thành công ở job sau
        r2 = self.go(job_id="j2")
        self.assertTrue(r2.ok, r2.reason)
        d = self.svc.worker_detail(a)
        self.assertEqual(d["name"], "a")
        hist = [h["state"] for h in d["health_history"]]
        self.assertIn(HealthState.COOLDOWN.value, hist)      # circuit mở lúc streak lỗi
        self.assertIn(HealthState.HEALTHY.value, hist)       # probe thành công sau cooldown
        self.assertGreaterEqual(d["stats"]["attempts"], 1)
        self.assertTrue(d["recent_attempts"])
        self.assertTrue(all(x["worker_id"] == a for x in d["recent_attempts"]))
        self.assertTrue(any(x["worker_id"] == b for x in self.svc.attempts(worker_id=b, limit=50)))

    def test_attempts_filters_by_worker(self):
        a = self.worker("a", ["quota"])
        b = self.worker("b", ["ok"])
        self.pool([a, b])
        self.go()
        only_b = self.svc.attempts(worker_id=b, limit=50)
        self.assertTrue(only_b)
        self.assertTrue(all(x["worker_id"] == b for x in only_b))


if __name__ == "__main__":
    unittest.main()