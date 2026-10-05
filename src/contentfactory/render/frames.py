"""Frame PNG trong suốt đúng kích thước (stdlib: zlib + struct). ContentFlow lấy kích thước output từ frame, nên cần một frame cho mỗi profile."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

from ..fsutil import atomic_write_bytes


def transparent_png(width: int, height: int) -> bytes:
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    row = b"\x00" + b"\x00\x00\x00\x00" * width            # filter 0 + RGBA(0,0,0,0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(row * height, 9)) + chunk(b"IEND", b""))


def ensure_frame(frames_dir: Path, width: int, height: int) -> Path:
    p = Path(frames_dir) / f"frame_{width}x{height}.png"
    if not p.is_file() or p.stat().st_size == 0:
        atomic_write_bytes(p, transparent_png(width, height))
    return p
