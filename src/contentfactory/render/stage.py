"""Stage RENDER (YouTube / TikTok). Output của adapter là atomic: file tồn tại <=> hoàn chỉnh,
nên resume dùng lại video/part đã render thay vì render lại."""
from __future__ import annotations

from ..contracts import RenderAdapter, StageContext, StageResult


def _profile(ctx: StageContext, pid: str, base: dict) -> dict:
    return {"id": pid, **base, **ctx.params.get("render", {}).get(pid, {})}


def run_youtube(ctx: StageContext, render: RenderAdapter) -> StageResult:
    meta = ctx.read_json("metadata")
    video, thumb = ctx.stage_dir / "video.mp4", ctx.stage_dir / "thumbnail.jpg"
    if not video.is_file():
        render.render_video({"audio": ctx.one("audio_youtube"), "output": video,
                             "profile": _profile(ctx, "youtube", {"aspect_ratio": "16:9", "resolution": "1920x1080"})}, ctx)
    ctx.cancel.check()
    if not thumb.is_file():
        render.render_thumbnail({"title": meta["title"], "channel_name": ctx.params.get("channel", ""), "output": thumb}, ctx)
    return StageResult([ctx.draft(video, "video_youtube"), ctx.draft(thumb, "thumbnail")])


def run_tiktok(ctx: StageContext, render: RenderAdapter) -> StageResult:
    prof = _profile(ctx, "tiktok", {"aspect_ratio": "9:16", "resolution": "1080x1920"})
    arts = []
    for ref in sorted(ctx.inputs["audio_tiktok"], key=lambda r: r["meta"]["index"]):
        ctx.cancel.check()
        i = ref["meta"]["index"]
        out = ctx.stage_dir / f"part_{i:02d}.mp4"
        if not out.is_file():
            render.render_video({"audio": ctx.path(ref), "output": out, "profile": prof}, ctx)
        arts.append(ctx.draft(out, "video_tiktok", index=i))
    return StageResult(arts, {"parts": len(arts)})
