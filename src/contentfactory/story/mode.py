"""Chế độ truyện của job: `story_branch` (Story hiện có, mặc định, không đổi) hoặc `story_remix` (Story Remix + Living Character Universe).

`params.story_mode` CHỈ được lưu khi job chọn `story_remix` (job cũ và job Story thường không có field ⇒ y như trước, khóa stage không đổi).
Schema ở đây là NGUỒN DUY NHẤT: server kiểm bằng `parse`, UI lấy cùng schema qua `describe()`; sai thì từ chối rõ ràng (không ép kiểu âm thầm).
Giá trị hiệu lực = mặc định hệ thống (`SYSTEM_DEFAULTS`, ghi đè bằng Settings `story.*`) < giá trị job gửi lên; kết quả chốt vào params lúc tạo job.
"""
from __future__ import annotations

import copy
import hashlib
import json

from ..contracts import ErrorClass, StageError
from . import guidance as GD

MODES = ("story_branch", "story_remix")
DEFAULT_MODE = "story_branch"
LABELS = {"story_branch": "Story hiện có", "story_remix": "Story Remix — Xào truyện theo mô-típ"}
DESCRIPTIONS = {
    "story_branch": "Viết lại truyện từ transcript nguồn bằng quy trình hiện có (giữ nguyên cách chạy cũ).",
    "story_remix": "Học mô-típ, thể loại và cơ chế cảm xúc của nguồn rồi viết một truyện ORIGINAL: nhân vật, xung đột và diễn biến khác hẳn. "
                   "Tự chọn/tạo nhân vật từ Kho nhân vật và tự cập nhật kho sau khi truyện đạt QA.",
}
# Story Remix chỉ chạy được khi backend đã hoàn tất; lật True ở Phase 5. Cờ người dùng: Settings `story.remix_enabled`.
BACKEND_READY = False

ENDINGS = ("auto", "happy", "bittersweet", "open", "tragic")
READABILITY = ("standard", "high")
RIGHTS = ("unknown", "own", "licensed", "permitted")
REUSE = ("reuse", "create_new")
CANON = ("parallel",)

# (khóa, kiểu, mặc định, ràng buộc, nhãn, gợi ý) — UI render form từ đúng bảng này
STORY_FIELDS = [
    ("target_genre", "text", "", {"max_len": 80}, "Thể loại mong muốn", "Để trống = AI tự chọn theo mô-típ của nguồn."),
    ("tone", "text", "", {"max_len": 80}, "Giọng văn / không khí", "Ví dụ: u ám, hài hước nhẹ, hồi hộp."),
    ("ending", "select", "auto", {"options": ENDINGS}, "Kiểu kết thúc", "Tự động = hợp với lời hứa cảm xúc của nguồn."),
    ("audio_readability", "select", "standard", {"options": READABILITY}, "Độ dễ nghe", "Cao = câu ngắn, ít đại từ mơ hồ, hợp để đọc thành audio."),
    ("blocked_themes", "list", [], {"max_items": 20, "max_len": 60}, "Chủ đề cấm", "Mỗi dòng một chủ đề AI không được đưa vào."),
    ("source_rights", "select", "unknown", {"options": RIGHTS}, "Quyền sử dụng nguồn", "Không rõ ⇒ báo cáo originality sẽ đánh dấu cần người xem lại."),
    ("source_provenance", "text", "", {"max_len": 300}, "Nguồn gốc / ghi chú bản quyền", "Ghi lại nguồn và quyền bạn có (chỉ để lưu vết)."),
    ("rights_ack", "bool", False, {}, "Tôi hiểu: nguồn chỉ dùng để học mô-típ", "Hệ thống không tuyên bố ‘an toàn bản quyền’."),
    ("auto_select_premise", "bool", True, {}, "Tự chọn ý tưởng tốt nhất", "Tắt = dừng lại cho bạn xem/chọn ý tưởng trước khi viết."),
    ("outline_gate", "bool", True, {}, "Kiểm tra đại cương trước khi viết dài", "Chặn sớm đại cương yếu/giống nguồn, tránh tốn chi phí viết cả truyện."),
    ("premise_candidates", "int", 3, {"min": 2, "max": 6}, "Số ý tưởng ứng viên", "Nhiều hơn = chọn tốt hơn nhưng tốn hơn."),
    ("quality_repair_max_passes", "int", 1, {"min": 0, "max": 3}, "Số lượt sửa lỗi tối đa", "Giới hạn chi phí sửa chương không đạt."),
    ("budget_usd", "number_or_null", None, {"min": 1, "max": 5000}, "Ngân sách tối đa (USD)", "Trống = không giới hạn. Dừng an toàn khi ước tính vượt."),
]
UNIVERSE_FIELDS = [
    ("auto_cast", "bool", True, {}, "Tự chọn nhân vật", "AI tự chọn nhân vật có sẵn hoặc tạo mới."),
    ("reuse_strategy", "select", "reuse", {"options": REUSE}, "Ưu tiên nhân vật hiện có", "‘Ưu tiên’ nghĩa là thích hợp thì dùng lại, không ép dùng nhân vật không hợp."),
    ("canon_mode", "select", "parallel", {"options": CANON}, "Dòng thời gian độc lập", "Mỗi truyện có dòng thời gian riêng; sự kiện không lan sang truyện khác."),
    ("auto_update_after_qa", "bool", True, {}, "Tự cập nhật kho sau QA", "Chỉ ghi vào kho khi truyện đã đạt QA."),
    ("allow_new_characters", "bool", True, {}, "Cho phép tạo nhân vật mới", "Tắt = chỉ dùng nhân vật có sẵn (có thể không đủ vai)."),
    ("pinned_character_ids", "list", [], {"max_items": 12, "max_len": 64}, "Nhân vật bắt buộc dùng", "Tuỳ chọn: ghim nhân vật vào truyện; không ghim thì AI tự chọn."),
]
SECTIONS = {"story": STORY_FIELDS, "character_universe": UNIVERSE_FIELDS}


