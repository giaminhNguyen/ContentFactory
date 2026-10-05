"""Transcript Processor (ContentFactory sở hữu): phụ đề thô -> structured transcript -> clean transcript.

Không biết phụ đề đến từ provider nào, chỉ biết định dạng: srt | vtt | json (snippet start/duration/text) | txt.
Timestamp KHÔNG bị bỏ ngay: `transcript_structured.json` giữ start/end/gap của cue, câu và đoạn; chỉ
`transcript_clean.txt` (sinh SAU khi dựng lại câu/đoạn) là không có timestamp.

Idempotent: structured được dùng lại khi (sha256 raw, định dạng, phiên bản parser, hash cấu hình) không đổi;
clean được dùng lại khi sha256 khớp `clean_sha256` trong structured.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..contracts import ErrorClass, StageContext, StageError
from ..fsutil import atomic_write_json, atomic_write_text, sha256_file
from .reconstruct import PARSER_VERSION, ReconstructConfig, build_structured, build_structured_plain, clean_text
from .subtitles import parse_snippets, parse_subtitle

STRUCTURED, CLEAN = "transcript_structured.json", "transcript_clean.txt"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class TranscriptProcessor:
    def __init__(self, cfg: ReconstructConfig | dict | None = None) -> None:
        self.cfg = cfg if isinstance(cfg, ReconstructConfig) else ReconstructConfig(**(cfg or {}))

    def process(self, raw: Path, fmt: str, out_dir: Path, ctx: StageContext, provenance: dict | None = None) -> dict:
        out_dir.mkdir(parents=True, exist_ok=True)
        structured_path, clean_path = out_dir / STRUCTURED, out_dir / CLEAN
        raw_sha = sha256_file(raw)
        want = (raw_sha, fmt, PARSER_VERSION, self.cfg.hash())
        d = _read_json(structured_path)
        prov = (d or {}).get("provenance", {})
        reused = bool(d and (prov.get("raw_sha256"), prov.get("format"), prov.get("parser_version"),
                             prov.get("config_hash")) == want)
        if reused:
            ctx.log("transcript_structured_reused")
        else:
            d = self._build(raw, fmt, {**(provenance or {}), "raw_sha256": raw_sha, "format": fmt})
            atomic_write_json(structured_path, d)
        if not (clean_path.is_file() and sha256_file(clean_path) == d["clean_sha256"]):
            atomic_write_text(clean_path, clean_text(d))
        return {"structured": structured_path, "clean": clean_path, "stats": d["stats"], "reused": reused}

    def _build(self, raw: Path, fmt: str, provenance: dict) -> dict:
        text = raw.read_text(encoding="utf-8", errors="replace")
        if fmt == "txt":
            if not text.strip():
                raise StageError(ErrorClass.POLICY, "EMPTY_SUBTITLE", "văn bản nguồn rỗng")
            return build_structured_plain(text, self.cfg, provenance)
        if fmt in ("srt", "vtt"):
            cues = parse_subtitle(text)
        elif fmt == "json":
            try:
                items = json.loads(text)
                cues = parse_snippets(items)
            except (ValueError, TypeError, KeyError) as e:
                raise StageError(ErrorClass.POLICY, "BAD_SUBTITLE_FILE", f"JSON snippet không hợp lệ: {e!r}") from None
        else:
            raise StageError(ErrorClass.POLICY, "UNSUPPORTED_FORMAT", fmt)
        if not cues:
            raise StageError(ErrorClass.POLICY, "EMPTY_SUBTITLE", "phụ đề không có cue nào dùng được")
        return build_structured(cues, self.cfg, provenance)
