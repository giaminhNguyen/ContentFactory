"""W1.15/W1.16: WorkerService facade + CLI `workers` + doctor nhóm Worker Runtime + API webui.

Kiểm tra: JSON dump không lộ session/token; worker hỏng không crash doctor/CLI; CRUD worker/pool/routing
qua service và qua HTTP về cùng một bộ field; lệnh CLI `update` không còn trùng subparser.
"""
import contextlib
import io
import json
import unittest
import urllib.request

from contentfactory.orchestrator.cli import main as cli_main
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.doctor import run_doctor
from contentfactory.orchestrator.worker_admin import WorkerService, attempt_dump
from contentfactory.orchestrator.webui import App, UiServer
from contentfactory.workers.drivers import FakeDriver
from contentfactory.workers.models import Attempt, AttemptState
from contentfactory.workers.registry import WorkerRegistry
from contentfactory.workers.store import WorkerStore
from tests import test_ui
from tests.support import RootCase


def fake_svc(root, driver: FakeDriver | None = None) -> WorkerService:
    cfg = load_config(root)
    reg = WorkerRegistry(WorkerStore(cfg.path("runtime") / "workers.db"))
    reg.register_driver("fake", driver or FakeDriver())
    return WorkerService(cfg, registry=reg)


class WorkerDumpTest(unittest.TestCase):
    def test_attempt_dump_never_leaks_session_or_token_value(self):
        a = Attempt(attempt_id="att_1", work_type="story.write", worker_id="wkr_x", worker_name="A",
                    driver_id="fake", state=AttemptState.FAILED, session_id="resume-token-bi-mat",
                    cost_usd=0.42, validation=["x"])
        d = attempt_dump(a)
        self.assertTrue(d["session"])                                  # chỉ báo "có session", không lộ giá trị
        self.assertNotIn("resume-token-bi-mat", json.dumps(d))
        self.assertNotIn("session_id", json.dumps(d))
        d2 = attempt_dump(Attempt(attempt_id="att_2", work_type="w", worker_id="w", driver_id="d"))
        self.assertFalse(d2["session"])


class WorkerServiceTest(RootCase):
    def test_scan_add_list_update_disable_remove_via_fake(self):
        svc = fake_svc(self.root)
        self.assertEqual(svc.workers(), [])
        w = svc.add("W A", "fake", "cf-fake-worker", models=["m1", "m2"], probe=True)
        self.assertEqual((w["driver_id"], w["status"], w["default_model"]), ("fake", "READY", "fake-fast"))   # probe thay model thủ công bằng danh sách driver
        got = {x["id"]: x for x in svc.workers()}
        self.assertEqual(got[w["id"]]["name"], "W A")
        self.assertEqual(got[w["id"]]["routable"], {"ok": True, "reason": "ready"})
        nw = svc.update(w["id"], name="W B", concurrency=2)
        self.assertEqual((nw["name"], nw["concurrency"]), ("W B", 2))
        self.assertFalse(svc.set_enabled(w["id"], False)["enabled"])
        self.assertEqual(svc.pick("story.write")["ok"], False)
        svc.set_enabled(w["id"], True)
        svc.remove(w["id"])
        self.assertEqual(svc.workers(), [])

    def test_pool_routing_and_pick_honour_priority_and_fallback(self):
        svc = fake_svc(self.root)
        a = svc.add("A", "fake", "cf-fake-a", probe=True)
        b = svc.add("B", "fake", "cf-fake-b", probe=True)
        svc.create_pool("p", "Nhóm P", [a["id"], b["id"]])
        svc.set_routing("story.write", "p")
        t = svc.pick("story.write")["target"]
        self.assertEqual((t["worker"]["id"], t["pool"]), (a["id"], "p"))
        p = svc.pick("story.write", exclude=(a["id"],))["target"]      # A loại -> rơi xuống B
        self.assertEqual(p["worker"]["id"], b["id"])
        self.assertEqual(svc.routing()["routing"]["story.write"]["pool"], "p")
        self.assertEqual(svc.delete_routing("story.write"), True)
        self.assertEqual(svc.delete_pool("p")["deleted"], "p")

    def test_pool_delete_blocked_when_routed_then_force(self):
        svc = fake_svc(self.root)
        w = svc.add("A", "fake", "cf-fake-a", probe=True)
        svc.create_pool("p", members=[w["id"]])
        svc.set_routing("story.write", "p")
        with self.assertRaises(ValueError) as cm:
            svc.delete_pool("p")
        self.assertIn("story.write", str(cm.exception))
        self.assertEqual(svc.delete_pool("p", force=True)["routing_removed"], ["story.write"])


