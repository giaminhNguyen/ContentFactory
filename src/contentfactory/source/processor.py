"""SourceProcessor cho YouTube: URL -> phụ đề gốc -> structured transcript -> clean transcript.

Idempotent theo từng bước (mỗi bước có "dấu vân tay"; hợp lệ thì KHÔNG làm lại):
  tải      : raw/download.json ghi sha256 của file phụ đề; file còn đúng sha thì không tải lại.
             Ngoài job còn có cache theo (video_id, danh sách ngôn ngữ ưu tiên) nên job mới cho cùng URL cũng không tải lại.
  parse    : structured.json ghi sha256 của raw + phiên bản parser + hash cấu hình dựng câu.
  clean    : transcript.txt được so với clean_sha256 trong structured.json.
Phụ đề gốc được giữ nguyên byte; timestamp còn nguyên trong structured.json, chỉ clean transcript là không có.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from ..contracts import ErrorClass, SourceInput, SourceResult, StageContext, StageError
from ..fsutil import atomic_write, atomic_write_json, atomic_write_text, sha256_file
from .reconstruct import PARSER_VERSION, ReconstructConfig, build_structured, clean_text
from .subtitles import parse_subtitle
from .youtube import YtDlp, choose_subtitle, parse_video_id

INFO_KEYS = ("id", "title", "channel", "uploader", "duration", "upload_date", "language", "webpage_url")


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json_if_changed(path: Path, obj: dict) -> None:
    if _read_json(path) != json.loads(json.dumps(obj)):
        atomic_write_json(path, obj)


class YouTubeSourceProcessor:
    def __init__(self, cfg: dict | None = None, cache_dir: Path | None = None, ytdlp: YtDlp | None = None) -> None:
        cfg = cfg or {}
        self.preferred: list[str] = cfg.get("preferred_langs", ["vi", "en"])
        self.reconstruct_cfg = ReconstructConfig(**cfg.get("reconstruct", {}))
        self.ytdlp = ytdlp or YtDlp(cfg.get("yt_dlp_cmd", ["yt-dlp"]), extra_args=cfg.get("yt_dlp_args", []))
        self.cache = Path(cache_dir) if cache_dir else None

    def health(self) -> dict:
        ok = self.ytdlp.available()
        return {"ok": ok, "tool": "yt-dlp", "fix_hint": None if ok else "pip install yt-dlp"}

    # -- bước 1: phụ đề gốc ----------------------------------------------------------------------
    def _cache_entry(self, vid: str, preferred: list[str]) -> Path | None:
        if not self.cache:
            return None
        return self.cache / "youtube" / vid / "-".join(sorted(preferred)).replace("/", "_")

    def _fetch_to_cache(self, url: str, vid: str, preferred: list[str], entry: Path, ctx: StageContext) -> dict:
        info = self.ytdlp.info(url)
        chosen = choose_subtitle(info, preferred)
        if not chosen:
            raise StageError(ErrorClass.POLICY, "NO_SUBTITLES", "video không có phụ đề (kể cả auto-caption)",
                             {"manual": sorted((info.get("subtitles") or {})),
                              "auto": sorted((info.get("automatic_captions") or {}))[:20]})
        ctx.log("source_download", lang=chosen["lang"], auto=chosen["auto"])
        dl_dir = entry / "dl"
        shutil.rmtree(dl_dir, ignore_errors=True)
        got = self.ytdlp.download_subtitle(url, chosen["lang"], chosen["auto"], dl_dir)
        final = entry / f"subtitle{got.suffix}"
        entry.mkdir(parents=True, exist_ok=True)
        shutil.move(str(got), final)
        shutil.rmtree(dl_dir, ignore_errors=True)
        meta = {"video_id": vid, "lang": chosen["lang"], "auto": chosen["auto"], "file": final.name,
                "sha256": sha256_file(final), "fetched_at": time.time(),
                "info": {k: info.get(k) for k in INFO_KEYS}}
        atomic_write_json(entry / "meta.json", meta)
        return meta

    def _ensure_raw(self, url: str, vid: str, raw_dir: Path, ctx: StageContext) -> tuple[dict, Path, str]:
        refresh = bool(ctx.params.get("refresh_source"))
        sidecar = raw_dir / "download.json"
        d = _read_json(sidecar)
        if d and not refresh:
            raw = raw_dir / d["file"]
            if raw.is_file() and sha256_file(raw) == d["sha256"]:
                ctx.log("source_raw_reused", where="job")
                return d, raw, "job"
        preferred = ctx.params.get("source_languages") or self.preferred
        entry = self._cache_entry(vid, preferred)
        meta, origin = None, "network"
        if entry and not refresh:
            m = _read_json(entry / "meta.json")
            if m and (entry / m["file"]).is_file() and sha256_file(entry / m["file"]) == m["sha256"]:
                meta, origin = m, "cache"
                ctx.log("source_raw_reused", where="cache")
        if meta is None:
            if entry is None:                                    # không có cache dir: dùng thư mục tạm trong job
                entry = raw_dir / "_dl"
            meta = self._fetch_to_cache(url, vid, preferred, entry, ctx)
        src = (entry / meta["file"])
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw = raw_dir / f"subtitle{src.suffix}"
        atomic_write(raw, lambda tmp: shutil.copyfile(src, tmp))     # byte-identical với phụ đề nguồn
        d = {"video_id": vid, "url": url, "lang": meta["lang"], "kind": "auto" if meta["auto"] else "manual",
             "file": raw.name, "ext": src.suffix.lstrip("."), "sha256": meta["sha256"], "origin": origin,
             "fetched_at": meta["fetched_at"], "info": meta["info"]}
        atomic_write_json(sidecar, d)
        shutil.rmtree(raw_dir / "_dl", ignore_errors=True)
        return d, raw, origin

    # -- bước 2, 3: structured và clean ---------------------------------------------------------
    def _ensure_structured(self, dl: dict, raw: Path, path: Path, ctx: StageContext) -> tuple[dict, bool]:
        cfg = self.reconstruct_cfg
        d = _read_json(path)
        prov = (d or {}).get("provenance", {})
        if d and (prov.get("raw_sha256"), prov.get("parser_version"), prov.get("config_hash")) == \
                (dl["sha256"], PARSER_VERSION, cfg.hash()):
            ctx.log("source_structured_reused")
            return d, True
        cues = parse_subtitle(raw.read_text(encoding="utf-8", errors="replace"))
        if not cues:
            raise StageError(ErrorClass.POLICY, "EMPTY_SUBTITLE", "phụ đề không có cue nào dùng được")
        d = build_structured(cues, cfg, {"video_id": dl["video_id"], "lang": dl["lang"], "kind": dl["kind"],
                                         "raw_sha256": dl["sha256"]})
        atomic_write_json(path, d)
        return d, False

    def process(self, src: SourceInput, out_dir: Path, ctx: StageContext) -> SourceResult:
        url = src["value"]
        vid = parse_video_id(url)
        out_dir.mkdir(parents=True, exist_ok=True)
        dl, raw, origin = self._ensure_raw(url, vid, out_dir / "raw", ctx)
        structured_path = out_dir / "structured.json"
        structured, reused_parse = self._ensure_structured(dl, raw, structured_path, ctx)
        transcript = out_dir / "transcript.txt"
        if not (transcript.is_file() and sha256_file(transcript) == structured["clean_sha256"]):
            atomic_write_text(transcript, clean_text(structured))
        info = dl["info"]
        lang = dl["lang"].split("-")[0]
        metadata = out_dir / "metadata.json"
        _write_json_if_changed(metadata, {
            "title": info.get("title") or vid, "language": lang,
            "source": {"url": url, "video_id": vid, "channel": info.get("channel") or info.get("uploader"),
                       "duration": info.get("duration"), "upload_date": info.get("upload_date"),
                       "subtitle": {"lang": dl["lang"], "kind": dl["kind"], "ext": dl["ext"]}}})
        stats = {**structured["stats"], "subtitle_lang": dl["lang"], "subtitle_kind": dl["kind"],
                 "raw_origin": origin, "reused_parse": reused_parse}
        return {"title": info.get("title") or vid, "language": lang, "subtitle_raw": raw, "structured": structured_path,
                "transcript": transcript, "metadata": metadata, "stats": stats}
