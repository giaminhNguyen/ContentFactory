"""Phần giao tiếp với YouTube: nhận diện URL, chọn track phụ đề, gọi yt-dlp (công cụ ngoài, chạy bằng subprocess).

yt-dlp không phải phụ thuộc Python của dự án: lệnh gọi cấu hình được (mặc định `yt-dlp` trên PATH).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..contracts import ErrorClass, StageError

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "music.youtube.com", "youtube-nocookie.com", "youtu.be"}


def parse_video_id(url: str) -> str:
    u = urlparse(url.strip())
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host not in _HOSTS:
        raise StageError(ErrorClass.POLICY, "NOT_YOUTUBE_URL", url)
    segs = [s for s in u.path.split("/") if s]
    if host == "youtu.be":
        vid = segs[0] if segs else ""
    elif u.path == "/watch":
        vid = parse_qs(u.query).get("v", [""])[0]
    elif len(segs) >= 2 and segs[0] in ("shorts", "embed", "live", "v"):
        vid = segs[1]
    else:
        vid = ""
    if not _ID.match(vid):
        raise StageError(ErrorClass.POLICY, "BAD_VIDEO_ID", url)
    return vid


def _match(keys: list[str], lang: str) -> str | None:
    """'vi' khớp 'vi' hoặc 'vi-VN', 'vi-orig'..."""
    if lang in keys:
        return lang
    return next((k for k in keys if k.split("-")[0] == lang.split("-")[0] and not k.endswith("-orig")), None)


def choose_subtitle(info: dict, preferred: list[str]) -> dict | None:
    """Ưu tiên phụ đề có sẵn do người đăng; auto-caption chỉ khi không có.

    Thứ tự: thủ công (ngôn ngữ ưu tiên) > thủ công (ngôn ngữ gốc của video) > auto ngôn ngữ gốc
    > auto (ngôn ngữ ưu tiên) > thủ công bất kỳ > không có.
    """
    manual = sorted((info.get("subtitles") or {}).keys())
    auto = sorted((info.get("automatic_captions") or {}).keys())
    orig = info.get("language") or ""
    for lang in preferred:
        if (k := _match(manual, lang)):
            return {"lang": k, "auto": False}
    if orig and (k := _match(manual, orig)):
        return {"lang": k, "auto": False}
    for k in auto:                                           # track gốc do YouTube đánh dấu "-orig"
        if k.endswith("-orig"):
            return {"lang": k, "auto": True}
    if orig and (k := _match(auto, orig)):
        return {"lang": k, "auto": True}
    for lang in preferred:
        if (k := _match(auto, lang)):
            return {"lang": k, "auto": True}
    if manual:
        return {"lang": manual[0], "auto": False}
    return None


def classify_error(stderr: str) -> StageError:
    s = stderr.lower()
    tail = stderr.strip()[-400:]
    if re.search(r"private video|video unavailable|has been removed|not available|terminated|copyright|members-only|age-restricted", s):
        return StageError(ErrorClass.POLICY, "VIDEO_UNAVAILABLE", tail)
    if re.search(r"sign in to confirm|not a bot|login required|cookies", s):
        return StageError(ErrorClass.AUTH, "YOUTUBE_SIGNIN_REQUIRED", tail)
    if re.search(r"429|too many requests|rate.?limit", s):          # bị giới hạn tốc độ: tài nguyên TẠM THỜI (quota)
        return StageError(ErrorClass.TRANSIENT, "YTDLP_FAILED", tail, resource="quota")
    if re.search(r"timed out|connection|network|name or service|temporary failure|unable to download|getaddrinfo", s):
        return StageError(ErrorClass.TRANSIENT, "YTDLP_FAILED", tail, resource="network")
    return StageError(ErrorClass.TRANSIENT, "YTDLP_FAILED", tail)


class YtDlp:
    def __init__(self, cmd: tuple[str, ...] | list[str] = ("yt-dlp",), timeout: float = 180,
                 extra_args: tuple[str, ...] | list[str] = ()) -> None:
        self.cmd, self.timeout, self.extra = list(cmd), timeout, list(extra_args)

    def available(self) -> bool:
        return shutil.which(self.cmd[0]) is not None or Path(self.cmd[0]).exists()

    def _run(self, args: list[str]) -> str:
        try:
            p = subprocess.run([*self.cmd, *self.extra, *args], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=self.timeout)
        except FileNotFoundError:
            raise StageError(ErrorClass.RESOURCE, "YTDLP_MISSING",
                             f"không chạy được {self.cmd!r}; cài bằng `pip install yt-dlp` hoặc đặt youtube.yt_dlp_cmd",
                             resource="runtime") from None
        except subprocess.TimeoutExpired:
            raise StageError(ErrorClass.TRANSIENT, "YTDLP_TIMEOUT", f">{self.timeout}s", resource="network") from None
        if p.returncode != 0:
            raise classify_error(p.stderr)
        return p.stdout

    def info(self, url: str) -> dict:
        out = self._run(["--skip-download", "--dump-single-json", "--no-warnings", "--no-playlist", url])
        try:
            return json.loads(out)
        except ValueError:
            raise StageError(ErrorClass.TRANSIENT, "YTDLP_BAD_JSON", out[:200]) from None

    def download_subtitle(self, url: str, lang: str, auto: bool, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        self._run(["--skip-download", "--write-auto-subs" if auto else "--write-subs", "--sub-langs", re.escape(lang),
                   "--sub-format", "vtt/srt/best", "--no-warnings", "--no-playlist",
                   "-o", str(out_dir / "%(id)s.%(ext)s"), url])
        found = sorted(p for p in out_dir.glob("*") if p.suffix in (".vtt", ".srt") and f".{lang}." in p.name)
        if not found:
            raise StageError(ErrorClass.TRANSIENT, "SUBTITLE_NOT_WRITTEN", f"yt-dlp không ghi file phụ đề '{lang}'")
        return found[0]
