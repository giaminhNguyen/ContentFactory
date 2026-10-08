"""Core của Worker Runtime (W1.1 + W1.2): model, lỗi chuẩn hoá, hợp đồng driver.

Chạy thuần trong tiến trình test — KHÔNG cài bất kỳ CLI agent nào.
"""
from __future__ import annotations

import dataclasses
import re
import sys
import tempfile
import unittest
from pathlib import Path

from contentfactory.contracts import ErrorClass, StageError
from contentfactory.workers import models as M
from contentfactory.workers.drivers import FORBIDDEN_MARKER, build, driver_ids
from contentfactory.workers.drivers.base import BaseDriver, Driver, ExecRequest
from contentfactory.workers.drivers.fake import FakeDriver
from contentfactory.workers.errors import WorkerError, WorkerErrorClass, classify_text


class ClassifyTest(unittest.TestCase):
    def test_common_raw_errors_map_to_six_classes(self):
        cases = {
            "usage limit reached, please try again later": WorkerErrorClass.QUOTA,
            "429 Too Many Requests": WorkerErrorClass.QUOTA,
            "credit balance is too low": WorkerErrorClass.QUOTA,
            "Not logged in. Please run /login": WorkerErrorClass.AUTH,
            "Error: invalid api key": WorkerErrorClass.AUTH,
            "request timed out after 30s": WorkerErrorClass.TIMEOUT,
            "connection reset by peer, try again": WorkerErrorClass.TEMPORARY,
            "503 Service Unavailable": WorkerErrorClass.TEMPORARY,
            "something nobody has ever seen": WorkerErrorClass.UNKNOWN,
            "": WorkerErrorClass.UNKNOWN,
        }
        for raw, want in cases.items():
            self.assertEqual(classify_text(raw), want, raw)

    def test_driver_can_add_vendor_specific_pattern(self):
        d = BaseDriver({"models": []})
        d.extra_patterns = ((WorkerErrorClass.QUOTA, re.compile(r"vendor-specific-limit", re.I)),)
        self.assertEqual(d.classify("Vendor-Specific-Limit hit"), WorkerErrorClass.QUOTA)
        self.assertEqual(d.classify("vô danh"), WorkerErrorClass.UNKNOWN)

    def test_error_raw_is_clipped(self):
        e = WorkerError.from_text(WorkerErrorClass.UNKNOWN, "X", raw="a" * 5000)
        self.assertEqual(len(e.raw), 400)

    def test_error_structured_dict_for_ui(self):
        e = WorkerError.from_text(WorkerErrorClass.QUOTA, "FAKE_QUOTA", raw="hết quota", retry_after_s=60.0)
        d = e.to_dict()
        self.assertEqual(d["kind"], "QUOTA")
        self.assertEqual(d["code"], "FAKE_QUOTA")
        self.assertEqual(d["retry_after_s"], 60.0)


class BridgeTest(unittest.TestCase):
    """WorkerError <-> StageError: hai phía phải nói cùng một chuyện."""

    def test_to_stage_error(self):
        want = {
            WorkerErrorClass.TEMPORARY: (ErrorClass.TRANSIENT, "runtime"),
            WorkerErrorClass.TIMEOUT: (ErrorClass.TRANSIENT, "runtime"),
            WorkerErrorClass.QUOTA: (ErrorClass.RESOURCE, "quota"),
            WorkerErrorClass.AUTH: (ErrorClass.AUTH, None),
            WorkerErrorClass.INVALID_OUTPUT: (ErrorClass.POLICY, "output"),
            WorkerErrorClass.UNKNOWN: (ErrorClass.AMBIGUOUS, None),
        }
        for kind, (ec, res) in want.items():
            err = WorkerError(kind=kind, code="C").to_stage_error()
            self.assertEqual(err.error_class, ec, kind)
            self.assertEqual(err.resource, res, kind)

    def test_from_stage_error(self):
        def w(err: StageError) -> WorkerErrorClass:
            return WorkerError.from_stage_error(err).kind

        self.assertEqual(w(StageError(ErrorClass.RESOURCE, "CLAUDE_USAGE_LIMIT", resource="token")),
                         WorkerErrorClass.QUOTA)
        self.assertEqual(w(StageError(ErrorClass.RESOURCE, "NO_NETWORK", resource="network")),
                         WorkerErrorClass.TEMPORARY)
        self.assertEqual(w(StageError(ErrorClass.AUTH, "NOT_LOGGED_IN")), WorkerErrorClass.AUTH)
        self.assertEqual(w(StageError(ErrorClass.TRANSIENT, "AGENT_TIMEOUT")), WorkerErrorClass.TIMEOUT)
        self.assertEqual(w(StageError(ErrorClass.TRANSIENT, "AGENT_NO_RESULT")), WorkerErrorClass.TEMPORARY)

    def test_roundtrip_survives_quota_and_auth(self):
        for src in (StageError(ErrorClass.RESOURCE, "LIMIT", resource="quota"),
                    StageError(ErrorClass.AUTH, "LOGIN")):
            self.assertIsNotNone(WorkerError.from_stage_error(src).to_stage_error())


