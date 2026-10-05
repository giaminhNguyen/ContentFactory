"""Stage SOURCE: input người dùng -> phụ đề gốc + structured + clean transcript + metadata.
Chỉ biết contract SourceProcessor."""
from __future__ import annotations

from ..contracts import ErrorClass, SourceProcessor, StageContext, StageError, StageResult


def run(ctx: StageContext, source: SourceProcessor) -> StageResult:
    src = ctx.params.get("input")
    if not src or not src.get("value"):
        raise StageError(ErrorClass.POLICY, "MISSING_INPUT", "job.params.input.value trống")
    res = source.process(src, ctx.stage_dir, ctx)
    for key in ("subtitle_raw", "structured", "transcript", "metadata"):
        if not res[key].is_file() or res[key].stat().st_size == 0:
            raise StageError(ErrorClass.POLICY, "EMPTY_OUTPUT", f"source trả {key} rỗng")
    return StageResult(
        [ctx.draft(res["subtitle_raw"], "subtitle_raw"),
         ctx.draft(res["structured"], "transcript_structured"),
         ctx.draft(res["transcript"], "transcript"),
         ctx.draft(res["metadata"], "metadata", title=res["title"], language=res["language"])],
        {"title": res["title"], "language": res["language"], **res.get("stats", {})})
