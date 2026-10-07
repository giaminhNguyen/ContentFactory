"""Agent Plan Phase 10: hardening — tương thích ngược/migration, restart/crash, đồng thời/idempotency, bảo mật, hiệu năng.

Chỉ thêm những gì CHƯA có test ở nơi khác (ví dụ: two-workers-claim, batch-concurrent, pending-revision-restart, mid-render kill đã có ở test_batches/test_job_control/
test_render); các test dưới đây lấp khoảng trống còn lại của mục 13 trong kế hoạch."""
import ast
import json
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from contentfactory.contracts import StageError
from contentfactory.jobs import pipeline as P
from contentfactory.jobs.db import SCHEMA, SCHEMA_VERSION, JobStore
from contentfactory.jobs.workspace import job_dir
from contentfactory.orchestrator import channels as CH
from contentfactory.orchestrator import revisions as REV
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.runner import Orchestrator
from contentfactory.orchestrator.service import Service
from tests.support import RootCase, REPO, params, wait_until
from tests.test_automode import write_channel, write_config
from tests.test_batches import BatchCase, FakeYouTube, discovery, entry
from tests.test_image_pools import PoolCase, fill
from tests.test_job_control import spec
from tests.test_templates_api import _Http
from tests.test_ui import URL, UiCase

SRC = REPO / "src" / "contentfactory"


# =============================================================================================== 13.1 tương thích ngược / migration
class LegacyCompatTest(RootCase):
    def legacy_db(self, rows):
        db = load_config(self.root).path("db")
        db.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(db)
        c.executescript(SCHEMA)                                                           # lược đồ v0: trước mọi migration (không có pipeline/control/batch)
        for jid, seq, state, p in rows:
            c.execute("INSERT INTO jobs(id,seq,created_at,updated_at,state,params) VALUES(?,?,?,?,?,?)", (jid, seq, 100.0 + seq, 100.0 + seq, state, json.dumps(p, ensure_ascii=False)))
        c.commit()
        c.close()
        return db

    def test_old_database_opens_migrates_idempotently_and_old_jobs_stay_single(self):
        db = self.legacy_db([("000001", 1, P.NEW, {**params(), "title": "Job cũ A", "channel": "kenh_cu"}), ("000002", 2, P.PUBLISHED, {**params(), "title": "Job cũ B"})])
        results = []

        def open_it():                                                                    # nhiều tiến trình khởi động cùng lúc: migration tuần tự hoá, không lỗi
            results.append(JobStore(db).schema_version())
        ts = [threading.Thread(target=open_it) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results, [SCHEMA_VERSION] * 4)
        st = JobStore(db)
        self.assertEqual(st.schema_version(), SCHEMA_VERSION)
        j1 = st.get_job("000001")
        self.assertEqual((j1["batch_id"], j1["pipeline"], j1["control_state"], j1["channel_id"]), (None, None, "RUNNING", "kenh_cu"))      # Single Job, chưa có spec v2, không tạm dừng
        self.assertEqual(st.image_pool_update("p", lambda s: (s, {"n": 1})), None)       # bảng v6 có mặt
        self.assertEqual(len(list(db.parent.glob("*.bak-v0"))), 1)                        # đã sao lưu nhất quán trước khi sửa

    def test_legacy_job_maps_deterministically_to_a_spec_and_renders_in_the_ui(self):
        self.legacy_db([("000001", 1, P.NEW, {**params(), "title": "Job cũ"})])
        orc = self.orc()
        svc = Service(orc)
        j = orc.store.get_job("000001")
        cur = REV.current_pipeline(j, set())
        self.assertEqual(cur["run"], [st.name for st in P.STAGES])                          # job kiểu cũ (không start/target) = FULL
        self.assertEqual(REV.current_pipeline(j, set()), REV.current_pipeline(j, set()))     # tất định
        d = svc.job_detail("000001")
        self.assertEqual((d["status"], d["thumbnail"], d["batch"], d["control"]["state"]), ("queued", None, None, "RUNNING"))
        self.assertEqual(len(d["pipeline"]), 8)
        self.assertTrue(all(p["timeline"] in d["timeline_legend"] and p["why"] for p in d["pipeline"]))
        rows = svc.list_jobs()["jobs"]
        self.assertEqual([(r["id"], r["type"]) for r in rows], [("000001", "job")])        # hiển thị như Job đơn
        orc.run()
        self.assertEqual(orc.store.get_job("000001")["state"], P.PUBLISHED)               # và chạy tới cùng như trước

    def test_old_channel_config_and_unknown_keys_still_resolve(self):
        write_channel(self.root, "kenh_cu", {"name": "Kênh Cũ", "title_template": "{project_title} - Tập {sequence}", "publishing": {"made_for_kids": False},
                                              "thumbnail": {"legacy_only": True}, "mot_khoa_la": {"x": 1}})
        ch = CH.load_channel(load_config(self.root), "kenh_cu")
        self.assertEqual((ch["name"], ch["thumbnail"], ch["templates"]), ("Kênh Cũ", {"legacy_only": True}, {}))       # không có pool ảnh/template: hành vi cũ
        orc = self.orc()
        jid = orc.submit(params(channel="kenh_cu"), pipeline=spec("render_youtube"))
        self.assertNotIn("thumbnail_source", orc.store.get_job(jid)["params"])

    def test_old_job_snapshot_of_a_deleted_template_keeps_rendering(self):
        orc = self.orc()
        api = orc.adapters["render"].templates
        api.create_draft(type="thumbnail", id="thumb_cu", name="Cũ")
        write_channel(self.root, "kenh_t", {"name": "T", "publishing": {"made_for_kids": False}, "templates": {"thumbnail": "thumb_cu"}})
        jid = orc.submit(params(channel="kenh_t"), pipeline=spec("render_youtube"))
        snap = orc.store.get_job(jid)["params"]["templates"]["thumbnail"]
        self.assertEqual((snap["id"], snap["version"]), ("thumb_cu", 1))
        orc.adapters["render"].templates.delete_template(id="thumb_cu")                  # xoá template SAU khi job đã chốt snapshot
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.BY_NAME["render_youtube"].done_state)            # job cũ vẫn dựng được từ snapshot

    def test_output_package_without_newer_fields_is_still_readable(self):
        orc = self.orc()
        svc = Service(orc)
        jid = orc.submit(params(channel="default"))                                         # chạy tới hết gói output (và đăng giả)
        orc.run()
        art = next(a for a in orc.store.artifacts(jid) if a["kind"] == "output_package")
        p = job_dir(self.root / "workspace", jid) / art["path"]
        pkg = json.loads(p.read_text(encoding="utf-8"))
        for k in ("version", "files", "reused"):                                           # gói kiểu cũ thiếu các trường mới
            pkg.pop(k, None)
        p.write_text(json.dumps(pkg), encoding="utf-8")
        svc._outputs.clear()
        info = svc.output_info(jid)
        self.assertTrue(info["project_dir"])
        self.assertEqual((info.get("files"), info["tiktok_parts"]), ([], 0))


