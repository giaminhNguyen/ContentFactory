"""Wrapper ffmpeg/ffprobe (công cụ ngoài, như yt-dlp: không phải phụ thuộc Python). Chạy bằng subprocess, hủy được, phân loại lỗi.

Các hàm `parse_*` là hàm thuần trên văn bản stderr của ffmpeg nên test được không cần ffmpeg.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..contracts import ErrorClass, StageContext, StageError

_DECODE_RX = re.compile(r"invalid data|moov atom|could not find codec|error while decoding|header missing|does not contain any stream|"
                        r"unknown format|not supported|end of file|truncated|corrupt|invalid argument", re.I)
_FILTER_LINE = re.compile(r"^\s*\[(Parsed_\w+|silencedetect|out#|vist#|aost#|AVFilterGraph|graph_)")


# ================================================================ parser thuần
def parse_astats(text: str) -> dict:
    """Khối "Overall" (lần cuối cùng xuất hiện) của astats."""
    lines = text.splitlines()
    idx = max((i for i, ln in enumerate(lines) if ln.rstrip().endswith("] Overall")), default=None)
    out: dict = {}
    if idx is None:
        return out
    keys = {"Peak level dB": "peak_db", "RMS level dB": "rms_db", "Peak count": "peak_count", "Number of samples": "samples",
            "DC offset": "dc_offset", "Flat factor": "flat_factor", "Noise floor dB": "noise_floor_db"}
    for ln in lines[idx + 1:]:
        m = re.match(r"^\[Parsed_astats\w*\s*@\s*\w+\]\s*([^:]+):\s*(\S+)\s*$", ln)
        if not m:
            if ln.strip() and not ln.startswith("[Parsed_astats"):
                break
            continue
        k = keys.get(m.group(1).strip())
        if k:
            v = m.group(2)
            out[k] = float("-inf") if v == "-inf" else float("inf") if v == "inf" else float(v)
    return out


def parse_silence(text: str, duration: float | None = None) -> list[dict]:
    """[{start, end, duration}] từ silencedetect; khoảng im lặng kéo dài đến hết file (không có silence_end) được khép tại `duration`."""
    out, cur = [], None
    for ln in text.splitlines():
        if "silencedetect" not in ln:
            continue
        if (m := re.search(r"silence_start:\s*(-?[\d.]+)", ln)):
            cur = max(0.0, float(m.group(1)))
        elif (m := re.search(r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)", ln)) and cur is not None:
            out.append({"start": cur, "end": float(m.group(1)), "duration": float(m.group(2))})
            cur = None
    if cur is not None and duration is not None and duration > cur:
        out.append({"start": cur, "end": duration, "duration": duration - cur})
    return out


def parse_ebur128(text: str) -> dict:
    """Khối Summary của ebur128: {I, LRA, TP} (LUFS / LU / dBFS)."""
    i = text.rfind("Summary:")
    if i < 0:
        return {}
    s = text[i:]
    out = {}
    for key, rx in (("I", r"I:\s*(-?[\d.]+|-inf)\s*LUFS"), ("LRA", r"LRA:\s*(-?[\d.]+)\s*LU"), ("TP", r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS")):
        if (m := re.search(rx, s)):
            out[key] = float(m.group(1))
    return out


def parse_loudnorm_json(text: str) -> dict:
    m = list(re.finditer(r"\{[^{}]*\"input_i\"[^{}]*\}", text, re.S))
    if not m:
        raise StageError(ErrorClass.TRANSIENT, "LOUDNORM_NO_OUTPUT", text[-300:])
    raw = json.loads(m[-1].group(0))
    return {k: (float(v) if re.fullmatch(r"-?[\d.]+|-?inf", str(v)) else v) for k, v in raw.items()}


def count_decode_errors(text: str) -> int:
    n = 0
    for ln in text.splitlines():
        if _FILTER_LINE.match(ln) or not ln.strip():
            continue
        if _DECODE_RX.search(ln) and not ln.lstrip().startswith(("Input #", "Output #", "Stream #", "Metadata", "Duration", "size=")):
            n += 1
    return n


# ================================================================ chạy công cụ
class Tools:
    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None, runner=None) -> None:
        self.ffmpeg_bin = ffmpeg or "ffmpeg"
        self.ffprobe_bin = ffprobe or "ffprobe"
        self._filters: set[str] | None = None
        self._version: str | None = None
        self._soxr: bool | None = None
        self._runner = runner or self._popen

    # -- sẵn sàng -----------------------------------------------------------------------------
    def resolved(self, name: str) -> str | None:
        return shutil.which(name) or (name if Path(name).exists() else None)

    def available(self) -> bool:
        return bool(self.resolved(self.ffmpeg_bin) and self.resolved(self.ffprobe_bin))

    def version(self) -> str:
        if self._version is None:
            try:
                out = subprocess.run([self.ffmpeg_bin, "-version"], capture_output=True, text=True, timeout=30).stdout
                self._version = (out.splitlines() or ["?"])[0].strip()
            except (OSError, subprocess.SubprocessError):
                self._version = "unavailable"
        return self._version

    def has_filter(self, name: str) -> bool:
        if self._filters is None:
            try:
                out = subprocess.run([self.ffmpeg_bin, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30).stdout
            except (OSError, subprocess.SubprocessError):
                out = ""
            self._filters = {ln.split()[1] for ln in out.splitlines() if len(ln.split()) > 2 and ln.startswith(" ")}
        return name in self._filters

    def has_soxr(self) -> bool:
        if self._soxr is None:
            try:
                r = subprocess.run([self.ffmpeg_bin, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=8000", "-t", "0.01",
                                    "-af", "aresample=resampler=soxr:osr=16000", "-f", "null", "-"], capture_output=True, timeout=30)
                self._soxr = r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                self._soxr = False
        return self._soxr

    # -- chạy -------------------------------------------------------------------------------------
    @staticmethod
    def _popen(argv, timeout, cancel):
        with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe)
            t0 = time.time()
            while True:
                try:
                    p.wait(0.2)
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set():
                        p.kill()
                        p.wait()
                        cancel.check()
                    if time.time() - t0 > timeout:
                        p.kill()
                        p.wait()
                        raise StageError(ErrorClass.TRANSIENT, "FFMPEG_TIMEOUT", f">{timeout:.0f}s: {Path(argv[0]).name}") from None
            fo.seek(0)
            fe.seek(0)
            return p.returncode, fo.read().decode("utf-8", "replace"), fe.read().decode("utf-8", "replace")

    def run(self, argv: list[str], ctx: StageContext | None = None, timeout: float = 3600, check: bool = True) -> tuple[int, str, str]:
        if not self.available():
            raise StageError(ErrorClass.RESOURCE, "FFMPEG_MISSING", f"không chạy được {self.ffmpeg_bin!r}/{self.ffprobe_bin!r}; cài ffmpeg "
                             f"hoặc đặt tools.ffmpeg/tools.ffprobe trong config", resource="runtime")
        try:
            rc, out, err = self._runner(argv, timeout, ctx.cancel if ctx else None)
        except FileNotFoundError:
            raise StageError(ErrorClass.RESOURCE, "FFMPEG_MISSING", argv[0], resource="runtime") from None
        if check and rc != 0:
            raise classify(rc, err, argv)
        return rc, out, err

    def ffmpeg(self, args: list[str], ctx: StageContext | None = None, **kw) -> tuple[int, str, str]:
        return self.run([self.ffmpeg_bin, "-hide_banner", "-nostdin", "-nostats", *args], ctx, **kw)

    def probe(self, path: Path, ctx: StageContext | None = None) -> dict:
        rc, out, err = self.run([self.ffprobe_bin, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                                ctx, timeout=120, check=False)
        if rc != 0:
            raise StageError(ErrorClass.POLICY, "AUDIO_UNREADABLE", f"{path.name}: {err.strip()[-300:]}", {"path": str(path)})
        d = json.loads(out or "{}")
        st = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
        if not st:
            raise StageError(ErrorClass.POLICY, "AUDIO_UNREADABLE", f"{path.name}: không có luồng audio", {"path": str(path)})
        dur = st.get("duration") or d.get("format", {}).get("duration")
        return {"codec": st.get("codec_name"), "sample_rate": int(st.get("sample_rate") or 0), "channels": int(st.get("channels") or 0),
                "bits": int(st.get("bits_per_sample") or st.get("bits_per_raw_sample") or 0), "format": d.get("format", {}).get("format_name"),
                "duration": float(dur) if dur not in (None, "N/A") else 0.0}

    # -- đo -------------------------------------------------------------------------------------
    def measure(self, path: Path, silence_db: float, silence_min_s: float, ctx: StageContext | None = None) -> dict:
        """Một lượt decode: astats + silencedetect + ebur128 (+ đếm lỗi decode). Không sửa file."""
        pr = self.probe(path, ctx)
        af = f"astats=measure_perchannel=none,silencedetect=n={silence_db}dB:d={silence_min_s},ebur128=peak=true:framelog=quiet"
        rc, _, err = self.ffmpeg(["-v", "info", "-i", str(path), "-af", af, "-f", "null", "-"], ctx, check=False)
        return {"probe": pr, "stats": parse_astats(err), "silences": parse_silence(err, pr["duration"]), "loudness": parse_ebur128(err),
                "decode_errors": count_decode_errors(err) + (1 if rc != 0 else 0)}

    def loudnorm_measure(self, path: Path, pre: str, target: dict, ctx: StageContext | None = None) -> dict:
        chain = (pre + "," if pre else "") + (f"loudnorm=I={target['target_lufs']}:TP={target['true_peak_db']}:LRA={target['lra']}:"
                                              f"print_format=json")
        _, _, err = self.ffmpeg(["-v", "info", "-i", str(path), "-af", chain, "-f", "null", "-"], ctx)
        return parse_loudnorm_json(err)


def classify(rc: int, err: str, argv: list[str]) -> StageError:
    tail = err.strip()[-500:]
    if re.search(r"no space left|disk full|not enough space", err, re.I):
        return StageError(ErrorClass.RESOURCE, "DISK_FULL", tail, resource="disk")
    if re.search(r"no such file or directory|does not exist", err, re.I):
        return StageError(ErrorClass.POLICY, "AUDIO_INPUT_MISSING", tail, resource="input")
    if _DECODE_RX.search(err):
        return StageError(ErrorClass.POLICY, "AUDIO_DECODE_FAILED", tail, {"rc": rc})
    if re.search(r"no such filter|unrecognized option|error (initializing|parsing) filter|invalid (filter|argument)", err, re.I):
        return StageError(ErrorClass.POLICY, "FFMPEG_BAD_FILTER", tail, {"rc": rc})
    return StageError(ErrorClass.TRANSIENT, "FFMPEG_FAILED", f"rc={rc}: {tail}", {"rc": rc})


def finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def atomic_out(path: Path) -> Path:
    """Đường dẫn tạm cho ffmpeg ghi (cùng thư mục), sau đó os.replace."""
    return path.with_name(path.name + ".part")


def commit(tmp: Path, final: Path) -> None:
    os.replace(tmp, final)
