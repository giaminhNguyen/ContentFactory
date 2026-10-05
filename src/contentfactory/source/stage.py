"""Stage SOURCE: input -> SourceAdapter (thu thập phụ đề) -> Transcript Processor -> artifact.

    SourceAdapter (ProviderChain -> Subtitle_supperVip / yt-dlp / local / text)  ->  SourceResult
    TranscriptProcessor (của ContentFactory)  ->  transcript_structured.json + transcript_clean.txt

File trong workspace/<job>/source/ (HANDOFF §Source):
    source.json                  SourceResult + tóm tắt transcript (artifact kind `metadata`)
    subtitle_raw.<srt|vtt|json|txt>   phụ đề thô đúng như provider trả về (artifact `subtitle_raw`)
    transcript_structured.json   cue/câu/đoạn kèm start, end, gap (artifact `transcript_structured`)
    transcript_clean.txt         transcript sạch, không timestamp (artifact `transcript`)
Nội bộ (không đăng ký artifact): subtitle_raw.meta.json (dấu vân tay thu thập), _acq/ (tạm).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..contracts import ErrorClass, SourceAdapter, StageContext, StageError, StageResult
from ..fsutil import atomic_write_json
from .transcript import TranscriptProcessor

SOURCE_JSON = "source.json"


def _write_if_changed(path: Path, obj: dict) -> None:
    try:
        same = json.loads(path.read_text(encoding="utf-8")) == json.loads(json.dumps(obj))
    except (OSError, ValueError):
        same = False
    if not same:
        atomic_write_json(path, obj)


def run(ctx: StageContext, source: SourceAdapter) -> StageResult:
    src = ctx.params.get("input")
    if not src or not src.get("value"):
        raise StageError(ErrorClass.POLICY, "MISSING_INPUT", "job.params.input.value trống")
    res = source.acquire(src, ctx.stage_dir, ctx)
    if res.get("status", "ok") != "ok":
        e = res.get("error") or {}
        raise StageError(ErrorClass(e.get("error_class", "TRANSIENT")), e.get("code", "SOURCE_FAILED"), e.get("message", ""))
    ctx.progress(1, 3, "subtitle acquired")
    raw = Path(res["raw_subtitle_path"])
    if not raw.is_file() or raw.stat().st_size == 0:
        raise StageError(ErrorClass.POLICY, "EMPTY_OUTPUT", "phụ đề thô rỗng")

    tp = TranscriptProcessor(ctx.config.get("source", {}).get("reconstruct", {}))
    out = tp.process(raw, res["subtitle_format"], ctx.stage_dir, ctx,
                     {"provider": res.get("provider"), "video_id": res.get("video_id"), "lang": res.get("language"),
                      "kind": res.get("subtitle_kind")})

    ctx.progress(2, 3, "transcript processed")

    def rel(p: Path) -> str:
        return p.resolve().relative_to(ctx.workspace.resolve()).as_posix()

    doc = {k: v for k, v in res.items() if k not in ("raw_subtitle_path", "origin")}
    doc.update(raw_subtitle_path=rel(raw), transcript={"structured": rel(out["structured"]), "clean": rel(out["clean"]),
                                                       "stats": out["stats"]})
    _write_if_changed(ctx.stage_dir / SOURCE_JSON, doc)

    title, lang = res.get("title") or "untitled", res.get("language") or "und"
    return StageResult(
        [ctx.draft(raw, "subtitle_raw", format=res["subtitle_format"], provider=res.get("provider")),
         ctx.draft(out["structured"], "transcript_structured"),
         ctx.draft(out["clean"], "transcript"),
         ctx.draft(ctx.stage_dir / SOURCE_JSON, "metadata", title=title, language=lang)],
        {"title": title, "language": lang, "provider": res.get("provider"), "video_id": res.get("video_id"),
         "subtitle_format": res["subtitle_format"], "subtitle_kind": res.get("subtitle_kind"),
         "raw_origin": res.get("origin"), "reused_parse": out["reused"], **out["stats"]})
