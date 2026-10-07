"""Story Guidance (D-112): đề xuất sáng tạo tự do của người dùng cho agent viết truyện, theo 2 tầng.

    đề xuất riêng của job (mode=custom)  >  đề xuất mặc định trong Cài đặt (story.guidance)  >  không có

`resolve` là NƠI DUY NHẤT quyết định "đề xuất hiệu lực": runner gọi nó ngay trước mỗi lần chạy stage `story` (lần đầu, retry, manual rerun) rồi chốt kết quả
(`source`, `text`, `hash`) vào stage_runs.meta — nên đổi Cài đặt sau này không viết lại lịch sử. Chuỗi rỗng KHÔNG dùng để biểu diễn "kế thừa": job lưu
`params.story_guidance = {"mode": "inherit"|"custom"|"none", "text": ...}`; thiếu field (job cũ) = inherit.

Guidance là DỮ LIỆU sáng tạo, không phải chỉ dẫn hệ thống: adapter bọc nó trong thẻ và dặn agent không để nó đổi giao thức/định dạng đầu ra.
"""
from __future__ import annotations

import hashlib
import json

from ..contracts import ErrorClass, StageError

MAX_LEN = 8000
MODES = ("inherit", "custom", "none")
DEFAULT_MODE = "inherit"


def normalize(text) -> str:
    """Giữ xuống dòng có chủ ý (chỉ gộp CRLF/CR về LF và bỏ khoảng trắng đầu/cuối); không cắt âm thầm."""
    if text is None:
        return ""
    if not isinstance(text, str):
        raise StageError(ErrorClass.POLICY, "INVALID_STORY_GUIDANCE", "Đề xuất truyện phải là văn bản.", {"hint": "Nhập chữ vào ô Đề xuất truyện."}, resource="input")
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def check_length(text: str, what: str = "Đề xuất truyện") -> str:
    if len(text) > MAX_LEN:
        raise StageError(ErrorClass.POLICY, "STORY_GUIDANCE_TOO_LONG", f"{what} dài {len(text)} ký tự, tối đa {MAX_LEN}.",
                         {"max": MAX_LEN, "length": len(text), "hint": f"Rút gọn xuống còn {MAX_LEN} ký tự."}, resource="input")
    return text


def parse(raw) -> dict:
    """Chuẩn hóa giá trị người dùng gửi lên → {"mode", "text"} hợp lệ, hoặc ném StageError (không sửa âm thầm)."""
    if raw is None:
        return {"mode": DEFAULT_MODE, "text": ""}
    if not isinstance(raw, dict):
        raise StageError(ErrorClass.POLICY, "INVALID_STORY_GUIDANCE", "story_guidance phải là {mode, text}.", resource="input")
    mode = raw.get("mode") or DEFAULT_MODE
    if mode not in MODES:
        raise StageError(ErrorClass.POLICY, "INVALID_STORY_GUIDANCE", f"Chế độ đề xuất không hợp lệ: {mode!r}.", {"valid": list(MODES)}, resource="input")
    text = check_length(normalize(raw.get("text"))) if mode == "custom" else ""
    if mode == "custom" and not text:
        raise StageError(ErrorClass.POLICY, "STORY_GUIDANCE_EMPTY", "Chọn “Dùng đề xuất riêng” thì cần nhập nội dung.",
                         {"hint": "Nhập đề xuất hoặc chọn “Dùng đề xuất trong Cài đặt”."}, resource="input")
    return {"mode": mode, "text": text}


def of_job(params: dict) -> dict:
    """Cấu hình đề xuất hiện tại của job (job cũ không có field = inherit). Không ném lỗi: dữ liệu đã được kiểm lúc ghi."""
    raw = (params or {}).get("story_guidance")
    if not isinstance(raw, dict) or raw.get("mode") not in MODES:
        return {"mode": DEFAULT_MODE, "text": ""}
    return {"mode": raw["mode"], "text": str(raw.get("text") or "") if raw["mode"] == "custom" else ""}


def hash_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def resolve(params: dict, default_text: str | None, now: float | None = None) -> dict:
    """Đề xuất hiệu lực: {"source": job|settings|none, "text", "hash", "mode"}. `custom` ghi đè HOÀN TOÀN mặc định (không nối); `none` chủ động tắt;
    `inherit` lấy mặc định hiện tại trong Cài đặt. Cả hai rỗng => source none, text rỗng (prompt giữ nguyên hành vi cũ)."""
    job = of_job(params)
    mode = job["mode"]
    if mode == "custom" and job["text"].strip():
        source, text = "job", job["text"]
    elif mode == "none":
        source, text = "none", ""
    else:
        text = normalize(default_text) if isinstance(default_text, str) else ""
        source = "settings" if text else "none"
    out = {"source": source, "text": text, "hash": hash_of(text) if text else "", "mode": mode}
    if now is not None:
        out["resolved_at"] = now
    return out


def key_extra(eff: dict | None) -> dict | None:
    """Phần đóng góp vào stage_key của `story`: chỉ có khi guidance không rỗng, nên khóa của job không dùng guidance giữ nguyên như cũ."""
    return {"story_guidance": eff["text"]} if eff and eff.get("text") else None


def run_extra(run_row: dict | None) -> dict | None:
    """key_extra của một lần chạy đã lưu (từ snapshot guidance trong stage_runs.meta): tính lại stage_key của story theo ĐÚNG đề xuất lần chạy đó dùng,
    nên việc sửa Cài đặt/đề xuất job sau này không biến Truyện và mọi thứ phía sau thành "không đồng bộ"."""
    if not run_row or run_row.get("stage") != "story":
        return None
    try:
        meta = json.loads(run_row.get("meta") or "{}")
    except ValueError:
        return None
    return key_extra(meta.get("guidance"))
