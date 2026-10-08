"""Output validation gate (W1.14): exit 0 chưa phải là thành công.

Phần contract-specific (kiểm theo loại artifact) do orchestrator tiêm vào `WorkerManager`;
module này giữ phần kiểm CHUNG phải đúng với mọi work type: file tồn tại, đọc được,
không rỗng, không chứa marker bị cấm.
"""
from __future__ import annotations

from pathlib import Path

# marker mà validator cấm — fake driver cố tình sinh ra để test gate
FORBIDDEN_MARKER = "<<INVALID_OUTPUT>>"


def basic_errors(path: Path | str | None) -> list[str]:
    """Lỗi kiểm chung. Danh sách rỗng = pass."""
    if path is None:
        return ["không có output"]
    p = Path(path)
    if not p.exists():
        return [f"output không tồn tại: {p}"]
    if p.is_dir():
        return [f"output là thư mục, cần file: {p}"]
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ["output không phải UTF-8"]
    except OSError as e:
        return [f"không đọc được output: {e}"]
    if not text.strip():
        return ["output rỗng"]
    if FORBIDDEN_MARKER in text:
        return ["output chứa marker bị cấm"]
    return []


def validate_output(path: Path | str | None, work_type: str = "", extra=None) -> list[str]:
    """Kiểm chung trước, rồi kiểm contract-specific (nếu có). Rỗng = hợp lệ."""
    errors = basic_errors(path)
    if errors:
        return errors
    if extra is not None:
        return list(extra(Path(path), work_type))
    return []