def _err(field: str, msg: str, hint: str = "") -> StageError:
    return StageError(ErrorClass.POLICY, "INVALID_STORY_MODE", f"{field}: {msg}", {"field": field, "hint": hint or "Sửa giá trị rồi thử lại."}, resource="input")


def defaults() -> dict:
    return {sec: {k: copy.deepcopy(d) for k, _, d, *_ in fields} for sec, fields in SECTIONS.items()}


def _check(sec: str, key: str, typ: str, rule: dict, v):
    where = f"{sec}.{key}"
    if typ == "bool":
        if not isinstance(v, bool):
            raise _err(where, "phải là true/false.")
    elif typ == "int":
        if isinstance(v, bool) or not isinstance(v, int) or not rule["min"] <= v <= rule["max"]:
            raise _err(where, f"phải là số nguyên {rule['min']}–{rule['max']}.")
    elif typ == "number_or_null":
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not rule["min"] <= v <= rule["max"]):
            raise _err(where, f"phải để trống hoặc là số {rule['min']}–{rule['max']}.")
    elif typ == "select":
        if v not in rule["options"]:
            raise _err(where, f"không hợp lệ ({v!r}).", "Giá trị cho phép: " + ", ".join(rule["options"]))
    elif typ == "text":
        if not isinstance(v, str) or len(v.strip()) > rule["max_len"]:
            raise _err(where, f"phải là văn bản tối đa {rule['max_len']} ký tự.")
        return v.strip()
    elif typ == "list":
        if not isinstance(v, list) or len(v) > rule["max_items"] or not all(isinstance(x, str) and x.strip() and len(x.strip()) <= rule["max_len"] for x in v):
            raise _err(where, f"phải là danh sách tối đa {rule['max_items']} mục văn bản (mỗi mục ≤ {rule['max_len']} ký tự, không rỗng).")
        out = []
        for x in (s.strip() for s in v):
            if x not in out:
                out.append(x)
        return out
    return v


def _section(sec: str, raw, base: dict) -> dict:
    out = dict(base)
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise _err(sec, "phải là một object.")
    known = {k for k, *_ in SECTIONS[sec]}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise _err(sec, f"khóa không hỗ trợ: {', '.join(unknown)}.")
    spec = {k: (t, r) for k, t, _, r, *_ in SECTIONS[sec]}
    for k, v in raw.items():
        out[k] = _check(sec, k, *spec[k], v)
    return out


def system_defaults(story_cfg: dict | None) -> dict:
    """Mặc định hệ thống = DEFAULTS trong code + Settings `story.character_universe.*` (nếu người dùng đã đặt)."""
    d = defaults()
    cu = (story_cfg or {}).get("character_universe")
    if isinstance(cu, dict):
        d["character_universe"] = _section("character_universe", {k: v for k, v in cu.items() if k in d["character_universe"]}, d["character_universe"])
    return d


