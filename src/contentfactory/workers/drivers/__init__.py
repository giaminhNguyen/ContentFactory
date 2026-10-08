"""Registry driver: ánh xạ `driver_id` -> lớp driver.

Đây là nơi DUY NHẤT được phép biết danh sách vendor. `WorkerManager`, router, UI chỉ đưa
`driver_id` vào đây lấy ra hợp đồng, nên thêm CLI mới = thêm một dòng ở đây, không sửa core.
"""
from __future__ import annotations

from .base import BaseDriver, DetectedExecutable, Driver, ExecRequest, ExecResult, ProbeResult
from .claude_cli import ClaudeCliDriver
from .codex_cli import CodexCliDriver
from .fake import FORBIDDEN_MARKER, FakeDriver
from .gemini_cli import GeminiCliDriver
from .opencode_cli import OpencodeCliDriver

CLASSES: dict[str, type[BaseDriver]] = {
    "fake": FakeDriver,
    "claude_cli": ClaudeCliDriver,
    "codex_cli": CodexCliDriver,
    "gemini_cli": GeminiCliDriver,
    "opencode_cli": OpencodeCliDriver,
}


def driver_ids() -> list[str]:
    return sorted(CLASSES)


def build(driver_id: str, cfg: dict | None = None) -> BaseDriver:
    cls = CLASSES.get(driver_id)
    if cls is None:
        raise ValueError(f"driver {driver_id!r} không tồn tại; hợp lệ: {driver_ids()}")
    return cls(cfg)


__all__ = [
    "BaseDriver", "CLASSES", "ClaudeCliDriver", "CodexCliDriver", "DetectedExecutable", "Driver",
    "ExecRequest", "ExecResult", "FakeDriver", "FORBIDDEN_MARKER", "GeminiCliDriver",
    "OpencodeCliDriver", "ProbeResult", "build", "driver_ids",
]
