"""Prosody Profile: bảng khoảng nghỉ theo LOẠI ranh giới (ms), tách khỏi voice/TTS profile (D-100).

TTS profile = giọng/engine/giới hạn của engine (`max_chars`, `settings`...). Prosody profile = NHỊP đọc: mỗi loại ranh giới nghỉ bao lâu.
Các giá trị là mặc định theo profile (khoảng gợi ý trong plan: micro 60–120, comma 100–180, colon 180–280, semicolon 200–320, sentence 300–450,
question 350–500, exclamation 300–480, ellipsis 500–850, paragraph 600–900, dialogue 450–750, scene 900–1500), KHÔNG phải hằng số gắn vào dấu câu:
cùng một dấu có thể ra thời lượng khác nhau tùy vị trí (cuối đoạn, trong hội thoại, trước cảnh mới) — xem `plan.py`. Cần nghe thử để chỉnh.

    params.prosody = {"profile": "natural|fast|dramatic|custom", "custom": {<loại>: ms}, "scale": 1.0,
                      "overrides": {<khóa ranh giới>: {"pause_ms": N}}, "semantic_llm": false, "qc": {...}}

Mặc định hoàn toàn tất định, KHÔNG gọi LLM. `semantic_llm` chỉ là tùy chọn có chủ đích (xem plan.semantic_*).
"""
from __future__ import annotations

import copy

from ...contracts import ErrorClass, StageError

RULES_VERSION = "vi-1"                  # đổi khi luật tách câu/ranh giới/ghép nhóm đổi ⇒ speech plan cũ không còn dùng lại được
MAX_PAUSE_MS = 10_000

# loại ranh giới -> nhãn hiển thị
BOUNDARY_LABELS = {
    "micro": "Nghỉ rất ngắn", "comma": "Dấu phẩy", "colon": "Dấu hai chấm", "semicolon": "Dấu chấm phẩy", "sentence": "Hết câu",
    "question": "Hết câu hỏi", "exclamation": "Hết câu cảm thán", "ellipsis_hesitation": "Ngập ngừng (…)", "ellipsis": "Dấu ba chấm cuối câu",
    "paragraph": "Hết đoạn", "dialogue": "Đổi lượt thoại", "scene": "Chuyển cảnh", "reveal": "Tiết lộ kịch tính",
}
BOUNDARY_KINDS = tuple(BOUNDARY_LABELS)
EDITABLE_KINDS = ("sentence", "question", "exclamation", "ellipsis", "paragraph", "dialogue", "scene", "reveal")   # hiển thị ở giao diện nâng cao

PROFILES: dict[str, dict[str, int]] = {
    "natural": {"micro": 90, "comma": 140, "colon": 230, "semicolon": 260, "sentence": 380, "question": 420, "exclamation": 380,
                "ellipsis_hesitation": 300, "ellipsis": 650, "paragraph": 720, "dialogue": 560, "scene": 1200, "reveal": 900},
    "fast": {"micro": 60, "comma": 100, "colon": 180, "semicolon": 200, "sentence": 300, "question": 350, "exclamation": 300,
             "ellipsis_hesitation": 220, "ellipsis": 500, "paragraph": 600, "dialogue": 450, "scene": 900, "reveal": 700},
    "dramatic": {"micro": 120, "comma": 180, "colon": 280, "semicolon": 320, "sentence": 450, "question": 500, "exclamation": 480,
                 "ellipsis_hesitation": 400, "ellipsis": 850, "paragraph": 900, "dialogue": 750, "scene": 1500, "reveal": 1400},
}
PROFILE_LABELS = {"natural": "Tự nhiên", "fast": "Nhanh", "dramatic": "Kịch tính", "custom": "Tùy chỉnh"}
QC_DEFAULTS = {"max_pause_ms": 2500, "long_sentence_chars": 300, "tiny_group_words": 2}


def _bad(msg: str, **detail) -> StageError:
    return StageError(ErrorClass.POLICY, "INVALID_PROSODY", msg, detail, resource="input")


def resolve_prosody(raw: dict | None) -> dict:
    """Prosody hiệu lực (thuần, tất định). `raw` None/{} = không dùng prosody (đường cũ) — chỉ gọi khi `raw` có mặt.
    Trả {"profile", "pauses": {loại: ms}, "scale", "overrides", "semantic_llm", "qc", "rules_version"}. Raise POLICY INVALID_PROSODY nếu sai."""
    raw = copy.deepcopy(raw or {})
    name = str(raw.get("profile") or "natural").lower()
    if name not in PROFILE_LABELS:
        raise _bad(f"prosody.profile không hợp lệ: {name!r}; hợp lệ: {list(PROFILE_LABELS)}")
    base = dict(PROFILES["natural" if name == "custom" else name])
    errs: list[str] = []
    custom = raw.get("custom") or {}
    if not isinstance(custom, dict):
        errs.append("prosody.custom phải là object {loại_ranh_giới: ms}")
        custom = {}
    for k, v in custom.items():
        if k not in BOUNDARY_LABELS:
            errs.append(f"prosody.custom.{k}: không phải loại ranh giới; hợp lệ: {list(BOUNDARY_KINDS)}")
        elif not _is_ms(v):
            errs.append(f"prosody.custom.{k}={v!r} phải là số nguyên ms trong [0, {MAX_PAUSE_MS}]")
        else:
            base[k] = int(v)
    scale = raw.get("scale", 1.0)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0 <= scale <= 5:
        errs.append(f"prosody.scale={scale!r} phải là số trong [0, 5]")
        scale = 1.0
    overrides = raw.get("overrides") or {}
    clean: dict[str, dict] = {}
    if not isinstance(overrides, dict):
        errs.append("prosody.overrides phải là object {khóa_ranh_giới: {pause_ms}}")
        overrides = {}
    for key, v in overrides.items():
        ms = v.get("pause_ms") if isinstance(v, dict) else None
        if isinstance(v, dict) and "pause_ms" in v and ms is None:
            continue                                                  # {"pause_ms": null} = "Reset Auto": bỏ override (params_patch gộp sâu nên không xóa khóa được)
        if not _is_ms(ms):
            errs.append(f"prosody.overrides.{key}.pause_ms={ms!r} phải là số nguyên ms trong [0, {MAX_PAUSE_MS}]")
        else:
            clean[str(key)] = {"pause_ms": int(ms)}
    qc = {**QC_DEFAULTS, **(raw.get("qc") or {})}
    if errs:
        raise _bad("; ".join(errs), errors=errs)
    pauses = {k: min(MAX_PAUSE_MS, max(0, round(v * float(scale)))) for k, v in base.items()}
    return {"profile": name, "pauses": pauses, "scale": float(scale), "overrides": clean, "semantic_llm": bool(raw.get("semantic_llm", False)),
            "qc": qc, "rules_version": RULES_VERSION}


def _is_ms(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= MAX_PAUSE_MS and float(v).is_integer()


def describe() -> dict:
    """Mô tả cho giao diện: các profile + bảng ms + nhãn loại ranh giới (nguồn sự thật nằm ở backend)."""
    return {"profiles": [{"id": k, "label": PROFILE_LABELS[k], "pauses": PROFILES[k] if k != "custom" else PROFILES["natural"]} for k in PROFILE_LABELS],
            "kinds": [{"id": k, "label": BOUNDARY_LABELS[k], "editable": k in EDITABLE_KINDS} for k in BOUNDARY_KINDS],
            "max_pause_ms": MAX_PAUSE_MS, "rules_version": RULES_VERSION}
