"""WAV PCM bằng stdlib: ghép, chèn im lặng, cắt kèm fade ở điểm cắt. Không giải mã/đổi định dạng (việc của ffmpeg).

Chỉ dùng cho file ĐÃ ở định dạng chuẩn (do chuỗi xử lý sinh ra) nên mọi thao tác ở đây là lossless và streaming
(không nạp cả file vào RAM). PCM 16/24/32-bit.
"""
from __future__ import annotations

import array
import sys
import wave
from pathlib import Path

from ..fsutil import wav_header

CHUNK_FRAMES = 1 << 16


def info(path: Path) -> dict:
    h = wav_header(path)
    if h["format_tag"] != 1:
        raise ValueError(f"{Path(path).name}: chỉ hỗ trợ PCM số nguyên (format_tag={h['format_tag']})")
    return {"channels": h["channels"], "width": h["bits"] // 8, "rate": h["rate"], "frames": h["frames"], "duration": h["duration"],
            "offset": h["data_offset"], "align": h["align"]}


def read_frames(path: Path, i: dict, start: int, count: int):
    """Đọc streaming `count` frame từ frame `start` (đọc trực tiếp vùng data: không phụ thuộc module wave)."""
    with open(path, "rb") as f:
        f.seek(i["offset"] + start * i["align"])
        left = count
        while left > 0:
            b = f.read(min(CHUNK_FRAMES, left) * i["align"])
            if not b:
                return
            yield b
            left -= len(b) // i["align"]


def _open_out(path: Path, ch: int, width: int, rate: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    w = wave.open(str(path), "wb")
    w.setnchannels(ch)
    w.setsampwidth(width)
    w.setframerate(rate)
    return w


def write_silence(path: Path, ms: float, rate: int, ch: int, width: int) -> Path:
    n = int(round(rate * ms / 1000))
    with _open_out(path, ch, width, rate) as w:
        w.writeframes(b"\x00" * (n * ch * width))
    return path


def concat(parts: list[Path], out: Path) -> dict:
    """Nối các WAV cùng định dạng thành `out` (ghi `.part` rồi rename). Trả {frames, duration, offsets: [giây bắt đầu của từng part]}."""
    if not parts:
        raise ValueError("không có file để nối")
    first = info(parts[0])
    tmp = out.with_name(out.name + ".part")
    offsets, total = [], 0
    try:
        with _open_out(tmp, first["channels"], first["width"], first["rate"]) as w:
            for p in parts:
                i = info(p)
                if (i["channels"], i["width"], i["rate"]) != (first["channels"], first["width"], first["rate"]):
                    raise ValueError(f"{p.name}: định dạng khác ({i['channels']}ch/{i['width']*8}bit/{i['rate']}Hz) "
                                     f"so với {parts[0].name}")
                offsets.append(total / first["rate"])
                for b in read_frames(p, i, 0, i["frames"]):
                    w.writeframes(b)
                total += i["frames"]
        tmp.replace(out)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"frames": total, "duration": total / first["rate"], "offsets": offsets, **{k: first[k] for k in ("channels", "width", "rate")}}


def _decode(b: bytes, width: int) -> list[int]:
    if width == 2:
        a = array.array("h")
        a.frombytes(b)
        if sys.byteorder == "big":
            a.byteswap()
        return list(a)
    if width == 4:
        a = array.array("i")
        a.frombytes(b)
        if sys.byteorder == "big":
            a.byteswap()
        return list(a)
    return [int.from_bytes(b[i:i + 3], "little", signed=True) for i in range(0, len(b), 3)]


def _encode(vals: list[int], width: int) -> bytes:
    if width in (2, 4):
        a = array.array("h" if width == 2 else "i", vals)
        if sys.byteorder == "big":
            a.byteswap()
        return a.tobytes()
    return b"".join(int(v).to_bytes(3, "little", signed=True) for v in vals)


def _fade(frames: bytes, width: int, ch: int, fade_in: bool, fade_frames: int) -> bytes:
    """Fade tuyến tính trên `fade_frames` frame đầu (fade_in) hoặc cuối (fade-out) của `frames`."""
    vals = _decode(frames, width)
    n = len(vals) // ch
    k = min(fade_frames, n)
    for i in range(k):
        g = (i + 0.5) / k if fade_in else 1 - (i + 0.5) / k
        pos = i if fade_in else n - k + i
        for c in range(ch):
            vals[pos * ch + c] = int(vals[pos * ch + c] * g)
    return _encode(vals, width)


def slice_wav(src: Path, dst: Path, start_sec: float, end_sec: float, fade_ms: float = 0.0) -> dict:
    """Cắt [start, end) ra `dst`, fade-in/out `fade_ms` ở hai đầu để điểm cắt không gây click. Streaming."""
    i = info(src)
    rate, ch, width = i["rate"], i["channels"], i["width"]
    a, b = max(0, int(round(start_sec * rate))), min(i["frames"], int(round(end_sec * rate)))
    if b <= a:
        raise ValueError(f"đoạn cắt rỗng [{start_sec}, {end_sec}]")
    fade = int(rate * fade_ms / 1000)
    tmp = dst.with_name(dst.name + ".part")
    try:
        with _open_out(tmp, ch, width, rate) as w:
            left, first = b - a, True
            for data in read_frames(src, i, a, b - a):
                got = len(data) // (ch * width)
                left -= got
                if fade and first:
                    data = _fade(data, width, ch, True, fade)
                first = False
                if fade and left <= 0:
                    data = _fade(data, width, ch, False, fade)
                w.writeframes(data)
        tmp.replace(dst)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"frames": b - a, "duration": (b - a) / rate}
