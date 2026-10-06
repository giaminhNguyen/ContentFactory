"""Chẩn đoán job cho người dùng (D-88): CLI (`status`, `go`) và UI dùng CHUNG một nguồn để thông điệp không lệch nhau.

`explain(orc, job_id)` trả về một dict đủ để biết: job nào, stage nào, artifact nào, provider nào, vì sao, đã thử mấy lần, checkpoint ở đâu,
đường đi tiếp (tự động / bấm gì). Người dùng bình thường không cần đọc stack trace; log chi tiết chỉ là đường dẫn đính kèm.
`ui_status(job)` gom trạng thái backend (state + hold + điều khiển của người dùng) về 8 nhóm hiển thị: running, queued, waiting, attention, paused, cancelled, completed, failed.
"""
from __future__ import annotations

import time

from ..jobs import pipeline as P
from ..jobs.workspace import job_dir

USER_ONLY_HOLDS = frozenset({P.PAUSED_CREDENTIAL, P.PAUSED_MISSING_INPUT})

# hold_reason -> (tiêu đề ngắn, giải thích, việc cần làm). {job} được thay bằng id job.
HOLD_INFO: dict[str, tuple[str, str, str, str]] = {
    P.PAUSED_NETWORK: ("Đang chờ mạng", "Mất kết nối mạng hoặc dịch vụ không trả lời.", "mạng ổn định trở lại", "Kiểm tra kết nối mạng."),
    P.PAUSED_TOKEN: ("Đang chờ hạn mức AI", "Hết token/usage của dịch vụ AI (Claude/TTS API).", "hạn mức AI được reset", "Chờ reset hạn mức hoặc đổi tài khoản/API key."),
    P.PAUSED_QUOTA: ("Đang chờ quota", "Hết quota của nhà cung cấp (vd YouTube upload mỗi ngày).", "quota được reset", "Chờ quota reset (thường theo ngày)."),
    P.PAUSED_DISK: ("Không đủ chỗ trống đĩa", "Ổ đĩa không còn đủ dung lượng cho bước này.", "ổ đĩa có đủ chỗ trống", "Dọn dẹp (Cài đặt → Lưu trữ) hoặc giải phóng ổ đĩa."),
    P.PAUSED_RESOURCE: ("Một công cụ chưa sẵn sàng", "ffmpeg, daemon upload, ContentFlow hoặc công cụ khác chưa chạy được.", "công cụ đó chạy được trở lại", "Mở Doctor để xem công cụ nào thiếu và sửa."),
    P.PAUSED_CREDENTIAL: ("Cần xử lý tài khoản/credential", "Đăng nhập hoặc API key không hợp lệ/hết hạn.", "", "Đăng nhập lại (vd `yt-uploader login`) hoặc cập nhật key, rồi bấm Tiếp tục."),
    P.PAUSED_MISSING_INPUT: ("Thiếu dữ liệu đầu vào", "Thiếu file người dùng phải cung cấp (template thumbnail, thư mục video nguồn…).", "", "Bổ sung theo chi tiết rồi bấm Tiếp tục."),
}

STAGE_LABEL = {"source": "Phụ đề", "story": "Truyện", "tts": "Giọng đọc (TTS)", "audio": "Audio", "render_youtube": "Video YouTube",
               "render_tiktok": "Video TikTok", "output": "Gói output", "publish": "Đăng YouTube"}


def stage_of(job: dict) -> str | None:
    """Stage mà job đang ở / đã dừng (cho job FAILED: stage lỗi). None khi đã xong hết."""
    if job["state"] == P.FAILED:
        return job.get("failed_stage")
    pos = P.position(job["state"])
    if pos is None or pos >= len(P.STAGES):
        return None
    return P.STAGES[pos].name


def ui_status(job: dict) -> str:
    st = job["state"]
    cs = job.get("control_state") or "RUNNING"
    if cs == "CANCELLED":
        return "cancelled"
    if st == P.FAILED:
        return "failed"
    if P.is_complete(st, job.get("target_idx")):
        return "completed"
    if cs == "PAUSED":                                    # người dùng tự tạm dừng: khác hold tài nguyên (waiting/attention)
        return "paused"
    hr = job.get("hold_reason")
    if hr:
        return "attention" if (job.get("needs_user") or hr in USER_ONLY_HOLDS) else "waiting"
    if st in P.BY_RUNNING:
        return "running"
    return "queued"


def hold_text(job: dict) -> dict | None:
    hr = job.get("hold_reason")
    if not hr:
        return None
    title, why, cond, todo = HOLD_INFO.get(hr, (hr, job.get("hold_detail") or "", "", "Mở Doctor để kiểm tra."))
    return {"reason": hr, "title": title, "why": why, "cond": cond, "todo": todo, "detail": job.get("hold_detail"), "needs_user": bool(job.get("needs_user")),
            "auto_resume": bool(job.get("auto_resume")), "since": job.get("hold_since"), "resume_after": job.get("resume_after")}


