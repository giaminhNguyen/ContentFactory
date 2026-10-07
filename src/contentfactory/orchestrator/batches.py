"""Channel Run / Batch (D-101): dán kênh/playlist YouTube -> xem + chọn video -> MỘT batch -> N job con ĐỘC LẬP.

Batch chỉ ĐIỀU PHỐI:
  - tạo job con (enqueue); concurrency do lane tài nguyên hiện có quyết định (100 video không chạy song song 100 TTS/render);
  - tổng hợp trạng thái từ job con (không có checkpoint/runner riêng); một job con lỗi KHÔNG dừng batch;
  - hành động hàng loạt dùng đúng cơ chế điều khiển job (pause/resume/retry/cancel/revision) — job con vẫn retry/checkpoint/output riêng.
Tạo batch idempotent: cùng `request_id` -> cùng batch; chỉ mục duy nhất (batch_id, source_key) chặn hai job cho một video; mọi item được ghi cùng batch trong một
transaction nên crash giữa chừng vẫn hoàn tất được (`ensure_created` chạy lúc khởi động runner và khi tạo).
"""
from __future__ import annotations

import copy
import sqlite3
import time

from ..contracts import ErrorClass, StageError
from ..jobs import pipeline as P
from ..jobs.db import CONTROL_CANCELLED, CONTROL_PAUSED, CONTROL_RUNNING
from ..source import discovery as DISC
from ..source.youtube import YtDlp
from . import channels as CH
from . import diagnose as DG

SCOPES = ("unstarted", "unfinished", "selected", "all_compatible")
TERMINAL_UI = {"completed", "failed", "cancelled"}
# trạng thái batch (suy ra) -> nhóm hiển thị chung với job (đếm bộ lọc, huy hiệu "cần xử lý")
UI_OF_BATCH = {"QUEUED": "queued", "RUNNING": "running", "PAUSED": "paused", "NEEDS_ATTENTION": "attention", "COMPLETED": "completed",
               "COMPLETED_WITH_ERRORS": "attention", "CANCELLED": "cancelled"}
# nhóm của các tab lọc trong chi tiết Channel Run (một item chưa có job hiển thị là "pending")
TAB_GROUPS = {"running": {"running"}, "queued": {"queued", "pending"}, "paused": {"paused", "waiting"}, "attention": {"attention", "failed"}, "failed": {"failed"},
              "completed": {"completed"}, "cancelled": {"cancelled"}}
STATUS_ORDER = ("running", "queued", "waiting", "paused", "attention", "failed", "completed", "cancelled")


def _err(code: str, message: str, hint: str = "", **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, message, {**detail, **({"hint": hint} if hint else {})}, resource="input")


