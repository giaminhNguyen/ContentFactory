"""Story Assembler: các section/chương nội bộ -> MỘT văn bản liền mạch (story.txt).

Tất định, độc lập với engine sinh truyện. Việc làm:
  1. gỡ heading chương/section, đường kẻ, marker kỹ thuật, dòng "hết chương/còn tiếp", đoạn "tóm tắt chương trước";
  2. mỗi dòng chữ là một đoạn (oh-story ghi đoạn cách nhau một '\\n') -> xuất đoạn cách nhau MỘT DÒNG TRỐNG
     (bước TTS tách theo dòng trống);
  3. làm mượt chỗ nối section: bỏ phần đầu section lặp lại đuôi section trước; nối lại câu bị cắt giữa chừng;
  4. phát hiện trùng lặp vô tình: câu lặp liền kề, đoạn trùng khít, đoạn gần trùng (shingle Jaccard);
  5. lưới an toàn: nếu bộ lọc xóa quá nhiều nội dung thì lỗi thay vì âm thầm làm mất truyện.
Mọi thứ đã gỡ được ghi vào report để kiểm tra.
"""
from __future__ import annotations

import re

from ..contracts import ErrorClass, StageError

_NUM_DIGIT = r"(?:\d+|[ivxlc]{1,6}|[一二三四五六七八九十百千零〇两]+)"
_NUM_WORD = r"(?:một|hai|ba|bốn|năm|sáu|bảy|tám|chín|mười)"
_HEAD = r"(?:chương|chapter|section|part|phần)"
HEADING = [
    re.compile(rf"^\s*{_HEAD}\s*{_NUM_DIGIT}\b\s*(?:[:：.)\-–—]\s*)?(?P<t>.{{0,80}})$", re.I),
    re.compile(rf"^\s*{_HEAD}\s*{_NUM_WORD}\s*(?:[:：.)\-–—]\s*|$)(?P<t>.{{0,80}})$", re.I),
    re.compile(r"^\s*第[一-鿿\d]{1,6}[章节節回卷部篇集](?P<t>.{0,60})$"),
    re.compile(r"^\s{0,3}#{1,6}\s+(?P<t>.*)$"),
    re.compile(r"^\s*(?:\*\*[^*\n]{1,80}\*\*|__[^_\n]{1,80}__|【[^】\n]{1,80}】|\[[^\]\n]{1,80}\])\s*$"),
]
DECOR = re.compile(r"^\s*(?:[-*_=~#]{3,}|\*\s*\*\s*\*|•\s*•\s*•)\s*$")
MARKER = re.compile(r"^\s*(?:<!--.*?-->|\[\[.*?\]\]|\{\{.*?\}\}|TODO\b.*)\s*$", re.I)
END_META = re.compile(r"^\W*(?:còn tiếp|hết chương.*|hết phần.*|to be continued|未完待续|待续)\W*$", re.I)
RECAP = re.compile(r"(?:(?:ở|trong|từ)\s+)?(?:chương|phần|tập)\s+(?:trước|vừa rồi)|nối tiếp\s+(?:chương|phần)|"
                   r"tiếp nối\s+(?:chương|phần)|previous chapter|last chapter|上一章|前文提要", re.I)
SENT_SPLIT = re.compile(r"(?<=[.!?…。！？])[\"'”’»)\]]*\s+")
TERMINAL = re.compile(r"[.!?…。！？][\"'”’»)\]]*$")
FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
MIN_DUP_CHARS, NEAR_DUP_MIN_CHARS, NEAR_WINDOW, NEAR_THRESHOLD, SHINGLE = 40, 80, 40, 0.85, 8


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", " ", s.lower()).strip()


def _sentences(par: str) -> list[str]:
    return [s for s in SENT_SPLIT.split(par.strip()) if s]


def _is_heading(line: str) -> bool:
    for rx in HEADING:
        m = rx.match(line)
        if m:
            t = (m.groupdict().get("t") or "").strip()
            if not t or not TERMINAL.search(t):           # "Chương 2 đã kết thúc trong im lặng." là câu văn, không phải heading
                return True
    return False


def _shingles(key: str) -> set[str]:
    return {key[i:i + SHINGLE] for i in range(max(1, len(key) - SHINGLE + 1))}


