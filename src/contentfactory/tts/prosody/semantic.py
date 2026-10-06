"""Gán nhãn ngữ nghĩa bằng LLM — TÙY CHỌN, mặc định TẮT (`prosody.semantic_llm = false`), có cache (D-100).

Đường mặc định của Prosody Engine hoàn toàn tất định và KHÔNG gọi LLM; chất lượng nhịp đọc không phụ thuộc vào đây. Nếu bật:
  - LLM chỉ trả NHÃN cho từng câu (`dramatic_reveal | scene_transition | emphasis | hesitation | speaker_turn`), không được viết lại chữ,
    không được đặt thời gian: mili-giây vẫn do bảng profile quyết định (plan._apply_semantic);
  - kết quả được snapshot vào `semantic.json` của stage theo khóa nội dung: retry/resume/rerender dùng lại, không tốn token lần nữa;
  - tắt tùy chọn (hoặc không có bộ gán nhãn) thì plan vẫn đầy đủ.

`labeler` là đối tượng có `name`, `version` và `labels(sentences) -> {số_thứ_tự_câu: nhãn}`; `LLMSemanticLabeler` bọc một hàm `llm(prompt) -> str` do bên ngoài tiêm vào.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable, Protocol

from ...fsutil import atomic_write_json
from .plan import SEMANTIC_LABELS

PROMPT_VERSION = "1"
PROMPT = """Bạn gắn nhãn ngữ nghĩa cho các câu truyện (đã đánh số) để bộ đọc quyết định chỗ ngắt nghỉ. Nhãn hợp lệ:
dramatic_reveal (câu tiết lộ bất ngờ/đỉnh điểm), scene_transition (câu khép một cảnh), emphasis (câu cần nhấn), hesitation (câu ngập ngừng), speaker_turn (đổi người nói).
Chỉ gắn nhãn khi THẬT SỰ cần; đa số câu không có nhãn. KHÔNG viết lại câu, KHÔNG đặt thời gian.
Chỉ trả JSON: {{"labels": {{"3": "dramatic_reveal"}}}}
{body}"""


class SemanticLabeler(Protocol):
    name: str
    version: str

    def labels(self, sentences: list[str]) -> dict[str, str]: ...


class LLMSemanticLabeler:
    name = "llm"
    version = PROMPT_VERSION

    def __init__(self, llm: Callable[[str], str], window: int = 80) -> None:
        self.llm, self.window = llm, window

    def labels(self, sentences: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for lo in range(0, len(sentences), self.window):
            chunk = sentences[lo:lo + self.window]
            raw = self.llm(PROMPT.format(body="\n".join(f"{lo + i + 1}. {s}" for i, s in enumerate(chunk))))
            try:
                got = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))["labels"]
            except (AttributeError, KeyError, ValueError, TypeError):
                continue                                             # phản hồi hỏng: bỏ qua cửa sổ này (plan vẫn tất định, chỉ thiếu nhãn)
            for k, v in got.items():
                if str(k).isdigit() and 1 <= int(k) <= len(sentences) and v in SEMANTIC_LABELS:
                    out[str(int(k))] = v
        return out


def semantic_for(labeler, sentences: list[str], text_sha: str, cache: Path) -> tuple[dict, dict]:
    """(semantic, report). semantic = {"source", "labels"}. Dùng lại `cache` nếu cùng khóa (không gọi LLM)."""
    key = hashlib.sha256(json.dumps({"text": text_sha, "labeler": [labeler.name, labeler.version], "prompt": PROMPT_VERSION}, sort_keys=True).encode()).hexdigest()
    try:
        d = json.loads(cache.read_text(encoding="utf-8"))
        if d["key"] == key:
            return {"source": d["source"], "labels": d["labels"]}, {"cached": True, "calls": 0}
    except (OSError, ValueError, KeyError):
        pass
    labels = {k: v for k, v in labeler.labels(sentences).items() if v in SEMANTIC_LABELS}
    sem = {"source": f"{labeler.name}@{labeler.version}", "labels": labels}
    atomic_write_json(cache, {"key": key, **sem})
    return sem, {"cached": False, "calls": 1}
