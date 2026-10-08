"""Stage STORY: transcript -> các section nội bộ (StoryAdapter) -> Story Assembler -> story.txt liền mạch.

Adapter chỉ trả danh sách section theo thứ tự (kèm blueprint/continuity nằm trong workspace nội bộ của nó).
Assembler và validator bất biến chạy ở đây nên áp dụng cho MỌI adapter. Dựng lại story.txt bị bỏ qua khi
các section không đổi (so sha256) và story.txt còn nguyên.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..contracts import ErrorClass, StageContext, StageError, StageResult, StoryAdapter
from ..fsutil import atomic_write_json, atomic_write_text, sha256_file
from .assembler import assemble
from .validate import validate_story_text

ASSEMBLER_VERSION = "1"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run(ctx: StageContext, story: StoryAdapter) -> StageResult:
    meta = ctx.read_json("metadata")
    profile = ctx.params.get("story_profile", {})
    bundle = {"title": meta["title"], "language": ctx.params.get("language") or meta.get("language", "vi"),
              "source_language": meta.get("language", ""), "transcript": ctx.one("transcript")}
    guide = ctx.extra.get("story_guidance") or {}
    if guide.get("text"):                                    # chỉ thêm khi có nội dung: không guidance => bundle y như cũ
        bundle["guidance"] = guide["text"]
    res = story.generate(bundle, profile, ctx.stage_dir, ctx)
    sections = res["sections"]
    if not sections:
        raise StageError(ErrorClass.POLICY, "NO_SECTIONS", "StoryAdapter không trả section nào")
    for p in sections:
        if not Path(p).is_file() or Path(p).stat().st_size == 0:
            raise StageError(ErrorClass.POLICY, "EMPTY_SECTION", str(p))

    sig = hashlib.sha256(("|".join(sha256_file(Path(p)) for p in sections) + ASSEMBLER_VERSION).encode()).hexdigest()
    story_path, report_path = ctx.stage_dir / "story.txt", ctx.stage_dir / "assembly_report.json"
    prev = _read_json(report_path) or {}
    reused = bool(story_path.is_file() and prev.get("sections_sha256") == sig
                  and prev.get("story_sha256") == sha256_file(story_path))
    if reused:
        ctx.log("story_assembly_reused", sections=len(sections))
        report = prev
    else:
        text, report = assemble([Path(p).read_text(encoding="utf-8") for p in sections],
                                float(profile.get("max_removed_ratio", 0.35)))
        issues = validate_story_text(text)
        report["issues"], report["sections_sha256"] = issues, sig
        if issues:
            atomic_write_json(report_path, report)               # giữ để debug; story.txt KHÔNG được ghi
            raise StageError(ErrorClass.POLICY, "STORY_INVALID", ",".join(issues), {"issues": issues})
        atomic_write_text(story_path, text)
        report["story_sha256"] = sha256_file(story_path)
        atomic_write_json(report_path, report)

    final = {}
    fin = getattr(story, "finalize", None)                        # tuỳ chọn: adapter có bước chốt sau khi story.txt hợp lệ (Story Remix: QA cuối + publish Kho nhân vật)
    if fin is not None:
        final = fin(story_path.read_text(encoding="utf-8"), ctx) or {}
    chars = len(story_path.read_text(encoding="utf-8"))
    summary = {k: len(v) if isinstance(v, list) else v for k, v in report.items()
               if k in ("sections", "headings_removed", "meta_removed", "recaps_removed", "overlaps_trimmed",
                        "joins", "duplicates_removed", "removed_ratio", "paragraphs")}
    return StageResult([ctx.draft(story_path, "story_text", chars=chars),
                        ctx.draft(report_path, "story_report")],
                       {"chars": chars, "assembly_reused": reused, **summary, **res.get("stats", {}), **final,
                        **({"guidance_source": guide.get("source"), "guidance_hash": guide.get("hash")} if guide.get("text") else {})})
