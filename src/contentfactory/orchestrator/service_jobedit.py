"""Facade giao diện cho “Sửa job”: Cập nhật pipeline (đổi ĐÍCH theo progress floor) và Xóa job.

Nghiệp vụ nằm ở `Orchestrator.update_target` / `delete_job` (một thao tác duy nhất cho UI, API và CLI). Ở đây chỉ dựng dữ liệu cho màn hình
(bước nào đã qua, bước nào chọn được và vì sao, hậu quả của từng lựa chọn bằng tiếng Việt) và dịch kết quả thành thông điệp cho người dùng.
Frontend không có đồ thị/thứ tự pipeline riêng và không tự quyết định đích nào hợp lệ: backend kiểm lại khi lưu.
"""
from __future__ import annotations

from ..contracts import ErrorClass, StageError
from ..jobs import pipeline as P
from ..jobs.plan import progress_floor
from . import diagnose as DG

STATE_VI = {"done": "Đã hoàn thành", "reused": "Dùng lại kết quả có sẵn", "provided": "Đã có sẵn", "running": "Đang chạy", "held": "Đang chờ", "failed": "Lỗi",
            "waiting": "Chưa chạy", "not_planned": "Không nằm trong kế hoạch"}
PASSED = {"done", "reused", "provided"}


def _err(code: str, msg: str, hint: str = "", **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, msg, {**detail, **({"hint": hint} if hint else {})}, resource="input")


