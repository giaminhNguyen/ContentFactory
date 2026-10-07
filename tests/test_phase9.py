"""Agent Plan Phase 9: preflight theo pipeline, timeline/why, tìm kiếm + lọc, hành động hàng loạt mở rộng, dashboard nhẹ."""
import shutil
import tempfile
import time
from pathlib import Path

from contentfactory.contracts import StageError
from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator import preflight as PF
from contentfactory.orchestrator.service import Service
from tests.support import params
from tests.test_automode import write_channel, write_config
from tests.test_job_control import sleeper, spec
from tests.test_templates_api import _Http
from tests.test_ui import URL, UiCase


class PreflightTest(UiCase):
    def pv(self, **kw):
        self.o.__dict__.pop("_preflight_cache", None)
        return self.svc.preview_run({"input": {"value": URL}, "channel": "kenh", **kw})

    def ids(self, r):
        return {c["id"]: c for c in r["preflight"]["checks"]}

    def test_only_what_the_selected_stages_need_is_checked(self):
        full = self.pv(run="full")
        self.assertIn("adapter.publish", self.ids(full))
        self.assertIn("adapter.tts", self.ids(full))
        story = self.pv(run="story")
        c = self.ids(story)
        self.assertNotIn("adapter.publish", c)                                   # không đăng => không đòi đăng nhập/daemon YouTube
        self.assertNotIn("adapter.tts", c)                                       # không đọc => không đòi engine TTS
        self.assertNotIn("tool.ffmpeg", c)
        self.assertTrue(any("Đăng YouTube" in s for s in story["preflight"]["skipped"]))
        self.assertTrue(any("Giọng đọc" in s for s in story["preflight"]["skipped"]))

    def test_publish_outage_is_a_warning_not_a_blocker_and_is_stage_scoped(self):
        self.o.adapters["publish"].health = lambda: {"ok": False, "error": "daemon chưa chạy"}
        r = self.pv(run="full")
        pub = self.ids(r)["adapter.publish"]
        self.assertEqual((pub["status"], pub["stages"]), ("warn", ["publish"]))
        self.assertIn("daemon chưa chạy", pub["detail"])
        self.assertIn("tự chạy tiếp", pub["hint"])
        self.assertTrue(r["can_run"])                                            # tài nguyên tạm thời: job sẽ giữ lại và tự chạy tiếp
        nopub = self.pv(run="story")
        self.assertTrue(nopub["can_run"])
        self.assertNotIn("adapter.publish", self.ids(nopub))                     # cùng sự cố nhưng không đăng => không hiện

    def test_provided_audio_does_not_need_tts_engine(self):
        from tests.test_ui import write_wav
        self.o.adapters["tts"].health = lambda: {"ok": False, "error": "engine tắt"}
        self.o.__dict__.pop("_preflight_cache", None)
        provided = self.svc.preview_run({"input": {"value": str(write_wav(self.root / "a.wav"))}, "channel": "kenh", "run": "audio_package", "title": "Có sẵn audio"})
        self.assertNotIn("adapter.tts", self.ids(provided))                      # đã có audio hợp lệ => engine TTS hỏng không chặn/không cảnh báo
        self.assertTrue(provided["can_run"])
        link = self.pv(run="full")
        self.assertEqual(self.ids(link)["adapter.tts"]["status"], "warn")        # nhưng khi TTS thật sự sẽ chạy thì báo rõ
        self.assertTrue(link["can_run"])

    def test_branch_scoped_checks_for_a_single_render_branch(self):
        yt = self.pv(pipeline={"mode": "custom", "requested_stages": ["render_youtube"]})
        tt = self.pv(pipeline={"mode": "custom", "requested_stages": ["render_tiktok"]})
        self.assertIn("template.youtube", self.ids(yt))
        self.assertNotIn("template.tiktok", self.ids(yt))
        self.assertIn("template.tiktok", self.ids(tt))
        self.assertNotIn("template.youtube", self.ids(tt))
        self.assertNotIn("image_pool", self.ids(tt))

    def test_unusable_image_pool_blocks_run_with_a_reason(self):
        folder = Path(tempfile.mkdtemp(prefix="cf-ip-"))
        self.addCleanup(shutil.rmtree, folder, True)
        write_config(self.root, image_pools={"anime": {"folder": str(folder), "selection_mode": "shuffle"}})
        write_channel(self.root, "kenh_ip", {"name": "IP", "publishing": {"made_for_kids": False}, "thumbnail": {"image_pool": "anime"}})
        self.o = self.orc()
        self.svc = Service(self.o)
        r = self.svc.preview_run({"input": {"value": URL}, "channel": "kenh_ip", "run": "full"})
        row = self.ids(r)["image_pool"]
        self.assertEqual((row["status"], row["blocking"]), ("fail", True))
        self.assertFalse(r["can_run"])
        self.assertTrue(row["hint"])
        r2 = self.svc.preview_run({"input": {"value": URL}, "channel": "kenh_ip", "run": "story"})
        self.assertTrue(r2["can_run"])                                           # không render => không cần pool ảnh


