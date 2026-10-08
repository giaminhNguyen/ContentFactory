"""Registry driver: ánh xạ `driver_id` -> lớp driver.

Đây là nơi DUY NHẤT được phép biết danh sách vendor. `WorkerManager`, router, UI chỉ đưa
`driver_id` vào đây lấy ra hợp đồng, nên thêm CLI mới = thêm một dòng ở đây, không sửa core.
"""
from __future__ import annotations

from .base import BaseDriver, DetectedExecutable, Driver, ExecRequest, ExecResult, ProbeResult
from .fake import FORBIDDEN_MARKER, FakeDriver

CLASSES: dict[str, type[BaseDriver]] = {
    "fake": FakeDriver,
}


def driver_ids() -> list[str]:
    return sorted(CLASSES)


def build(driver_id: str, cfg: dict | None = None) -> BaseDriver:
    cls = CLASSES.get(driver_id)
    if cls is None:
        raise ValueError(f"driver {driver_id!r} không tồn tại; hợp lệ: {driver_ids()}")
    return cls(cfg)


__all__ = [
    "BaseDriver", "CLASSES", "DetectedExecutable", "Driver", "ExecRequest", "ExecResult", "FakeDriver",
    "FORBIDDEN_MARKER", "ProbeResult", "build", "driver_ids",
]
