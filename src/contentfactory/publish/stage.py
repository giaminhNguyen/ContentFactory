"""Stage PUBLISH (YouTube). Dùng artifact trong WORKSPACE (video + thumbnail final + `publish_metadata` do Metadata Builder dựng), KHÔNG phụ thuộc output/ (HANDOFF §17).

- Upload lỗi KHÔNG làm mất video đã render và retry upload KHÔNG render lại: render nằm ở stage trước, stage này chỉ gửi artifact đã niêm phong.
- idempotency_key = stage_key (băm input + tham số khai báo + config): retry cùng đầu vào => cùng job trong yt_uploader => tối đa một video trên YouTube.
- Uploader không tự nghĩ title/description: nhận đúng `publish_metadata.youtube_title/description` (cũng là nội dung title.txt/description.txt của gói output).
- Mặc định đăng: params > Channel Config (`publishing`) > config `publishing.defaults` (privacy mặc định `private`). `made_for_kids` BẮT BUỘC khai báo (params hoặc channel), không có default.
"""
from __future__ import annotations

import json

from ..contracts import ErrorClass, PublishAdapter, StageContext, StageError, StageResult
from ..fsutil import atomic_write_json
from contentfactory import ytlimits as MD


def run(ctx: StageContext, publish: PublishAdapter, sequence) -> StageResult:
    p = ctx.params
    pm = ctx.read_json("publish_metadata")
    chp = (ctx.config.get("channel_config") or {}).get("publishing") or {}
    dfl = (ctx.config.get("publishing") or {}).get("defaults") or {}
    # Kiểm lần cuối NGAY TRƯỚC khi chạm uploader/YouTube (metadata có thể do tay chỉnh/import): sai ⇒ POLICY, không gọi daemon; lỗi định dạng nhỏ tự sửa tất định.
    title, description = MD.check_title(pm.get("youtube_title")), MD.check_description(pm.get("description"))
    pub, fixes = MD.clean_publishing(MD.pick_publishing(p, chp, dfl))
    for f in fixes:
        ctx.log("publish_metadata_autofix", "warning", message=f)
    res = publish.publish({
        "platform": getattr(publish, "platform", "youtube"), "video": ctx.one("video_youtube"), "thumbnail": ctx.one("thumbnail"),
        "title": title, "description": description, **pub, "idempotency_key": ctx.stage_key}, ctx)
    if res.get("state") != "completed":
        e = res.get("error")
        if e:
            raise StageError(ErrorClass(e["error_class"]), e["code"], e.get("message", ""), resource=e.get("resource"))
        raise StageError(ErrorClass.TRANSIENT, "PUBLISH_NOT_COMPLETED", json.dumps(res, default=str))
    sequence.mark_published(ctx.job_id)
    out = ctx.stage_dir / "publish_result.json"
    atomic_write_json(out, {**{k: res.get(k) for k in ("state", "remote_id", "remote_url", "warnings", "job_id")},
                            "title": pm["youtube_title"], "sequence": pm["sequence"], "channel_id": pm["channel_id"]})
    return StageResult([ctx.draft(out, "publish_result", remote_id=res.get("remote_id"))],
                       {"remote_id": res.get("remote_id"), "remote_url": res.get("remote_url"), "sequence": pm["sequence"],
                        "warnings": len(res.get("warnings") or [])})