# =============================================================================================== 13.2 restart / crash còn thiếu
class CrashRestartTest(PoolCase):
    def test_manual_pause_survives_restart_and_is_not_claimed(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="STORY_ONLY")
        orc.pause_job(jid)
        orc2 = self.orc()                                                                  # "khởi động lại": object mới, cùng DB
        orc2.run()
        j = orc2.store.get_job(jid)
        self.assertEqual((j["state"], j["control_state"], orc2.store.stage_runs(jid)), (P.NEW, "PAUSED", []))
        orc2.resume(jid)
        orc2.run()
        self.assertEqual(orc2.store.get_job(jid)["state"], P.STORY_READY)

    def test_crash_between_image_reservation_and_job_commit_leaves_nothing_behind(self):
        orc = self.setup_pool(4)
        boom = {"n": 0}
        real = orc.store.set_params

        def flaky(*a, **k):
            boom["n"] += 1
            raise OSError("đĩa đầy giả lập")
        orc.store.set_params = flaky
        with self.assertRaises(OSError):
            orc.submit(params(channel="kenh_p"), pipeline=spec("render_youtube"))
        orc.store.set_params = real
        self.assertEqual((boom["n"], orc.store.list_jobs()), (1, []))                      # job nửa vời bị gỡ
        self.assertEqual([p.name for p in (self.root / "workspace").glob("job_*")], [])    # workspace + ảnh đã chép cũng bị dọn
        jid = orc.submit(params(channel="kenh_p"), pipeline=spec("render_youtube"))        # lần sau vẫn chạy bình thường
        self.assertTrue((job_dir(self.root / "workspace", jid) / orc.store.get_job(jid)["params"]["thumbnail_source"]["file"]).is_file())

    def test_template_preview_never_touches_the_saved_draft_even_with_concurrent_saves(self):
        from tests.test_templates_api import PNG_MAGIC
        orc = self.orc()
        api = orc.adapters["render"].templates
        api.create_draft(type="video", id="tp_conc", name="Conc")
        errors = []
        d = Path(tempfile.mkdtemp(prefix="cf-pv-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "previews").mkdir()
        (d / "previews" / "x.png").write_bytes(PNG_MAGIC)
        api.preview = lambda **kw: {"path": str(d / "previews" / "x.png"), "canvas": [1, 1], "warnings": []}
        from contentfactory.orchestrator.service_templates import TemplateService
        ts = TemplateService(orc)
        base = api.get_template(id="tp_conc", version=1)["template"]

        def saver():
            for i in range(25):
                doc = json.loads(json.dumps(base))
                doc["description"] = f"bản {i}"
                try:
                    ts.save("tp_conc", 1, doc)
                except Exception as e:                                                      # noqa: BLE001
                    errors.append(repr(e))

        def previewer():
            for i in range(25):
                doc = json.loads(json.dumps(base))
                doc["description"] = f"CHƯA LƯU {i}"
                try:
                    ts.preview("tp_conc", doc)
                except Exception as e:                                                      # noqa: BLE001
                    errors.append(repr(e))
        t = [threading.Thread(target=saver), threading.Thread(target=previewer), threading.Thread(target=previewer)]
        [x.start() for x in t]
        [x.join() for x in t]
        self.assertEqual(errors, [])
        final = api.get_template(id="tp_conc", version=1)["template"]
        self.assertEqual(final["description"], "bản 24")                                    # xem trước không bao giờ ghi vào bản nháp; bản cuối là lần lưu cuối


# =============================================================================================== 13.3 đồng thời / idempotency
class ConcurrencyTest(UiCase):
    def test_double_template_delete_is_idempotent_under_threads(self):
        from contentfactory.orchestrator.service_templates import TemplateService
        ts = TemplateService(self.o)
        ts.create("thumbnail", "thumb_dbl", "Dbl")
        out, errs = [], []

        def rm():
            try:
                out.append(ts.delete("thumb_dbl"))
            except Exception as e:                                                          # noqa: BLE001
                errs.append(repr(e))
        t = [threading.Thread(target=rm) for _ in range(4)]
        [x.start() for x in t]
        [x.join() for x in t]
        self.assertEqual(errs, [])
        self.assertEqual(sum(1 for r in out if not r.get("already_deleted")), 1)           # đúng MỘT lần xoá thật, còn lại an toàn
        self.assertNotIn("thumb_dbl", [r["id"] for r in ts.overview(None)["templates"]])

    def test_double_run_from_two_threads_makes_one_job(self):
        res, errs = [], []

        def go():
            try:
                res.append(self.svc.create_run({"request_id": "dup-1", "input": {"value": URL}, "channel": "kenh", "run": "story"}))
            except Exception as e:                                                          # noqa: BLE001
                errs.append(repr(e))
        t = [threading.Thread(target=go) for _ in range(6)]
        [x.start() for x in t]
        [x.join() for x in t]
        self.assertEqual(errs, [])
        self.assertEqual(len({r["job_id"] for r in res}), 1)
        self.assertEqual(len(self.o.store.list_jobs()), 1)

    def test_double_pause_resume_from_threads_end_in_a_consistent_state(self):
        jid = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "run": "story"})["job_id"]
        t = [threading.Thread(target=lambda: self.svc.pause(jid)) for _ in range(6)]
        [x.start() for x in t]
        [x.join() for x in t]
        self.assertEqual(self.o.store.get_job(jid)["control_state"], "PAUSED")
        t = [threading.Thread(target=lambda: self.svc.resume(jid)) for _ in range(6)]
        [x.start() for x in t]
        [x.join() for x in t]
        self.assertEqual(self.o.store.get_job(jid)["control_state"], "RUNNING")


