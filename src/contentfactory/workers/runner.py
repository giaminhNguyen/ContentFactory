"""Cầu nối `AgentRunner` (hợp đồng của adapter) <-> WorkerManager.

Story đi qua Worker Runtime bằng hai runner, cả hai đều trả `AgentTurn` như `ClaudeCliRunner` cũ:

    WorkerRunner    đã cấu hình routing `story.write` -> chạy qua WorkerManager
                    (retry/fallback/cooldown/validation gate/attempt history)
    DriverRunner    chưa cấu hình routing -> chạy thẳng MỘT driver (hành vi cũ, một CLI)
"""
from __future__ import annotations

from pathlib import Path

from ..contracts import AgentTurn, ErrorClass, StageContext, StageError
from .drivers.base import Driver, ExecRequest
from .manager import WorkerManager


class WorkerRunner:
    def __init__(self, manager: WorkerManager, work_type: str = "story.write") -> None:
        self.manager = manager
        self.work_type = work_type

    def run(self, prompt: str, cwd: Path, session: str | None, ctx: StageContext) -> AgentTurn:
        r = self.manager.run(self.work_type, prompt=prompt, cancel=ctx.cancel,
                             meta={"session": session}, cwd=cwd)
        if not r.ok:
            if ctx.cancel.is_set():                # huỷ giữa chừng: không phải lỗi worker
                raise StageError(ErrorClass.CANCELLED, "CANCELLED", r.reason)
            ctx.log("worker_run_failed", work_type=self.work_type, attempts=len(r.attempts), reason=r.reason)
            raise (r.error.to_stage_error() if r.error is not None
                   else StageError(ErrorClass.TRANSIENT, "WORKER_UNAVAILABLE", r.reason))
        ctx.log("worker_run_done", work_type=self.work_type, attempts=len(r.attempts),
                worker=(r.final.worker_name if r.final else ""))
        return {"session_id": r.session_id, "text": r.text, "cost_usd": r.cost_usd, "is_error": False}


class DriverRunner:
    def __init__(self, driver: Driver, work_type: str = "story.write") -> None:
        self.driver = driver
        self.work_type = work_type
        self.cmd = getattr(driver, "cmd", None)     # adapter.health() kiểm "không thấy claude CLI"

    def run(self, prompt: str, cwd: Path, session: str | None, ctx: StageContext) -> AgentTurn:
        req = ExecRequest(work_type=self.work_type, prompt=prompt, cwd=Path(cwd), session=session,
                          cancel=ctx.cancel)
        try:
            res = self.driver.execute(req)
        except NotImplementedError as e:
            raise StageError(ErrorClass.TRANSIENT, "DRIVER_UNSUPPORTED", str(e)) from None
        if not res.ok:
            raise (res.error.to_stage_error() if res.error is not None
                   else StageError(ErrorClass.TRANSIENT, "DRIVER_FAILED", res.text))
        return {"session_id": res.session_id, "text": res.text, "cost_usd": res.cost_usd,
                "is_error": bool(res.meta.get("is_error"))}