class JobEditService:
    def __init__(self, orc, service) -> None:
        self.orc, self.svc = orc, service

    # ------------------------------------------------------------------------------------------ đọc
    def view(self, j: dict, summary: dict, runs: list[dict], rows: list[dict]) -> dict:
        """Dữ liệu cho drawer “Sửa job”. `rows` = pipeline từng stage của chi tiết job (cùng nguồn với timeline)."""
        status = summary["status"]
        floor = progress_floor(j, runs)
        n = len(P.STAGES)
        cur_idx = j["target_idx"] if j.get("target_idx") is not None else n - 1
        cancelled = j["control_state"] == "CANCELLED"
        have = {a["kind"] for a in self.orc.store.artifacts(j["id"])}
        label = DG.STAGE_LABEL
        stages = []
        for i, st in enumerate(P.STAGES):
            state = rows[i]["state"]
            reason, kind, selectable = "", "ok", False
            if cancelled:
                reason, kind = "Job đã bị hủy nên không đổi được pipeline.", "cancelled"
            elif i < floor:
                reason, kind = "Job đã chạy tới sau bước này nên không đặt đích ở đây.", "passed"
            else:
                plan, _ = self.orc.target_plan(j, st.name, have)
                if plan.errors:
                    reason, kind = "Thiếu dữ liệu cho bước này: " + "; ".join(plan.errors), "missing"
                else:
                    selectable = True
                    reason = "Đích hiện tại." if i == cur_idx else ("Job đang ở bước này." if i == floor and state == "running" else "Có thể chọn.")
            stages.append({"id": st.name, "label": label[st.name], "state": state, "state_label": STATE_VI.get(state, state), "is_target": i == cur_idx,
                           "is_floor": i == floor, "selectable": selectable, "reason": reason, "reason_kind": kind,
                           "effect": self.effect(j, status, floor, cur_idx, i, rows) if selectable else None})
        held = status == "paused" and j.get("pause_origin") == "EDIT"
        running = j["state"] in P.BY_RUNNING
        return {"floor": P.STAGES[floor].name, "floor_label": label[P.STAGES[floor].name], "target": P.STAGES[cur_idx].name, "target_label": label[P.STAGES[cur_idx].name],
                "stages": stages, "can_update": not cancelled and any(s["selectable"] and not s["is_target"] for s in stages), "awaiting_run": held,
                "awaiting_text": (f"Pipeline mới đã được lưu (đến “{label[P.STAGES[cur_idx].name]}”). Job sẽ không tự chạy: bấm “Chạy tiếp” khi sẵn sàng." if held else None),
                "delete": {"running": running, "status": status, "title": summary["title"],
                           "warning": ("Job đang chạy: bước hiện tại sẽ được dừng ở điểm an toàn rồi job bị xóa." if running else None),
                           "output_note": "Thư mục output đã tạo sẽ KHÔNG bị xóa."}}

    def effect(self, j: dict, status: str, floor: int, cur_idx: int, new_idx: int, rows: list[dict]) -> str:
        """Hậu quả của việc chọn bước `new_idx` làm đích, bằng ngôn ngữ người dùng (không thuật ngữ nội bộ)."""
        lab = DG.STAGE_LABEL
        new, floor_label = lab[P.STAGES[new_idx].name], lab[P.STAGES[floor].name]
        if new_idx == cur_idx:
            return f"Đây là đích hiện tại: job dừng sau “{new}”."
        if status == "completed":
            if new_idx > cur_idx:
                return f"Pipeline mới sẽ được lưu. Job sẽ không tự chạy. Bấm “Chạy tiếp” sau đó để hoàn tất đến “{new}”; các bước đã xong được dùng lại."
            return "Không đổi được."
        if status == "failed":
            return f"Job đang lỗi ở “{floor_label}”. Đích mới được lưu; lỗi và lịch sử giữ nguyên, chạy lại bước lỗi để tiếp tục đến “{new}”."
        if status == "paused":
            return f"Đích mới được lưu. Job vẫn đang tạm dừng; khi bấm Tiếp tục, job chạy đến “{new}” rồi dừng."
        if status in ("waiting", "attention"):
            return f"Đích mới được lưu. Job vẫn đang chờ; khi tiếp tục, job chạy đến “{new}” rồi dừng."
        if new_idx == floor and j["state"] in P.BY_RUNNING:
            return f"Job đang ở “{floor_label}”. Sau khi lưu, job hoàn tất bước này rồi dừng."
        if status == "queued":
            return f"Sau khi lưu, job chạy đến “{new}” rồi dừng."
        return f"Job đang ở “{floor_label}”. Sau khi lưu, job tiếp tục đến “{new}” rồi dừng."

    # ------------------------------------------------------------------------------------------ ghi
    def update_target(self, job_id: str, payload: dict) -> dict:
        target = str((payload or {}).get("target_stage") or "")
        if not target:
            raise _err("PIPELINE_TARGET_INVALID", "Chưa chọn bước đích.")
        self.svc._job_or_error(job_id)
        r = self.orc.update_target(job_id, target)
        label = DG.STAGE_LABEL[target]
        if r["result"] == "unchanged":
            msg = "Pipeline không đổi."
        elif r["held"]:
            msg = f"Đã lưu pipeline mới (đến “{label}”). Job sẽ không tự chạy: bấm “Chạy tiếp” khi sẵn sàng."
        else:
            msg = f"Đã cập nhật pipeline: job sẽ chạy đến “{label}” rồi dừng."
        return {**r, "message": msg}

    def delete(self, job_id: str) -> dict:
        j = self.orc.store.get_job(job_id)                                                # xóa lặp lại (bấm đúp/retry trình duyệt) là no-op, không lỗi
        if j is None:
            return {"result": "gone", "message": "Job không còn tồn tại.", "was_running": False}
        was_running = j["state"] in P.BY_RUNNING
        res = self.orc.delete_job(job_id)
        if res == "deleted" and j.get("batch_id"):                                       # item của Channel Run không còn trỏ vào job đã xóa
            for it in self.orc.store.batch_items(j["batch_id"]):
                if it["job_id"] == job_id:
                    self.orc.store.set_batch_item(j["batch_id"], it["source_video_id"], status="removed")
        self.svc.forget_job(job_id)
        msg = {"deleted": "Đã xóa job. Thư mục output đã tạo được giữ nguyên." + (" Bước đang chạy được dừng ở điểm an toàn." if was_running else ""),
               "already": "Job đã được xóa từ trước.", "gone": "Job không còn tồn tại."}[res]
        return {"result": res, "message": msg, "was_running": was_running}
