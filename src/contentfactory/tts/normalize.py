"""Text Normalizer: story.txt -> văn bản sẵn sàng cho TTS. Tất định, không dùng AI, trả báo cáo thay đổi.

Chỉ làm những thứ an toàn với mọi engine. KHÔNG làm: đọc số/ngày thành chữ (phụ thuộc engine và ngôn ngữ; engine tốt
tự xử lý, engine kém cần rule riêng qua `normalize.replacements` của profile), phiên âm tên riêng.
"""
from __future__ import annotations

import re
import unicodedata

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠﻿]")
_MD_LINE = re.compile(r"^\s*([-*_]{3,}|={3,})\s*$", re.M)
_MD_PREFIX = re.compile(r"^\s{0,3}(#{1,6}\s+|>\s?)", re.M)
_MD_INLINE = re.compile(r"(\*\*|__|`)")
_END = re.compile(r"[.!?…\"'”’)\]]\s*$")


def normalize_text(text: str, cfg: dict | None = None) -> tuple[str, dict]:
    cfg = cfg or {}
    rep: dict[str, int] = {}

    def step(name: str, new: str, old: str) -> str:
        if new != old:
            rep[name] = rep.get(name, 0) + 1
        return new

    t = text
    t = step("unicode_nfc", unicodedata.normalize("NFC", t), t)
    t = step("control_chars", _CTRL.sub("", t), t)
    t = step("newlines", t.replace("\r\n", "\n").replace("\r", "\n").replace(" ", "\n").replace(" ", " "), t)
    if cfg.get("strip_markdown", True):
        t = step("markdown", _MD_INLINE.sub("", _MD_PREFIX.sub("", _MD_LINE.sub("", t))), t)
    if cfg.get("ellipsis", True):
        t = step("ellipsis", re.sub(r"\.{3,}", "…", t), t)
    if cfg.get("collapse_punct", True):
        t = step("repeated_punct", re.sub(r"([!?…,;:])\1+", r"\1", t), t)
        t = step("repeated_punct", re.sub(r"\.{2}(?!\.)", ".", t), t)
    for r in cfg.get("replacements", []):                       # {"from": "TP.HCM", "to": "Thành phố Hồ Chí Minh"} | {"pattern", "to"}
        new = re.sub(r["pattern"], r["to"], t) if "pattern" in r else t.replace(r["from"], r["to"])
        t = step(f"replace:{r.get('from') or r.get('pattern')}", new, t)
    t = step("spaces", "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in t.split("\n")), t)
    t = step("blank_lines", re.sub(r"\n{3,}", "\n\n", t).strip(), t)
    if cfg.get("ensure_terminal_punct", False):
        paras = [p if _END.search(p) else p + "." for p in t.split("\n\n")]
        t = step("terminal_punct", "\n\n".join(paras), t)
    return t, {"changes": rep, "chars_in": len(text), "chars_out": len(t)}