class TimelineTest(UiCase):
    def test_every_step_has_timeline_branch_and_why(self):
        r = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "run": "full"})
        self.o.run()
        d = self.svc.job_detail(r["job_id"])
        for p in d["pipeline"]:
            self.assertIn(p["timeline"], d["timeline_legend"])
            self.assertTrue(p["why"])
        self.assertEqual([p["branch"] for p in d["pipeline"]], ["shared"] * 4 + ["youtube", "tiktok", "package", "youtube"])
        self.assertEqual({p["timeline"] for p in d["pipeline"]}, {"DONE"})
        self.assertEqual({p["state"] for p in d["pipeline"]}, {"done"})            # `state` cũ giữ nguyên (tương thích)

    def test_not_requested_branch_and_queued_vs_paused(self):
        jid = self.o.submit(params(channel="kenh"), pipeline=spec("render_tiktok"))
        by = {p["name"]: p for p in self.svc.job_detail(jid)["pipeline"]}
        self.assertEqual(by["render_youtube"]["timeline"], "NOT_REQUESTED")
        self.assertIn("không bước nào", by["render_youtube"]["why"])
        self.assertEqual((by["source"]["timeline"], by["render_tiktok"]["timeline"]), ("QUEUED", "QUEUED"))
        self.assertIn("cần kết quả của nó", by["audio"]["why"])                      # bước bị kéo vào vì phụ thuộc: nói rõ vì bước nào cần
        self.o.pause_job(jid)
        by = {p["name"]: p for p in self.svc.job_detail(jid)["pipeline"]}
        self.assertEqual((by["source"]["timeline"], by["render_tiktok"]["timeline"], by["render_youtube"]["timeline"]), ("PAUSED", "PAUSED", "NOT_REQUESTED"))
        self.assertIn("tạm dừng theo yêu cầu", by["source"]["why"])

    def test_provided_artifact_is_available(self):
        from tests.test_ui import write_wav
        r = self.svc.create_run({"input": {"value": str(write_wav(self.root / "a.wav"))}, "channel": "kenh", "run": "audio_package", "title": "Có sẵn audio"})
        self.o.run()
        d = self.svc.job_detail(r["job_id"])
        self.assertEqual([p["timeline"] for p in d["pipeline"]][:3], ["AVAILABLE", "NOT_REQUESTED", "AVAILABLE"])
        self.assertIn("đã có sẵn", d["pipeline"][0]["why"])

    def test_a_pending_change_marks_the_stale_step_invalidated(self):
        from tests.test_pause_resume import Loop
        from tests.support import wait_until
        jid = self.o.submit(params(channel="kenh", fake=sleeper("tts_chunk_3")), pipeline=spec("tts"))
        with Loop(self.o):
            wait_until(lambda: self.o.store.get_job(jid)["state"] == P.TTS_RUNNING, 30, "tts running")
            r = self.o.request_update(jid, params_patch={"story_profile": {"paragraphs": 3}})
            self.assertEqual(r["status"], "pending")
            by = {p["name"]: p for p in self.svc.job_detail(jid)["pipeline"]}
            self.assertEqual(by["story"]["timeline"], "INVALIDATED")                 # đã xong nhưng thay đổi đang chờ làm kết quả cũ không còn đúng
            self.assertIn("Sẽ chạy lại", by["story"]["why"])
            self.assertEqual(by["source"]["timeline"], "DONE")
            self.assertEqual(by["tts"]["timeline"], "RUNNING")
            self.o.pause_job(jid)