# =============================================================================================== 13.4 bảo mật / an toàn
class SecurityTest(UiCase):
    SECRET = "SEKRET-TOKEN-9f3a"

    def test_no_shell_true_or_os_system_anywhere_in_the_source(self):
        bad = []
        for f in SRC.rglob("*.py"):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, ast.keyword) and n.arg == "shell" and isinstance(n.value, ast.Constant) and n.value.value is True:
                    bad.append(f"{f.relative_to(SRC)}:{n.value.lineno}: shell=True")
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("system", "popen") and isinstance(n.func.value, ast.Name) and n.func.value.id == "os":
                    bad.append(f"{f.relative_to(SRC)}:{n.lineno}: os.{n.func.attr}")
        self.assertEqual(bad, [])                                                           # giá trị người dùng/template không bao giờ vào một chuỗi lệnh shell

    def test_secrets_do_not_leak_into_snapshots_manifests_api_or_logs(self):
        write_config(self.root, youtube={"api_key": self.SECRET}, adapter_config={"tts": {"auth_token": self.SECRET}}, tools={"contentflow": {"password": self.SECRET}})
        self.o = self.orc()
        self.svc = Service(self.o)
        jid = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "run": "story"})["job_id"]
        self.o.run()
        blob = [json.dumps(self.o.store.get_job(jid), default=str), json.dumps(self.svc.job_detail(jid), default=str), json.dumps(self.svc.list_jobs(), default=str),
                json.dumps(self.svc.dashboard(), default=str)]
        for f in list((self.root / "workspace").rglob("*.json")) + list((self.root / "runtime").rglob("*")):
            if f.is_file():
                blob.append(f.read_text(encoding="utf-8", errors="ignore"))
        leaked = [i for i, b in enumerate(blob) if self.SECRET in b]
        self.assertEqual(leaked, [])
        from contentfactory.orchestrator.service_admin import AdminService
        self.assertNotIn(self.SECRET, json.dumps(AdminService(self.o).get_settings(), default=str))      # trang Cài đặt cũng che

    def test_only_safe_canonical_links_reach_the_api(self):
        for evil in ("javascript:alert(1)", "http://www.youtube.com/watch?v=aaaaaaaaaaa", "https://evil.example/watch?v=aaaaaaaaaaa", "https://u:p@www.youtube.com/x", "//youtube.com/x", None):
            self.assertIsNone(Service._safe_yt(evil), evil)


