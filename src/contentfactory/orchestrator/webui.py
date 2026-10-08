"""Giao diện web cục bộ (D-89): `cf ui` = máy chủ HTTP (stdlib) + vòng lặp orchestrator trong cùng tiến trình + frontend tĩnh (không bước build).

An toàn cho ứng dụng cục bộ: chỉ nghe 127.0.0.1; kiểm tra `Host` (chống DNS-rebinding); mọi `/api` cần header `X-CF-Token` (token ngẫu nhiên mỗi lần chạy,
nhúng vào index.html) và với phương thức ghi thì `Origin` (nếu có) phải cùng nguồn (chống CSRF). Không có endpoint nhận đường dẫn tùy ý để mở/đọc.
Lỗi trả về dạng `{"error": {"code", "message", "hint"}}` bằng tiếng Việt dễ hiểu; stack trace chỉ nằm trong log (`runtime/logs/ui.log`).
"""
from __future__ import annotations

import json
import mimetypes
import re
import secrets
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..contracts import ErrorClass, StageError
from . import ops
from .config import load_config
from .runner import Orchestrator
from .service import Service
from .service_admin import AdminService
from .service_image_pools import ImagePoolService
from .service_templates import Raw, TemplateService
from .worker_admin import WorkerService

STATIC = Path(__file__).resolve().parent / "webui_static"
MAX_JSON = 1 << 20
MAX_ASSET = 60 << 20
VERSION = "1"

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")


class App:
    """Trạng thái dùng chung của một phiên `cf ui`: orchestrator, facade, token, vòng lặp nền."""

    def __init__(self, orc: Orchestrator, *, run_loop: bool = True, opener=ops.open_path, token: str | None = None) -> None:
        self.orc, self.cfg = orc, orc.cfg
        self.service, self.admin = Service(orc), AdminService(orc)
        self.templates = TemplateService(orc)
        self.watermarks = self.service.watermarks
        self.image_pools = ImagePoolService(orc, self.admin)
        self.workers = WorkerService(orc.cfg)
        self.token = token or secrets.token_urlsafe(24)
        self.opener = opener
        self.run_loop = run_loop
        self._stop = threading.Event()
        self._runner: threading.Thread | None = None
        self.port = 0

    # ------------------------------------------------------------------------------------------ nền
    def start_runner(self) -> None:
        if not self.run_loop or self._runner:
            return
        self._runner = threading.Thread(target=self._loop, name="orchestrator", daemon=True)
        self._runner.start()
        threading.Thread(target=self._uploader, name="uploader", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.orc.run(until_idle=False, stop=self._stop)
            except Exception:                                            # noqa: BLE001 - vòng lặp không được chết im lặng
                self.log_error("orchestrator loop crashed")
                self._stop.wait(2.0)

    def _uploader(self) -> None:
        try:
            ops.ensure_uploader(self.cfg, self.orc.adapters.get("publish"))
        except Exception:                                                # noqa: BLE001
            self.log_error("ensure_uploader")

    def runner_running(self) -> bool:
        return bool(self._runner and self._runner.is_alive())

    def stop(self) -> None:
        self._stop.set()
        if self._runner:
            self._runner.join(15)

    def log_error(self, what: str) -> None:
        try:
            f = self.cfg.path("runtime") / "logs" / "ui.log"
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {what}\n{traceback.format_exc()}\n")
        except OSError:
            pass

    # ------------------------------------------------------------------------------------------ route
    def bootstrap(self) -> dict:
        ch = self.service.list_channels()
        st = self.admin.get_settings()
        return {"version": VERSION, "channels": ch["channels"], "default_channel": ch["default"],
                "auto_resume_default": bool(self.cfg.data.get("auto_resume_default", True)), "runtime": self.admin.runtime_status(self.runner_running()),
                "root": str(self.cfg.root), "storage": st["storage"][:0]}


def _int(q: dict, key: str, default: int, lo: int = 0, hi: int = 1000) -> int:
    try:
        return max(lo, min(hi, int(q.get(key, [default])[0])))
    except (TypeError, ValueError):
        return default


ROUTES: list[tuple[str, re.Pattern, str]] = []
RAW_BODY = {"channel_asset", "asset_import", "watermark_upload", "watermark_replace"}                                 # PUT nhận byte thô (watermark, ảnh asset), không phải JSON


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn.__name__))
        return fn
    return deco