class BatchService:
    def __init__(self, orc, discovery: DISC.Discovery | None = None) -> None:
        self.orc, self.cfg, self.store = orc, orc.cfg, orc.store
        self._discovery = discovery

    # -- discovery ---------------------------------------------------------------------------------
    @property
    def discovery(self) -> DISC.Discovery:
        if self._discovery is None:
            yt = YtDlp(self.cfg.data.get("youtube", {}).get("yt_dlp_cmd", ["yt-dlp"]), extra_args=self.cfg.data.get("youtube", {}).get("yt_dlp_args", []))
            b = self.cfg.data.get("batch", {})
            self._discovery = DISC.Discovery(yt.list_flat, yt.info, max_scan=b.get("max_scan", 300), confirm_above=b.get("confirm_above", 100), hard_max=b.get("hard_max", 500))
        return self._discovery

    def inspect(self, value: str) -> dict:
        return self.discovery.inspect(value)

    def _processed(self, channel_id: str):
        return lambda vid: self.store.find_job_by_source(f"youtube:{vid}", channel_id)

    def discover(self, payload: dict) -> dict:
        """Danh sách ứng viên + lựa chọn mặc định (newest 10 chưa xử lý). Không tạo job."""
        channel = self._channel(payload)
        filters = self._filters(payload)
        sel = payload.get("selection") or {"mode": "newest", "n": self.cfg.data.get("batch", {}).get("default_n", DISC.DEFAULT_N)}
        return {**self.discovery.discover(payload.get("url") or "", sel, filters, self._processed(channel["id"])), "output_channel": {"id": channel["id"], "name": channel["name"]}}

    def _channel(self, payload: dict) -> dict:
        cid = str(payload.get("output_channel") or payload.get("channel") or self.cfg.data["job_defaults"].get("channel") or "default")
        return CH.load_channel(self.cfg, cid)

    @staticmethod
    def _filters(payload: dict) -> dict:
        f = dict(payload.get("filters") or {})
        if payload.get("skip_policy") == "rerun":
            f["skip_processed"] = False
        return f

    # -- tạo ---------------------------------------------------------------------------------------
    def create(self, payload: dict) -> dict:
        from .service import Service
        svc = Service(self.orc)
        channel = self._channel(payload)
        rid = str(payload.get("request_id") or "") or None
        if rid and (old := self.store.batch_by_request(rid)):                                    # bấm đúp/gửi lại: trả đúng batch đó, không quét lại, không tạo thêm
            self.ensure_created(old["id"])
            return {**self.detail(old["id"]), "deduped": True}
        filters = self._filters(payload)
        selection = ({"mode": "manual", "ids": [str(x) for x in payload["video_ids"]]} if payload.get("video_ids")
                     else payload.get("selection") or {"mode": "newest", "n": self.cfg.data.get("batch", {}).get("default_n", DISC.DEFAULT_N)})
        found = self.discovery.discover(payload.get("url") or "", selection, filters, self._processed(channel["id"]))
        chosen = [e for e in found["entries"] if e["selected"]]
        if not chosen:
            why = ", ".join(f"{n} {r}" for r, n in found["skipped"].items()) or "không có video phù hợp"
            raise _err("NOTHING_TO_RUN", f"Không có video nào để tạo job ({why}).", "Đổi cách chọn, bỏ bộ lọc “đã xử lý”, hoặc quét lại kênh.")
        lim = found["limits"]
        if len(chosen) > lim["hard_max"]:
            raise _err("BATCH_TOO_LARGE", f"Đã chọn {len(chosen)} video, vượt trần {lim['hard_max']} cho một Channel Run.", "Chia thành nhiều lần (newest/range) để không tạo quá nhiều job cùng lúc.")
        if len(chosen) > lim["confirm_above"] and not payload.get("confirm_large"):
            raise _err("CONFIRM_LARGE_BATCH", f"Sắp tạo {len(chosen)} job (hơn {lim['confirm_above']}). Cần xác nhận rõ.", "Gửi confirm_large=true sau khi người dùng đồng ý.", count=len(chosen))
        run = payload.get("run") or "full"
        custom = svc._custom(payload)
        kids, auto_resume = payload.get("kids"), payload.get("auto_resume")
        # kiểm cấu hình con TRƯỚC khi ghi gì (sai kiểu chạy/thiếu made_for_kids/pipeline lỗi => từ chối cả batch, không tạo batch nửa vời)
        det = svc.detect_input(chosen[0]["url"])
        params, kw = svc._spec(det, None if custom else run, None, channel["id"], kids, auto_resume, custom)
        kw.pop("_extend", None)
        plan = self.orc.plan(params, **{k: v for k, v in kw.items() if k in ("mode", "target_stage", "start_stage", "inputs", "pipeline")})
        if plan.errors:
            raise _err("INVALID_JOBSPEC", "; ".join(plan.errors))
        svc._require_kids(channel, params, "publish" in plan.run if custom else svc._target_reaches(run) >= P.INDEX["publish"])
        src = found["source"]
        spec = {"kind": f"youtube_{src['kind']}", "source_provider": "youtube", "source_id": src["id"], "source_url": src["canonical_url"], "source_title": src.get("title"),
                "source_channel_id": src.get("channel_id"), "source_channel_url": src.get("channel_url"), "source_channel_title": src.get("channel_title"),
                "output_channel_id": channel["id"], "selection_spec": found["selection"],
                "pipeline_spec": {"run": None if custom else run, "pipeline": custom},
                "options": {"kids": kids, "auto_resume": auto_resume, "filters": found["filters"], "skipped": found["skipped"], "warnings": found["warnings"], "truncated": found["truncated"]}}
        items = [{"video_id": e["video_id"], "url": e["url"], "title": e["title"], "published": e["published"], "duration": e["duration"],
                  "metadata": {"live_status": e["live_status"], "short": e["short"]}} for e in chosen]
        batch, created = self.store.create_batch(spec, items, rid)
        if created:
            self.orc.log.emit("batch_created", batch_id=batch["id"], items=len(items), channel=channel["id"], source=src["canonical_url"])
        self.ensure_created(batch["id"])
        return {**self.detail(batch["id"]), "deduped": not created}

    def ensure_created(self, bid: str | None = None) -> int:
        """Tạo job con cho các item còn `pending` (lần đầu, hoặc sau crash). Idempotent: item đã có job (tìm theo batch+source_key) chỉ được nối lại."""
        n = 0
        for it in self.store.pending_batch_items():
            if bid and it["batch_id"] != bid:
                continue
            n += self._create_child(self.store.get_batch(it["batch_id"]), it)
        return n

    def _create_child(self, batch: dict, it: dict) -> int:
        from .service import Service
        sk = f"youtube:{it['source_video_id']}"
        jid = self.store.find_batch_job(batch["id"], sk)
        if jid is None:
            svc = Service(self.orc)
            opt, pipe = batch["options"], batch["pipeline_spec"]
            try:
                det = svc.detect_input(it["source_video_url"])
                params, kw = svc._spec(det, None if pipe.get("pipeline") else pipe["run"], None, batch["output_channel_id"], opt.get("kids"), opt.get("auto_resume"), pipe.get("pipeline"))
                params["source"] = {"provider": "youtube", "video_id": it["source_video_id"], "video_url": it["source_video_url"],
                                    "channel_id": batch.get("source_channel_id"), "channel_url": batch.get("source_channel_url"), "channel_title": batch.get("source_channel_title")}
                params["batch"] = {"id": batch["id"], "position": it["position"]}
                extend = kw.pop("_extend", None)
                try:
                    jid = self.orc.submit(params, **kw, batch_id=batch["id"], source_key=sk)
                except sqlite3.IntegrityError:                                              # bấm đúp/hai tiến trình: job đã có
                    jid = self.store.find_batch_job(batch["id"], sk)
                else:
                    if extend:
                        self.orc.set_target(jid, extend)
            except StageError as e:                                                         # một video lỗi cấu hình không chặn các video khác
                self.store.set_batch_item(batch["id"], it["source_video_id"], status="error", error=f"{e.code}: {e.message}"[:300])
                self.orc.log.emit("batch_item_error", "warning", batch_id=batch["id"], video=it["source_video_id"], error=e.code)
                return 0
        if jid and self.store.get_batch(batch["id"])["control_state"] == CONTROL_PAUSED:
            self.orc.pause_job(jid, "BATCH")
        self.store.set_batch_item(batch["id"], it["source_video_id"], job_id=jid, status="created")
        return 1

    # -- đọc ---------------------------------------------------------------------------------------
    def _jobs(self, bid: str) -> list[dict]:
        return self.store.batch_job_index(bid)

    @staticmethod
    def _ui(j: dict) -> str:
        return DG.ui_status(j)

    def counts(self, bid: str, items: list[dict] | None = None, jobs: list[dict] | None = None) -> dict:
        jobs = self._jobs(bid) if jobs is None else jobs
        items = self.store.batch_items(bid) if items is None else items
        c = {k: 0 for k in STATUS_ORDER}
        for j in jobs:
            c[self._ui(j)] += 1
        c["pending_creation"] = sum(1 for i in items if i["status"] == "pending")
        c["error"] = sum(1 for i in items if i["status"] == "error")
        c["total"] = sum(1 for i in items if i["status"] != "removed")                      # video có job đã bị xóa (Sửa job) không còn tính vào Channel Run
        return c

    @staticmethod
    def derive_status(batch: dict, c: dict) -> str:
        """Trạng thái batch SUY RA từ job con (không phải trạng thái runner): QUEUED|RUNNING|PAUSED|NEEDS_ATTENTION|COMPLETED|COMPLETED_WITH_ERRORS|CANCELLED."""
        live = c["running"] + c["queued"] + c["waiting"] + c["paused"] + c["attention"] + c["pending_creation"]
        if batch["control_state"] == CONTROL_CANCELLED or (c["total"] and c["cancelled"] == c["total"] - c["error"] and not live and not c["completed"] and not c["failed"]):
            return "CANCELLED"
        if not live:
            return "COMPLETED_WITH_ERRORS" if (c["failed"] or c["error"]) else "COMPLETED"
        if batch["control_state"] == CONTROL_PAUSED or (c["paused"] and c["paused"] == live):
            return "PAUSED"
        if c["running"]:
            return "RUNNING"
        if c["attention"] and not c["queued"]:
            return "NEEDS_ATTENTION"
        return "QUEUED"

    def _fraction(self, jobs: list[dict]) -> float:
        if not jobs:
            return 0.0
        tot = 0.0
        for j in jobs:
            if self._ui(j) == "completed":
                tot += 1.0
                continue
            pos = P.position(j["state"]) if j["state"] != P.FAILED else P.INDEX.get(j.get("failed_stage") or "", 0)
            planned = (j["target_idx"] + 1) if j.get("target_idx") is not None else len(P.STAGES)
            tot += min(1.0, (pos or 0) / max(1, planned)) if self._ui(j) != "cancelled" else 0.0
        return round(tot / len(jobs), 3)

    def summary(self, batch: dict, jobs: list[dict] | None = None, items: list[dict] | None = None) -> dict:
        bid = batch["id"]
        jobs, items = (self._jobs(bid) if jobs is None else jobs), (self.store.batch_items(bid) if items is None else items)
        c = self.counts(bid, items, jobs)
        ch = self._channel_name(batch["output_channel_id"])
        derived = self.derive_status(batch, c)
        return {"id": bid, "type": "batch", "kind": batch["kind"], "title": batch.get("source_title") or batch["source_url"], "status": derived, "ui_status": UI_OF_BATCH[derived],
                "control": batch["control_state"], "fraction": self._fraction(jobs), "next_action": self._next_action(batch, c),
                "source": self._source(batch), "output_channel": {"id": batch["output_channel_id"], "name": ch}, "counts": c, "progress": self._fraction(jobs),
                "created_at": batch["created_at"], "updated_at": batch["updated_at"], "selection": batch["selection_spec"], "pipeline": batch["pipeline_spec"],
                **self._pipeline_view(batch["pipeline_spec"])}

    @staticmethod
    def _pipeline_view(spec: dict) -> dict:
        """Nhãn dễ đọc + các stage được yêu cầu của pipeline batch (để giao diện hiện/đổi mà không tự suy ra phụ thuộc)."""
        from ..jobs.plan import spec_for_mode
        from .service import RUN_MODES
        custom = spec.get("pipeline")
        if custom:
            stages = list(custom.get("requested_stages") or [])
            return {"pipeline_label": "Tùy chỉnh: " + ", ".join(DG.STAGE_LABEL.get(s, s) for s in stages), "requested_stages": stages}
        label, _, run = RUN_MODES.get(spec.get("run") or "full", RUN_MODES["full"])
        mode = run.get("mode") or "FULL"
        return {"pipeline_label": label, "requested_stages": spec_for_mode(mode)["requested_stages"]}

    @staticmethod
    def _next_action(b: dict, c: dict) -> str | None:
        """Hành động chính theo ngữ cảnh cho thẻ Channel Run: Tiếp tục khi đang tạm dừng, Chạy lại job lỗi khi chỉ còn lỗi, Tạm dừng khi đang chạy."""
        live = c["running"] + c["queued"] + c["waiting"] + c["paused"] + c["attention"] + c["pending_creation"]
        if b["control_state"] == CONTROL_PAUSED or (live and c["paused"] == live):
            return "resume"
        if not live and c["failed"]:
            return "retry_failed"
        if b["control_state"] == CONTROL_RUNNING and live and (c["running"] or c["queued"]):
            return "pause"
        return None

    def _channel_name(self, cid: str) -> str:
        try:
            return CH.load_channel(self.cfg, cid)["name"]
        except StageError:
            return cid

    @staticmethod
    def _source(b: dict) -> dict:
        return {"provider": b["source_provider"], "kind": b["kind"].removeprefix("youtube_"), "id": b["source_id"], "url": b["source_url"], "title": b.get("source_title"),
                "channel_id": b.get("source_channel_id"), "channel_url": b.get("source_channel_url"), "channel_title": b.get("source_channel_title")}

    def list(self) -> dict:
        by, items = self.store.batch_jobs_all(), self.store.batch_item_statuses()
        rows = [self.summary(b, by.get(b["id"], []), items.get(b["id"], [])) for b in self.store.list_batches()]
        return {"batches": rows, "version": self.store.jobs_version()}

    def detail(self, bid: str, status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        from .service import Service
        b = self.store.get_batch(bid)
        if b is None:
            raise _err("BATCH_NOT_FOUND", f"Không có Channel Run {bid}.", "Quay lại danh sách job.")
        svc = Service(self.orc)
        s = self.summary(b)
        items = self.store.batch_items(bid)
        by_job = {j["id"]: j for j in self._jobs(bid)}
        rows = []
        for it in items:
            if it["status"] == "removed":
                continue
            j = by_job.get(it["job_id"]) if it["job_id"] else None
            st = self._ui(j) if j else ("pending" if it["status"] == "pending" else it["status"])
            if status and status != "all" and st not in TAB_GROUPS.get(status, {status}):
                continue
            rows.append((it, j, st))
        page = rows[offset:offset + limit]
        full = {j["id"]: j for j in self.store.jobs_by_ids([j["id"] for _, j, _ in page if j])}
        out = []
        for it, j, st in page:
            row = {"position": it["position"], "video_id": it["source_video_id"], "title": it["title"], "published": it["published_at"], "status": st, "error": it["error"],
                   "links": {"source_video": it["source_video_url"], "source_channel": b.get("source_channel_url")}, "job_id": it["job_id"], "job": None}
            if j and j["id"] in full:
                sm = svc.summary(full[j["id"]])
                row["job"] = {k: sm[k] for k in ("id", "title", "status", "stage", "stage_label", "progress", "fraction", "next_action", "hold", "youtube_url", "output_dir") if k in sm}
                row["links"]["published_video"] = sm.get("youtube_url")
            out.append(row)
        running = [r for r in out if r["status"] == "running"][:3]
        s.update(items=out, total_items=len(rows), has_more=offset + limit < len(rows), skipped=b["options"].get("skipped", {}), warnings=b["options"].get("warnings", []),
                 current_activity=[f"{(r['job'] or {}).get('title') or r['title']} — {(r['job'] or {}).get('stage_label') or ''}".strip(" —") for r in running],
                 actions=self._eligibility(b, s["counts"]))
        return s

    def _eligibility(self, b: dict, c: dict) -> dict:
        live = c["running"] + c["queued"] + c["waiting"] + c["paused"] + c["attention"] + c["pending_creation"]
        return {"pause": b["control_state"] == CONTROL_RUNNING and live > 0, "resume": b["control_state"] == CONTROL_PAUSED or c["paused"] > 0, "retry_failed": c["failed"] > 0,
                "cancel_queued": c["queued"] + c["pending_creation"] > 0, "cancel": b["control_state"] != CONTROL_CANCELLED and live > 0, "rescan": b["control_state"] != CONTROL_CANCELLED,
                "update_pipeline": live > 0}

    # -- hành động hàng loạt (dùng cơ chế điều khiển job; idempotent) --------------------------------------
    def _batch(self, bid: str) -> dict:
        b = self.store.get_batch(bid)
        if b is None:
            raise _err("BATCH_NOT_FOUND", f"Không có Channel Run {bid}.", "Quay lại danh sách job.")
        return b

    def pause(self, bid: str) -> dict:
        b = self._batch(bid)
        if b["control_state"] == CONTROL_CANCELLED:
            raise _err("BATCH_CANCELLED", "Channel Run đã bị hủy.")
        self.store.set_batch_control(bid, CONTROL_PAUSED)
        res = {"paused": 0, "unchanged": 0, "skipped": 0}
        for j in self._jobs(bid):
            r = self.orc.pause_job(j["id"], "BATCH") if self._ui(j) not in TERMINAL_UI else "complete"
            res["paused" if r == "changed" else "unchanged" if r == "unchanged" else "skipped"] += 1
        self.orc.log.emit("batch_paused", batch_id=bid, **res)
        return {"batch": self.summary(self._batch(bid)), **res}

    def resume(self, bid: str) -> dict:
        """Tiếp tục các job con bị BATCH tạm dừng. Job người dùng tự tạm dừng (USER), job đang chờ tài nguyên (hold), job lỗi/xong/hủy giữ nguyên."""
        b = self._batch(bid)
        if b["control_state"] == CONTROL_CANCELLED:
            raise _err("BATCH_CANCELLED", "Channel Run đã bị hủy.")
        self.store.set_batch_control(bid, CONTROL_RUNNING)
        res = {"resumed": 0, "kept_paused_by_user": 0, "unchanged": 0}
        for j in self._jobs(bid):
            if j["control_state"] != CONTROL_PAUSED:
                res["unchanged"] += 1
                continue
            r = self.store.set_control(j["id"], CONTROL_RUNNING, "BATCH")
            res["resumed" if r == "changed" else "kept_paused_by_user" if r == "not_owner" else "unchanged"] += 1
        self.ensure_created(bid)
        self.orc.log.emit("batch_resumed", batch_id=bid, **res)
        return {"batch": self.summary(self._batch(bid)), **res}

    def retry_failed(self, bid: str) -> dict:
        self._batch(bid)
        res = {"retried": 0, "skipped": 0}
        for j in self._jobs(bid):
            if j["state"] == P.FAILED and j["control_state"] != CONTROL_CANCELLED:
                self.orc.retry(j["id"])
                res["retried"] += 1
            else:
                res["skipped"] += 1
        self.orc.log.emit("batch_retry_failed", batch_id=bid, **res)
        return {"batch": self.summary(self._batch(bid)), **res}

    def _unstarted(self, j: dict) -> bool:
        return not self.store.stage_runs(j["id"]) and self._ui(j) not in TERMINAL_UI

    def cancel_queued(self, bid: str) -> dict:
        """Hủy các việc CHƯA bắt đầu (item chưa tạo job + job con chưa chạy stage nào); việc đang chạy/đã xong giữ nguyên."""
        self._batch(bid)
        res = {"cancelled_jobs": 0, "cancelled_items": 0, "kept": 0}
        for it in self.store.batch_items(bid):
            if it["status"] == "pending":
                self.store.set_batch_item(bid, it["source_video_id"], status="cancelled")
                res["cancelled_items"] += 1
        for j in self._jobs(bid):
            if self._unstarted(j) and self.orc.cancel_job(j["id"]) == "changed":
                res["cancelled_jobs"] += 1
            else:
                res["kept"] += 1
        self.orc.log.emit("batch_cancel_queued", batch_id=bid, **res)
        return {"batch": self.summary(self._batch(bid)), **res}

    def cancel(self, bid: str) -> dict:
        self._batch(bid)
        self.store.set_batch_control(bid, CONTROL_CANCELLED)
        res = {"cancelled_jobs": 0, "kept": 0}
        for it in self.store.batch_items(bid):
            if it["status"] == "pending":
                self.store.set_batch_item(bid, it["source_video_id"], status="cancelled")
        for j in self._jobs(bid):
            r = self.orc.cancel_job(j["id"])
            res["cancelled_jobs" if r == "changed" else "kept"] += 1
        self.orc.log.emit("batch_cancelled", "warning", batch_id=bid, **res)
        return {"batch": self.summary(self._batch(bid)), **res}

    def update_pipeline(self, bid: str, target_stage: str, scope: str = "unfinished", job_ids: list[str] | None = None) -> dict:
        """Đổi ĐÍCH pipeline cho nhiều job con theo phạm vi, bằng đúng thao tác `update_target` của Sửa job (progress floor kiểm từng job; job đã xong được lưu và giữ chờ
        “Chạy tiếp”). Báo kết quả TỪNG job (thành công một phần là bình thường). Job đã hủy không đổi được."""
        if scope not in SCOPES:
            raise _err("INVALID_SCOPE", f"scope không hợp lệ: {scope!r}; hợp lệ: {list(SCOPES)}")
        if target_stage not in P.INDEX:
            raise _err("PIPELINE_TARGET_INVALID", f"Bước đích không hợp lệ: {target_stage!r}.")
        self._batch(bid)
        jobs = self._jobs(bid)
        if scope == "selected":
            want = set(job_ids or [])
            jobs = [j for j in jobs if j["id"] in want]
        elif scope == "unstarted":
            jobs = [j for j in jobs if self._unstarted(j)]
        results = []
        for j in jobs:
            if self._ui(j) == "cancelled":
                results.append({"job_id": j["id"], "result": "skipped", "reason": "Job đã bị hủy."})
                continue
            try:
                r = self.orc.update_target(j["id"], target_stage)
                results.append({"job_id": j["id"], "result": "applied" if r["result"] == "changed" else "unchanged", "held": r["held"]})
            except StageError as e:
                results.append({"job_id": j["id"], "result": "rejected", "reason": e.message})
        n = {k: sum(1 for r in results if r["result"] == k) for k in ("applied", "unchanged", "rejected", "skipped")}
        self.orc.log.emit("batch_pipeline_update", batch_id=bid, scope=scope, target=target_stage, **n)
        return {"batch": self.summary(self._batch(bid)), "scope": scope, "counts": n, "results": results}

    def rescan(self, bid: str) -> dict:
        """Thêm video MỚI của nguồn: những video đăng SAU video mới nhất đã có trong batch (đứng trước nó trong danh sách mới nhất-trước), qua cùng bộ lọc.
        Không đụng video đã có/đã xử lý và không kéo video cũ chưa chọn vào."""
        b = self._batch(bid)
        if b["control_state"] == CONTROL_CANCELLED:
            raise _err("BATCH_CANCELLED", "Channel Run đã bị hủy.")
        have = {i["source_video_id"] for i in self.store.batch_items(bid)}
        filters = dict(b["options"].get("filters") or {})
        cap = int(self.cfg.data.get("batch", {}).get("hard_max", 500))
        found = self.discovery.discover(b["source_url"], {"mode": "newest", "n": cap}, filters, self._processed(b["output_channel_id"]))
        ahead = []
        for e in found["entries"]:
            if e["video_id"] in have:
                break
            ahead.append(e)
        new = [e for e in ahead if e["selected"]]
        if len(new) > found["limits"]["hard_max"]:
            raise _err("BATCH_TOO_LARGE", f"Có {len(new)} video mới, vượt trần {found['limits']['hard_max']}.")
        added = self.store.add_batch_items(bid, [{"video_id": e["video_id"], "url": e["url"], "title": e["title"], "published": e["published"], "duration": e["duration"],
                                                  "metadata": {"live_status": e["live_status"], "short": e["short"]}} for e in new])
        self.ensure_created(bid)
        self.orc.log.emit("batch_rescan", batch_id=bid, added=added)
        return {"batch": self.summary(self._batch(bid)), "added": added}