class PreviewEndpointTest(_Http):
    def test_static_and_preview_endpoints_never_serve_arbitrary_paths(self):
        for evil in ("/../../config/config.json", "/..%2F..%2Fconfig%2Fconfig.json", "/js/../../config.py", "/%2e%2e/%2e%2e/HANDOFF.md",
                     "/api/templates/files/previews/..%2F..%2Fconfig.json", "/api/templates/files/previews/%2e%2e", "/api/templates/files/..%2Fprevious/x.png",
                     "/api/image-pools/x/images/..%2F..%2Fa", "/api/jobs/..%2F..%2Fx/thumbnail-source"):
            c, body = self.call("GET", evil)
            self.assertIn(c, (400, 403, 404), evil)
            self.assertNotIn(b"cf-token", body if isinstance(body, bytes) else json.dumps(body).encode())


# =============================================================================================== 13.5 hiệu năng
class PerformanceTest(BatchCase):
    def count_queries(self, store, fn):
        n = {"q": 0}
        real = JobStore._connect

        def traced(self_):
            c = real(self_)
            c.set_trace_callback(lambda s: n.__setitem__("q", n["q"] + 1))
            return c
        JobStore._connect = traced
        try:
            fn()
        finally:
            JobStore._connect = real
        return n["q"]

    def test_job_list_query_count_does_not_grow_with_batch_children(self):
        orc, bs, _ = self.make(FakeYouTube(videos=[entry(i) for i in range(60, 0, -1)]))
        svc = Service(orc)
        bs.create(self.payload(selection={"mode": "newest", "n": 3}, request_id="a"))
        small = self.count_queries(orc.store, lambda: svc.list_jobs(limit=30))
        for k in range(4):
            bs.create(self.payload(selection={"mode": "newest", "n": 10}, request_id=f"b{k}"))
        big = self.count_queries(orc.store, lambda: svc.list_jobs(limit=30))
        self.assertEqual(len(orc.store.list_jobs()), 43)
        self.assertLessEqual(big - small, 8 * 4)                                           # thêm 4 batch (40 job con) chỉ thêm truy vấn theo batch, KHÔNG theo từng job con
        self.assertLess(big, 120)

    def test_batch_and_job_lookups_use_indexes(self):
        orc = self.orc()
        plan = lambda sql: " ".join(str(tuple(r)) for r in orc.store._q("EXPLAIN QUERY PLAN " + sql))      # noqa: E731
        self.assertIn("jobs_batch", plan("SELECT id FROM jobs WHERE batch_id='B1'"))
        self.assertIn("jobs_source", plan("SELECT id FROM jobs WHERE source_key='youtube:x' AND channel_id='k'"))
        self.assertIn("batch_items_status", plan("SELECT 1 FROM batch_items WHERE status='pending'"))
        self.assertIn("stage_runs_job", plan("SELECT * FROM stage_runs WHERE job_id='1' AND stage='tts'"))

    def test_large_discovery_is_capped_and_paginated(self):
        orc, bs, fake = self.make(FakeYouTube(videos=[entry(i) for i in range(900, 0, -1)]), max_scan=300, hard_max=500)
        d = bs.create(self.payload(selection={"mode": "newest", "n": 500}, confirm_large=True))
        self.assertLessEqual(d["counts"]["total"], 500)
        self.assertTrue(all((end or 0) <= 300 + 1 for _, end in fake.calls))                # không bao giờ kéo cả kênh về
        self.assertEqual(Service(orc).list_jobs(limit=30)["limit"], 30)
        self.assertLessEqual(len(Service(orc).list_jobs(limit=30)["jobs"]), 30)


