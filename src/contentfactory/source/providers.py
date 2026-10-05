"""Các SourceProvider: mỗi provider biết CÁCH lấy phụ đề, không biết pipeline, cache hay state của job.

- SubtitleSupperVipProvider: provider chính. Gọi lại code acquisition của Subtitle_supperVip qua bridge subprocess
  (xem bridge/supervip_bridge.py). Không chạy API/worker/DB của module đó.
- YtDlpProvider: downloader của Phase 2 (yt-dlp), giữ làm fallback và nguồn metadata bổ sung (mô tả, kênh).
- LocalSubtitleProvider: file phụ đề trên đĩa (srt/vtt/json/txt).
- PlainTextProvider: văn bản thuần, không timestamp.
Provider raise StageError khi thất bại; ProviderChain quyết định fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..contracts import ErrorClass, SourceInput, SourceResult, StageContext, StageError
from ..fsutil import atomic_write_text, sha256_file
from .subtitles import FORMATS, format_from_suffix
from .youtube import YtDlp, choose_subtitle, parse_video_id

BRIDGE = Path(__file__).resolve().parent / "bridge" / "supervip_bridge.py"
INFO_KEYS = ("id", "title", "channel", "uploader", "duration", "upload_date", "language", "webpage_url")


def _base(src: SourceInput, provider: str, source_type: str, **kw) -> SourceResult:
    return {"source_url": src["value"] if source_type != "plain_text" else "text:" + hashlib.sha256(
        src["value"].encode()).hexdigest()[:12], "source_type": source_type, "provider": provider, "status": "ok",
            "error": None, "metadata": {}, "description": None, **kw}


# ---------------------------------------------------------------------------------------------------
class SubtitleSupperVipProvider:
    name = "supervip"

    def __init__(self, cfg: dict | None = None, root: Path | None = None, runner=None) -> None:
        cfg = cfg or {}
        root = Path(root or ".")
        backend = Path(cfg.get("backend_dir") or root / "modules" / "Subtitle_supperVip" / "backend")
        self.backend = backend if backend.is_absolute() else root / backend
        self.python = cfg.get("python") or self._venv_python() or sys.executable
        self.key_env = cfg.get("youtube_api_key_env", "YOUTUBE_API_KEY")
        self.env = dict(cfg.get("env", {}))
        self.timeout = float(cfg.get("timeout_s", 120))
        self.runner = runner or self._run_bridge        # tiêm được để test không cần mạng/môi trường thật

    def _venv_python(self) -> str | None:
        for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
            if (self.backend / rel).is_file():
                return str(self.backend / rel)
        return None

    def supports(self, kind: str) -> bool:
        return kind == "youtube_url"

    def available(self) -> bool:
        return (self.backend / "app" / "services" / "subtitles.py").is_file() and BRIDGE.is_file() \
            and (Path(self.python).is_file() or shutil.which(self.python) is not None)

    def health(self) -> dict:
        if not self.available():
            return {"ok": False, "provider": self.name, "fix_hint": f"cần {self.backend} và một Python có youtube-transcript-api "
                    "(cấu hình supervip.python hoặc tạo backend/.venv bằng `pip install -r requirements.txt`)"}
        try:
            r = self.runner(["health", "--backend-dir", str(self.backend)])
        except StageError as e:
            return {"ok": False, "provider": self.name, "error": e.to_dict()}
        return {"ok": bool(r.get("ok")), "provider": self.name, **{k: v for k, v in r.items() if k != "ok"}}

    def _run_bridge(self, args: list[str]) -> dict:
        # PYTHONDONTWRITEBYTECODE: chạy code của module mà không ghi __pycache__ vào cây thư mục của nó
        env = {**os.environ, **self.env, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            p = subprocess.run([self.python, str(BRIDGE), *args], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=self.timeout, env=env)
        except FileNotFoundError:
            raise StageError(ErrorClass.RESOURCE, "SUPERVIP_UNAVAILABLE", f"không chạy được {self.python!r}") from None
        except subprocess.TimeoutExpired:
            raise StageError(ErrorClass.TRANSIENT, "SUPERVIP_TIMEOUT", f">{self.timeout}s") from None
        lines = [l for l in p.stdout.splitlines() if l.strip()]
        try:
            return json.loads(lines[-1])
        except (IndexError, ValueError):
            raise StageError(ErrorClass.TRANSIENT, "SUPERVIP_BRIDGE_FAILED",
                             f"exit={p.returncode} {p.stderr.strip()[-300:] or p.stdout[-300:]}") from None

    @staticmethod
    def map_error(err: dict) -> StageError:
        kind, msg = err.get("kind"), err.get("message", "")
        if kind == "SubtitleUnavailable":
            return StageError(ErrorClass.POLICY, "NO_SUBTITLES", msg)
        if kind == "LanguageUnavailable":
            return StageError(ErrorClass.POLICY, "LANGUAGE_UNAVAILABLE", msg)
        if kind == "BlockedByYouTube":                 # IP bị chặn/rate limit: chờ hoặc đổi mạng, không phải lỗi dữ liệu
            return StageError(ErrorClass.RESOURCE, "YOUTUBE_BLOCKED", msg)
        if kind == "BridgeError" and err.get("type") in ("ModuleNotFoundError", "ImportError"):
            return StageError(ErrorClass.RESOURCE, "SUPERVIP_DEPENDENCY_MISSING", msg)
        return StageError(ErrorClass.TRANSIENT, "SUPERVIP_ERROR", f"{err.get('type', kind)}: {msg}")

    def acquire(self, src: SourceInput, work_dir: Path, ctx: StageContext, prefs: dict) -> SourceResult:
        vid = parse_video_id(src["value"])
        langs, allow = list(prefs["languages"]), bool(prefs.get("allow_translation", False))
        # Có sẵn (thủ công) trước, auto sau, cuối cùng là thủ công ở ngôn ngữ bất kỳ: giữ đúng tinh thần "ưu tiên sub có sẵn".
        passes = [{"preference": "manual", "languages": langs, "allow_translation": allow},
                  {"preference": "auto", "languages": langs + ["original"], "allow_translation": allow},
                  {"preference": "manual", "languages": ["original"], "allow_translation": False}]
        work_dir.mkdir(parents=True, exist_ok=True)
        out = work_dir / "subtitle_raw.json"
        r = self.runner(["fetch", "--backend-dir", str(self.backend), "--video-id", vid, "--passes", json.dumps(passes),
                         "--out", str(out), "--with-metadata"])
        if not r.get("ok"):
            raise self.map_error(r.get("error") or {"kind": "Other", "message": "bridge không trả lỗi cụ thể"})
        track, meta = r["track"], r.get("metadata") or {}
        kind = "translated" if track.get("translated") else ("auto" if track.get("is_generated") else "manual")
        return _base(src, self.name, "youtube", video_id=vid, title=meta.get("title"), language=track.get("language_code"),
                     raw_subtitle_path=out, subtitle_format="json", subtitle_kind=kind, has_timestamps=True,
                     metadata={**meta, "track": track, "pass": r.get("pass"), "snippets": r.get("snippets"),
                               "metadata_error": r.get("metadata_error")})

    def describe(self, src: SourceInput, ctx: StageContext) -> dict:
        return {}                                      # metadata đi kèm lần fetch (cần YOUTUBE_API_KEY)


# ---------------------------------------------------------------------------------------------------
class YtDlpProvider:
    name = "ytdlp"

    def __init__(self, cfg: dict | None = None, ytdlp: YtDlp | None = None) -> None:
        cfg = cfg or {}
        self.ytdlp = ytdlp or YtDlp(cfg.get("yt_dlp_cmd", ["yt-dlp"]), extra_args=cfg.get("yt_dlp_args", []))

    def supports(self, kind: str) -> bool:
        return kind == "youtube_url"

    def available(self) -> bool:
        return self.ytdlp.available()

    def health(self) -> dict:
        ok = self.available()
        return {"ok": ok, "provider": self.name, "fix_hint": None if ok else "pip install yt-dlp"}

    def acquire(self, src: SourceInput, work_dir: Path, ctx: StageContext, prefs: dict) -> SourceResult:
        vid = parse_video_id(src["value"])
        info = self.ytdlp.info(src["value"])
        chosen = choose_subtitle(info, list(prefs["languages"]))
        if not chosen:
            raise StageError(ErrorClass.POLICY, "NO_SUBTITLES", "video không có phụ đề (kể cả auto-caption)",
                             {"manual": sorted((info.get("subtitles") or {})),
                              "auto": sorted((info.get("automatic_captions") or {}))[:20]})
        got = self.ytdlp.download_subtitle(src["value"], chosen["lang"], chosen["auto"], work_dir)
        fmt = format_from_suffix(got.suffix) or "vtt"
        return _base(src, self.name, "youtube", video_id=vid, title=info.get("title"), description=info.get("description"),
                     language=chosen["lang"].split("-")[0], raw_subtitle_path=got, subtitle_format=fmt,
                     subtitle_kind="auto" if chosen["auto"] else "manual", has_timestamps=True,
                     metadata={k: info.get(k) for k in INFO_KEYS} | {"track": chosen})

    def describe(self, src: SourceInput, ctx: StageContext) -> dict:
        info = self.ytdlp.info(src["value"])
        return {"title": info.get("title"), "description": info.get("description"),
                **{k: info.get(k) for k in INFO_KEYS if k not in ("title",)}}


# ---------------------------------------------------------------------------------------------------
class LocalSubtitleProvider:
    name = "local"

    def supports(self, kind: str) -> bool:
        return kind == "transcript_file"

    def available(self) -> bool:
        return True

    def health(self) -> dict:
        return {"ok": True, "provider": self.name}

    def describe(self, src: SourceInput, ctx: StageContext) -> dict:
        return {}

    def acquire(self, src: SourceInput, work_dir: Path, ctx: StageContext, prefs: dict) -> SourceResult:
        path = Path(src["value"])
        if not path.is_file():
            raise StageError(ErrorClass.POLICY, "FILE_NOT_FOUND", str(path))
        fmt = format_from_suffix(path.suffix)
        if fmt not in FORMATS:
            raise StageError(ErrorClass.POLICY, "UNSUPPORTED_FORMAT", f"{path.suffix!r}; hỗ trợ {FORMATS}")
        work_dir.mkdir(parents=True, exist_ok=True)
        out = work_dir / f"subtitle_raw.{fmt}"
        shutil.copyfile(path, out)                    # byte-identical
        return _base(src, self.name, "local_subtitle", video_id=None, title=src.get("title") or path.stem,
                     language=src.get("language"), raw_subtitle_path=out, subtitle_format=fmt, subtitle_kind="unknown",
                     has_timestamps=fmt != "txt", metadata={"filename": path.name})


class PlainTextProvider:
    name = "text"

    def supports(self, kind: str) -> bool:
        return kind == "text"

    def available(self) -> bool:
        return True

    def health(self) -> dict:
        return {"ok": True, "provider": self.name}

    def describe(self, src: SourceInput, ctx: StageContext) -> dict:
        return {}

    def acquire(self, src: SourceInput, work_dir: Path, ctx: StageContext, prefs: dict) -> SourceResult:
        text = src["value"]
        if not text.strip():
            raise StageError(ErrorClass.POLICY, "EMPTY_SUBTITLE", "văn bản nguồn rỗng")
        work_dir.mkdir(parents=True, exist_ok=True)
        out = work_dir / "subtitle_raw.txt"
        atomic_write_text(out, text)
        first = next((l.strip() for l in text.splitlines() if l.strip()), "")
        return _base(src, self.name, "plain_text", video_id=None, title=src.get("title") or first[:80] or "untitled",
                     language=src.get("language"), raw_subtitle_path=out, subtitle_format="txt", subtitle_kind="unknown",
                     has_timestamps=False, metadata={"chars": len(text)})
