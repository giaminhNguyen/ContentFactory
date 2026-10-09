"""Stage RENDER (YouTube / TikTok): nối StageContext với RenderManager (logic ở manager.py). Output của adapter là atomic (file tồn tại <=> hoàn chỉnh)
và có sidecar khóa nội dung, nên resume/retry chỉ render lại output chưa hợp lệ (từng part TikTok riêng)."""
from __future__ import annotations

from ..contracts import RenderAdapter, StageContext, StageResult
from .manager import RenderManager


def run_youtube(ctx: StageContext, render: RenderAdapter, sequence) -> StageResult:
    return RenderManager(render).youtube(ctx, sequence)


def run_tiktok(ctx: StageContext, render: RenderAdapter) -> StageResult:
    return RenderManager(render).tiktok(ctx)
