"""Chọn adapter Story theo chế độ của job: mặc định giữ NGUYÊN adapter hiện có (Story hiện có); chỉ job `story_mode=story_remix` mới đi qua Story Remix.

Là một StoryAdapter bọc ngoài nên stage `story`, assembler, validator, resume, rerun… không phải biết có hai chế độ. Job cũ (không có story_mode) đi đúng đường cũ.
"""
from __future__ import annotations

from ..story import mode as SM


class StoryModeRouter:
    def __init__(self, default, remix_factory) -> None:
        self.default = default
        self._factory = remix_factory
        self._remix = None

    @property
    def remix(self):
        if self._remix is None:
            self._remix = self._factory()
        return self._remix

    def _pick(self, ctx):
        return self.remix if SM.of_job(getattr(ctx, "params", None) or {})["mode"] == "story_remix" else self.default

    def generate(self, bundle, profile, out_dir, ctx):
        return self._pick(ctx).generate(bundle, profile, out_dir, ctx)

    def finalize(self, story_text, ctx):
        a = self._pick(ctx)
        fin = getattr(a, "finalize", None)
        return fin(story_text, ctx) if fin else {}

    def health(self) -> dict:
        return self.default.health()

    def __getattr__(self, name):                       # thuộc tính khác (vd test kiểm adapter cũ) đi thẳng tới adapter mặc định
        if name.startswith("__") or name in ("default", "_factory", "_remix"):
            raise AttributeError(name)
        return getattr(self.default, name)
