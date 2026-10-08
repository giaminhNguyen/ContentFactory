"""Domain model của Worker Runtime (W1.1).

Tối thiểu nhưng đủ dùng cho toàn bộ W1:

    WorkType         tên công việc mà pipeline yêu cầu, vd "story.write"
    Worker           một instance CLI cụ thể trên máy (driver + executable + model)
    Driver           khác biệt kỹ thuật của một loại CLI (ở workers/drivers/)
    WorkerPool       nhóm worker theo thứ tự ưu tiên để router chọn
    Attempt          kết quả của MỘT lần thử, bất biến về kết quả
    ExecutionTarget  Worker + model sẽ thực thi

Quan trọng: worker chỉ có `driver_id` (chuỗi), KHÔNG có nhánh `if claude...` ở tầng này.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum

# Một work type là chuỗi do routing config định nghĩa ("story.write", "story.branch", ...).
# Không có registry cứng: thêm work type = thêm một dòng config, không sửa pipeline.
WorkType = str

MODEL_PROFILES = ("fast", "balanced", "high")
ModelProfile = str


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class WorkerStatus(str, Enum):
    DETECTED = "DETECTED"          # thấy CLI trên PATH nhưng chưa probe
    READY = "READY"                # probe xong, chạy được
    AUTH_REQUIRED = "AUTH_REQUIRED"  # CLI có mặt nhưng chưa đăng nhập
    BROKEN = "BROKEN"              # probe lỗi / binary hỏng
    DISABLED = "DISABLED"          # người dùng tắt
    NOT_FOUND = "NOT_FOUND"        # không tìm thấy executable (lịch sử vẫn giữ)


class PoolStrategy(str, Enum):
    PRIORITY = "priority"          # theo thứ tự trong pool
    LEAST_BUSY = "least_busy"      # ít việc nhất trong các worker đủ điều kiện


class AttemptState(str, Enum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"            # process xong + output qua validation
    FAILED = "FAILED"              # process lỗi / worker không chạy được
    INVALID = "INVALID"            # exit 0 nhưng output không qua validation gate


@dataclass
class WorkerModel:
    """Một model mà worker khai báo được (driver liệt kê hoặc người dùng thêm tay)."""
    id: str
    enabled: bool = True
    default: bool = False
    source: str = "driver"         # driver | manual
    note: str = ""


@dataclass
class Worker:
    """Một CLI cụ thể mà ContentFactory có thể gọi. `id` là khoá ổn định: disable/xóa CLI trên OS
    không đổi `id`, nên lịch sử attempt cũ vẫn tra được."""
    id: str
    name: str                      # tên hiển thị (người dùng sửa được)
    driver_id: str                 # "fake" | "claude_cli" | "codex_cli" | ... (không có nhánh vendor ở đây)
    executable: str                # đường dẫn tuyệt đối hoặc tên trên PATH
    status: WorkerStatus = WorkerStatus.DETECTED
    enabled: bool = True
    models: list[WorkerModel] = field(default_factory=list)
    profiles: dict = field(default_factory=dict)      # {"fast": "model-id", "balanced": ..., "high": ...}
    source: str = "manual"                           # scan | manual
    concurrency: int = 1                              # số attempt chạy song song tối đa
    timeout_s: float = 3600.0
    failure_streak: int = 0                           # W1.10: dãy lỗi liên tiếp
    cooldown_until: float = 0.0                       # epoch; > now nghĩa là đang cooldown
    meta: dict = field(default_factory=dict)          # driver-specific: version, auth hint, ...
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def default_model(self) -> str | None:
        for m in self.models:
            if m.default and m.enabled:
                return m.id
        enabled = [m.id for m in self.models if m.enabled]
        return enabled[0] if enabled else None

    def model_for(self, profile: ModelProfile | None) -> str | None:
        """Model theo profile (fast/balanced/high); không có -> model mặc định."""
        if profile:
            mid = self.profiles.get(profile)
            if mid:
                return mid
        return self.default_model

    def has_model(self, model: str | None) -> bool:
        """model=None là hợp lệ (= dùng mặc định)."""
        if not model:
            return True
        return any(m.id == model and m.enabled for m in self.models)

    def in_cooldown(self, now: float | None = None) -> bool:
        return self.cooldown_until > (now if now is not None else time.time())

    def routable(self, now: float | None = None) -> tuple[bool, str]:
        """(được chọn đi route?, lý do loại) — một nguồn sự thật duy nhất cho router + UI + simulator."""
        now = time.time() if now is None else now
        if not self.enabled or self.status is WorkerStatus.DISABLED:
            return False, "disabled"
        if self.status is WorkerStatus.NOT_FOUND:
            return False, "executable not found"
        if self.status is WorkerStatus.BROKEN:
            return False, "broken"
        if self.status is WorkerStatus.AUTH_REQUIRED:
            return False, "auth required"
        if self.in_cooldown(now):
            return False, "cooldown"
        if self.status is WorkerStatus.DETECTED:
            return False, "not probed yet"
        return True, "ready"

    def with_updates(self, **kw) -> "Worker":
        kw.setdefault("updated_at", time.time())
        return replace(self, **kw)


@dataclass
class WorkerPool:
    """Nhóm worker. `members` là danh sách worker_id SẮP THEO THỨ TỰ ưu tiên (priority)."""
    name: str                      # khoá, vd "story_workers"
    display_name: str = ""
    strategy: PoolStrategy = PoolStrategy.PRIORITY
    members: list[str] = field(default_factory=list)
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def with_updates(self, **kw) -> "WorkerPool":
        kw.setdefault("updated_at", time.time())
        return replace(self, **kw)


@dataclass
class ExecutionTarget:
    """Kết quả router chọn: worker nào + model nào cho lần chạy này."""
    worker: Worker
    model: str | None = None
    pool: str = ""
    reason: str = ""               # vì sao chọn (hiện trong simulator/timeline)


@dataclass(frozen=True)
class Attempt:
    """MỘT lần thử. Bất biến về kết quả: retry/fallback tạo Attempt MỚI, không ghi đè attempt cũ."""
    attempt_id: str
    work_type: WorkType
    worker_id: str
    worker_name: str = ""
    driver_id: str = ""
    model_id: str | None = None
    pool: str = ""
    job_id: str = ""
    stage: str = ""
    state: AttemptState = AttemptState.RUNNING
    started_at: float = 0.0
    ended_at: float | None = None
    error_kind: str = ""           # WorkerErrorClass.value khi thất bại
    error_code: str = ""
    error_message: str = ""
    workspace: str = ""            # attempt workspace riêng (W1.12)
    validation: list[str] = field(default_factory=list)   # lỗi validation (rỗng = pass)
    promoted: bool = False         # chỉ attempt thành công hiện hành được promote
    session_id: str | None = None
    cost_usd: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        if self.ended_at is None:
            return 0.0
        return max(0.0, self.ended_at - self.started_at)

    @property
    def ok(self) -> bool:
        return self.state is AttemptState.SUCCESS
