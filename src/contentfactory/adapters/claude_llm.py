"""TextLLM thật cho Story Remix: một lượt Claude Code không công cụ (cùng runner stream-json với story_branch, nên cùng cách đăng nhập/giới hạn/timeout/huỷ).

Chi phí lấy từ `total_cost_usd` của CLI; thiếu hoặc 0 ⇒ trả None (= không rõ, KHÔNG suy đoán). Token không được CLI báo ở lớp này ⇒ None.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from ..contracts import ErrorClass, LLMResult, StageContext, StageError
from .story_branch import ClaudeCliRunner


class _NoCtx:
    """Ngữ cảnh rỗng khi gọi ngoài pipeline (benchmark/CLI): không có huỷ, log bỏ qua."""
    class cancel:
        @staticmethod
        def is_set() -> bool:
            return False

        @staticmethod
        def check() -> None:
            return None

    @staticmethod
    def log(*a, **k) -> None:
        return None


class ClaudeCliLLM:
    def __init__(self, cfg: dict | None = None, runner=None) -> None:
        cfg = dict(cfg or {})
        self.runner = runner or ClaudeCliRunner({**cfg, "allowed_tools": [], "permission_mode": cfg.get("permission_mode", "default")})
        self._cwd: Path | None = None

    def _dir(self) -> Path:
        if self._cwd is None:
            self._cwd = Path(tempfile.mkdtemp(prefix="cf-remix-llm-"))             # thư mục trống: LLM không có gì để đọc/ghi ngoài prompt
        return self._cwd

    def complete(self, prompt: str, *, system: str, step: str, ctx: StageContext | None = None) -> LLMResult:
        t0 = time.time()
        turn = self.runner.run(f"{system}\n\n{prompt}", self._dir(), None, ctx or _NoCtx)
        if turn["is_error"]:
            raise StageError(ErrorClass.TRANSIENT, "LLM_ERROR", (turn["text"] or "lỗi không rõ")[:300])
        cost = float(turn.get("cost_usd") or 0.0)
        return {"text": turn["text"], "cost_usd": cost if cost > 0 else None, "tokens_in": None, "tokens_out": None, "seconds": round(time.time() - t0, 3)}
