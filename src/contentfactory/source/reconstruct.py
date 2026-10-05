"""Dựng lại câu và đoạn từ cue + timestamp, rồi mới sinh clean transcript không timestamp.

Caption là đơn vị hiển thị, không phải câu: một câu thường bị cắt ngang giữa hai cue, còn auto-caption
thì không có dấu câu. Timestamp được dùng như metadata để quyết định tại MỖI ranh giới giữa hai cue:
  continue  - cue sau tiếp nối câu đang dở (caption bị cắt giữa câu)
  sentence  - kết thúc câu
  paragraph - kết thúc đoạn (pause dài, hoặc đoạn đã quá dài)
Phục hồi dấu câu ở mức an toàn: thêm dấu chấm cuối câu thiếu dấu và viết hoa chữ đầu câu. Vị trí và độ dài
các pause nằm giữa câu được giữ lại (`internal_pauses`) để một bước khôi phục dấu phẩy bằng LLM dùng sau này.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from .subtitles import Cue

PARSER_VERSION = "2"
TERMINAL = re.compile(r"[.!?…。！？]+[\"'”’»)\]]*$")
ELLIPSIS = re.compile(r"(\.{3}|…)[\"'”’»)\]]*$")


@dataclass(frozen=True)
class ReconstructConfig:
    sentence_gap: float = 0.8          # pause >= ngưỡng này (không dấu câu) => hết câu
    paragraph_gap: float = 2.0         # pause >= ngưỡng này => hết đoạn
    max_sentence_chars: int = 240      # câu dài hơn thì chấp nhận ngắt ở pause nhỏ
    paragraph_max_chars: int = 900     # đoạn dài hơn thì ngắt ở ranh giới câu kế tiếp
    restore_punctuation: bool = True
    capitalize: bool = True
    internal_pause_min: float = 0.3

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


def _first_alpha(text: str) -> str:
    return next((ch for ch in text if ch.isalpha()), "")


def _decide(prev: Cue, nxt: Cue, sent_chars: int, cfg: ReconstructConfig) -> str:
    gap = max(0.0, nxt.start - prev.end)
    if gap >= cfg.paragraph_gap:
        return "paragraph"
    if TERMINAL.search(prev.text):
        # "..." rồi chữ thường ngay sau => vẫn là một câu
        if ELLIPSIS.search(prev.text) and _first_alpha(nxt.text).islower() and gap < cfg.sentence_gap:
            return "continue"
        return "sentence"
    if gap >= cfg.sentence_gap:
        return "sentence"
    if sent_chars >= cfg.max_sentence_chars and gap >= 0.3:
        return "sentence"
    if sent_chars >= 2 * cfg.max_sentence_chars:
        return "sentence"
    return "continue"


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,.;:!?…])", r"\1", text)


_BOUNDARY = re.compile(r"[.!?…]+[\"'”’»)\]]*\s+")


def _split_inside(text: str) -> list[tuple[int, int]]:
    """Tách một đoạn đã nối từ nhiều cue thành các câu theo dấu câu có sẵn (sau dấu kết câu là chữ hoa)."""
    cuts, last = [], 0
    for m in _BOUNDARY.finditer(text):
        nxt = text[m.end():m.end() + 1]
        if nxt and (nxt.isupper() or (not nxt.isalpha() and _first_alpha(text[m.end():]).isupper())):
            cuts.append((last, m.end() - len(m.group()) + len(m.group().rstrip())))
            last = m.end()
    cuts.append((last, len(text)))
    return [(a, b) for a, b in cuts if text[a:b].strip()]


def build_structured(cues: list[Cue], cfg: ReconstructConfig, provenance: dict) -> dict:
    cue_rows = []
    for i, c in enumerate(cues):
        gap = max(0.0, c.start - cues[i - 1].end) if i else 0.0
        cue_rows.append({"i": i, "start": round(c.start, 3), "end": round(c.end, 3),
                         "text": c.text, "gap_before": round(gap, 3)})

    sentences: list[dict] = []
    paragraphs: list[dict] = []
    cur: list[int] = []
    para_start_sentence, para_chars = 0, 0

    def flush(decision: str) -> None:
        nonlocal cur, para_start_sentence, para_chars
        pauses, text = [], ""
        for k, ci in enumerate(cur):
            if k:
                gap = max(0.0, cues[ci].start - cues[cur[k - 1]].end)
                if gap >= cfg.internal_pause_min:
                    pauses.append({"offset": len(_tidy(text)), "gap": round(gap, 3)})
            text = (text + " " + cues[ci].text).strip()
        text = _tidy(text)
        pieces = _split_inside(text)                     # cue có thể chứa nhiều câu: "...cổng. Ông ấy"
        bounds, pos = [], 0                              # vị trí từng cue trong văn bản đã nối, để nội suy thời gian
        for ci in cur:
            bounds.append((pos, pos + len(cues[ci].text)))
            pos += len(cues[ci].text) + 1
        scale = len(text) / max(1, pos - 1)

        def locate(p: float) -> tuple[int, float]:
            rp = p / scale
            for k, (s0, e0) in enumerate(bounds):
                if rp <= e0 or k == len(bounds) - 1:
                    return k, min(1.0, max(0.0, (rp - s0) / max(1, e0 - s0)))
            return 0, 0.0

        def time_at(p: float) -> float:
            k, frac = locate(p)
            c = cues[cur[k]]
            return c.start + (c.end - c.start) * frac

        for n, (a, b) in enumerate(pieces):
            piece = text[a:b]
            added = capitalized = False
            if n == len(pieces) - 1 and cfg.restore_punctuation and not TERMINAL.search(piece):
                piece, added = piece + ".", True
            if cfg.capitalize and _first_alpha(piece).islower():
                idx = next(j for j, ch in enumerate(piece) if ch.isalpha())
                piece, capitalized = piece[:idx] + piece[idx].upper() + piece[idx + 1:], True
            start, end = time_at(a), time_at(b)
            prev_end = sentences[-1]["end"] if sentences else None
            sentences.append({
                "i": len(sentences), "start": round(start, 3), "end": round(end, 3), "text": piece,
                "gap_before": round(max(0.0, start - prev_end), 3) if prev_end is not None and n == 0 else 0.0,
                "cue_range": [cur[locate(a)[0]], cur[locate(max(a, b - 1))[0]]],
                "interpolated_time": len(pieces) > 1 or len(cur) > 1,
                "punctuation_added": added, "capitalized": capitalized,
                "internal_pauses": [{"offset": q["offset"] - a, "gap": q["gap"]} for q in pauses if a <= q["offset"] < b]})
            para_chars += len(piece)
        cur = []
        if decision == "paragraph" or para_chars >= cfg.paragraph_max_chars:
            first = sentences[para_start_sentence]
            prev_para_end = paragraphs[-1]["end"] if paragraphs else None
            paragraphs.append({"i": len(paragraphs), "sentence_range": [para_start_sentence, len(sentences) - 1],
                               "start": first["start"], "end": sentences[-1]["end"],
                               "gap_before": round(max(0.0, first["start"] - prev_para_end), 3)
                               if prev_para_end is not None else 0.0})
            para_start_sentence, para_chars = len(sentences), 0

    for i, c in enumerate(cues):
        cur.append(i)
        if i + 1 == len(cues):
            flush("paragraph")
            break
        sent_chars = sum(len(cues[j].text) + 1 for j in cur)
        d = _decide(c, cues[i + 1], sent_chars, cfg)
        if d != "continue":
            flush(d)

    for p in paragraphs:
        for s in sentences[p["sentence_range"][0]: p["sentence_range"][1] + 1]:
            s["paragraph"] = p["i"]

    clean = clean_text({"sentences": sentences, "paragraphs": paragraphs})
    return {"schema": 1,
            "provenance": {**provenance, "parser_version": PARSER_VERSION, "config": asdict(cfg),
                           "config_hash": cfg.hash()},
            "cues": cue_rows, "sentences": sentences, "paragraphs": paragraphs,
            "stats": {"cues": len(cues), "sentences": len(sentences), "paragraphs": len(paragraphs),
                      "punctuation_added": sum(s["punctuation_added"] for s in sentences),
                      "duration_sec": round(cues[-1].end - cues[0].start, 3) if cues else 0.0},
            "clean_sha256": hashlib.sha256(clean.encode("utf-8")).hexdigest()}


def build_structured_plain(text: str, cfg: ReconstructConfig, provenance: dict) -> dict:
    """Văn bản thuần KHÔNG có timestamp (PlainTextProvider / file .txt): cùng schema, thời gian là None."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) == 1 and "\n" in blocks[0].strip():
        lines = [l.strip() for l in blocks[0].splitlines() if l.strip()]
        # đa số dòng tự kết thúc câu => mỗi dòng là một đoạn; ngược lại là văn bản bị ngắt dòng cứng => nối lại
        blocks = lines if sum(bool(TERMINAL.search(l)) for l in lines) >= 0.6 * len(lines) else [" ".join(lines)]
    sentences: list[dict] = []
    paragraphs: list[dict] = []
    for block in blocks:
        clean = _tidy(block)
        pieces = _split_inside(clean)
        start, chars = len(sentences), 0
        for n, (a, b) in enumerate(pieces):
            piece, added = clean[a:b], False
            if n == len(pieces) - 1 and cfg.restore_punctuation and not TERMINAL.search(piece):
                piece, added = piece + ".", True
            sentences.append({"i": len(sentences), "start": None, "end": None, "text": piece, "gap_before": None,
                              "cue_range": None, "interpolated_time": False, "punctuation_added": added,
                              "capitalized": False, "internal_pauses": []})
            chars += len(piece)
            if chars >= cfg.paragraph_max_chars and n < len(pieces) - 1:      # đoạn quá dài: ngắt ở ranh giới câu
                paragraphs.append({"i": len(paragraphs), "sentence_range": [start, len(sentences) - 1],
                                   "start": None, "end": None, "gap_before": None})
                start, chars = len(sentences), 0
        paragraphs.append({"i": len(paragraphs), "sentence_range": [start, len(sentences) - 1],
                           "start": None, "end": None, "gap_before": None})
    for p in paragraphs:
        for s in sentences[p["sentence_range"][0]: p["sentence_range"][1] + 1]:
            s["paragraph"] = p["i"]
    clean_out = clean_text({"sentences": sentences, "paragraphs": paragraphs}) if sentences else ""
    return {"schema": 1, "provenance": {**provenance, "parser_version": PARSER_VERSION, "config": asdict(cfg),
                                        "config_hash": cfg.hash()},
            "cues": [], "sentences": sentences, "paragraphs": paragraphs,
            "stats": {"cues": 0, "sentences": len(sentences), "paragraphs": len(paragraphs),
                      "punctuation_added": sum(s["punctuation_added"] for s in sentences), "duration_sec": None},
            "clean_sha256": hashlib.sha256(clean_out.encode("utf-8")).hexdigest()}


def clean_text(structured: dict) -> str:
    """Clean transcript: đoạn cách nhau một dòng trống, câu trong đoạn nối bằng dấu cách, không timestamp."""
    sents = structured["sentences"]
    paras = [" ".join(s["text"] for s in sents[a: b + 1])
             for a, b in (p["sentence_range"] for p in structured["paragraphs"])]
    return "\n\n".join(paras) + "\n"