def parse(raw, story_cfg: dict | None = None) -> dict:
    """Giá trị người dùng gửi → {"mode": "story_branch"} hoặc {"mode": "story_remix", "story": {...đủ}, "character_universe": {...đủ}}. Sai ⇒ StageError."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _err("story_mode", "phải là object {mode, story, character_universe}.")
    extra = sorted(set(raw) - {"mode", "story", "character_universe"})
    if extra:
        raise _err("story_mode", f"khóa không hỗ trợ: {', '.join(extra)}.")
    mode = raw.get("mode") or default_mode(story_cfg)
    if mode not in MODES:
        raise _err("mode", f"không hợp lệ ({mode!r}).", "Giá trị cho phép: " + ", ".join(MODES))
    base = system_defaults(story_cfg)
    story = _section("story", raw.get("story"), base["story"])               # luôn kiểm tra, kể cả khi mode cũ (UI giữ cấu hình khi chuyển qua lại)
    cu = _section("character_universe", raw.get("character_universe"), base["character_universe"])
    if mode == DEFAULT_MODE:
        return {"mode": DEFAULT_MODE}
    return {"mode": mode, "story": story, "character_universe": cu}


def default_mode(story_cfg: dict | None) -> str:
    m = (story_cfg or {}).get("default_mode") or DEFAULT_MODE
    return m if m in MODES and (m == DEFAULT_MODE or enabled(story_cfg)) else DEFAULT_MODE     # mặc định chưa khả dụng ⇒ vẫn Story cũ, không làm hỏng mọi job


def enabled(story_cfg: dict | None) -> bool:
    return bool(BACKEND_READY and (story_cfg or {}).get("remix_enabled", True))


def unavailable_reason(story_cfg: dict | None) -> str:
    if not BACKEND_READY:
        return "Story Remix đang được phát triển (chưa chạy được). Story hiện có vẫn hoạt động bình thường."
    if not (story_cfg or {}).get("remix_enabled", True):
        return "Story Remix đang tắt trong Cài đặt > Truyện."
    return ""


def resolve_for_job(raw, story_cfg: dict | None) -> dict | None:
    """Gọi lúc tạo job. None = giữ job như cũ (không lưu field). Story Remix mà chưa khả dụng ⇒ từ chối rõ ràng, không bao giờ chạy nhầm sang Story cũ."""
    m = parse(raw, story_cfg)
    if m["mode"] != "story_remix":
        return None
    if not enabled(story_cfg):
        raise StageError(ErrorClass.POLICY, "REMIX_UNAVAILABLE", unavailable_reason(story_cfg),
                         {"hint": "Chọn “Story hiện có” để chạy ngay."}, resource="input")
    return m


def of_job(params: dict) -> dict:
    """Chế độ đã chốt của job (job cũ = story_branch). Không ném lỗi: dữ liệu đã được kiểm lúc ghi."""
    raw = (params or {}).get("story_mode")
    if isinstance(raw, dict) and raw.get("mode") == "story_remix":
        return raw
    return {"mode": DEFAULT_MODE}


# Phần của cấu hình ảnh hưởng nội dung ⇒ vào stage_key. Cố ý BỎ: rights_ack, source_provenance (chỉ lưu vết), budget_usd (không đổi nội dung).
_NOT_IN_KEY = {"story": {"rights_ack", "source_provenance", "budget_usd"}, "character_universe": set()}


def _key(m: dict) -> dict:
    return {"story_mode": {"mode": m["mode"], **{s: {k: v for k, v in m[s].items() if k not in _NOT_IN_KEY[s]} for s in SECTIONS}}}


def stage_key_extra(extra: dict | None) -> dict | None:
    """Phần `extra` của stage_key `story`: guidance (D-112) + cấu hình Story Remix. Job Story thường/không guidance ⇒ None ⇒ khóa y như trước."""
    extra = extra or {}
    out = dict(GD.key_extra(extra.get("story_guidance")) or {})
    m = extra.get("story_mode")
    if isinstance(m, dict) and m.get("mode") == "story_remix":
        out.update(_key(m))
    return out or None


def run_extra(run_row: dict | None) -> dict | None:
    """stage_key_extra của một lần chạy đã lưu (từ stage_runs.meta): để tính lại khóa theo ĐÚNG đầu vào lần chạy đó, không bị sửa Cài đặt về sau làm lệch."""
    if not run_row or run_row.get("stage") != "story":
        return None
    try:
        meta = json.loads(run_row.get("meta") or "{}")
    except ValueError:
        return None
    return stage_key_extra({"story_guidance": meta.get("guidance"), "story_mode": meta.get("story_mode")})


def fingerprint(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def describe(story_cfg: dict | None) -> dict:
    """Mô tả cho UI: danh sách mode (kèm khả dụng), schema từng trường, mặc định hệ thống."""
    why = unavailable_reason(story_cfg)
    modes = [{"id": m, "label": LABELS[m], "description": DESCRIPTIONS[m], "available": m == DEFAULT_MODE or not why, "reason": "" if (m == DEFAULT_MODE or not why) else why} for m in MODES]
    schema = {sec: [{"key": k, "type": t, "default": d, "rule": {a: list(b) if isinstance(b, tuple) else b for a, b in r.items()}, "label": lab, "hint": hint}
                    for k, t, d, r, lab, hint in fields] for sec, fields in SECTIONS.items()}
    return {"modes": modes, "default_mode": default_mode(story_cfg), "available": not why, "reason": why,
            "schema": schema, "defaults": system_defaults(story_cfg)}


def effective(raw, story_cfg: dict | None) -> dict:
    """Cấu hình hiệu lực + nguồn từng giá trị (system|job) để UI hiện ‘kế thừa’ và xem trước trước khi chạy."""
    parsed = parse(raw, story_cfg)
    base = system_defaults(story_cfg)
    given = raw if isinstance(raw, dict) else {}
    out = {"mode": parsed["mode"], "label": LABELS[parsed["mode"]], "available": parsed["mode"] == DEFAULT_MODE or enabled(story_cfg),
           "reason": "" if (parsed["mode"] == DEFAULT_MODE or enabled(story_cfg)) else unavailable_reason(story_cfg)}
    full = _section("story", given.get("story"), base["story"]), _section("character_universe", given.get("character_universe"), base["character_universe"])
    for sec, vals in zip(("story", "character_universe"), full):
        out[sec] = {k: {"value": v, "source": "job" if k in (given.get(sec) or {}) else "system"} for k, v in vals.items()}
    return out
