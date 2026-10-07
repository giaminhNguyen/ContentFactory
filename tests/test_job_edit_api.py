"""Sửa job qua facade + HTTP: bộ chọn pipeline do backend dựng (progress floor), hậu quả từng lựa chọn, Chạy tiếp, xóa job, job đã xóa → 404."""
import json
import urllib.error
import urllib.request

from contentfactory.contracts import StageError
from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator.webui import App, UiServer
from tests import test_ui
from tests.test_ui import URL


class EditViewTest(test_ui.UiCase):
    def stages(self, jid):
        return {s["id"]: s for s in self.svc.job_detail(jid)["edit"]["stages"]}

    def test_queued_job_every_stage_selectable_and_effect_text_by_status(self):
        jid = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "run": "story"})["job_id"]
        d = self.svc.job_detail(jid)
        e = d["edit"]
        self.assertEqual([s["id"] for s in e["stages"]], [s.name for s in P.STAGES])               # thứ tự lấy từ pipeline, không phải frontend
        self.assertTrue(all(s["selectable"] for s in e["stages"]))
        self.assertIn("chạy đến “Audio” rồi dừng", self.stages(jid)["audio"]["effect"])
        self.assertTrue(e["stages"][1]["is_target"])                                                # đích hiện tại = Truyện
        self.assertFalse(e["awaiting_run"])

    def test_completed_job_floor_disables_earlier_stages_and_save_waits_for_run(self):
        jid = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "run": "through_tts"})["job_id"]
        self.o.run()
        by = self.stages(jid)
        for sid in ("source", "story"):
            self.assertFalse(by[sid]["selectable"])
            self.assertIn("không đặt đích", by[sid]["reason"])
        self.assertEqual((by["tts"]["is_floor"], by["tts"]["selectable"]), (True, True))
        self.assertIn("Job sẽ không tự chạy", by["audio"]["effect"])
        with self.assertRaises(StageError) as cm:                                                   # backend là authority, không tin giao diện
            self.svc.update_target(jid, {"target_stage": "story"})
        self.assertEqual(cm.exception.code, "PIPELINE_TARGET_BEFORE_PROGRESS")
        r = self.svc.update_target(jid, {"target_stage": "audio"})
        self.assertTrue(r["held"])
        self.assertIn("Chạy tiếp", r["message"])
        d = self.svc.job_detail(jid)
        self.assertEqual((d["status"], d["edit"]["awaiting_run"], d["actions"]["unpause"]), ("paused", True, True))
        self.assertIn("Chạy tiếp", d["edit"]["awaiting_text"])
        self.o.run()
        self.assertNotIn("audio", self.runs(self.o, jid))                                           # Lưu không chạy
        self.svc.resume(jid)
        self.o.run()
        self.assertEqual(self.svc.job_detail(jid)["status"], "completed")
        self.assertEqual(self.runs(self.o, jid)["source"], ["succeeded"])                           # không chạy lại bước cũ

    def test_cancelled_job_offers_no_target(self):
        jid = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "run": "story"})["job_id"]
        self.svc.cancel(jid)
        e = self.svc.job_detail(jid)["edit"]
        self.assertFalse(e["can_update"])
        self.assertTrue(all(not s["selectable"] for s in e["stages"]))

    def test_delete_service_is_idempotent_and_hides_job(self):
        jid = self.run_job(run="story")
        r = self.svc.delete_job(jid)
        self.assertEqual((r["result"], r["was_running"]), ("deleted", False))
        self.assertIn("output", r["message"])
        self.assertEqual(self.svc.delete_job(jid)["result"], "already")
        self.assertEqual(self.svc.delete_job("999999")["result"], "gone")
        with self.assertRaises(StageError) as cm:
            self.svc.job_detail(jid)
        self.assertEqual(cm.exception.code, "JOB_NOT_FOUND")
        self.assertNotIn(jid, [j["id"] for j in self.svc.list_jobs()["jobs"]])
        with self.assertRaises(StageError):
            self.svc.update_target(jid, {"target_stage": "tts"})


class EditHttpTest(test_ui.UiCase):
    def setUp(self):
        super().setUp()
        self.app = App(self.o, run_loop=True, opener=lambda p: None)
        self.srv = UiServer(self.app, 0)
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.base = self.srv.url.rstrip("/")

    def call(self, method, path, body=None, token=True):
        h = {"Content-Type": "application/json", **({"X-CF-Token": self.app.token} if token else {})}
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}"), r.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else {}), e.headers

    def test_delete_endpoint_and_deleted_detail_is_404(self):
        self.app.stop()
        jid = self.call("POST", "/api/runs", {"input": {"value": URL}, "channel": "kenh", "run": "story"})[1]["job_id"]
        self.assertEqual(self.call("DELETE", f"/api/jobs/{jid}", token=False)[0], 401)
        code, r, _ = self.call("DELETE", f"/api/jobs/{jid}")
        self.assertEqual((code, r["result"]), (200, "deleted"))
        self.assertEqual(self.call("DELETE", f"/api/jobs/{jid}")[1]["result"], "already")            # trình duyệt gửi lại
        self.assertEqual(self.call("GET", f"/api/jobs/{jid}")[0], 404)
        self.assertEqual(self.call("PUT", f"/api/jobs/{jid}/target", {"target_stage": "tts"})[0], 404)
        self.assertNotIn(jid, [j["id"] for j in self.call("GET", "/api/jobs")[1]["jobs"]])

    def test_target_endpoint_rejects_rollback_with_domain_error(self):
        self.app.stop()
        jid = self.call("POST", "/api/runs", {"input": {"value": URL}, "channel": "kenh", "run": "through_tts"})[1]["job_id"]
        self.o.run()
        code, e, _ = self.call("PUT", f"/api/jobs/{jid}/target", {"target_stage": "story"})
        self.assertEqual((code, e["error"]["code"]), (400, "PIPELINE_TARGET_BEFORE_PROGRESS"))
        self.assertEqual(self.call("GET", f"/api/jobs/{jid}")[1]["mode"]["target"], "tts")           # DB không đổi