class ListFilterTest(UiCase):
    def make(self, url, title=None, channel="kenh"):
        body = {"input": {"value": url}, "channel": channel, "run": "story"}
        if title:
            body["title"] = title
        return self.svc.create_run(body)["job_id"]

    def test_search_status_kind_channel_days_and_counts_follow_the_filters(self):
        write_channel(self.root, "kenh2", {"name": "Kênh Hai", "publishing": {"made_for_kids": False}})
        a = self.make("https://www.youtube.com/watch?v=aaaaaaaaaaa", "Truyện Ma Đêm Khuya")
        b = self.make("https://www.youtube.com/watch?v=bbbbbbbbbbb", "Cổ tích", "kenh2")
        self.o.run()
        ids = lambda **kw: [x["id"] for x in self.svc.list_jobs(**kw)["jobs"]]          # noqa: E731
        self.assertEqual(set(ids()), {a, b})
        self.assertEqual(ids(q="ma đêm"), [a])                                           # tiêu đề, không phân biệt hoa thường
        self.assertEqual(ids(q="  TRUYỆN   ma "), [a])
        self.assertEqual(ids(q="bbbbbbbbbbb"), [b])                                      # mã video
        self.assertEqual(ids(q="youtube.com/watch?v=aaaaaaaaaaa"), [a])                  # URL
        self.assertEqual(ids(q=a), [a])                                                  # mã job
        self.assertEqual(ids(q="không có gì khớp"), [])
        self.assertEqual(ids(channel="kenh2"), [b])
        self.assertEqual(set(ids(kind="single")), {a, b})
        self.assertEqual(ids(kind="channel"), [])
        self.assertEqual(set(ids(days=1)), {a, b})
        with self.o.store._tx() as c:
            c.execute("UPDATE jobs SET created_at=? WHERE id=?", (time.time() - 40 * 86400, a))
        self.assertEqual(ids(days=7), [b])
        self.assertEqual(set(ids(days=0)), {a, b})                                       # 0 = không lọc theo ngày
        r = self.svc.list_jobs(q="cổ tích")
        self.assertEqual((r["counts"]["all"], r["counts"]["completed"]), (1, 1))         # số đếm khớp danh sách đã lọc
        v1 = self.svc.list_jobs(q="cổ tích")["version"]
        self.assertTrue(self.svc.list_jobs(q="cổ tích", since=v1) == {"changed": False, "version": v1})
        self.assertTrue(self.svc.list_jobs(q="khác", since=v1)["changed"])               # đổi bộ lọc không bao giờ bị coi là "không đổi"

    def test_channel_run_rows_match_by_source_and_child_titles(self):
        from tests.test_batches import FakeYouTube, discovery, entry
        bs = self.o.batch_service()
        bs._discovery = discovery(FakeYouTube(videos=[entry(2), entry(1, title="Chương Sáu Mươi")], title="Truyện ABC"))
        d = bs.create({"url": "@abc", "output_channel": "kenh", "run": "story", "request_id": "q1", "selection": {"mode": "newest", "n": 2}})
        single = self.make("https://www.youtube.com/watch?v=ccccccccccc", "Đơn lẻ")
        rows = lambda **kw: [x["id"] for x in self.svc.list_jobs(**kw)["jobs"]]         # noqa: E731
        self.assertEqual(rows(kind="channel"), [d["id"]])
        self.assertEqual(rows(kind="single"), [single])
        self.assertEqual(rows(q="chương sáu"), [d["id"]])                                # tìm theo tiêu đề video con
        self.assertEqual(rows(q="truyện abc"), [d["id"]])
        self.assertEqual(rows(channel="kenh", kind="channel"), [d["id"]])


