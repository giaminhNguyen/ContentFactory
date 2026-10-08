"""Worker Runtime (W1): chuẩn hoá việc chạy công việc cho một agent CLI bất kỳ.

Nguyên tắc (xem Promtps/WORKER_RUNTIME_EXECUTION_PLAN.md):
  - Pipeline chỉ nói `work_type` (vd `story.write`), KHÔNG biết tên vendor/model.
  - Mọi khác biệt CLI nằm trong `drivers/`; `WorkerManager` chỉ làm việc với `worker_id`,
    `driver_id`, `model_id`, pool, policy và trạng thái.
  - Lỗi được chuẩn hoá về `WorkerErrorClass` (TEMPORARY | QUOTA | AUTH | TIMEOUT |
    INVALID_OUTPUT | UNKNOWN) để retry/fallback là bảng quyết định, không phải chuỗi regex rải rác.
"""
from __future__ import annotations

from .errors import WorkerError, WorkerErrorClass, classify_text
from .models import (
    MODEL_PROFILES,
    Attempt,
    AttemptState,
    ExecutionTarget,
    ModelProfile,
    PoolStrategy,
    Worker,
    WorkerModel,
    WorkerPool,
    WorkerStatus,
    WorkType,
)

__all__ = [
    "Attempt", "AttemptState", "ExecutionTarget", "MODEL_PROFILES", "ModelProfile", "PoolStrategy",
    "Worker", "WorkerError", "WorkerErrorClass", "WorkerModel", "WorkerPool", "WorkerStatus", "WorkType",
    "classify_text",
]
