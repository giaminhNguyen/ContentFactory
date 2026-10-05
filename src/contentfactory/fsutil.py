"""Tiện ích file dùng chung (stdlib). Module được phép import file này."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable


def atomic_write(path: Path, write: Callable[[Path], None]) -> Path:
    """Gọi write(tmp) rồi os.replace(tmp, path): path chỉ tồn tại khi file hoàn chỉnh."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    return atomic_write(path, lambda t: t.write_bytes(data))


def atomic_write_text(path: Path, text: str) -> Path:
    return atomic_write(path, lambda t: t.write_text(text, encoding="utf-8", newline="\n"))


def atomic_write_json(path: Path, obj: Any) -> Path:
    return atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def sha256_file(path: Path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(bufsize):
            h.update(chunk)
    return h.hexdigest()
