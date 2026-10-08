"""Hợp đồng Driver (W1.2).

Mỗi loại CLI có giao diện khác nhau; phần khác biệt đó PHẢI nằm trong driver. `WorkerManager`
chỉ biết `driver_id` và hợp đồng dưới đây — không biết cú pháp CLI, không biết vendor.

    discover()            tìm binary trên PATH / đường dẫn cấu hình
    probe()               kiểm tra an toàn: có chạy được không, phiên bản, đã đăng nhập chưa
    list_models()         model mà CLI khai báo được
    validate_model()      chặn model sai TRƯỚC khi thực thi
    build_command()       dựng lệnh headless
    execute()             chạy + chuẩn hoá kết quả
    classify()            đưa lỗi thô về WorkerErrorClass

Driver ĐƯỢC PHÉP biết vendor. Không driver nào được ghi secret/token vào log.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from contentfactory.contracts import CancelToken
from contentfactory.workers.errors import WorkerError, WorkerErrorClass, classify_text
from contentfactory.workers.models import WorkerStatus

PROBE_TIMEOUT_S = 20.0
OUT_LIMIT = 400        # cắt output của probe để không tràn log


def _clip(raw: str, limit: int = OUT_LIMIT) -> str:
    return (raw or "").strip()[:limit]


@dataclass
class DetectedExecutable:
    """Một binary tìm được. `source`: path (quét PATH) | manual (người dùng gõ)."""
    driver_id: str
    executable: str
    source: str = "path"
    version: str = ""
    detail: str = ""


@dataclass
class ProbeResult:
    """Kết quả probe an toàn (không tốn token, không gửi prompt)."""
    status: WorkerStatus
    executable: str = ""
    version: str = ""
    auth: str = "unknown"          # ok | required | unknown
    models: list[str] = field(default_factory=list)
    detail: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (WorkerStatus.READY, WorkerStatus.DETECTED)


@dataclass
class ExecRequest:
    """Yêu cầu thực thi một lượt. `cwd` là ATTEMPT WORKSPACE riêng của lần thử (W1.12)."""
    work_type: str
    prompt: str
    cwd: Path
    model: str | None = None
    session: str | None = None
    timeout_s: float = 3600.0
    cancel: CancelToken = field(default_factory=CancelToken)
    meta: dict = field(default_factory=dict)


@dataclass
class ExecResult:
    """Kết quả CHUẨN HOÁ của một lượt chạy.

    `ok=True` CHỈ nghĩa là tiến trình chạy xong — KHÔNG đồng nghĩa công việc thành công:
    output còn phải qua validation gate (W1.14) trước khi promote.
    """
    ok: bool
    output_path: Path | None = None
    text: str = ""
    session_id: str | None = None
    cost_usd: float = 0.0
    duration_s: float = 0.0
    exit_code: int | None = None
    error: WorkerError | None = None   # khi ok=False
    meta: dict = field(default_factory=dict)


@runtime_checkable
class Driver(Protocol):
    id: str
    label: str

    def discover(self) -> list[DetectedExecutable]: ...
    def probe(self, executable: str) -> ProbeResult: ...
    def list_models(self, executable: str) -> list[str]: ...
    def validate_model(self, model: str | None, known: list[str] | None = None) -> bool: ...
    def build_command(self, req: ExecRequest) -> list[str]: ...
    def execute(self, req: ExecRequest) -> ExecResult: ...
    def classify(self, raw: str) -> WorkerErrorClass: ...


class BaseDriver:
    """Hành vi mặc định: quét PATH theo `exe_names`, probe bằng `--version`, classify dùng regex chung.

    Driver thật chỉ cần ghi đè phần mà CLI của nó khác đi.
    """

    id = ""
    label = ""
    exe_names: tuple[str, ...] = ()            # tên binary cần tìm trên PATH, vd ("claude",)
    extra_patterns: tuple[tuple[WorkerErrorClass, re.Pattern], ...] = ()   # mẫu lỗi riêng

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = dict(cfg or {})

    # -- discovery ------------------------------------------------------------------------------
    def discover(self) -> list[DetectedExecutable]:
        out: list[DetectedExecutable] = []
        for name in self.exe_names:
            hit = shutil.which(name) or (self.cfg.get("executable") if self.cfg.get("executable") else None)
            if hit:
                out.append(DetectedExecutable(self.id, str(hit), source="path"))
        return out

    def resolve(self, executable: str) -> str | None:
        """Đưa executable (tên trên PATH hoặc đường dẫn) về đường dẫn chạy được; None nếu mất."""
        if not executable:
            return None
        return shutil.which(executable) or (executable if Path(executable).exists() else None)

    # -- probe ----------------------------------------------------------------------------------
    def probe(self, executable: str) -> ProbeResult:
        """Probe mặc định: `<exe> --version`. Lỗi KHÔNG làm app crash — trả status kèm lý do."""
        path = self.resolve(executable)
        if not path:
            return ProbeResult(WorkerStatus.NOT_FOUND, executable, detail=f"không tìm thấy {executable!r}")
        try:
            r = subprocess.run([path, "--version"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=PROBE_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            return ProbeResult(WorkerStatus.BROKEN, path, detail=_clip(repr(e)))
        out = _clip((r.stdout or "") + (r.stderr or ""))
        if r.returncode != 0:
            return ProbeResult(WorkerStatus.BROKEN, path, detail=out or f"exit={r.returncode}")
        return ProbeResult(WorkerStatus.READY, path, version=out.splitlines()[0] if out else "",
                           auth=self.cfg.get("auth", "unknown"), detail=out)

    # -- models ---------------------------------------------------------------------------------
    def list_models(self, executable: str) -> list[str]:
        return [m["id"] if isinstance(m, dict) else str(m) for m in self.cfg.get("models", [])]

    def validate_model(self, model: str | None, known: list[str] | None = None) -> bool:
        """None = dùng model mặc định -> luôn hợp lệ. Danh sách rỗng = không biết thì không chặn."""
        if not model:
            return True
        if not known:
            return True
        return model in known

    # -- execute --------------------------------------------------------------------------------
    def build_command(self, req: ExecRequest) -> list[str]:
        raise NotImplementedError(f"driver {self.id} chưa dựng được lệnh")

    def execute(self, req: ExecRequest) -> ExecResult:
        raise NotImplementedError(f"driver {self.id} chưa chạy được")

    # -- error classification -------------------------------------------------------------------
    def classify(self, raw: str) -> WorkerErrorClass:
        for kind, pat in self.extra_patterns:
            if pat.search(raw or ""):
                return kind
        return classify_text(raw)

    @staticmethod
    def error(kind: WorkerErrorClass, code: str, raw: str = "", **detail) -> WorkerError:
        return WorkerError.from_text(kind, code, raw=raw, **detail)

    @staticmethod
    def timed(fn, timeout_s: float) -> tuple[float, str]:
        """Chạy một lệnh probe có timeout; trả (số giây, output)."""
        start = time.time()
        try:
            r = subprocess.run(fn, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout_s)
            return time.time() - start, _clip((r.stdout or "") + (r.stderr or ""))
        except (OSError, subprocess.SubprocessError) as e:
            return time.time() - start, _clip(repr(e))
