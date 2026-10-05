"""Parse phụ đề WebVTT/SRT thành cue chuẩn hóa. Không xóa timestamp; không coi một dòng caption là một câu.

Hai việc ở đây:
1. `parse_cues`: đọc nguyên văn thành cue thô (start, end, các dòng).
2. `normalize_cues`: làm sạch thẻ/HTML, bỏ chú thích âm thanh, khử "rolling caption" của auto-caption
   (mỗi cue lặp lại dòng cuối của cue trước), vẫn giữ nguyên thời gian.
Việc ghép cue thành câu/đoạn nằm ở reconstruct.py.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

TIMING = re.compile(
    r"(?P<a>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*(?P<b>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})")
TAG = re.compile(r"<[^>]*>")
SOUND_ONLY = re.compile(r"^\s*[\[(（【][^\])）】]{0,40}[\])）】]\s*$")
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


@dataclass
class RawCue:
    start: float
    end: float
    lines: list[str]


@dataclass
class Cue:
    start: float
    end: float
    text: str


def _seconds(ts: str) -> float:
    parts = ts.replace(",", ".").split(":")
    h, m, s = ([0.0] * (3 - len(parts)) + [float(p) for p in parts])
    return h * 3600 + m * 60 + s


def parse_cues(text: str) -> list[RawCue]:
    """WebVTT và SRT dùng chung một bộ đọc: khối = (id tùy chọn) + dòng thời gian + các dòng chữ."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[RawCue] = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            m = TIMING.search(line)
            if m:
                body = [x for x in lines[i + 1:]]
                cues.append(RawCue(_seconds(m["a"]), _seconds(m["b"]), body))
                break
    return cues


def clean_line(line: str) -> str:
    line = TAG.sub("", line)
    line = html.unescape(line).translate(ZERO_WIDTH).replace("\xa0", " ")
    line = re.sub(r"^\s*>>+\s*", "", line)                 # ký hiệu đổi người nói
    line = re.sub(r"[♪♫]+", " ", line)
    return re.sub(r"\s+", " ", line).strip()


def normalize_cues(raw: list[RawCue]) -> list[Cue]:
    out: list[Cue] = []
    prev_lines: list[str] = []
    for rc in sorted(raw, key=lambda c: (c.start, c.end)):
        lines = [l for l in (clean_line(x) for x in rc.lines) if l and not SOUND_ONLY.match(l)]
        if not lines:
            continue
        dur = rc.end - rc.start
        # rolling caption: dòng đã có ở cue liền trước, và cue này là kiểu "chồng" (nhiều dòng hoặc rất ngắn)
        rolling = len(prev_lines) >= 2 or len(lines) >= 2 or dur < 0.1
        new = [l for l in lines if not (rolling and l in prev_lines)]
        prev_lines = lines
        if not new:
            if out:
                out[-1].end = max(out[-1].end, rc.end)
            continue
        out.append(Cue(rc.start, max(rc.end, rc.start), " ".join(new)))
    return out


def parse_subtitle(text: str) -> list[Cue]:
    return normalize_cues(parse_cues(text))
