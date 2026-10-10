"""LLM giả tất định, không mạng, cho Remix bám sự việc: dùng làm adapter `fake` và để thử đường ống từ đầu đến cuối.

Chỉ chứng minh ĐƯỜNG ỐNG (định tuyến, checkpoint, validator, QA, ngân sách) — KHÔNG chứng minh chất lượng truyện của LLM thật.
`script`: {step_prefix: callable(prompt, step) -> text | dict} để test ép hành vi sai (rewrite hỏng, QA báo lỗi, JSON hỏng...); trả None = hành vi mặc định.
"""
from __future__ import annotations

import json
import re

OBJECT_RE = re.compile(r"\b(?:chiếc|cái|hộp|quyển|chiếc|bức)\s+\w+", re.I)


class FakeSceneRemixLLM:
    def __init__(self, cost: float = 0.0, new: str = "hộp cơm", script: dict | None = None) -> None:
        self.calls: list[str] = []
        self.prompts: dict[str, list[str]] = {}
        self.cost, self.new, self.script = cost, new, script or {}

    def count(self, prefix: str) -> int:
        return sum(1 for c in self.calls if c.startswith(prefix))

    @staticmethod
    def _tail_json(prompt: str, marker: str):
        return json.loads(prompt.split(marker, 1)[1].strip())

    def complete(self, prompt: str, *, system: str, step: str, ctx=None):
        self.calls.append(step)
        self.prompts.setdefault(step, []).append(prompt)
        for prefix, fn in self.script.items():
            if step.startswith(prefix):
                out = fn(prompt, step)
                if out is not None:
                    return self._res(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))
        if "JSON HIỆN TẠI:\n" in prompt:                # lượt sửa JSON gọn của ask_json: trả lại đúng JSON đó (fake không tự sửa được nội dung)
            return self._res(prompt.split("JSON HIỆN TẠI:\n", 1)[1])
        if step.startswith("source_map_"):
            group = self._tail_json(prompt, "NGUỒN (JSON):\n")
            scenes = []
            for k, x in enumerate(group):
                objs = [m.group(0) for m in OBJECT_RE.finditer(x["text"])][:3]
                scenes.append({"id": x["id"], "event": x["text"][:90], "cause": "xung đột trước đó", "effect": "ảnh hưởng diễn biến sau", "emotional_role": "duy trì tò mò",
                               "characters": [], "objects": objs, "beat": "hook" if x["id"] == "s001" else "none"})
            return self._res(json.dumps({"scenes": scenes}, ensure_ascii=False))
        if step == "scene_change_plan":
            data = self._tail_json(prompt, "BẢN ĐỒ TRUYỆN (JSON):\n")
            freq: dict[str, list[str]] = {}
            for s in data["scenes"]:
                for o in s["objects"]:
                    freq.setdefault(o, []).append(s["id"])
            if not freq:
                return self._res(json.dumps({"changes": [], "needs_level3": "không có chi tiết thay thế nhẹ"}, ensure_ascii=False))
            old = max(freq, key=lambda o: len(freq[o]))
            new = self.new if self.new.lower() != old.lower() else "cái ví"
            return self._res(json.dumps({"changes": [{"id": "c001", "old": old, "new": new, "level": 1, "scene_ids": freq[old], "why": "thay vật chứng tương đương"}], "global_rules": []}, ensure_ascii=False))
        if step == "scene_continuity_qa":
            return self._res(json.dumps({"issues": []}))
        if step.startswith(("scene_rewrite_", "scene_repair_")):
            src = prompt.split("CẢNH NGUỒN:\n" if "CẢNH NGUỒN:\n" in prompt else "CẢNH HIỆN TẠI:\n", 1)[1]
            text = src
            for old, new in re.findall(r"\"([^\"]+)\" → \"([^\"]+)\"", prompt.split("THAY ĐỔI ÁP DỤNG", 1)[-1].split("TOÀN BỘ thay đổi", 1)[0]):
                text = re.sub(re.escape(old), new, text, flags=re.I)
            return self._res(text)
        raise ValueError(f"Fake scene remix: bước lạ {step}")

    def _res(self, text: str):
        return {"text": text, "cost_usd": self.cost or None, "tokens_in": 100 if self.cost else None, "tokens_out": 50 if self.cost else None, "seconds": 0.0}