class StaticBundleTest(RootCase):
    def test_frontend_bundle_stays_small_and_offline(self):
        static = SRC / "orchestrator" / "webui_static"
        total = sum(f.stat().st_size for f in static.rglob("*") if f.is_file() and f.suffix in (".js", ".css", ".html"))
        self.assertLess(total, 900 * 1024)
        html = (static / "index.html").read_text(encoding="utf-8")
        clean = html.replace("http://www.w3.org/2000/svg", "")                              # chỉ namespace SVG của favicon nhúng (không phải tài nguyên mạng)
        self.assertNotRegex(clean, r"https?://")                                            # không CDN/phông ngoài: chạy offline
        for f in static.rglob("*.js"):
            if "vendor" in f.parts:
                continue
            self.assertNotRegex(f.read_text(encoding="utf-8"), r"""import\s*\(?\s*['"]https?://""", f.name)


# =============================================================================================== 13.7 smoke chế độ cũ + hồi quy phát hiện trong Phase 10
class LegacySmokeTest(RootCase):
    def test_split_never_leaves_a_tail_shorter_than_the_audio_qa_floor(self):
        from contentfactory.audio.split import plan_split
        # target nhỏ (như `cf demo`) + audio dài ra do khoảng nghỉ Prosody: trước đây ra part cuối 0.21 s và audio QA coi là "rỗng"
        for total in (3.52, 3.45, 3.31, 4.58, 2.31):
            plan = plan_split(total, [{"t": t / 10, "kind": "sentence"} for t in range(5, int(total * 10), 5)], 1.0, min_part_sec=0.6)
            lens = [p["end"] - p["start"] for p in plan["parts"]]
            self.assertTrue(all(x >= 0.6 for x in lens), (total, lens))
            self.assertAlmostEqual(sum(lens), total, places=6)
        for total in (105.0, 95.0, 61.0):                                                     # mục tiêu thật (30 s): kế hoạch y hệt khi chưa có sàn tuyệt đối
            self.assertEqual(plan_split(total, [], 30.0, min_part_sec=0.6), plan_split(total, [], 30.0))
        self.assertEqual((plan_split(105.0, [], 30.0)["mode"], plan_split(105.0, [], 30.0)["n"]), ("tail", 4))

    def test_cf_demo_still_runs_the_whole_legacy_pipeline(self):
        from contentfactory.orchestrator import ops
        res = ops.demo(echo=lambda *a, **k: None)
        self.assertEqual(res["state"], P.PUBLISHED)
        self.assertTrue(res["output_dir"])


class TempCleanupTest(RootCase):
    def test_tts_temp_cleanup_survives_a_briefly_locked_file_and_never_raises(self):
        from contentfactory.adapters.command_tts import _remove_quietly
        f = self.root / "o.wav.stdout"
        h = open(f, "wb")                                                                    # Windows: file đang mở không xoá được => PermissionError
        threading.Timer(0.3, h.close).start()
        _remove_quietly(f)                                                                   # trước đây ném PermissionError và che mất TTS_TIMEOUT thật
        self.assertFalse(f.exists())
        _remove_quietly(None)
        _remove_quietly(self.root / "khong_co.tmp")
