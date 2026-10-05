"""slug ASCII không dấu cho tên thư mục output (DECISIONS D-07)."""
from __future__ import annotations

import re
import unicodedata


def slugify(title: str, max_len: int = 60) -> str:
    s = title.replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:max_len].strip("-")
    return s or "untitled"
