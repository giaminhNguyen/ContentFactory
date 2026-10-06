"""Stage OUTPUT: (1) Metadata Builder dựng tiêu đề/mô tả đăng từ project.title + Channel Config + sequence (artifact `publish_metadata`),
(2) xuất gói cho người dùng từ artifact trong workspace (không đọc output/ cũ).

`title.txt`/`description.txt` của gói output và payload của stage `publish` cùng lấy từ `publish_metadata` nên không thể lệch nhau (D-48).
Sequence được reserve MỘT lần ở đây (lười, idempotent): retry/rerender không đổi số.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..contracts import OutputPublisher, StageContext, StageResult, project_of
from ..fsutil import atomic_write_json
from . import metadata as MD


def _ref_entry(ctx: StageContext, ref: dict) -> dict:
    return {"path": ctx.path(ref), "source": ref["path"], "sha256": ref["sha256"]}


def _durations(ctx: StageContext) -> dict[int, float]:
    """Độ dài từng part TikTok từ báo cáo render (nếu có); thiếu thì bỏ qua."""
    out: dict[int, float] = {}
    if ctx.inputs.get("tiktok_render_report"):
        try:
            rep = ctx.read_json("tiktok_render_report")
            out = {int(k): float(v["duration_sec"]) for k, v in (rep.get("parts") or {}).items() if v.get("duration_sec")}
        except (OSError, ValueError, KeyError):
            pass
    return out


def run(ctx: StageContext, output: OutputPublisher, sequence) -> StageResult:
    meta = ctx.read_json("metadata")
    project = project_of(ctx, meta)
    channel = MD.normalize_channel(ctx.config.get("channel_config"), project["channel_id"])
    project["sequence"] = sequence.reserve(project["channel_id"], ctx.job_id, int(channel["sequence"].get("last_used", 0)))
    pm = MD.build(project, channel, project["sequence"])
    pm_file = ctx.stage_dir / "publish_metadata.json"
    atomic_write_json(pm_file, {**pm, "project": project})
    for w in pm["warnings"]:
        ctx.log("publish_metadata_warning", "warning", message=w)
    dur = _durations(ctx)
    parts = [{**_ref_entry(ctx, r), "index": r["meta"]["index"], "duration_sec": dur.get(r["meta"]["index"])}
             for r in sorted(ctx.inputs.get("video_tiktok") or [], key=lambda r: r["meta"]["index"])]     # nhánh nào không đóng gói thì runner đã bỏ kind đó khỏi inputs
    yt_video, yt_thumb = ctx.inputs.get("video_youtube"), ctx.inputs.get("thumbnail")
    pkg = output.publish({
        "job_id": ctx.job_id, "project": project, "youtube_title": pm["youtube_title"], "description": pm["description"],
        "output_root": Path(ctx.config["output_dir"]), "story": _ref_entry(ctx, ctx.inputs["story_text"][0]) if ctx.inputs.get("story_text") else None,
        "youtube_video": _ref_entry(ctx, yt_video[0]) if yt_video else None, "youtube_thumbnail": _ref_entry(ctx, yt_thumb[0]) if yt_thumb else None,
        "tiktok_parts": parts, "warnings": pm["warnings"]}, ctx)
    receipt = ctx.stage_dir / "receipt.json"
    atomic_write_json(receipt, pkg)
    return StageResult([ctx.draft(receipt, "output_package", version=pkg["version"]), ctx.draft(pm_file, "publish_metadata", sequence=pm["sequence"])],
                       {"project_dir": pkg["project_dir"], "version": pkg["version"], "reused": pkg["reused"], "files": len(pkg["files"]),
                        "sequence": pm["sequence"], "title_source": project["title_source"]})
