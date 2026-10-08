"""W1.9–W1.12: retry/fallback/cooldown + attempt history + workspace/promote.

Mỗi worker dùng một driver giả riêng (script hành vi) nên fault injection được từng worker.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from contentfactory.contracts import CancelToken
from contentfactory.workers.drivers.fake import FakeDriver
from contentfactory.workers.models import AttemptState, WorkerStatus
from contentfactory.workers.registry import WorkerRegistry
from contentfactory.workers.manager import WorkerManager
from contentfactory.workers.store import WorkerStore

_REAL_IDS = ("claude_cli", "codex_cli", "gemini_cli", "opencode_cli")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = WorkerStore(root / "workers.db")
        self.drivers = {rid: FakeDriver({"detected": []}) for rid in _REAL_IDS}
        self.reg = WorkerRegistry(self.store, drivers=self.drivers)
        self.mgr = WorkerManager(self.reg, root / "workspace")

    def worker(self, name: str, script: list[str]) -> str:
        """Thêm worker có driver giả riêng với đúng script hành vi."""
        did = f"drv_{name}"
        self.reg.register_driver(did, FakeDriver({"script": script}))
        w = self.reg.add(name, did, f"cf-{name}")
        return w.id

    def pool(self, members: list[str], policy: dict | None = None, **pool_kw) -> None:
        self.reg.create_pool("p", members=members, **pool_kw)
        self.reg.set_routing("story.write", pool="p", policy=policy or {})

    def go(self, **kw) -> object:
        kw.setdefault("job_id", "j1")
        return self.mgr.run("story.write", prompt="viết", **kw)


class RetryFallbackTests(Base):
    def test_temporary_retries_same_worker_then_success(self):
        a = self.worker("a", ["temporary", "ok"])
        b = self.worker("b", ["ok"])
        self.pool([a, b])
        r = self.go(output=Path(self.tmp.name) / "canonical.md")
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(len(r.attempts), 2)
        self.assertEqual({x.worker_id for x in r.attempts}, {a})
        self.assertEqual(r.attempts[0].state, AttemptState.FAILED)
        self.assertEqual(r.attempts[1].state, AttemptState.SUCCESS)
        self.assertTrue(r.output_path.exists())
        self.assertIn("thành công", r.reason)

    def test_quota_falls_to_next_worker_and_blocks_first(self):
        a = self.worker("a", ["quota"])
        b = self.worker("b", ["ok"])
        self.pool([a, b])
        r = self.go()
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(len(r.attempts), 2)
        self.assertEqual(r.attempts[0].worker_id, a)
        self.assertEqual(r.attempts[1].worker_id, b)
        wa = self.reg.get(a)
        self.assertGreater(wa.cooldown_until, time.time())     # QUOTA block ngay
        self.assertFalse(wa.routable()[0])

    def test_auth_marks_worker_and_uses_next(self):
        a = self.worker("a", ["auth"])
        b = self.worker("b", ["ok"])
        self.pool([a, b])
        r = self.go()
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.attempts[-1].worker_id, b)
        self.assertEqual(self.reg.get(a).status, WorkerStatus.AUTH_REQUIRED)
        self.assertFalse(self.reg.get(a).routable()[0])

    def test_timeout_bounded_retry_then_fallback(self):
        a = self.worker("a", ["timeout", "timeout", "ok"])   # retry TIMEOUT=1 -> 2 lần rồi rơi
        b = self.worker("b", ["ok"])
        self.pool([a, b])
        r = self.go()
        self.assertTrue(r.ok, r.reason)
        self.assertEqual([x.worker_id for x in r.attempts], [a, a, b])
        self.assertEqual(r.attempts[0].error_kind, "TIMEOUT")

    def test_all_workers_fail_falls_to_third(self):
        a = self.worker("a", ["quota"])
        b = self.worker("b", ["quota"])
        c = self.worker("c", ["ok"])
        self.pool([a, b, c])
        r = self.go()
        self.assertTrue(r.ok, r.reason)
        self.assertEqual([x.worker_id for x in r.attempts], [a, b, c])

    def test_everything_unavailable_fails_clearly_without_loop(self):
        a = self.worker("a", ["quota"])
        b = self.worker("b", ["auth"])
        self.pool([a, b])
        r = self.go()
        self.assertFalse(r.ok)
        self.assertEqual(len(r.attempts), 2)                  # không lặp vô hạn
        self.assertIn("không worker nào đủ điều kiện", r.reason)
        self.assertIsNotNone(r.error)

    def test_failure_streak_triggers_cooldown_then_success_elsewhere(self):
        a = self.worker("a", ["temporary", "temporary", "temporary"])
        b = self.worker("b", ["ok"])
        self.pool([a, b], policy={"cooldown_after": 3, "cooldown_s": 120})
        r = self.go()
        self.assertTrue(r.ok, r.reason)
        self.assertEqual([x.worker_id for x in r.attempts], [a, a, a, b])
        self.assertEqual(self.reg.get(a).failure_streak, 3)
        self.assertGreater(self.reg.get(a).cooldown_until, time.time())
        self.assertEqual(self.reg.get(b).failure_streak, 0)

    def test_max_total_attempts_bounds_run(self):
        a = self.worker("a", ["temporary"] * 10)
        self.pool([a], policy={"max_total_attempts": 4, "retry_on": {"TEMPORARY": 99},
                               "cooldown_after": 99})
        r = self.go()
        self.assertFalse(r.ok)
        self.assertEqual(len(r.attempts), 4)
        self.assertIn("max_total_attempts", r.reason)

    def test_max_distinct_workers_respected(self):
        a = self.worker("a", ["quota"])
        b = self.worker("b", ["quota"])
        c = self.worker("c", ["ok"])
        self.pool([a, b, c], policy={"max_distinct_workers": 2})
        r = self.go()
        self.assertFalse(r.ok)
        self.assertEqual(len({x.worker_id for x in r.attempts}), 2)
        self.assertIn("max_distinct_workers", r.reason)


class ValidationGateTests(Base):
    def test_exit_zero_but_invalid_output_retries_then_promotes(self):
        a = self.worker("a", ["invalid_output", "ok"])
        self.pool([a])
        out = Path(self.tmp.name) / "canonical.md"
        r = self.go(output=out)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.attempts[0].state, AttemptState.INVALID)
        self.assertTrue(r.attempts[0].validation)
        self.assertEqual(r.attempts[0].error_kind, "INVALID_OUTPUT")
        self.assertTrue(out.exists())
        self.assertNotIn("<<INVALID_OUTPUT>>", out.read_text(encoding="utf-8"))

    def test_no_promote_when_everything_fails(self):
        a = self.worker("a", ["invalid_output", "invalid_output", "invalid_output"])
        self.pool([a], policy={"max_total_attempts": 3, "retry_on": {"INVALID_OUTPUT": 0}})
        out = Path(self.tmp.name) / "canonical.md"
        r = self.go(output=out)
        self.assertFalse(r.ok)
        self.assertFalse(out.exists())                        # output lỗi không bao giờ ra canonical
        ws = Path(r.attempts[-1].workspace)                   # evidence vẫn còn để debug
        self.assertTrue(ws.exists())
        self.assertTrue(any(ws.glob("output.md")))

    def test_contract_validator_is_used(self):
        a = self.worker("a", ["ok", "ok"])
        self.pool([a])
        calls = []

        def reject(path: Path, work_type: str) -> list[str]:
            calls.append((path, work_type))
            return ["không đạt contract"]

        mgr = WorkerManager(self.reg, Path(self.tmp.name) / "ws2", validate=reject)
        r = mgr.run("story.write", job_id="j2")
        self.assertFalse(r.ok)
        self.assertEqual(r.attempts[0].state, AttemptState.INVALID)
        self.assertEqual(calls[0][1], "story.write")
        self.assertEqual(r.error.kind.value, "INVALID_OUTPUT")


class AttemptHistoryTests(Base):
    def test_attempts_are_new_rows_not_overwrites(self):
        a = self.worker("a", ["temporary", "ok"])
        self.pool([a])
        self.go(job_id="j9")
        rows = self.store.attempts(job_id="j9")            # mới nhất trước
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0].attempt_id, rows[1].attempt_id)
        self.assertEqual(rows[0].state, AttemptState.SUCCESS)
        self.assertEqual(rows[1].state, AttemptState.FAILED)
        self.assertTrue(rows[0].workspace and rows[1].workspace)
        self.assertNotEqual(rows[0].workspace, rows[1].workspace)

    def test_cancel_stops_before_new_attempt(self):
        a = self.worker("a", ["ok"])
        self.pool([a])
        token = CancelToken()
        token.set()
        r = self.go(cancel=token)
        self.assertFalse(r.ok)
        self.assertEqual(r.attempts, [])
        self.assertIn("hủy", r.reason)


class W1FinalAcceptanceTests(Base):
    """W1 Final Acceptance Scenario (plan dòng 918–936): fault-injection chạy qua toàn bộ
    pipeline — A quota -> A hết routable -> B invalid output -> validation reject ->
    fallback -> C success -> canonical đúng một lần -> timeline đủ A/B/C + lý do."""

    def test_fault_injection_flow_commits_canonical_once_with_full_timeline(self):
        a = self.worker("a", ["quota"])               # A: quota failure, rồi bị block khỏi routing
        b = self.worker("b", ["invalid_output"])      # B: invalid output -> validation reject
        c = self.worker("c", ["ok"])                  # C: thành công
        self.pool([a, b, c], policy={"retry_on": {"INVALID_OUTPUT": 0}})  # invalid không retry B, rơi thẳng C
        out = Path(self.tmp.name) / "canonical.md"
        r = self.go(output=out)

        self.assertTrue(r.ok, r.reason)
        self.assertEqual([x.worker_id for x in r.attempts], [a, b, c])          # timeline đủ A/B/C
        self.assertEqual([x.error_kind for x in r.attempts], ["QUOTA", "INVALID_OUTPUT", ""])
        self.assertEqual([x.state for x in r.attempts],
                         [AttemptState.FAILED, AttemptState.INVALID, AttemptState.SUCCESS])
        self.assertFalse(self.reg.get(a).routable()[0])                         # A hết routable sau quota
        self.assertTrue(out.exists())
        canonical = out.read_text(encoding="utf-8")                             # canonical commit ĐÚNG MỘT lần
        self.assertNotIn("<<INVALID_OUTPUT>>", canonical)
        self.assertIn("chương thử", canonical)

    def test_validation_rejected_output_kept_but_never_promoted_to_canonical(self):
        a = self.worker("a", ["invalid_output"])
        b = self.worker("b", ["ok"])
        self.pool([a, b], policy={"retry_on": {"INVALID_OUTPUT": 0}})
        out = Path(self.tmp.name) / "canonical.md"
        r = self.go(output=out)
        self.assertTrue(r.ok, r.reason)
        bad_ws = Path(r.attempts[0].workspace)
        self.assertTrue((bad_ws / "output.md").exists())                        # evidence lỗi giữ để debug
        self.assertNotIn("<<INVALID_OUTPUT>>", out.read_text(encoding="utf-8")) # nhưng không bao giờ ra canonical


if __name__ == "__main__":
    unittest.main()