class WorkerModelTest(unittest.TestCase):
    def make(self, **kw) -> M.Worker:
        base = dict(id="w1", name="Claude Main", driver_id="fake", executable="claude",
                    models=[M.WorkerModel("m1", default=True), M.WorkerModel("m2")],
                    profiles={"fast": "m1", "high": "m2"})
        base.update(kw)
        return M.Worker(**base)

    def test_default_model_and_profiles(self):
        w = self.make()
        self.assertEqual(w.default_model, "m1")
        self.assertEqual(w.model_for("high"), "m2")
        self.assertEqual(w.model_for("fast"), "m1")
        self.assertEqual(w.model_for(None), "m1")
        self.assertEqual(self.make(profiles={}).model_for("high"), "m1")
        self.assertIsNone(self.make(models=[]).default_model)

    def test_disabled_model_is_not_default(self):
        w = self.make(models=[M.WorkerModel("m1", default=True, enabled=False), M.WorkerModel("m2")])
        self.assertEqual(w.default_model, "m2")
        self.assertFalse(w.has_model("m1"))
        self.assertTrue(w.has_model("m2"))
        self.assertTrue(w.has_model(None))          # None = dùng mặc định -> luôn hợp lệ

    def test_routable_explains_every_exclusion(self):
        now = 1000.0
        self.assertEqual(self.make(status=M.WorkerStatus.READY, cooldown_until=0.0).routable(now), (True, "ready"))
        self.assertEqual(self.make(enabled=False).routable(now), (False, "disabled"))
        self.assertEqual(self.make(status=M.WorkerStatus.AUTH_REQUIRED).routable(now), (False, "auth required"))
        self.assertEqual(self.make(status=M.WorkerStatus.NOT_FOUND).routable(now), (False, "executable not found"))
        self.assertEqual(self.make(status=M.WorkerStatus.BROKEN).routable(now), (False, "broken"))
        self.assertEqual(self.make(status=M.WorkerStatus.DETECTED).routable(now), (False, "not probed yet"))
        self.assertEqual(self.make(cooldown_until=now + 60).routable(now), (False, "cooldown"))
        self.assertEqual(self.make(status=M.WorkerStatus.READY, cooldown_until=now - 1).routable(now),
                         (True, "ready"))

    def test_disable_keeps_identity(self):
        """CLI bị gỡ/không dùng nữa thì lịch sử vẫn tra được: id không đổi."""
        w = self.make()
        self.assertEqual(w.with_updates(enabled=False).id, w.id)

    def test_pool_member_order_is_priority(self):
        p = M.WorkerPool(name="story_workers", members=["a", "b", "c"])
        self.assertEqual(p.members, ["a", "b", "c"])
        p = p.with_updates(members=["b", "a", "c"])
        self.assertEqual(p.members[0], "b")


class AttemptTest(unittest.TestCase):
    def test_attempt_is_immutable(self):
        a = M.Attempt(attempt_id="att1", work_type="story.write", worker_id="w1",
                      state=M.AttemptState.SUCCESS)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            a.state = M.AttemptState.FAILED
        b = dataclasses.replace(a, attempt_id="att2", state=M.AttemptState.FAILED)
        self.assertTrue(a.ok)
        self.assertFalse(b.ok)
        self.assertEqual(a.attempt_id, "att1")       # retry không ghi đè attempt cũ

    def test_duration(self):
        a = M.Attempt(attempt_id="x", work_type="t", worker_id="w", started_at=10.0, ended_at=25.0)
        self.assertEqual(a.duration_s, 15.0)
        self.assertEqual(M.Attempt(attempt_id="x", work_type="t", worker_id="w", started_at=10.0).duration_s, 0.0)


