"""Stage PUBLISH (YouTube). Dùng artifact trong workspace, KHÔNG phụ thuộc output/ (HANDOFF §17).
idempotency_key = stage_key: cùng đầu vào => tối đa một video trên nền tảng."""
from __future__ import annotations

import json

from ..contracts import ErrorClass, PublishAdapter, StageContext, StageError, StageResult
from ..fsutil import atomic_write_json


def run(ctx: StageContext, publish: PublishAdapter) -> StageResult:
    p = ctx.params
    if not isinstance(p.get("made_for_kids"), bool):          # bắt buộc khai báo, không có default
        raise StageError(ErrorClass.POLICY, "MISSING_MADE_FOR_KIDS", "job.params.made_for_kids phải là true/false")
    meta = ctx.read_json("metadata")
    story = ctx.one("story_text").read_text(encoding="utf-8")
    res = publish.publish({
        "platform": getattr(publish, "platform", "youtube"),
        "video": ctx.one("video_youtube"), "thumbnail": ctx.one("thumbnail"),
        "title": meta["title"], "description": story[:300].strip(),                 # không dùng mô tả của nguồn (D-31)
        "tags": p.get("tags", []), "privacy": p.get("privacy", "private"),
        "made_for_kids": p["made_for_kids"], "account_id": p.get("account_id"),
        "idempotency_key": ctx.stage_key}, ctx)
    if res.get("state") != "completed":
        e = res.get("error")
        if e:
            raise StageError(ErrorClass(e["error_class"]), e["code"], e.get("message", ""))
        raise StageError(ErrorClass.TRANSIENT, "PUBLISH_NOT_COMPLETED", json.dumps(res, default=str))
    out = ctx.stage_dir / "publish_result.json"
    atomic_write_json(out, {k: res.get(k) for k in ("state", "remote_id", "remote_url", "warnings")})
    return StageResult([ctx.draft(out, "publish_result", remote_id=res.get("remote_id"))],
                       {"remote_id": res.get("remote_id"), "remote_url": res.get("remote_url")})