def explain(orc, job_id: str) -> dict:
    cfg = orc.cfg
    j = orc.store.get_job(job_id)
    runs = orc.store.stage_runs(job_id)
    stage = stage_of(j)
    st = P.BY_NAME.get(stage) if stage else None
    mine = [r for r in runs if r["stage"] == stage]
    err = j.get("last_error") or {}
    status = ui_status(j)
    adapter_key = st.adapters[0] if st else None
    provider = (err.get("detail") or {}).get("provider") or (cfg.data["adapters"].get(adapter_key) if adapter_key else None)
    hold = hold_text(j)
    budget = int(cfg.data["retry"]["max_attempts"])
    if status == "failed":
        resume = {"mode": "retry", "text": f"Chạy lại đúng stage '{stage}': các stage trước giữ nguyên, không làm lại.", "actions": ["retry"], "cli": f"retry {job_id}"}
    elif hold:
        cond = hold["cond"] or "nguyên nhân đã hết"
        if hold["reason"] in USER_ONLY_HOLDS:
            resume = {"mode": "manual", "text": hold["todo"], "actions": ["resume"], "cli": f"resume {job_id}"}
        elif hold["needs_user"]:
            resume = {"mode": "manual", "text": f"{cond[:1].upper()}{cond[1:]} — bấm Tiếp tục để chạy tiếp.", "actions": ["resume"], "cli": f"resume {job_id}"}
        elif j.get("auto_resume"):
            when = time.strftime("%H:%M", time.localtime(j["resume_after"])) if j.get("resume_after") else None
            resume = {"mode": "auto", "text": f"Auto Resume đang bật: tự chạy tiếp khi {cond}" + (f" (dự kiến sau {when})" if when else "") + ".",
                      "actions": ["resume_now", "disable_auto_resume"], "cli": f"resume {job_id} --now"}
        else:
            resume = {"mode": "manual", "text": f"Auto Resume đang tắt: bấm Tiếp tục khi {cond}.", "actions": ["resume", "enable_auto_resume"], "cli": f"resume {job_id}"}
    else:
        resume = {"mode": "none", "text": "", "actions": [], "cli": None}
    cs = j.get("control_state") or "RUNNING"
    if cs == "PAUSED":                                    # ý định của người dùng thắng: Auto Resume không tự chạy tiếp
        also = f" Job cũng đang chờ tài nguyên ({hold['title'].lower()}); sau khi tiếp tục vẫn chờ tới khi sẵn sàng." if hold else ""
        resume = {"mode": "manual", "text": "Bạn đã tạm dừng job. Bấm Tiếp tục để chạy tiếp từ đúng chỗ dừng; kết quả đã xong được giữ nguyên." + also,
                  "actions": ["resume"], "cli": f"resume {job_id}"}
    elif cs == "CANCELLED":
        resume = {"mode": "none", "text": "Job đã bị hủy và sẽ không tự chạy lại. Kết quả đã có vẫn được giữ; dùng “Chạy lại với thay đổi” để tạo job mới.", "actions": [],
                  "cli": f"clone {job_id}"}
    arts = [{"kind": a["kind"], "stage": a["stage"], "path": a["path"]} for a in orc.store.artifacts(job_id)]
    needs = list(st.requires) if st else []
    human = ""
    if status == "failed":
        human = f"{STAGE_LABEL.get(stage, stage)} thất bại: {err.get('message') or err.get('code') or 'không rõ nguyên nhân'}"
    elif hold:
        human = f"{hold['title']}: {hold['why']}"
    if cs == "PAUSED":
        pausing = j["state"] in P.BY_RUNNING
        human = "Đang tạm dừng: hoàn tất đơn vị đang chạy rồi dừng" if pausing else "Đã tạm dừng bởi bạn"
    elif cs == "CANCELLED":
        human = "Job đã bị hủy"
    return {
        "job_id": job_id, "state": j["state"], "status": status, "stage": stage, "stage_label": STAGE_LABEL.get(stage, stage),
        "reason_code": err.get("code") or (hold and hold["reason"]), "reason": err.get("message") or (hold and hold["detail"]),
        "error_class": err.get("error_class"), "resource": err.get("resource"), "provider": provider, "human": human,
        "inputs_needed": needs, "inputs_present": sorted({a["kind"] for a in arts} & set(needs)),
        "attempts": len(mine), "failed_attempts": sum(1 for r in mine if r["status"] in ("failed", "held", "interrupted")), "retry_budget": budget,
        "checkpoint": (j.get("checkpoint") or {}).get(stage) if stage else None, "progress": j.get("progress"),
        "hold": hold, "resume": resume, "auto_resume": bool(j.get("auto_resume")),
        "log_file": str(job_dir(cfg.path("workspace"), job_id) / "job.log.jsonl"),
    }


def format_lines(d: dict) -> list[str]:
    """Các dòng văn bản cho CLI từ kết quả explain()."""
    out = [f"  job {d['job_id']} · stage {d['stage']} · {d['status']}"]
    if d["human"]:
        out.append(f"  {d['human']}")
    bits = []
    if d.get("provider"):
        bits.append(f"provider={d['provider']}")
    if d["attempts"]:
        bits.append(f"lần thử={d['attempts']} (lỗi/giữ {d['failed_attempts']}, tối đa {d['retry_budget']} lần tự retry/lượt)")
    if d.get("reason_code"):
        bits.append(f"mã={d['reason_code']}")
    if bits:
        out.append("  " + " · ".join(bits))
    cp = d.get("checkpoint")
    if cp:
        out.append(f"  checkpoint: {cp.get('done')}/{cp.get('total')} {cp.get('detail') or ''}".rstrip())
    if d["resume"]["text"]:
        out.append(f"  -> {d['resume']['text']}" + (f"  (lệnh: {d['resume']['cli']})" if d["resume"].get("cli") else ""))
    if d["status"] in ("failed", "waiting", "attention"):
        out.append(f"  log: {d['log_file']}")
    return out
