"""Validator tất định cho story.txt (MODULE_CONTRACTS §2): chạy sau MỌI StoryAdapter."""
from __future__ import annotations

import re

HEADER_RE = re.compile(r"^\s*(第.{1,8}[章节節回]|chapter\s*\d+|section\s*\d+|part\s*\d+|chương\s*\d+|phần\s*\d+)",
                       re.IGNORECASE | re.MULTILINE)
MARKER_RE = re.compile(r"<!--|\[\[|\{\{|\bTODO\b|^#{1,6}\s", re.MULTILINE)
MAX_DUP_PARAGRAPH_RATIO = 0.3


def validate_story_text(text: str) -> list[str]:
    """Trả danh sách vi phạm (rỗng = hợp lệ)."""
    if not text.strip():
        return ["EMPTY"]
    issues = []
    if HEADER_RE.search(text):
        issues.append("CHAPTER_HEADER")
    if MARKER_RE.search(text):
        issues.append("TECHNICAL_MARKER")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) > 1 and 1 - len(set(paras)) / len(paras) > MAX_DUP_PARAGRAPH_RATIO:
        issues.append("REPEATED_PARAGRAPHS")
    return issues