class CliWorkersTest(RootCase):
    def test_doctor_and_workers_cli_no_longer_crash_on_duplicate_update(self):
        r = cli_main(["--root", str(self.root), "workers", "list"])
        self.assertIsInstance(r, int)                                   # trước đây: argparse.ArgumentError (conflicting subparser: update)
        r = cli_main(["--root", str(self.root), "doctor", "--json"])
        self.assertIn(r, (0, 1))
        self.assertEqual(cli_main(["--root", str(self.root), "workers", "probe", "wkr_khong_co"]), 2)

    def test_workers_scan_and_list_roundtrip(self):
        fake_svc(self.root).add("W", "fake", "cf-fake-worker", probe=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = cli_main(["--root", str(self.root), "workers", "list"])
        self.assertEqual(r, 0)
        self.assertIn("W", buf.getvalue())
        self.assertIn("cf-fake-worker", buf.getvalue())
        self.assertIn("READY", buf.getvalue())

    def test_doctor_lists_workers_and_missing_cli_is_warn_not_crash(self):
        svc = fake_svc(self.root)
        ok = svc.add("OK", "fake", "cf-fake-ok", probe=True)
        svc.add("MatCLI", "claude_cli", "khong-co-cli-that-xyz", probe=False)
        rep = run_doctor(load_config(self.root))                       # doctor tự mở registry mới, probe tất cả
        byname = {c["name"]: c for c in rep["checks"]}
        self.assertTrue(any(c["group"] == "Worker Runtime" for c in rep["checks"]))
        self.assertEqual(byname[f"workers.{ok['id']}"]["status"], "ok")
        miss = next(c for c in rep["checks"] if c["name"].startswith("workers.wkr_") and c["name"] != f"workers.{ok['id']}")
        self.assertEqual(miss["status"], "warn")                       # CLI mất -> cảnh báo, KHÔNG crash doctor


class WorkersHttpTest(test_ui.UiCase):
    def setUp(self):
        super().setUp()
        self.app = App(self.o, run_loop=False, opener=lambda p: None)
        self.srv = UiServer(self.app, 0)
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.base = self.srv.url.rstrip("/")

    def call(self, method, path, body=None, token=True):
        h = {"Content-Type": "application/json", **({"X-CF-Token": self.app.token} if token else {})}
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}"), r.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else {}), e.headers

    def test_workers_endpoints_roundtrip(self):
        self.assertEqual(self.call("GET", "/api/workers", token=False)[0], 401)
        code, d, _ = self.call("GET", "/api/workers")
        self.assertEqual(code, 200)
        self.assertTrue(any(x["id"] == "fake" for x in d["drivers"]))
        self.assertEqual(d["workers"], [])
        code, w, _ = self.call("POST", "/api/workers", {"name": "HTTP", "driver_id": "fake",
                                                        "executable": "cf-fake-worker"})
        self.assertEqual((code, w["status"]), (200, "READY"))
        self.assertEqual(self.call("PUT", f"/api/workers/{w['id']}", {"name": "HTTP2"})[1]["name"], "HTTP2")
        self.assertEqual(self.call("POST", f"/api/workers/{w['id']}/disable")[1]["enabled"], False)
        self.assertEqual(self.call("POST", f"/api/workers/{w['id']}/enable")[1]["enabled"], True)
        self.assertEqual(self.call("POST", "/api/workers/scan")[0], 200)
        self.assertEqual(self.call("DELETE", f"/api/workers/{w['id']}")[1]["removed"], w["id"])

    def test_pools_and_routing_endpoints(self):
        code, w, _ = self.call("POST", "/api/workers", {"name": "A", "driver_id": "fake", "executable": "cf-a"})
        code, p, _ = self.call("POST", "/api/workers/pools", {"name": "p", "display_name": "P", "members": [w["id"]]})
        self.assertEqual((code, p["strategy"]), (200, "priority"))
        self.assertEqual(self.call("PUT", "/api/workers/routing", {"work_type": "story.write", "pool": "p"})[1]["pool"], "p")
        self.assertEqual(self.call("GET", "/api/workers/routing")[1]["routing"]["story.write"]["pool"], "p")
        self.assertEqual(self.call("DELETE", "/api/workers/routing/story.write")[1]["deleted"], True)
        self.assertEqual(self.call("DELETE", "/api/workers/pools/p")[1]["deleted"], "p")

    def test_attempts_endpoint_filters_and_hides_token(self):
        store = self.app.workers.store
        store.add_attempt(Attempt(attempt_id="a1", work_type="story.write", worker_id="wkr_x", worker_name="A",
                                  driver_id="fake", job_id="000001", stage="story", state=AttemptState.FAILED,
                                  error_kind="QUOTA", session_id="tok-1", cost_usd=0.1))
        store.add_attempt(Attempt(attempt_id="a2", work_type="story.write", worker_id="wkr_y", worker_name="B",
                                  driver_id="fake", job_id="000001", stage="story", state=AttemptState.SUCCESS,
                                  promoted=True))
        store.add_attempt(Attempt(attempt_id="a3", work_type="overseer.review", worker_id="wkr_z", worker_name="C",
                                  driver_id="fake", job_id="000002", stage="story"))
        d = self.call("GET", "/api/workers/attempts?job_id=000001")[1]["attempts"]
        self.assertEqual({x["attempt_id"] for x in d}, {"a1", "a2"})
        self.assertTrue(any(x["session"] for x in d))
        self.assertNotIn("tok-1", json.dumps(d))
        d = self.call("GET", "/api/workers/attempts?stage=story")[1]["attempts"]
        self.assertEqual({x["attempt_id"] for x in d}, {"a1", "a2", "a3"})

    def test_wrong_driver_or_missing_worker_is_400(self):
        self.assertEqual(self.call("POST", "/api/workers", {"driver_id": "nope", "executable": "x"})[0], 400)
        self.assertEqual(self.call("PUT", "/api/workers/wkr_khong_co", {"name": "x"})[0], 400)


if __name__ == "__main__":
    unittest.main()