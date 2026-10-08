"""Fake driver: core test chạy được mà KHÔNG cài bất kỳ CLI thật nào (W1.1 acceptance).

Điều khiển bằng "script" hành vi, lần lượt cho các lần execute liên tiếp:

    ok               output hợp lệ            -> ok=True, file có nội dung
    temporary        lỗi tạm thời             -> ok=False, TEMPORARY
    quota            hết quota                 -> ok=False, QUOTA
    auth             chưa đăng nhập            -> ok=False, AUTH
    timeout          quá hạn                  -> ok=False, TIMEOUT
    invalid_output   chạy xong nhưng output hỏng -> ok=True, file có marker cấm (validation phải từ chối)
    no_output        chạy xong nhưng không có file -> ok=True, output_path=None

Khi script hết, các lần sau mặc định `ok`.
"""
from __future__ import annotations

import time
from pathlib import Path

from contentfactory.workers.errors import WorkerErrorClass
from contentfactory.workers.models import WorkerStatus
from contentfactory.workers.validation import FORBIDDEN_MARKER
from .base import BaseDriver, DetectedExecutable, ExecRequest, ExecResult, ProbeResult

# marker mà validator của ContentFactory cấm (W1.14) — định nghĩa ở workers/validation.py
_OK_TEXT = "# chương thử\n\nNội dung do fake worker tạo ra.\n"
_BEHAVIOR = {
    "temporary": WorkerErrorClass.TEMPORARY,
    "quota": WorkerErrorClass.QUOTA,
    "auth": WorkerErrorClass.AUTH,
    "timeout": WorkerErrorClass.TIMEOUT,
}


class FakeDriver(BaseDriver):
    id = "fake"
    label = "Fake Driver"
    exe_names = ("cf-fake-worker",)

    def __init__(self, cfg: dict | None = None) -> None:
        super().__init__(cfg)
        self.script: list[str] = list(self.cfg.get("script") or [])
        self.calls: list[dict] = []          # lịch sử để test/assert
        self.detected: list[str] = list(self.cfg.get("detected") or [])
        self.probe_status: str = str(self.cfg.get("probe_status", "ready"))
        self.models_list: list[str] = list(self.cfg.get("models") or ["fake-fast", "fake-high"])

    # -- discovery / probe ----------------------------------------------------------------------
    def discover(self) -> list[DetectedExecutable]:
        return [DetectedExecutable(self.id, exe, source="path") for exe in self.detected]

    def probe(self, executable: str) -> ProbeResult:
        if self.probe_status == "missing":
            return ProbeResult(WorkerStatus.NOT_FOUND, executable, detail="không tìm thấy (fake)")
        if self.probe_status == "broken":
            return ProbeResult(WorkerStatus.BROKEN, executable, detail="exit=1 (fake)")
        if self.probe_status == "auth":
            return ProbeResult(WorkerStatus.AUTH_REQUIRED, executable, version="0.0.0-fake",
                               auth="required", detail="chưa đăng nhập (fake)")
        return ProbeResult(WorkerStatus.READY, executable, version="0.0.0-fake", auth="ok",
                           models=self.models_list, detail="fake probe ok")

    def list_models(self, executable: str) -> list[str]:
        return list(self.models_list)

    # -- execute --------------------------------------------------------------------------------
    def build_command(self, req: ExecRequest) -> list[str]:
        return ["fake-worker", req.work_type, req.model or ""]

    def execute(self, req: ExecRequest) -> ExecResult:
        behavior = self.script.pop(0) if self.script else "ok"
        self.calls.append({"work_type": req.work_type, "model": req.model, "behavior": behavior,
                           "cwd": str(req.cwd), "session": req.session})
        start = time.time()

        if behavior in _BEHAVIOR:
            return ExecResult(ok=False, duration_s=time.time() - start, exit_code=1,
                              error=self.error(_BEHAVIOR[behavior], f"FAKE_{behavior.upper()}",
                                               raw=f"{behavior} (fake)"),
                              meta={"behavior": behavior})

        out = Path(req.cwd) / str(req.meta.get("output_name", "output.md"))
        if behavior == "no_output":
            return ExecResult(ok=True, duration_s=time.time() - start, exit_code=0, meta={"behavior": behavior})

        out.parent.mkdir(parents=True, exist_ok=True)
        body = FORBIDDEN_MARKER if behavior == "invalid_output" else str(req.meta.get("output_text", _OK_TEXT))
        out.write_text(body, encoding="utf-8")
        return ExecResult(ok=True, output_path=out, text=body, exit_code=0,
                          duration_s=time.time() - start, meta={"behavior": behavior})