class DriverContractTest(unittest.TestCase):
    def test_registry_lists_fake(self):
        self.assertIn("fake", driver_ids())

    def test_unknown_driver_raises_with_hint(self):
        with self.assertRaises(ValueError) as cm:
            build("claude")
        self.assertIn("không tồn tại", str(cm.exception))

    def test_fake_driver_satisfies_protocol(self):
        self.assertIsInstance(FakeDriver(), Driver)
        self.assertIsInstance(build("fake"), BaseDriver)

    def test_missing_executable_probe_does_not_crash(self):
        r = BaseDriver().probe(Path(__file__).with_name("khong-ton-tai.exe").as_posix())
        self.assertEqual(r.status.value, "NOT_FOUND")
        self.assertTrue(r.detail)

    def test_probe_of_real_python_reports_version(self):
        r = BaseDriver().probe(sys.executable)
        self.assertEqual(r.status.value, "READY")
        self.assertTrue(r.version)

    def test_driver_never_puts_cfg_secrets_in_results(self):
        d = FakeDriver({"api_key": "SUPER-SECRET", "probe_status": "ready"})
        self.assertNotIn("SUPER-SECRET", repr(d.probe("anything")))
        self.assertNotIn("SUPER-SECRET", repr(d.discover()))
        with tempfile.TemporaryDirectory() as td:
            r = d.execute(ExecRequest(work_type="t", prompt="p", cwd=Path(td)))
        self.assertNotIn("SUPER-SECRET", repr(r))


class FakeDriverBehaviorTest(unittest.TestCase):
    def req(self, tmp: Path) -> ExecRequest:
        return ExecRequest(work_type="story.write", prompt="viết", cwd=tmp, model="fake-high")

    def test_ok_writes_output_file(self):
        with tempfile.TemporaryDirectory() as td:
            r = FakeDriver().execute(self.req(Path(td)))
            self.assertTrue(r.ok)
            self.assertIsNotNone(r.output_path)
            self.assertTrue(r.output_path.is_file())
            self.assertNotIn(FORBIDDEN_MARKER, r.output_path.read_text(encoding="utf-8"))
            self.assertEqual(r.exit_code, 0)

    def test_scripted_failures_come_out_normalized(self):
        d = FakeDriver({"script": ["quota", "auth", "timeout", "temporary"]})
        with tempfile.TemporaryDirectory() as td:
            got = [d.execute(self.req(Path(td))).error.kind for _ in range(4)]
        self.assertEqual(got, [WorkerErrorClass.QUOTA, WorkerErrorClass.AUTH,
                               WorkerErrorClass.TIMEOUT, WorkerErrorClass.TEMPORARY])

    def test_scripted_invalid_output_still_reports_process_ok(self):
        """Exit 0 nhưng output cấm marker: process ok, validation (ở tầng khác) sẽ từ chối."""
        with tempfile.TemporaryDirectory() as td:
            r = FakeDriver({"script": ["invalid_output"]}).execute(self.req(Path(td)))
            self.assertTrue(r.ok)
            self.assertIn(FORBIDDEN_MARKER, r.output_path.read_text(encoding="utf-8"))

    def test_no_output_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            r = FakeDriver({"script": ["no_output"]}).execute(self.req(Path(td)))
            self.assertTrue(r.ok)
            self.assertIsNone(r.output_path)

    def test_calls_are_recorded_for_simulation(self):
        import tempfile
        d = FakeDriver({"detected": ["a", "b"]})
        with tempfile.TemporaryDirectory() as td:
            d.execute(self.req(Path(td)))
        self.assertEqual(len(d.calls), 1)
        self.assertEqual(d.calls[0]["work_type"], "story.write")
        self.assertEqual([x.executable for x in d.discover()], ["a", "b"])

    def test_failed_result_has_error_and_no_output(self):
        with tempfile.TemporaryDirectory() as td:
            r = FakeDriver({"script": ["quota"]}).execute(self.req(Path(td)))
            self.assertFalse(r.ok)
            self.assertIsNone(r.output_path)
            self.assertEqual(r.error.code, "FAKE_QUOTA")


if __name__ == "__main__":
    unittest.main()
