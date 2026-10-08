"""Lỗi chuẩn hoá của Worker Runtime (W1.8).

Mỗi CLI nói một kiểu lỗi khác nhau; fallback chỉ đáng tin khi mọi driver đều dồn về CÙNG một tập
trạng thái. W1 dừng ở 6 loại (không cần taxonomy 20+):

    TEMPORARY       lỗi tạm thời -> retry worker này N lần, rồi sang worker kế
    QUOTA           hết quota/token -> block worker, sang worker kế (KHÔNG rotate account)
    AUTH            thiếu/hết đăng nhập -> loại worker khỏi candidate, sang worker kế
    TIMEOUT         quá hạn -> retry ít lần, rồi sang worker kế
    INVALID_OUTPUT  process chạy nhưng output không qua validation -> retry/repair bounded
    UNKNOWN         không nhận dạng được -> retry/fallback có giới hạn (không loop vô hạn)

Cầu nối với hệ thống hiện có: pipeline dùng `contracts.StageError`/`ErrorClass`, nên có
`to_stage_error()` / `from_stage_error()` để hai phía nói cùng một chuyện.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from ..contracts import ErrorClass, StageError

# snippet lỗi tối đa giữ lại để hiện UI (KHÔNGIn log secret)
RAW_LIMIT = 400


class WorkerErrorClass(str, Enum):
    TEMPORARY = "TEMPORARY"
    QUOTA = "QUOTA"
    AUTH = "AUTH"
    TIMEOUT = "TIMEOUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    UNKNOWN = "UNKNOWN"


class WorkerInUse(ValueError):
    """Xoá worker đang nằm trong pool: nêu đúng pool nào để UI/API báo dependency (không xoá im lặng)."""

    def __init__(self, worker_id: str, pools: list[str]) -> None:
        self.worker_id = worker_id
        self.pools = pools
        super().__init__(f"worker {worker_id} đang thuộc pool: {', '.join(pools)}")


class PoolInUse(ValueError):
    """Xoá pool đang được một work_type chỉ tới: nêu work_type nào để UI/API báo dependency."""

    def __init__(self, pool_name: str, work_types: list[str]) -> None:
        self.pool_name = pool_name
        self.work_types = work_types
        super().__init__(f"pool {pool_name!r} đang được routing của: {', '.join(work_types)}")


# Mẫu nhận dạng dùng CHUNG cho mọi driver (driver chỉ thêm mẫu riêng của mình khi thật sự cần).
_PATTERNS: tuple[tuple[WorkerErrorClass, re.Pattern], ...] = (
    (WorkerErrorClass.QUOTA, re.compile(
        r"usage limit|rate limit|too many requests|credit balance|out of (?:credits|quota)|quota exceeded|"
        r"quota exhausted|billing limit|insufficient credit", re.I)),
    (WorkerErrorClass.AUTH, re.compile(
        r"not logged in|invalid api key|please run /login|authentication|unauthorized|forbidden|"
        r"expired token|invalid token|permission denied", re.I)),
    (WorkerErrorClass.TIMEOUT, re.compile(
        r"\btimeout\b|timed out|deadline exceeded|no activity|idle[_ ]timeout", re.I)),
    (WorkerErrorClass.TEMPORARY, re.compile(
        r"overloaded|temporarily unavailable|econnreset|connection reset|network error|"
        r"try again|service unavailable|internal server error|502 bad gateway|503", re.I)),
)


def classify_text(raw: str) -> WorkerErrorClass:
    """Đưa lỗi dạng văn bản (stderr/stdout/exception) về một trong 6 loại chung."""
    text = raw or ""
    for kind, pat in _PATTERNS:
        if pat.search(text):
            return kind
    return WorkerErrorClass.UNKNOWN


@dataclass
class WorkerError:
    """Một lỗi của worker, có metadata có cấu trúc để retry/fallback và UI cùng dùng."""
    kind: WorkerErrorClass
    code: str
    message: str = ""
    detail: dict = field(default_factory=dict)
    raw: str = ""                      # trích đoạn lỗi thô (đã cắt ngắn) để hiển thị
    retry_after_s: float | None = None  # Retry-After của provider nếu đọc được

    def __post_init__(self) -> None:
        if self.raw and len(self.raw) > RAW_LIMIT:
            self.raw = self.raw[:RAW_LIMIT]

    @classmethod
    def from_text(cls, kind: WorkerErrorClass, code: str, raw: str = "", message: str = "",
                  retry_after_s: float | None = None, **detail) -> "WorkerError":
        return cls(kind=kind, code=code, message=message or raw[:RAW_LIMIT], raw=raw, detail=detail,
                   retry_after_s=retry_after_s)

    @property
    def retryable(self) -> bool:
        """Có đáng thử lại (worker này hoặc worker khác) hay không — UNKNOWN vẫn retry CÓ GIỚI HẠN."""
        return True

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "code": self.code, "message": self.message, "detail": self.detail,
                "raw": self.raw, "retry_after_s": self.retry_after_s}

    # -- cầu nối pipeline hiện có ---------------------------------------------------------------
    def to_stage_error(self) -> StageError:
        """Khi đã hết retry/fallback mà vẫn lỗi: trả về StageError để orchestrator quyết định hold/fail."""
        ec = {
            WorkerErrorClass.TEMPORARY: (ErrorClass.TRANSIENT, "runtime"),
            WorkerErrorClass.TIMEOUT: (ErrorClass.TRANSIENT, "runtime"),
            WorkerErrorClass.QUOTA: (ErrorClass.RESOURCE, "quota"),
            WorkerErrorClass.AUTH: (ErrorClass.AUTH, None),
            # output không hợp lệ mà mọi worker đều thế => lỗi của đầu vào/công việc, không phải lỗi tạm thời
            WorkerErrorClass.INVALID_OUTPUT: (ErrorClass.POLICY, "output"),
            WorkerErrorClass.UNKNOWN: (ErrorClass.AMBIGUOUS, None),
        }[self.kind]
        return StageError(ec[0], self.code, self.message or self.raw, dict(self.detail),
                          resource=ec[1], retry_after_s=self.retry_after_s)

    @classmethod
    def from_stage_error(cls, err: StageError) -> "WorkerError":
        """Đọc StageError mà adapter/driver ném ra (vd CLAUDE_USAGE_LIMIT) về loại lỗi chung."""
        code = err.code
        if err.error_class == ErrorClass.AUTH:
            kind = WorkerErrorClass.AUTH
        elif err.error_class == ErrorClass.RESOURCE:
            kind = WorkerErrorClass.QUOTA if (err.resource or "") in ("quota", "token") else WorkerErrorClass.TEMPORARY
        elif err.error_class in (ErrorClass.TRANSIENT, ErrorClass.AMBIGUOUS):
            kind = WorkerErrorClass.TIMEOUT if re.search(r"timeout|timed[_ ]out|no activity", code + " " + err.message, re.I) \
                else WorkerErrorClass.TEMPORARY
        else:
            kind = WorkerErrorClass.INVALID_OUTPUT if err.resource == "output" else WorkerErrorClass.UNKNOWN
        return cls(kind=kind, code=code, message=err.message, detail=dict(err.detail), raw=err.message,
                   retry_after_s=err.retry_after_s)