class BulkAndDashboardTest(UiCase):
    def test_bulk_update_pipeline_reports_each_job_and_holds_finished_ones(self):
        live = self.o.submit(params(channel="kenh"), pipeline=spec("render_youtube", "render_tiktok", "output"))
        done = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "run": "story"})["job_id"]
        self.o.run()
        live2 = self.o.submit(params(channel="kenh", input={"kind": "youtube_url", "value": "https://youtu.be/zzzzzzzzzzz"}), pipeline=spec("render_youtube", "render_tiktok", "output"))
        r = self.svc.bulk("update_pipeline", [live2, done, "999999"], {"target_stage": "render_tiktok"})
        by = {x["job_id"]: x for x in r["results"]}
        self.assertEqual(by[live2]["result"], "done")
        self.assertEqual(by[done]["result"], "done")                                     # job đã xong: lưu đích mới, giữ chờ “Chạy tiếp”
        self.assertIn("Chạy tiếp", by[done]["reason"])
        self.assertEqual(self.o.store.get_job(done)["pause_origin"], "EDIT")
        self.assertEqual(by["999999"]["result"], "error")
        self.assertEqual(self.o.store.get_job(live2)["pipeline"]["requested_stages"], ["render_youtube", "render_tiktok"])
        again = self.svc.bulk("update_pipeline", [live2], {"target_stage": "render_tiktok"})
        self.assertEqual(again["results"][0]["result"], "skipped")                       # đã đúng như vậy: không ghi gì thừa
        with self.assertRaises(StageError):
            self.svc.bulk("update_pipeline", [live], {})

    def test_bulk_template_only_for_unfinished_idle_jobs(self):
        live = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "run": "full"})["job_id"]
        self.o.pause_job(live)
        done = self.svc.create_run({"input": {"value": "https://youtu.be/ddddddddddd"}, "channel": "kenh", "run": "full"})["job_id"]
        self.o.run()
        gold = self.o.adapters["render"].templates.list_templates()["templates"]
        self.assertTrue(any(t["id"] == "thumb_gold" for t in gold))
        r = self.svc.bulk("template", [live, done], {"kind": "thumbnail", "template_id": "thumb_gold"})
        by = {x["job_id"]: x for x in r["results"]}
        self.assertEqual(by[live]["result"], "done")
        self.assertEqual(self.o.store.get_job(live)["params"]["templates"]["thumbnail"]["id"], "thumb_gold")
        self.assertEqual(by[done]["result"], "skipped")
        self.assertEqual(self.o.store.get_job(done)["params"]["templates"]["thumbnail"]["id"], "thumb_default")     # job đã xong không bị sửa tại chỗ
        with self.assertRaises(StageError):
            self.svc.bulk("template", [live], {"kind": "thumbnail"})

    def test_dashboard_answers_whether_anything_needs_the_user(self):
        d0 = self.svc.dashboard()
        self.assertEqual((d0["attention"], d0["headline"]), (0, "Không có việc cần bạn xử lý"))
        ok = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "run": "story"})["job_id"]
        bad = self.o.submit(params(channel="kenh", fake={"story": {"error_class": "POLICY", "fail_until_attempt": 99}}), mode="STORY_ONLY")
        paused = self.o.submit(params(channel="kenh", input={"kind": "youtube_url", "value": "https://youtu.be/eeeeeeeeeee"}), mode="STORY_ONLY")
        self.o.pause_job(paused)
        self.o.run()
        d = self.svc.dashboard()
        self.assertEqual((d["completed_today"], d["attention"], d["paused"], d["running"]), (1, 1, 1, 0))
        self.assertEqual(d["headline"], "Có việc cần bạn xử lý")
        self.assertEqual([n["id"] for n in d["needs_attention"]], [bad])
        self.assertEqual({lane["id"] for lane in d["lanes"]}, {"gpu", "tts"})
        self.assertTrue(all(lane["limit"] >= 1 and lane["used"] == 0 for lane in d["lanes"]))
        self.assertTrue(d["disk"] and all(x["free_gb"] >= 0 for x in d["disk"]))


class Phase9HttpTest(_Http):
    def test_endpoints_filters_dashboard_bulk_and_preflight_over_http(self):
        r = self.call("POST", "/api/runs", {"input": {"value": URL}, "channel": "kenh", "run": "story", "title": "Truyện Thử Lọc"})[1]
        self.app.stop()
        c, d = self.call("GET", "/api/dashboard")
        self.assertEqual((c, sorted(d)[:3]), (200, ["attention", "batches_running", "completed_today"]))
        self.assertEqual(self.call("GET", "/api/jobs?q=th%E1%BB%AD%20l%E1%BB%8Dc")[1]["total"], 1)       # "thử lọc"
        self.assertEqual(self.call("GET", "/api/jobs?q=khong-khop")[1]["total"], 0)
        self.assertEqual(self.call("GET", "/api/jobs?kind=channel")[1]["total"], 0)
        self.assertEqual(self.call("GET", "/api/jobs?channel=kenh&days=7")[1]["total"], 1)
        c, b = self.call("POST", "/api/jobs/bulk", {"action": "update_pipeline", "job_ids": [r["job_id"]], "args": {"target_stage": "story"}})
        self.assertEqual((c, b["results"][0]["result"]), (200, "skipped"))                              # pipeline đã đúng như vậy
        c, e = self.call("POST", "/api/jobs/bulk", {"action": "template", "job_ids": [r["job_id"]], "args": {}})
        self.assertEqual((c, e["error"]["code"]), (400, "BULK_ARGS"))
        c, pv = self.call("POST", "/api/preview", {"input": {"value": URL}, "channel": "kenh", "run": "story"})
        self.assertEqual((c, pv["preflight"]["ok"], pv["can_run"]), (200, True, True))
        self.assertNotIn("adapter.publish", [x["id"] for x in pv["preflight"]["checks"]])
        d = self.call("GET", f"/api/jobs/{r['job_id']}")[1]
        self.assertEqual({p["timeline"] for p in d["pipeline"]}, {"QUEUED", "NOT_REQUESTED"})
