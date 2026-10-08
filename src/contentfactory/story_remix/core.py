"""Hạ tầng dùng chung của Story Remix: sổ chi phí/ngân sách trung thực, gọi LLM trả JSON có kiểm tra + thử lại có giới hạn, checkpoint theo bước (fingerprint).

- Chi phí chỉ ghi khi nhà cung cấp BÁO; thiếu ⇒ `unknown` (không suy đoán). Ngân sách (`budget_usd`) chỉ dừng được khi biết chi phí thật; ước lượng ghi rõ là ước lượng.
- Mỗi bước có fingerprint = băm(đầu vào + phiên bản bước + tùy chọn ảnh hưởng nội dung). Trùng fingerprint và file còn hợp lệ ⇒ bỏ qua (không gọi LLM): đổi nhãn UI/ngưỡng QA không làm viết lại bước đắt.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ..contracts import ErrorClass, LLMResult, StageError, TextLLM
from ..fsutil import atomic_write_json


class Invalid(ValueError):
    """Dữ liệu LLM trả về không đạt schema (message tiếng Việt, dùng làm phản hồi để thử lại)."""


def fail(code: str, msg: str, detail: dict | None = None, resource: str | None = None) -> StageError:
    return StageError(ErrorClass.POLICY, code, msg, detail or {}, resource=resource)


# ---------------------------------------------------------------------------------------------- chi phí
class Ledger:
    def __init__(self, path: Path, budget_usd: float | None = None) -> None:
        self.path, self.budget = path, budget_usd
        self.calls: list[dict] = []
        if path.is_file():
            try:
                self.calls = json.loads(path.read_text(encoding="utf-8")).get("call_log", [])
            except (OSError, ValueError):
                self.calls = []

    def record(self, step: str, res: LLMResult, attempt: int = 1) -> None:
        self.calls.append({"step": step, "attempt": attempt, "cost_usd": res.get("cost_usd"), "tokens_in": res.get("tokens_in"), "tokens_out": res.get("tokens_out"),
                           "seconds": round(float(res.get("seconds") or 0.0), 3), "at": time.time()})
        self.save()

    def known_cost(self) -> float:
        return round(sum(c["cost_usd"] for c in self.calls if c["cost_usd"] is not None), 6)

    def unknown_calls(self) -> int:
        return sum(1 for c in self.calls if c["cost_usd"] is None)

    def guard(self) -> None:
        """Chặn TRƯỚC lượt gọi kế tiếp nếu chi phí ĐÃ BIẾT vượt ngân sách (resume sau khi nâng ngân sách: các bước xong được giữ)."""
        if self.budget is not None and self.known_cost() >= self.budget:
            raise fail("BUDGET_EXCEEDED", f"Đã dùng ${self.known_cost():.2f} (≥ ngân sách ${self.budget:.2f}); dừng an toàn, các bước đã xong được giữ.",
                       {"hint": "Nâng ngân sách của job rồi chạy lại để tiếp tục từ bước đang dở.", "known_cost_usd": self.known_cost(), "budget_usd": self.budget})

    def report(self) -> dict:
        by: dict[str, dict] = {}
        for c in self.calls:
            b = by.setdefault(c["step"], {"calls": 0, "cost_usd": 0.0, "unknown_cost_calls": 0, "tokens_in": 0, "tokens_out": 0, "seconds": 0.0})
            b["calls"] += 1
            b["seconds"] = round(b["seconds"] + c["seconds"], 3)
            if c["cost_usd"] is None:
                b["unknown_cost_calls"] += 1
            else:
                b["cost_usd"] = round(b["cost_usd"] + c["cost_usd"], 6)
            for k in ("tokens_in", "tokens_out"):
                b[k] += c[k] or 0
        return {"known_cost_usd": self.known_cost(), "calls": len(self.calls), "calls_with_unknown_cost": self.unknown_calls(), "budget_usd": self.budget,
                "retries": sum(1 for c in self.calls if c["attempt"] > 1), "by_step": by,
                "note": "Chi phí chỉ gồm các lượt nhà cung cấp báo; lượt không báo được đếm ở calls_with_unknown_cost (không ước đoán)."}

    def save(self) -> None:
        atomic_write_json(self.path, {**self.report(), "call_log": self.calls})


# ---------------------------------------------------------------------------------------------- JSON
def extract_json(text: str):
    """Lấy object/array JSON đầu tiên trong phản hồi (chấp nhận bọc ```json ... ```)."""
    t = (text or "").strip()
    dec = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch in "{[":
            try:
                return dec.raw_decode(t[i:])[0]
            except ValueError:
                continue
    raise Invalid("Phản hồi không chứa JSON hợp lệ.")


def ask_json(llm: TextLLM, ledger: Ledger, step: str, system: str, prompt: str, validate, ctx=None, retries: int = 2):
    """Gọi LLM → JSON → validate(data) (ném Invalid để thử lại kèm lý do). Quá số lần ⇒ StageError REMIX_LLM_INVALID. Mọi lượt đều vào sổ chi phí."""
    err = ""
    for attempt in range(1, retries + 2):
        if ctx is not None:
            ctx.cancel.check()
        ledger.guard()
        res = llm.complete(prompt + (f"\n\nLẦN TRƯỚC BỊ TỪ CHỐI: {err}\nTrả lại JSON hợp lệ, sửa đúng lỗi trên." if err else ""), system=system, step=step, ctx=ctx)
        ledger.record(step, res, attempt)
        try:
            return validate(extract_json(res.get("text", "")))
        except Invalid as e:
            err = str(e)[:500]
    raise fail("REMIX_LLM_INVALID", f"Bước {step}: LLM không trả dữ liệu hợp lệ sau {retries + 1} lần ({err}).", {"hint": "Chạy lại; nếu lặp lại, đổi mô hình hoặc rút gọn tuỳ chọn.", "step": step})


# ---------------------------------------------------------------------------------------------- checkpoint theo bước
def fingerprint(**parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:20]


class Steps:
    """`remix_state.json`: {step: {fp, file}}. `run` bỏ qua khi fp khớp và file còn đọc được + hợp lệ."""

    def __init__(self, out_dir: Path) -> None:
        self.dir = out_dir
        self.path = out_dir / "remix_state.json"
        try:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        self.ran: list[str] = []
        self.skipped: list[str] = []

    def run(self, step: str, fp: str, filename: str, produce, validate=None):
        f = self.dir / filename
        st = self.state.get(step)
        if st and st.get("fp") == fp and f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if validate:
                    validate(data)
                self.skipped.append(step)
                return data
            except (ValueError, Invalid):
                pass
        data = produce()
        atomic_write_json(f, data)
        self.state[step] = {"fp": fp, "file": filename}
        atomic_write_json(self.path, self.state)
        self.ran.append(step)
        return data