class Api:
    """Các handler; mỗi hàm nhận (app, match, query, body) và trả đối tượng JSON-able."""

    @route("GET", "/api/bootstrap")
    def bootstrap(app, m, q, b):
        return app.bootstrap()

    # ----- Worker Runtime (W1)
    @route("GET", "/api/workers")
    def workers_list(app, m, q, b):
        return {"drivers": app.workers.drivers(), "workers": app.workers.workers(), "pools": app.workers.pools()}

    @route("POST", "/api/workers/scan")
    def workers_scan(app, m, q, b):
        return app.workers.scan()

    @route("POST", "/api/workers")
    def worker_add(app, m, q, b):
        return app.workers.add(str(b.get("name") or ""), str(b.get("driver_id") or ""),
                               str(b.get("executable") or ""), models=b.get("models"),
                               probe=bool(b.get("probe", True)))

    @route("POST", r"/api/workers/(?P<id>wkr_\w+)/probe")
    def worker_probe(app, m, q, b):
        return app.workers.probe(m["id"])

    @route("PUT", r"/api/workers/(?P<id>wkr_\w+)")
    def worker_update(app, m, q, b):
        allowed = {k: b[k] for k in ("name", "executable", "enabled", "models", "concurrency",
                                     "timeout_s", "driver_id", "profiles") if k in b}
        return app.workers.update(m["id"], **allowed)

    @route("DELETE", r"/api/workers/(?P<id>wkr_\w+)")
    def worker_delete(app, m, q, b):
        return app.workers.remove(m["id"], force=bool(b.get("force")))

    @route("POST", r"/api/workers/(?P<id>wkr_\w+)/enable")
    def worker_enable(app, m, q, b):
        return app.workers.set_enabled(m["id"], True)

    @route("POST", r"/api/workers/(?P<id>wkr_\w+)/disable")
    def worker_disable(app, m, q, b):
        return app.workers.set_enabled(m["id"], False)

    @route("POST", "/api/workers/pools")
    def pool_create(app, m, q, b):
        return app.workers.create_pool(str(b.get("name") or ""), str(b.get("display_name") or ""),
                                       b.get("members"), str(b.get("strategy") or "priority"),
                                       enabled=bool(b.get("enabled", True)))

    @route("PUT", r"/api/workers/pools/(?P<name>[^/]+)")
    def pool_update(app, m, q, b):
        allowed = {k: b[k] for k in ("display_name", "strategy", "members", "enabled", "new_name") if k in b}
        return app.workers.update_pool(m["name"], **allowed)

    @route("DELETE", r"/api/workers/pools/(?P<name>[^/]+)")
    def pool_delete(app, m, q, b):
        return app.workers.delete_pool(m["name"], force=bool(b.get("force")))

    @route("GET", "/api/workers/routing")
    def routing_get(app, m, q, b):
        return app.workers.routing()

    @route("PUT", "/api/workers/routing")
    def routing_put(app, m, q, b):
        return app.workers.set_routing(str(b.get("work_type") or ""), str(b.get("pool") or ""),
                                       str(b.get("model_profile") or ""), b.get("policy"))

    @route("DELETE", r"/api/workers/routing/(?P<work_type>[^/]+)")
    def routing_delete(app, m, q, b):
        return {"deleted": app.workers.delete_routing(m["work_type"])}

    @route("GET", "/api/workers/attempts")
    def worker_attempts(app, m, q, b):
        return {"attempts": app.workers.attempts(job_id=q.get("job_id", [""])[0] or "",
                                                 work_type=q.get("work_type", [""])[0] or "",
                                                 stage=q.get("stage", [""])[0] or "",
                                                 limit=_int(q, "limit", 100, 1, 500))}

    @route("GET", "/api/runtime")
    def runtime(app, m, q, b):
        return app.admin.runtime_status(app.runner_running())

    @route("POST", "/api/detect")
    def detect(app, m, q, b):
        return app.service.detect_input(b.get("value", ""), b.get("kind"))

    @route("GET", "/api/pipeline")
    def pipeline(app, m, q, b):
        return app.service.pipeline_descriptor()

    @route("POST", "/api/pipeline/plan")
    def pipeline_plan(app, m, q, b):
        return app.service.plan_pipeline(b)

    @route("POST", "/api/sources/inspect")
    def source_inspect(app, m, q, b):
        return app.orc.batch_service().inspect(b.get("value", ""))

    @route("POST", "/api/sources/youtube/discover")
    def source_discover(app, m, q, b):
        return app.orc.batch_service().discover(b)

    @route("GET", "/api/batches")
    def batches(app, m, q, b):
        return app.orc.batch_service().list()

    @route("POST", "/api/batches")
    def batch_create(app, m, q, b):
        return app.orc.batch_service().create(b)

    @route("GET", r"/api/batches/(?P<id>B\d+)")
    def batch_detail(app, m, q, b):
        return app.orc.batch_service().detail(m["id"], q.get("status", [None])[0], _int(q, "limit", 50, 1, 200), _int(q, "offset", 0, 0, 10 ** 6))

    @route("POST", r"/api/batches/(?P<id>B\d+)/(?P<action>pause|resume|retry-failed|cancel-queued|cancel|rescan)")
    def batch_action(app, m, q, b):
        return getattr(app.orc.batch_service(), m["action"].replace("-", "_"))(m["id"])

    @route("POST", r"/api/batches/(?P<id>B\d+)/target")
    def batch_target(app, m, q, b):
        return app.orc.batch_service().update_pipeline(m["id"], str(b.get("target_stage") or ""), b.get("scope") or "unfinished", b.get("job_ids"))

    @route("POST", "/api/preview")
    def preview(app, m, q, b):
        return app.service.preview_run(b)

    @route("POST", "/api/runs")
    def create_run(app, m, q, b):
        return app.service.create_run(b)

    @route("GET", "/api/dashboard")
    def dashboard(app, m, q, b):
        return app.service.dashboard()

    @route("GET", "/api/jobs")
    def jobs(app, m, q, b):
        return app.service.list_jobs(q.get("status", ["all"])[0], _int(q, "limit", 30, 1, 200), _int(q, "offset", 0, 0, 10 ** 7), q.get("since", [None])[0],
                                     q=q.get("q", [None])[0], kind=q.get("kind", [None])[0], channel=q.get("channel", [None])[0], days=_int(q, "days", 0, 0, 3650))

    @route("GET", r"/api/jobs/(?P<id>[\w\-]+)")
    def job(app, m, q, b):
        return app.service.job_detail(m["id"])

    @route("GET", r"/api/jobs/(?P<id>[\w\-]+)/log")
    def job_log(app, m, q, b):
        before = q.get("before", [None])[0]
        return app.service.job_log(m["id"], _int(q, "tail", 150, 1, 500), int(before) if before else None)

    @route("POST", r"/api/jobs/(?P<id>[\w\-]+)/resume")
    def resume(app, m, q, b):
        return app.service.resume(m["id"], bool(b.get("now")))

    @route("POST", "/api/jobs/bulk")
    def jobs_bulk(app, m, q, b):
        return app.service.bulk(b.get("action", ""), b.get("job_ids") or [], b.get("args"))

    @route("POST", r"/api/jobs/(?P<id>[\w\-]+)/pause")
    def pause(app, m, q, b):
        return app.service.pause(m["id"])

    @route("POST", r"/api/jobs/(?P<id>[\w\-]+)/cancel")
    def cancel(app, m, q, b):
        return app.service.cancel(m["id"])

    @route("POST", r"/api/jobs/(?P<id>[\w\-]+)/pipeline-impact")
    def pipeline_impact(app, m, q, b):
        return app.service.preview_update(m["id"], b)

    @route("POST", r"/api/jobs/(?P<id>[\w\-]+)/pipeline-revisions")
    def pipeline_revisions(app, m, q, b):
        return app.service.request_update(m["id"], b)

    @route("PUT", r"/api/jobs/(?P<id>[\w\-]+)/target")
    def job_target(app, m, q, b):
        return app.service.update_target(m["id"], b)

    @route("DELETE", r"/api/jobs/(?P<id>[\w\-]+)")
    def job_delete(app, m, q, b):
        return app.service.delete_job(m["id"])

    @route("POST", r"/api/jobs/(?P<id>[\w\-]+)/clone")
    def clone(app, m, q, b):
        return app.service.clone(m["id"], b)

    @route("POST", r"/api/jobs/(?P<id>[\w\-]+)/reroll-thumbnail/impact")
    def reroll_impact(app, m, q, b):
        return app.service.reroll_preview(m["id"])

    @route("POST", r"/api/jobs/(?P<id>[\w\-]+)/reroll-thumbnail")
    def reroll_thumbnail(app, m, q, b):
        return app.service.reroll_thumbnail(m["id"])

    @route("GET", r"/api/jobs/(?P<id>[\w\-]+)/thumbnail-source")
    def thumbnail_source(app, m, q, b):
        return app.service.thumbnail_source_file(m["id"])

    @route("POST", r"/api/jobs/(?P<id>[\w\-]+)/retry")
    def retry(app, m, q, b):
        return app.service.retry(m["id"])

    @route("POST", r"/api/jobs/(?P<id>[\w\-]+)/auto-resume")
    def auto_resume(app, m, q, b):
        return app.service.set_auto_resume(m["id"], bool(b.get("enabled")))

    @route("POST", r"/api/jobs/(?P<id>[\w\-]+)/open-output")
    def open_output(app, m, q, b):
        return app.service.open_output(m["id"], app.opener)

    @route("GET", "/api/channels")
    def channels(app, m, q, b):
        return app.service.list_channels()

    @route("POST", "/api/channels")
    def channel_create(app, m, q, b):
        return app.service.create_channel(str(b.get("id") or ""), b.get("name"), bool(b.get("kids")), int(b.get("last_used") or 0))

    @route("GET", r"/api/channels/(?P<id>[\w\-]+)")
    def channel_get(app, m, q, b):
        return app.service.get_channel(m["id"])

    @route("PUT", r"/api/channels/(?P<id>[\w\-]+)")
    def channel_put(app, m, q, b):
        return app.service.save_channel(m["id"], b.get("raw"))

    @route("GET", r"/api/channels/(?P<id>[\w\-]+)/preview")
    def channel_preview(app, m, q, b):
        return app.service.channel_preview(m["id"], q.get("title", [None])[0])

    @route("PUT", r"/api/channels/(?P<id>[\w\-]+)/asset")
    def channel_asset(app, m, q, b):
        return app.service.save_channel_asset(m["id"], q.get("name", [""])[0], b)

    # ------------------------------------------------------------------------------------------ Watermark Library (End-to-End Task, Phần A)
    def _watermark_task(app, channel_id: str, fn) -> dict:
        """Tổng hợp giọng có thể chậm: chạy ở tác vụ nền (giao diện theo dõi bằng /api/tasks/<id>, tiến trình thật = trạng thái tác vụ). Mỗi kênh một lượt."""
        kind = f"watermark:{channel_id}"
        if app.admin.tasks.running(kind):
            raise StageError(ErrorClass.POLICY, "WATERMARK_BUSY", "Kênh này đang tạo một watermark.", {"hint": "Đợi bản đang tạo xong rồi thử lại."}, resource="input")
        return {"task": app.admin.tasks.start(kind, fn)}

    @route("GET", r"/api/channels/(?P<id>[\w\-]+)/watermarks")
    def watermark_list(app, m, q, b):
        return app.watermarks.overview(m["id"], q.get("archived", ["0"])[0] == "1")

    @route("POST", r"/api/channels/(?P<id>[\w\-]+)/watermarks")
    def watermark_create(app, m, q, b):
        cid, svc = m["id"], app.watermarks
        p = svc.prepare(cid, None, b.get("name"), b.get("text"), b.get("tts"))                # lỗi nhập liệu/profile/engine báo NGAY, không mở tác vụ
        return Api._watermark_task(app, cid, lambda: svc.create_tts(cid, p["name"], p["text"], p["selection"], activate=bool(b.get("activate")), request_id=b.get("request_id")))

    @route("PUT", r"/api/channels/(?P<id>[\w\-]+)/watermarks/upload")
    def watermark_upload(app, m, q, b):
        return app.watermarks.create_upload(m["id"], q.get("name", [""])[0], q.get("filename", [""])[0], b, activate=q.get("activate", ["0"])[0] == "1",
                                            request_id=q.get("request_id", [None])[0])

    @route("GET", r"/api/channels/(?P<id>[\w\-]+)/watermarks/(?P<wm>wm_[a-z0-9]{8}|legacy)")
    def watermark_get(app, m, q, b):
        return app.watermarks.get(m["id"], m["wm"])

    @route("PUT", r"/api/channels/(?P<id>[\w\-]+)/watermarks/(?P<wm>wm_[a-z0-9]{8})")
    def watermark_update(app, m, q, b):
        cid, wm, svc = m["id"], m["wm"], app.watermarks
        if "text" not in b and "tts" not in b:
            return {"result": "unchanged", "item": svc.rename(cid, wm, b.get("name"))}
        p = svc.prepare(cid, wm, b.get("name"), b.get("text"), b.get("tts"))
        if p["unchanged"]:                                                                      # chỉ đổi tên hoặc không đổi gì ảnh hưởng âm thanh: không cần tổng hợp
            return svc.update_tts(cid, wm, name=b.get("name"), text=b.get("text"), selection=b.get("tts"))
        return Api._watermark_task(app, cid, lambda: svc.update_tts(cid, wm, name=b.get("name"), text=b.get("text"), selection=b.get("tts")))

    @route("PUT", r"/api/channels/(?P<id>[\w\-]+)/watermarks/(?P<wm>wm_[a-z0-9]{8})/file")
    def watermark_replace(app, m, q, b):
        return app.watermarks.replace_upload(m["id"], m["wm"], q.get("filename", [""])[0], b, q.get("name", [None])[0])

    @route("POST", r"/api/channels/(?P<id>[\w\-]+)/watermarks/(?P<wm>wm_[a-z0-9]{8})/regenerate")
    def watermark_regenerate(app, m, q, b):
        cid, wm, svc = m["id"], m["wm"], app.watermarks
        p = svc.prepare(cid, wm, None, None, None)
        if p["unchanged"]:
            return svc.regenerate(cid, wm)
        return Api._watermark_task(app, cid, lambda: svc.regenerate(cid, wm))

    @route("POST", r"/api/channels/(?P<id>[\w\-]+)/watermarks/(?P<wm>wm_[a-z0-9]{8}|legacy)/activate")
    def watermark_activate(app, m, q, b):
        rev = b.get("revision")
        return app.watermarks.activate(m["id"], m["wm"], int(rev) if rev else None)

    @route("POST", r"/api/channels/(?P<id>[\w\-]+)/watermarks/(?P<wm>wm_[a-z0-9]{8})/restore")
    def watermark_restore(app, m, q, b):
        return app.watermarks.restore(m["id"], m["wm"])

    @route("POST", r"/api/channels/(?P<id>[\w\-]+)/watermark/deactivate")
    def watermark_deactivate(app, m, q, b):
        return app.watermarks.deactivate(m["id"])

    @route("DELETE", r"/api/channels/(?P<id>[\w\-]+)/watermarks/(?P<wm>wm_[a-z0-9]{8}|legacy)")
    def watermark_delete(app, m, q, b):
        return app.watermarks.delete(m["id"], m["wm"], q.get("unset", ["0"])[0] == "1")

    @route("GET", r"/api/channels/(?P<id>[\w\-]+)/watermarks/(?P<wm>wm_[a-z0-9]{8}|legacy)/audio")
    def watermark_audio(app, m, q, b):
        rev = q.get("revision", [None])[0]
        return app.watermarks.audio(m["id"], m["wm"], int(rev) if rev and rev.isdigit() else None)

    # ------------------------------------------------------------------------------------------ Template / asset (Template Studio)
    @route("GET", "/api/templates")
    def templates(app, m, q, b):
        return app.templates.overview(q.get("type", [None])[0], q.get("archived", ["0"])[0] == "1")

    @route("GET", "/api/templates/preview-sources")
    def template_preview_sources(app, m, q, b):
        return app.templates.preview_sources(q.get("type", ["thumbnail"])[0])

    @route("GET", "/api/templates/options")
    def template_options(app, m, q, b):
        return app.templates.options()

    @route("POST", "/api/templates")
    def template_create(app, m, q, b):
        return app.templates.create(str(b.get("type") or ""), str(b.get("id") or ""), str(b.get("name") or ""), str(b.get("description") or ""), b.get("width"), b.get("height"))

    @route("GET", r"/api/templates/files/(?P<kind>[a-z_]+)/(?P<rel>[\w\-.]+(?:/[\w\-.]+)?)")
    def template_file(app, m, q, b):
        return app.templates.file(m["kind"], m["rel"])

    @route("GET", r"/api/templates/(?P<id>[a-z0-9_]+)")
    def template_get(app, m, q, b):
        v = q.get("version", [None])[0]
        return app.templates.get(m["id"], int(v) if v and v.isdigit() else v)

    @route("PUT", r"/api/templates/(?P<id>[a-z0-9_]+)/(?P<ver>\d+)")
    def template_save(app, m, q, b):
        return app.templates.save(m["id"], int(m["ver"]), b.get("template") or {})

    @route("DELETE", r"/api/templates/(?P<id>[a-z0-9_]+)/(?P<ver>\d+)")
    def template_delete_draft(app, m, q, b):
        return app.templates.delete_draft(m["id"], int(m["ver"]))

    @route("DELETE", r"/api/templates/(?P<id>[a-z0-9_]+)")
    def template_delete(app, m, q, b):
        return app.templates.delete(m["id"])

    @route("POST", r"/api/templates/(?P<id>[a-z0-9_]+)/(?P<ver>\d+)/publish")
    def template_publish(app, m, q, b):
        return app.templates.publish(m["id"], int(m["ver"]))

    @route("POST", r"/api/templates/(?P<id>[a-z0-9_]+)/archive")
    def template_archive(app, m, q, b):
        return app.templates.archive(m["id"], b.get("version"))

    @route("POST", r"/api/templates/(?P<id>[a-z0-9_]+)/duplicate")
    def template_duplicate(app, m, q, b):
        return app.templates.duplicate(m["id"], str(b.get("new_id") or ""), b.get("name"), b.get("version"))

    @route("POST", r"/api/templates/(?P<id>[a-z0-9_]+)/restore")
    def template_restore(app, m, q, b):
        return app.templates.restore(m["id"])

    @route("POST", r"/api/templates/(?P<id>[a-z0-9_]+)/new-draft")
    def template_new_draft(app, m, q, b):
        return app.templates.new_draft(m["id"], b.get("from_version"))

    @route("POST", r"/api/templates/(?P<id>[a-z0-9_]+)/validate")
    def template_validate(app, m, q, b):
        return app.templates.validate(m["id"], b.get("template"), b.get("version"))

    @route("POST", r"/api/templates/(?P<id>[a-z0-9_]+)/preview")
    def template_preview(app, m, q, b):
        return app.templates.preview(m["id"], b.get("template"), b.get("version"), b.get("sample"))

    @route("POST", r"/api/templates/(?P<id>[a-z0-9_]+)/test-render")
    def template_test_render(app, m, q, b):
        return app.templates.test_render(m["id"], b.get("template"), b.get("version"), b.get("sample"))

    @route("GET", "/api/assets")
    def assets(app, m, q, b):
        return app.templates.assets(q.get("type", [None])[0])

    @route("GET", r"/api/assets/(?P<id>[a-z0-9_\-]+)/file")
    def asset_file(app, m, q, b):
        return app.templates.asset_file(m["id"])

    @route("PUT", r"/api/assets/(?P<id>[a-z0-9_\-]+)")
    def asset_import(app, m, q, b):
        return app.templates.import_asset(m["id"], q.get("type", [""])[0], q.get("name", [""])[0], b)

    @route("DELETE", r"/api/assets/(?P<id>[a-z0-9_\-]+)")
    def asset_delete(app, m, q, b):
        return app.templates.delete_asset(m["id"], q.get("force", ["0"])[0] == "1")

    @route("GET", r"/api/channels/(?P<id>[\w\-]+)/templates")
    def channel_templates(app, m, q, b):
        return app.templates.channel_resolution(m["id"])

    @route("PUT", r"/api/channels/(?P<id>[\w\-]+)/templates")
    def channel_template_set(app, m, q, b):
        return app.templates.set_channel_template(m["id"], str(b.get("key") or ""), b.get("template_id"), b.get("version_policy") or "latest_published", b.get("fallback"))

    @route("GET", "/api/tts")
    def tts(app, m, q, b):
        return app.admin.tts_overview()

    @route("GET", "/api/tts/prosody")
    def prosody_info(app, m, q, b):
        return app.admin.prosody_info()

    @route("POST", "/api/tts/prosody/preview")
    def prosody_preview(app, m, q, b):
        return app.admin.prosody_preview(b)

    @route("GET", r"/api/tts/prosody/preview/(?P<id>[0-9a-f]{16})")
    def prosody_preview_file(app, m, q, b):
        return app.admin.prosody_preview_file(m["id"])

    @route("GET", r"/api/jobs/(?P<id>[\w\-]+)/speech-plan")
    def speech_plan(app, m, q, b):
        return app.service.speech_plan(m["id"], q.get("scope", ["external"])[0])

    @route("GET", r"/api/tts/profiles/(?P<name>[\w\-\.]+)")
    def tts_profile(app, m, q, b):
        return app.admin.tts_profile_detail(m["name"])

    @route("POST", "/api/tts/onboard")
    def tts_onboard(app, m, q, b):
        return app.admin.tts_onboard(b.get("reference", ""))

    @route("GET", r"/api/tasks/(?P<id>\w+)")
    def task(app, m, q, b):
        t = app.admin.tasks.get(m["id"])
        if t is None:
            raise StageError(ErrorClass.POLICY, "TASK_NOT_FOUND", "Tác vụ không còn (đã quá cũ).")
        return t

    @route("GET", "/api/image-pools")
    def image_pools(app, m, q, b):
        return app.image_pools.overview()

    @route("PUT", r"/api/image-pools/(?P<name>[\w\-]+)")
    def image_pool_put(app, m, q, b):
        return app.image_pools.upsert(m["name"], b.get("folder", ""), b.get("selection_mode"))

    @route("DELETE", r"/api/image-pools/(?P<name>[\w\-]+)")
    def image_pool_del(app, m, q, b):
        return app.image_pools.delete(m["name"])

    @route("POST", r"/api/image-pools/(?P<name>[\w\-]+)/scan")
    def image_pool_scan(app, m, q, b):
        return app.image_pools.scan(m["name"])

    @route("GET", r"/api/image-pools/(?P<name>[\w\-]+)/images/(?P<i>\d+)")
    def image_pool_image(app, m, q, b):
        return app.image_pools.image(m["name"], int(m["i"]))

    @route("GET", "/api/pools")
    def pools(app, m, q, b):
        return app.admin.pools()

    @route("POST", "/api/pools/sync")
    def pools_sync(app, m, q, b):
        return app.admin.pool_sync(b.get("name"))

    @route("PUT", r"/api/pools/(?P<name>[\w\-]+)")
    def pool_put(app, m, q, b):
        return app.admin.pool_upsert(m["name"], b.get("raw_dir", ""), b.get("orientation"))

    @route("DELETE", r"/api/pools/(?P<name>[\w\-]+)")
    def pool_del(app, m, q, b):
        return app.admin.pool_delete(m["name"])

    @route("GET", "/api/settings")
    def settings(app, m, q, b):
        return app.admin.get_settings()

    @route("PUT", "/api/settings")
    def settings_put(app, m, q, b):
        return app.admin.update_settings(b.get("changes") or {})

    @route("GET", "/api/config/effective")
    def effective(app, m, q, b):
        return app.admin.effective_config()

    @route("POST", "/api/cleanup")
    def cleanup(app, m, q, b):
        return app.admin.cleanup(bool(b.get("dry_run", True)))

    @route("GET", "/api/doctor")
    def doctor(app, m, q, b):
        return app.admin.doctor_status()

    @route("POST", "/api/doctor/run")
    def doctor_run(app, m, q, b):
        return app.admin.doctor_run()

    @route("POST", "/api/samples")
    def samples(app, m, q, b):
        return app.admin.make_samples()

    @route("POST", "/api/pick")
    def pick(app, m, q, b):
        return app.admin.pick_path(b.get("kind", "file"), b.get("title", ""))


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ContentFactoryUI/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a) -> None:                               # im lặng: log lỗi đi vào runtime/logs/ui.log
            pass

        # ------------------------------------------------------------------ tiện ích
        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8", {"Cache-Control": "no-store"})

        def _error(self, code: int, err: str, message: str, hint: str = "") -> None:
            self._json(code, {"error": {"code": err, "message": message, "hint": hint}})

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

        # ------------------------------------------------------------------ phân phối
        def do_GET(self) -> None:
            self._dispatch()

        do_POST = do_PUT = do_DELETE = do_HEAD = do_GET

        def _dispatch(self) -> None:
            try:
                if not self._host_ok():
                    return self._error(403, "BAD_HOST", "Yêu cầu không hợp lệ (Host).")
                u = urlparse(self.path)
                path = unquote(u.path)
                if path.startswith("/api/"):
                    return self._api(path, parse_qs(u.query))
                if self.command not in ("GET", "HEAD"):
                    return self._error(405, "METHOD", "Không hỗ trợ.")
                return self._static(path)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:                                            # noqa: BLE001
                app.log_error(f"{self.command} {self.path}")
                try:
                    self._error(500, "INTERNAL", "Lỗi nội bộ của giao diện.", "Chi tiết trong runtime/logs/ui.log.")
                except OSError:
                    pass

        def _api(self, path: str, query: dict) -> None:
            if not secrets.compare_digest(self.headers.get("X-CF-Token") or "", app.token):
                return self._error(401, "NO_TOKEN", "Thiếu hoặc sai token phiên.", "Tải lại trang.")
            if self.command != "GET":
                origin = self.headers.get("Origin")
                if origin and origin not in (f"http://127.0.0.1:{app.port}", f"http://localhost:{app.port}"):
                    return self._error(403, "BAD_ORIGIN", "Yêu cầu từ nguồn không được phép.")
            method = "GET" if self.command == "HEAD" else self.command
            for meth, rx, name in ROUTES:
                m = rx.match(path)
                if m and meth == method:
                    body = self._body(name)
                    if body is None:
                        return
                    try:
                        res = getattr(Api, name)(app, m, query, body)
                        if isinstance(res, Raw):                         # ảnh/video xem trước template: nhị phân, không cache lâu
                            return self._send(200, res.body, res.ctype, {"Cache-Control": "private, max-age=60"})
                        return self._json(200, res)
                    except StageError as e:
                        code = 404 if e.code.endswith("NOT_FOUND") else 400
                        return self._json(code, {"error": {"code": e.code, "message": e.message, "hint": (e.detail or {}).get("hint", ""), "class": e.error_class.value
                                                             if hasattr(e.error_class, "value") else str(e.error_class)}})
                    except (ValueError, KeyError, TypeError) as e:
                        return self._error(400, "BAD_REQUEST", f"Yêu cầu không hợp lệ: {e}")
            if any(rx.match(path) for _, rx, _ in ROUTES):
                return self._error(405, "METHOD", "Phương thức không được hỗ trợ.")
            return self._error(404, "NOT_FOUND", "Không có API này.")

        def _body(self, name: str):
            if self.command in ("GET", "HEAD", "DELETE"):
                return {}
            n = int(self.headers.get("Content-Length") or 0)
            limit = MAX_ASSET if name in RAW_BODY else MAX_JSON
            if n > limit:
                left = min(n, 8 << 20)                                       # đọc bỏ phần thân (có trần) để trình duyệt nhận được 413 thay vì bị ngắt kết nối giữa chừng
                while left > 0:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                self.close_connection = True
                self._error(413, "TOO_LARGE", "Dữ liệu gửi lên quá lớn.")
                return None
            raw = self.rfile.read(n) if n else b""
            if name in RAW_BODY:
                return raw
            if not raw:
                return {}
            try:
                d = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._error(400, "BAD_JSON", "Nội dung gửi lên không phải JSON hợp lệ.")
                return None
            if not isinstance(d, dict):
                self._error(400, "BAD_JSON", "Nội dung phải là một object JSON.")
                return None
            return d

        # ------------------------------------------------------------------ tĩnh
        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            f = (STATIC / rel).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                if "." not in Path(rel).name:                              # đường dẫn của router phía client
                    f, rel = STATIC / "index.html", "index.html"
                else:
                    return self._error(404, "NOT_FOUND", "Không tìm thấy.")
            data = f.read_bytes()
            ctype = (mimetypes.guess_type(str(f))[0] or "application/octet-stream") + ("; charset=utf-8" if f.suffix in (".html", ".js", ".css", ".mjs", ".svg") else "")
            headers = {}
            if rel == "index.html":
                data = data.replace(b"<!--CF_TOKEN-->", f'<meta name="cf-token" content="{app.token}">'.encode())
                headers["Cache-Control"] = "no-store"
                headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                                                      "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            else:
                etag = f'"{f.stat().st_mtime_ns:x}-{len(data):x}"'
                headers["ETag"] = etag
                headers["Cache-Control"] = "no-cache" if "vendor" not in rel else "public, max-age=604800"
                if self.headers.get("If-None-Match") == etag:
                    return self._send(HTTPStatus.NOT_MODIFIED, b"", ctype, headers)
            self._send(200, data, ctype, headers)

    return Handler


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:           # trình duyệt đóng kết nối giữa chừng là chuyện bình thường: không in traceback
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class UiServer:
    def __init__(self, app: App, port: int = 8765) -> None:
        self.app = app
        self.httpd = _Server(("127.0.0.1", port), make_handler(app))
        self.httpd.daemon_threads = True
        app.port = self.httpd.server_address[1]
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.app.port}/"

    def start(self) -> None:
        self.app.start_runner()
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="ui-http", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.stop()


def serve(root: Path, port: int = 8765, open_browser: bool = True, run_loop: bool = True, echo=print) -> int:
    import webbrowser
    cfg = load_config(root)
    orc = Orchestrator(cfg)
    app = App(orc, run_loop=run_loop)
    try:
        srv = UiServer(app, port)
    except OSError:
        srv = UiServer(app, 0)                                           # cổng bận: tự chọn cổng khác
    srv.start()
    echo(f"ContentFactory đang chạy: {srv.url}   (Ctrl-C để dừng)")
    if open_browser:
        try:
            webbrowser.open(srv.url)
        except Exception:                                                # noqa: BLE001
            pass
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        echo("Đang dừng…")
    finally:
        srv.stop()
    return 0