def _clean_section(text: str, idx: int, rep: dict) -> list[list[str]]:
    """Trả về danh sách đoạn; mỗi đoạn là danh sách câu."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    text = FRONT_MATTER.sub("", text, count=1)
    paras: list[str] = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t　]+", " ", raw).strip()
        if not line:
            continue
        if _is_heading(line):
            rep["headings_removed"].append({"section": idx, "line": line[:100]})
        elif DECOR.match(line) or MARKER.match(line) or END_META.match(line):
            rep["meta_removed"].append({"section": idx, "line": line[:100]})
        else:
            paras.append(line)
    while paras and len(paras[0]) < 300 and RECAP.search(paras[0]):    # "Ở chương trước, ..." mở đầu section
        rep["recaps_removed"].append({"section": idx, "line": paras[0][:100]})
        paras.pop(0)
    out: list[list[str]] = []
    for p in paras:
        sents = _sentences(p)
        kept: list[str] = []
        for s in sents:
            if kept and len(_norm(s)) >= 20 and _norm(s) == _norm(kept[-1]):      # câu lặp liền kề
                rep["duplicates_removed"].append({"kind": "sentence", "section": idx, "text": s[:100]})
                continue
            kept.append(s)
        out.append(kept)
    return out


def _trim_overlap(prev: list[list[str]], cur: list[list[str]], idx: int, rep: dict) -> None:
    """Section sau mở đầu bằng cách chép lại đuôi section trước (LLM hay làm vậy) -> bỏ phần chép."""
    if not prev or not cur:
        return
    tail = [s for par in prev[-2:] for s in par][-6:]
    head = [s for par in cur[:2] for s in par][:6]
    for j in range(min(len(tail), len(head), 5), 0, -1):
        a, b = [_norm(x) for x in tail[-j:]], [_norm(x) for x in head[:j]]
        if a == b and sum(len(x) for x in a) >= 40:
            rep["overlaps_trimmed"].append({"section": idx, "sentences": j, "text": " ".join(head[:j])[:100]})
            left = j
            while left and cur:
                take = min(left, len(cur[0]))
                cur[0] = cur[0][take:]
                left -= take
                if not cur[0]:
                    cur.pop(0)
            return


def assemble(sections: list[str], max_removed_ratio: float = 0.35) -> tuple[str, dict]:
    rep: dict = {"sections": len(sections), "headings_removed": [], "meta_removed": [], "recaps_removed": [],
                 "overlaps_trimmed": [], "joins": [], "duplicates_removed": [], "punctuation_added": 0}
    chars_in = sum(len(s) for s in sections)
    merged: list[list[str]] = []                      # đoạn -> câu
    for i, text in enumerate(sections):
        cur = _clean_section(text, i, rep)
        _trim_overlap(merged, cur, i, rep)
        if merged and cur:
            last = " ".join(merged[-1])
            if not TERMINAL.search(last):
                if cur[0][0][:1].islower():           # câu bị cắt giữa chừng ở ranh giới section: nối lại
                    merged[-1] = merged[-1] + cur[0]
                    cur.pop(0)
                    rep["joins"].append({"section": i, "kind": "continued_sentence"})
                else:
                    merged[-1][-1] += "."
                    rep["punctuation_added"] += 1
        merged.extend(cur)

    paragraphs, seen, window = [], set(), []
    for par in merged:
        text = " ".join(par).strip()
        key = _norm(text)
        if len(key) >= MIN_DUP_CHARS and key in seen:
            rep["duplicates_removed"].append({"kind": "exact", "text": text[:100]})
            continue
        sh = _shingles(key) if len(key) >= NEAR_DUP_MIN_CHARS else None
        if sh and any(len(sh & w) / len(sh | w) >= NEAR_THRESHOLD for w in window):
            rep["duplicates_removed"].append({"kind": "near", "text": text[:100]})
            continue
        seen.add(key)
        if sh:
            window = (window + [sh])[-NEAR_WINDOW:]
        paragraphs.append(text)

    out = "\n\n".join(paragraphs) + "\n" if paragraphs else ""
    removed = (chars_in - len(out)) / chars_in if chars_in else 0.0
    rep.update({"chars_in": chars_in, "chars_out": len(out), "paragraphs": len(paragraphs),
                "removed_ratio": round(removed, 4)})
    if removed > max_removed_ratio:
        raise StageError(ErrorClass.POLICY, "ASSEMBLER_REMOVED_TOO_MUCH",
                         f"assembler loại {removed:.0%} nội dung (> {max_removed_ratio:.0%}); kiểm tra đầu ra của adapter",
                         {"report": {k: v for k, v in rep.items() if not isinstance(v, list)}})
    return out, rep
