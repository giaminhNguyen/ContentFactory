"""Tiện ích file dùng chung (stdlib). Module được phép import file này."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable


def atomic_write(path: Path, write: Callable[[Path], None]) -> Path:
    """Gọi write(tmp) rồi os.replace(tmp, path): path chỉ tồn tại khi file hoàn chỉnh."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # tên tạm riêng cho từng người ghi: hai job ghi cùng một file dùng chung (cache) không được dẫm lên file tạm của nhau
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    try:
        write(tmp)
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:                         # Windows: đích đang được mở/thay bởi tiến trình khác -> thử lại ngắn rồi mới báo lỗi
                if attempt == 7:
                    raise
                time.sleep(0.02 * (attempt + 1))
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


def wav_header(path: Path) -> dict:
    """Đọc header WAV bằng stdlib (module `wave` không đọc được WAVE_FORMAT_EXTENSIBLE/float mà ffmpeg và nhiều engine TTS ghi).

    Trả {format_tag (1=PCM, 3=float), channels, rate, bits, align, data_offset, data_size, frames, duration}. Raise ValueError nếu không phải
    RIFF/WAVE hợp lệ. Phần data bị cắt cụt/ghi dở được kẹp theo kích thước file thật (nên frames phản ánh dữ liệu thực có trong file).
    """
    import struct
    size = Path(path).stat().st_size
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
            raise ValueError("không phải RIFF/WAVE")
        fmt = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                raise ValueError("thiếu chunk data")
            cid, csz = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                body = f.read(csz + (csz & 1))
                tag, ch, rate, _, align, bits = struct.unpack("<HHIIHH", body[:16])
                if tag == 0xFFFE and csz >= 26:
                    tag = struct.unpack("<H", body[24:26])[0]       # SubFormat GUID bắt đầu bằng format tag thật
                fmt = {"format_tag": tag, "channels": ch, "rate": rate, "bits": bits, "align": align}
            elif cid == b"data":
                if fmt is None:
                    raise ValueError("data trước fmt")
                off = f.tell()
                dsz = min(csz, size - off) if csz != 0xFFFFFFFF else size - off
                dsz = max(0, dsz)
                align = fmt["align"] or (fmt["channels"] * fmt["bits"] // 8) or 1
                frames = dsz // align
                return {**fmt, "data_offset": off, "data_size": frames * align, "frames": frames,
                        "duration": frames / fmt["rate"] if fmt["rate"] else 0.0}
            else:
                f.seek(csz + (csz & 1), 1)
