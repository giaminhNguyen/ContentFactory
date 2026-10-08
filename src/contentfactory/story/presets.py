"""Mẫu cấu hình Story Remix (preset) có tên: lưu một lần, dùng cho mọi job. `default` = mẫu áp cho job mới khi người dùng không chọn gì (chọn Story Remix MỘT lần rồi chỉ bấm RUN).

Lưu ở runtime/story_presets.json (ghi nguyên tử). Mọi mẫu được kiểm bằng ĐÚNG schema của job (story/mode.py); sai ⇒ từ chối rõ ràng, không lưu.
Kế thừa hiệu lực: mặc định hệ thống (Cài đặt) < mẫu < giá trị riêng của job.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..contracts import ErrorClass, StageError
from ..fsutil import atomic_write_json
from . import mode as SM

NAME_RX = re.compile(r"^[\w][\w \-]{0,39}$", re.UNICODE)
MAX_PRESETS = 30


def _err(code: str, msg: str, hint: str = "") -> StageError:
    return StageError(ErrorClass.POLICY, code, msg, {"hint": hint} if hint else {}, resource="input")


def load(path: Path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(d.get("presets"), dict):
            return {"presets": d["presets"], "default": d.get("default") if d.get("default") in d["presets"] else None}
    except (OSError, ValueError, AttributeError):
        pass
    return {"presets": {}, "default": None}


def _save(path: Path, d: dict) -> None:
    atomic_write_json(Path(path), d)


def save_preset(path: Path, name: str, raw, story_cfg: dict | None) -> dict:
    name = (name or "").strip()
    if not NAME_RX.match(name):
        raise _err("INVALID_PRESET_NAME", "Tên mẫu gồm chữ, số, khoảng trắng, - hoặc _ (tối đa 40 ký tự).")
    parsed = SM.parse({**(raw if isinstance(raw, dict) else {}), "mode": "story_remix"}, story_cfg)
    d = load(path)
    if name not in d["presets"] and len(d["presets"]) >= MAX_PRESETS:
        raise _err("TOO_MANY_PRESETS", f"Tối đa {MAX_PRESETS} mẫu.", "Xoá bớt mẫu không dùng.")
    d["presets"][name] = {"story": parsed["story"], "character_universe": parsed["character_universe"], "saved_at": time.time()}
    _save(path, d)
    return d


def delete_preset(path: Path, name: str) -> dict:
    d = load(path)
    if name not in d["presets"]:
        raise _err("PRESET_NOT_FOUND", f"Không có mẫu “{name}”.")
    del d["presets"][name]
    if d["default"] == name:
        d["default"] = None
    _save(path, d)
    return d


def set_default(path: Path, name: str | None) -> dict:
    d = load(path)
    if name is not None and name not in d["presets"]:
        raise _err("PRESET_NOT_FOUND", f"Không có mẫu “{name}”.")
    d["default"] = name
    _save(path, d)
    return d


def default_raw(path: Path) -> dict | None:
    """Cấu hình thô của mẫu mặc định (None nếu chưa đặt) — runner dùng khi job không chọn chế độ."""
    d = load(path)
    p = d["presets"].get(d["default"]) if d["default"] else None
    return {"mode": "story_remix", "story": p["story"], "character_universe": p["character_universe"]} if p else None
