"""Chọn adapter Story theo chế độ của job: mặc định giữ NGUYÊN adapter hiện có (Story hiện có); chỉ job `story_mode=story_remix` mới đi qua Story Remix.

Là một StoryAdapter bọc ngoài nên stage `story`, assembler, validator, resume, rerun… không phải biết có hai chế độ. Job cũ (không có story_mode) đi đúng đường cũ.
"""
from __future__ import annotations

from ..output import metadata as MD
from ..story import mode as SM


class StoryModeRouter:
    def __init__(self, default, remix_factory, titler=None, scene_remix_factory=None) -> None:
        self.default = default
        self._factory = remix_factory
        self._remix = None
        self._scene_factory = scene_remix_factory
        self._scene_remix = None
        self._titler = titler                          # callable(story_text, bundle, ctx) -> tên | None: đặt tên cho truyện của adapter mặc định

    @property
    def remix(self):
        if self._remix is None:
            self._remix = self._factory()
        return self._remix

    @property
    def scene_remix(self):
        if self._scene_factory is None:
            raise RuntimeError("Story Scene Remix chưa có factory")
        if self._scene_remix is None:
            self._scene_remix = self._scene_factory()
        return self._scene_remix

    def _pick(self, ctx):
        mode = SM.of_job(getattr(ctx, "params", None) or {})["mode"]
        if mode == "story_scene_remix":
            return self.scene_remix
        return self.remix if mode == "story_remix" else self.default

    def generate(self, bundle, profile, out_dir, ctx):
        return self._pick(ctx).generate(bundle, profile, out_dir, ctx)

    def finalize(self, story_text, ctx):
        a = self._pick(ctx)
        fin = getattr(a, "finalize", None)
        return fin(story_text, ctx) if fin else {}

    def title(self, story_text, bundle, ctx):
        """Tên truyện tự nghĩ (dùng khi người dùng để trống): AI đặt từ DÀN Ý đã thiết kế (cả Story thường lẫn Remix) — ngắn hơn nhiều so với
        cả truyện; không có dàn ý thì đặt từ truyện. Không có titler (vd cấu hình fake): Remix dùng tên trong story bible."""
        a = self._pick(ctx)
        if not self._titler:
            return a.title(story_text, bundle, ctx) if a is not self.default else None
        outline = getattr(a, "outline_of", lambda *_: None)(bundle, ctx.params.get("story_profile") or {}, ctx.stage_dir)
        prefix, budget = title_budget(ctx.config.get("channel_config"), ctx.params.get("channel") or "default")
        return self._titler(outline or story_text, bundle, ctx, prefix, budget)

    def health(self) -> dict:
        return self.default.health()

    def __getattr__(self, name):                       # thuộc tính khác (vd test kiểm adapter cũ) đi thẳng tới adapter mặc định
        if name.startswith("__") or name in ("default", "_factory", "_remix", "_scene_factory", "_scene_remix"):
            raise AttributeError(name)
        return getattr(self.default, name)


def title_budget(channel_cfg: dict | None, channel_id: str) -> tuple[str, int]:
    """(tiền tố tiêu đề YouTube của kênh, số ký tự còn lại cho tên truyện trong giới hạn 100)."""
    ch = MD.normalize_channel(channel_cfg, (channel_cfg or {}).get("id") or channel_id)
    # shortcut: số tập chỉ được cấp ở bước render, sau bước đặt tên ⇒ ước số tập kế tiếp và dư 1 chữ số cho an toàn (job song song có thể nhận số lớn hơn)
    seq = "9" * (len(str(int(ch["sequence"].get("last_used", 0)) + 1)) + 1)
    prefix = MD.render_template(ch["title_template"], {"channel_name": ch["name"], "project_title": "", "sequence": seq}, "title_template").lstrip()
    return prefix, max(MD.TITLE_TARGET_CHARS - len(prefix), 1)         # nhắm ≤ 90 (đệm), trần cứng 100 vẫn do Metadata Builder kiểm
