"""Stage STORY: transcript -> story.txt liền mạch. Validator bất biến luôn chạy sau adapter."""
from __future__ import annotations

from ..contracts import ErrorClass, StageContext, StageError, StageResult, StoryAdapter
from .validate import validate_story_text


def run(ctx: StageContext, story: StoryAdapter) -> StageResult:
    meta = ctx.read_json("metadata")
    bundle = {"title": meta["title"], "language": ctx.params.get("language") or meta.get("language", "vi"),
              "transcript": ctx.one("transcript")}
    res = story.generate(bundle, ctx.params.get("story_profile", {}), ctx.stage_dir, ctx)
    text = res["story"].read_text(encoding="utf-8") if res["story"].is_file() else ""
    issues = validate_story_text(text)
    if issues:
        raise StageError(ErrorClass.POLICY, "STORY_INVALID", ",".join(issues), {"issues": issues})
    return StageResult([ctx.draft(res["story"], "story_text", chars=len(text))],
                       {"chars": len(text), **res.get("stats", {})})
