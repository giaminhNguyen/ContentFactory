"""Kiểm tra chunk audio tất định bằng stdlib (WAV PCM 16-bit): hỏng file, rỗng, im lặng, độ dài bất thường.

Định dạng khác WAV: không kiểm được ở đây (cần ffprobe ở Phase 4) => trả stats=None và không kết luận gì.
"""
from __future__ import annotations

import array
import sys
import wave
from pathlib import Path

from ..fsutil import wav_header

SILENT_AMPLITUDE = 64          # |mẫu| < 64/32768 coi là im lặng (~ -54 dBFS)
LONG_SLACK_SEC = 1.5           # engine luôn đệm im lặng ~0.3-0.5s + kéo dài từ ngắn ("Ting.") => câu rất ngắn không bị đánh trượt oan


def wav_stats(path: Path) -> dict | None:
    try:
        with wave.open(str(path), "rb") as w:
            n, rate, width, ch = w.getnframes(), w.getframerate(), w.getsampwidth(), w.getnchannels()
            frames = w.readframes(n)
    except (wave.Error, EOFError):
        try:                                       # WAVE_FORMAT_EXTENSIBLE / float: module wave không đọc được nhưng file vẫn hợp lệ
            h = wav_header(path)
            ok = h["frames"] > 0 and h["format_tag"] in (1, 3)
            return {"decodable": ok, "duration_sec": h["duration"], "silence_ratio": 0.0 if ok else 1.0,
                    "sample_rate": h["rate"], "channels": h["channels"]}
        except (ValueError, OSError):
            return {"decodable": False, "duration_sec": 0.0, "silence_ratio": 1.0, "sample_rate": 0, "channels": 0}
    except OSError:
        return None
    if width != 2 or not n:
        return {"decodable": bool(n), "duration_sec": n / rate if rate else 0.0, "silence_ratio": 0.0 if n else 1.0,
                "sample_rate": rate, "channels": ch}
    a = array.array("h")
    a.frombytes(frames)
    if sys.byteorder == "big":
        a.byteswap()
    quiet = sum(1 for v in a if -SILENT_AMPLITUDE < v < SILENT_AMPLITUDE)
    return {"decodable": True, "duration_sec": n / rate, "silence_ratio": quiet / len(a), "sample_rate": rate, "channels": ch}


def chunk_issues(path: Path, text: str, qa: dict) -> list[str]:
    """Danh sách vấn đề của một chunk (rỗng = đạt). `qa` là profile["qa"]."""
    st = wav_stats(path)
    if st is None:
        return []
    if not st["decodable"]:
        return ["UNDECODABLE"]
    issues = []
    if st["duration_sec"] < float(qa.get("min_duration_sec") or 0):
        issues.append("TOO_SHORT")
    if st["silence_ratio"] > float(qa.get("silence_ratio_max") or 1.0):
        issues.append("SILENT")
    cps = qa.get("duration_chars_per_sec")             # [min, max] ký tự/giây; None = không kiểm
    if cps and st["duration_sec"] > 0:
        rate = len(text) / st["duration_sec"]
        if rate > cps[1]:
            issues.append("DURATION_TOO_SHORT_FOR_TEXT")     # đọc nhanh bất thường / thiếu chữ
        elif st["duration_sec"] > len(text) / cps[0] + LONG_SLACK_SEC:
            issues.append("DURATION_TOO_LONG_FOR_TEXT")      # kéo dài / lặp / im lặng chèn vào
    return issues
