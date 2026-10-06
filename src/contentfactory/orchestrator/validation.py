"""Validator theo KIND artifact (HANDOFF §15A): một artifact hợp lệ khi file còn nguyên (sha256/size) VÀ qua validator của kind.

Dùng ở ba chỗ: import artifact (từ chối file xấu ngay khi tạo job), `validate_inputs`/`validate_outputs` của stage,
và quyết định skip. Phase sau đăng ký validator giàu hơn bằng `register` (vd audio QA, ffprobe video) mà không sửa core.
Validator CHỈ nhìn vào file (rẻ, tất định, không LLM).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from ..fsutil import wav_header
from ..story.validate import validate_story_text

Validator = Callable[[Path, dict], list[str]]
VALIDATORS: dict[str, Validator] = {}


def register(kind: str) -> Callable[[Validator], Validator]:
    def deco(fn: Validator) -> Validator:
        VALIDATORS[kind] = fn
        return fn
    return deco


def validate_kind(kind: str, path: Path, meta: dict | None = None) -> list[str]:
    """Trả danh sách vi phạm (rỗng = hợp lệ)."""
    path = Path(path)
    if not path.is_file():
        return ["MISSING"]
    if path.stat().st_size == 0:
        return ["EMPTY"]
    fn = VALIDATORS.get(kind)
    return fn(path, meta or {}) if fn else []


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@register("story_text")
def _story_text(path: Path, meta: dict) -> list[str]:
    return validate_story_text(_text(path))               # không heading, không marker, không lặp quá mức


@register("transcript")
def _transcript(path: Path, meta: dict) -> list[str]:
    return [] if _text(path).strip() else ["EMPTY"]


def _json_ok(path: Path) -> dict | list | None:
    try:
        return json.loads(_text(path))
    except ValueError:
        return None


for _kind in ("transcript_structured", "story_report", "tts_manifest", "audio_timeline", "speech_plan", "audio_report", "youtube_render_report",
              "tiktok_render_report", "output_package", "publish_result"):
    VALIDATORS[_kind] = lambda path, meta: [] if _json_ok(path) is not None else ["BAD_JSON"]


@register("metadata")
def _metadata(path: Path, meta: dict) -> list[str]:
    d = _json_ok(path)
    if not isinstance(d, dict):
        return ["BAD_JSON"]
    return [] if str(d.get("title") or "").strip() else ["NO_TITLE"]


def _audio(path: Path, meta: dict) -> list[str]:
    if path.suffix.lower() != ".wav":                    # định dạng khác: validator giàu hơn do Audio phase đăng ký
        return []
    try:                                                 # wav_header đọc được cả WAVE_FORMAT_EXTENSIBLE/float (ffmpeg 24-bit, nhiều engine TTS)
        h = wav_header(path)
    except (ValueError, OSError):
        return ["UNDECODABLE"]
    return [] if h["frames"] > 0 and h["rate"] > 0 else ["ZERO_DURATION"]


for _kind in ("audio_master", "narration_master", "audio_youtube", "audio_tiktok"):
    VALIDATORS[_kind] = _audio
