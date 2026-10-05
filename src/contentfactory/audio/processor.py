"""FfmpegAudio: AudioProcessor thật (ffmpeg/ffprobe + stdlib WAV). Độc lập hoàn toàn với engine TTS.

  join   : chunk TTS --[chuẩn hóa kỹ thuật + dọn biên + fade nhỏ]--> chunk chuẩn --[Pause Engine]--> narration thô (lossless) + timeline
  master : narration thô --[highpass? -> compressor? -> loudnorm 2-pass(linear) -> limiter]--> Narration Master (lossless)
  youtube: watermark (chuẩn hóa, khớp độ to, fade) + gap + Narration Master  (ghép lossless, KHÔNG master lại)
  tiktok : Narration Master --[rubberband x speed, giữ cao độ]--> cắt thông minh ở ranh giới câu/đoạn --> các part WAV

Mọi tham số lấy từ profile (audio/profile.py). Nội bộ luôn là WAV PCM (mặc định 48 kHz mono 24-bit): chỉ MỘT lần mã hóa lossy ở bước
render video. Kết quả trung gian được cache trong thư mục làm việc của stage theo khóa nội dung, nên resume không làm lại việc đã xong và
đổi watermark chỉ làm lại nhánh YouTube.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..contracts import ErrorClass, StageContext, StageError
from ..fsutil import atomic_write_json, sha256_file
from . import ffmpeg as F
from . import wavio
from .pause import plan_pauses
from .profile import resolve
from .qa import evaluate
from .split import plan_split


def _h(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _stat_key(p: Path) -> str:
    st = Path(p).stat()
    return f"{Path(p).name}:{st.st_size}:{st.st_mtime_ns}"


def db_to_lin(db: float) -> float:
    return 10 ** (db / 20)


class FfmpegAudio:
    def __init__(self, tools: dict | None = None, tools_obj: F.Tools | None = None) -> None:
        t = tools or {}
        self.tools = tools_obj or F.Tools(t.get("ffmpeg"), t.get("ffprobe"))

    # ------------------------------------------------------------------ sức khỏe / QA nhanh
    def health(self) -> dict:
        ok = self.tools.available()
        return {"ok": ok, "version": self.tools.version() if ok else None,
                "rubberband": ok and self.tools.has_filter("rubberband"), "loudnorm": ok and self.tools.has_filter("loudnorm"),
                "soxr": ok and self.tools.has_soxr()}

    def qa(self, audio: Path) -> dict:
        """QA nhanh (đọc được, dài > 0). QA đầy đủ (hỏng, clipping, im lặng…) là `qa_full`."""
        try:
            pr = self.tools.probe(Path(audio))
        except StageError as e:
            if e.error_class == ErrorClass.RESOURCE:
                raise
            return {"ok": False, "duration_sec": 0.0, "issues": ["UNREADABLE"]}
        return {"ok": pr["duration"] > 0, "duration_sec": round(pr["duration"], 3), "issues": [] if pr["duration"] > 0 else ["ZERO_DURATION"]}

    def qa_full(self, audio: Path, expect: dict, ctx: StageContext) -> dict:
        prof = resolve(ctx.params)
        cfg = prof["qa"]
        try:
            m = self.tools.measure(Path(audio), cfg["silence"]["threshold_db"], cfg["silence"]["min_s"], ctx)
        except StageError as e:
            if e.error_class == ErrorClass.RESOURCE or e.code != "AUDIO_UNREADABLE":
                raise
            return {"ok": False, "errors": [{"code": "CORRUPT", "message": e.message}], "warnings": [], "kind": expect.get("kind"),
                    "measures": {}}
        fmt = prof["format"]
        expect = {"sample_rate": fmt["sample_rate"], "channels": fmt["channels"], "codec_prefix": "pcm", **expect} \
            if expect.get("kind") != "input" else expect
        return evaluate(m, expect, cfg)

    # ------------------------------------------------------------------ cache bước trung gian
    @staticmethod
    def _cached(out: Path, key: str, fn) -> tuple[dict, bool]:
        """Chạy fn(tmp)->info nếu `out` chưa có hoặc sai khóa; trả (info, reused). `out` chỉ tồn tại khi hoàn chỉnh (ghi tạm rồi rename)."""
        side = out.with_name(out.name + ".key.json")
        try:
            d = json.loads(side.read_text(encoding="utf-8"))
            if d["key"] == key and out.is_file():
                return d["info"], True
        except (OSError, ValueError, KeyError):
            pass
        for f in (side, out):
            if f.exists():
                f.unlink()
        tmp = F.atomic_out(out)
        try:
            info = fn(tmp) or {}
            F.commit(tmp, out)
        finally:
            if tmp.exists():
                tmp.unlink()
        atomic_write_json(side, {"key": key, "info": info})
        return info, False

    def _out_args(self, prof: dict) -> list[str]:
        f = prof["format"]
        return ["-ac", str(f["channels"]), "-c:a", f"pcm_{f['sample_fmt']}", "-f", "wav"]

    def _resample(self, prof: dict) -> str:
        sr = prof["format"]["sample_rate"]
        return f"aresample={sr}:resampler=soxr:precision=28" if self.tools.has_soxr() else f"aresample={sr}"

    @staticmethod
    def _limiter(cfg: dict) -> str:
        return (f"alimiter=limit={db_to_lin(cfg['ceiling_db']):.6f}:attack={cfg['attack_ms']}:release={cfg['release_ms']}:level=0"
                if cfg.get("enabled") else "")

    @staticmethod
    def edge_chain(edge: dict, keep_ms: float | None = None) -> str:
        """Dọn biên một chunk: (1) cắt im lặng đầu/cuối đúng tới mép tín hiệu, (2) fade vào/ra vài ms NGAY TẠI mép tín hiệu (chống click/pop
        khi chunk bị cắt cứng), (3) thêm lại `keep_ms` im lặng chuẩn ở hai đầu để mọi chunk có biên giống nhau (Pause Engine dựa vào đó).
        Đối xứng nhờ areverse nên không cần biết độ dài chunk."""
        keep = int(edge["keep_ms"] if keep_ms is None else keep_ms)
        trim = f"silenceremove=start_periods=1:start_threshold={edge['threshold_db']}dB:start_silence=0"
        fade = f"afade=t=in:d={edge['fade_ms'] / 1000}"
        hp = f"highpass=f={edge['highpass_hz']}," if edge.get("highpass_hz") else ""
        pad = f"adelay=delays={keep}:all=1,apad=pad_dur={keep / 1000}" if keep > 0 else ""
        return f"{hp}{trim},{fade},areverse,{trim},{fade},areverse" + (f",{pad}" if pad else "")

    # ------------------------------------------------------------------ JOIN: chunk -> narration thô
    def assemble(self, chunks: list[Path], pauses_ms: list[int], out: Path, ctx: StageContext) -> dict:
        prof = resolve(ctx.params)
        fmt, jn = prof["format"], prof["join"]
        chunks = [Path(c) for c in chunks]
        if len(chunks) != len(pauses_ms) or not chunks:
            raise StageError(ErrorClass.POLICY, "MISSING_CHUNKS", f"{len(chunks)} chunk nhưng {len(pauses_ms)} pause")
        missing = [i for i, c in enumerate(chunks, 1) if not c.is_file()]
        if missing:
            raise StageError(ErrorClass.POLICY, "MISSING_CHUNKS", f"thiếu file chunk: {missing[:20]}", {"missing": missing})
        work = ctx.stage_dir / "join"
        work.mkdir(parents=True, exist_ok=True)
        ver = self.tools.version()
        chain = self.edge_chain(jn["edge"]) + "," + self._resample(prof)

        def prep(i: int) -> Path:
            dst = work / f"{i:06d}.wav"
            key = _h("prep1", sha256_file(chunks[i - 1]), jn["edge"], fmt, ver)

            def build(tmp: Path) -> dict:
                self.tools.ffmpeg(["-v", "error", "-i", str(chunks[i - 1]), "-af", chain, *self._out_args(prof), "-y", str(tmp)], ctx)
                return {}
            self._cached(dst, key, build)
            if wavio.info(dst)["duration"] <= 2 * jn["edge"]["keep_ms"] / 1000 + 0.005:       # chỉ còn phần biên chèn thêm => không có tiếng
                raise StageError(ErrorClass.POLICY, "CHUNK_EMPTY", f"chunk {i} rỗng sau khi dọn biên (toàn im lặng?)", {"chunk": i})
            return dst

        with ThreadPoolExecutor(max_workers=int(jn.get("workers", 4))) as ex:
            prepped = list(ex.map(prep, range(1, len(chunks) + 1)))
        ctx.cancel.check()
        keep = jn["edge"]["keep_ms"]
        plan = plan_pauses(pauses_ms, jn["pause"], keep)
        rate, ch, width = fmt["sample_rate"], fmt["channels"], {"s16le": 2, "s24le": 3, "s32le": 4}[fmt["sample_fmt"]]
        order: list[Path] = []
        slots: list[int] = []
        for i, (p, pl) in enumerate(zip(prepped, plan)):
            slots.append(len(order))
            order.append(p)
            if pl["inserted"] > 0:
                s = work / f"silence_{int(round(pl['inserted']))}ms.wav"
                if not s.is_file():
                    wavio.write_silence(s, round(pl["inserted"]), rate, ch, width)
                order.append(s)
        res = wavio.concat(order, out)
        durs = [wavio.info(p)["duration"] for p in prepped]
        expected_frames = sum(wavio.info(p)["frames"] for p in order)
        if res["frames"] != expected_frames:
            raise StageError(ErrorClass.TRANSIENT, "JOIN_MISMATCH", f"{res['frames']} frame, kỳ vọng {expected_frames}")
        timeline = []
        for i, d in enumerate(durs):
            start = res["offsets"][slots[i]]
            nxt = order[slots[i] + 1] if slots[i] + 1 < len(order) else None
            gap = wavio.info(nxt)["duration"] if nxt is not None and nxt.name.startswith("silence_") else 0.0
            timeline.append({"index": i + 1, "start_sec": round(start, 4), "end_sec": round(start + d, 4), "gap_after_sec": round(max(gap, 0.0), 4),
                             "cut_sec": round(start + d + max(gap, 0.0) / 2, 4)})
        return {"duration_sec": round(res["duration"], 3), "timeline": timeline, "chunks": len(chunks), "pauses": plan,
                "format": {"sample_rate": rate, "channels": ch, "sample_fmt": fmt["sample_fmt"]}}

    # ------------------------------------------------------------------ MASTER: narration thô -> Narration Master
    def master(self, src: Path, out: Path, ctx: StageContext) -> dict:
        prof = resolve(ctx.params)
        ms, fmt = prof["master"], prof["format"]
        src, out = Path(src), Path(out)
        key = _h("master1", _stat_key(src), ms, fmt, self.tools.version())

        def build(tmp: Path) -> dict:
            pre = []
            if ms["highpass_hz"]:
                pre.append(f"highpass=f={ms['highpass_hz']}")
            c = ms["compressor"]
            if c["enabled"]:
                pre.append(f"acompressor=threshold={db_to_lin(c['threshold_db']):.6f}:ratio={c['ratio']}:attack={c['attack_ms']}:"
                           f"release={c['release_ms']}:makeup={db_to_lin(c['makeup_db']):.4f}")
            pre_s = ",".join(pre)
            info: dict = {"compressor": bool(c["enabled"]), "highpass_hz": ms["highpass_hz"]}
            chain = list(pre)
            lo = ms["loudness"]
            if lo["enabled"]:
                m = self.tools.loudnorm_measure(src, pre_s, lo, ctx)
                if not F.finite(m.get("input_i")):
                    raise StageError(ErrorClass.POLICY, "AUDIO_SILENT", "không đo được độ to: audio im lặng/rỗng")
                chain.append(f"loudnorm=I={lo['target_lufs']}:TP={lo['true_peak_db']}:LRA={lo['lra']}:measured_I={m['input_i']}:"
                             f"measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
                             f"offset={m['target_offset']}:linear=true:print_format=summary")
                info["measured"] = {k: m[k] for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
                info["target"] = {k: lo[k] for k in ("target_lufs", "true_peak_db", "lra")}
            chain.append(self._resample(prof))
            if (lim := self._limiter(ms["limiter"])):
                chain.append(lim)
            _, _, err = self.tools.ffmpeg(["-v", "info", "-i", str(src), "-af", ",".join(chain), *self._out_args(prof), "-y", str(tmp)], ctx)
            if lo["enabled"]:
                mt = re.search(r"Normalization Type:\s*(\w+)", err)
                info["normalization"] = mt.group(1).lower() if mt else "unknown"
            return info

        info, reused = self._cached(out, key, build)
        return {**info, "reused": reused, "duration_sec": round(wavio.info(out)["duration"], 3), "path": str(out)}

    # ------------------------------------------------------------------ YOUTUBE
    def build_youtube_audio(self, master: Path, watermark: Path | None, out: Path, ctx: StageContext) -> dict:
        prof = resolve(ctx.params)
        wmc, fmt, ms = prof["youtube"]["watermark"], prof["format"], prof["master"]
        master, out = Path(master), Path(out)
        rate, ch = fmt["sample_rate"], fmt["channels"]
        width = {"s16le": 2, "s24le": 3, "s32le": 4}[fmt["sample_fmt"]]
        work = out.parent / "work"
        work.mkdir(parents=True, exist_ok=True)
        if watermark is None:
            key = _h("yt-plain", _stat_key(master))

            def plain(tmp: Path) -> dict:
                try:
                    os.link(master, tmp)
                except OSError:
                    shutil.copyfile(master, tmp)
                return {}
            self._cached(out, key, plain)
            d = wavio.info(out)["duration"]
            return {"duration_sec": round(d, 3), "watermark": False, "story_start_sec": 0.0}
        watermark = Path(watermark)
        wsha = sha256_file(watermark)
        lo = ms["loudness"]
        target = lo["target_lufs"] + wmc["offset_db"]
        prepped = work / f"watermark_{wsha[:12]}.wav"
        pkey = _h("wm-prep1", wsha, wmc, target, ms["limiter"], fmt, ctx.params.get("audio", {}).get("join", {}).get("edge"), self.tools.version())

        def prep(tmp: Path) -> dict:
            m = self.tools.loudnorm_measure(watermark, "", {"target_lufs": target, "true_peak_db": -1.5, "lra": 11}, ctx)
            if not F.finite(m.get("input_i")):
                raise StageError(ErrorClass.POLICY, "WATERMARK_SILENT", f"watermark im lặng/rỗng: {watermark.name}")
            gain = target - m["input_i"]
            chain = []
            if wmc.get("trim", True):
                edge = {**prof["join"]["edge"], "fade_ms": wmc["fade_ms"]}
                chain.append(self.edge_chain(edge, keep_ms=20))
            else:
                chain.append(f"afade=t=in:d={wmc['fade_ms'] / 1000},areverse,afade=t=in:d={wmc['fade_ms'] / 1000},areverse")
            chain.append(f"volume={gain:.3f}dB")
            chain.append(self._resample(prof))
            if (lim := self._limiter(ms["limiter"])):
                chain.append(lim)
            self.tools.ffmpeg(["-v", "error", "-i", str(watermark), "-af", ",".join(chain), *self._out_args(prof), "-y", str(tmp)], ctx)
            return {"input_lufs": m["input_i"], "gain_db": round(gain, 3), "target_lufs": target}
        winfo, _ = self._cached(prepped, pkey, prep)
        wdur = wavio.info(prepped)["duration"]
        if wdur <= 0:
            raise StageError(ErrorClass.POLICY, "WATERMARK_EMPTY", "watermark rỗng sau khi dọn biên")
        gap = work / f"gap_{int(wmc['gap_ms'])}ms.wav"
        if not gap.is_file():
            wavio.write_silence(gap, wmc["gap_ms"], rate, ch, width)
        pos = wmc["position"]
        seq = {"start": [prepped, gap, master], "end": [master, gap, prepped],
               "both": [prepped, gap, master, gap, prepped]}[pos]
        key = _h("yt-wm1", _stat_key(master), pkey, wmc)

        def build(tmp: Path) -> dict:
            r = wavio.concat(seq, tmp)
            return {"duration_sec": r["duration"]}
        self._cached(out, key, build)
        d = wavio.info(out)["duration"]
        mdur = wavio.info(master)["duration"]
        story_start = (wdur + wmc["gap_ms"] / 1000) if pos in ("start", "both") else 0.0
        return {"duration_sec": round(d, 3), "watermark": True, "watermark_sha256": wsha, "watermark_duration_sec": round(wdur, 3),
                "gap_ms": wmc["gap_ms"], "position": pos, "story_start_sec": round(story_start, 3), "narration_duration_sec": round(mdur, 3),
                **{f"watermark_{k}": v for k, v in winfo.items()}}

    # ------------------------------------------------------------------ TIKTOK
    def _stretch_filter(self, speed: float, st: dict) -> tuple[str, str, list[str]]:
        warns: list[str] = []
        engine = st["engine"]
        if engine == "rubberband" and not self.tools.has_filter("rubberband"):
            warns.append("ffmpeg không có bộ lọc rubberband: dùng atempo (chất lượng thấp hơn)")
            engine = "atempo"
        if engine == "rubberband":
            opts = ":".join(f"{k}={v}" for k, v in st.get("options", {}).items())
            return f"rubberband=tempo={speed}:pitch=1" + (f":{opts}" if opts else ""), "rubberband", warns
        parts, s = [], speed
        while s > 2.0:                                              # atempo cũ giới hạn 2.0 mỗi bước
            parts.append("atempo=2.0")
            s /= 2.0
        while s < 0.5:
            parts.append("atempo=0.5")
            s /= 0.5
        parts.append(f"atempo={s:.6f}")
        return ",".join(parts), "atempo", warns

    def build_tiktok_parts(self, master: Path, speed: float, target_part_sec: float, out_dir: Path, ctx: StageContext,
                           timeline: dict | list | None = None) -> dict:
        prof = resolve(ctx.params)
        tk, fmt = prof["tiktok"], prof["format"]
        master, out_dir = Path(master), Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        work = out_dir.parent / "work"
        work.mkdir(parents=True, exist_ok=True)
        ver = self.tools.version()
        d_in = wavio.info(master)["duration"]
        warns: list[str] = []
        # --- 1) tăng tốc giữ cao độ + khớp độ to đích + limiter: một lượt ffmpeg duy nhất (một lần lượng tử hóa)
        stretched = work / "tiktok_stretched.wav"
        key = _h("tk-stretch1", _stat_key(master), speed, tk["stretch"], tk["loudness"], tk["limiter"], fmt, ver)

        def build(tmp: Path) -> dict:
            chain, engine, w = self._stretch_filter(speed, tk["stretch"]) if abs(speed - 1.0) > 1e-9 else ("", "none", [])
            info = {"engine": engine, "warnings": w}
            parts = [chain] if chain else []
            if tk["loudness"]["enabled"]:
                lo = {"target_lufs": tk["loudness"]["target_lufs"], "true_peak_db": tk["limiter"]["ceiling_db"], "lra": 11}
                m = self.tools.loudnorm_measure(master, "", lo, ctx)
                if F.finite(m.get("input_i")):
                    gain = tk["loudness"]["target_lufs"] - m["input_i"]
                    info["gain_db"] = round(gain, 3)
                    if abs(gain) > 0.05:
                        parts.append(f"volume={gain:.3f}dB")
            parts.append(self._resample(prof))
            if (lim := self._limiter(tk["limiter"])):
                parts.append(lim)
            self.tools.ffmpeg(["-v", "error", "-i", str(master), "-af", ",".join(parts), *self._out_args(prof), "-y", str(tmp)], ctx)
            return info
        sinfo, reused = self._cached(stretched, key, build)
        warns += sinfo.get("warnings", [])
        d_out = wavio.info(stretched)["duration"]
        ratio = d_out / d_in if d_in else 1.0
        # --- 2) ranh giới: timeline của TTS (chính xác) hoặc dò khoảng im lặng (audio nhập từ ngoài)
        sp = tk["split"]
        boundaries, source = [], "timeline"
        entries = (timeline.get("segments") if isinstance(timeline, dict) else timeline) or []
        for e in entries[:-1]:
            kind = {"paragraph": "paragraph", "scene": "scene", "sentence": "sentence", "dialogue": "sentence"}.get(e.get("kind"), "cut")
            boundaries.append({"t": float(e["cut_sec"]) * ratio, "kind": kind})
        if not entries:
            source = "silence"             # dò trên audio GỐC (ngưỡng tính theo thời gian nói thật, không bị co lại khi tăng tốc), rồi đổi sang thời gian sau tăng tốc
            _, _, err = self.tools.ffmpeg(["-v", "info", "-i", str(master), "-af",
                                           f"silencedetect=n={sp['silence']['threshold_db']}dB:d={sp['silence']['min_s']}", "-f", "null", "-"], ctx)
            for s in F.parse_silence(err, d_in):
                if s["end"] >= d_in - 0.05 or s["start"] <= 0.05:
                    continue                                                  # im lặng ở đầu/cuối file không phải ranh giới giữa
                boundaries.append({"t": (s["start"] + s["end"]) / 2 * ratio,
                                   "kind": "paragraph" if s["duration"] >= sp["silence"]["paragraph_s"] else "silence"})
        plan = plan_split(d_out, boundaries, target_part_sec, sp["min_ratio"], sp["max_ratio"], sp["bonus_sec"], sp.get("min_last_ratio", 0.4))
        warns += plan["warnings"]
        # --- 3) cắt (lossless, fade ngắn ở điểm cắt)
        for old in out_dir.glob("part_*.wav"):
            old.unlink()
        parts = []
        for i, p in enumerate(plan["parts"], 1):
            path = out_dir / f"part_{i:02d}.wav"
            ctx.cancel.check()
            r = wavio.slice_wav(stretched, path, p["start"], p["end"], sp["fade_ms"])
            parts.append({"path": path, "index": i, "start_sec": round(p["start"], 3), "end_sec": round(p["end"], 3),
                          "duration_sec": round(r["duration"], 3), "boundary": p["boundary"], "forced": p["forced"],
                          "mid_sentence": p["mid_sentence"], "source_start_sec": round(p["start"] / ratio, 3),
                          "source_end_sec": round(p["end"] / ratio, 3)})
        return {"parts": parts, "warnings": warns,
                "stretch": {"engine": sinfo.get("engine"), "speed": speed, "duration_in_sec": round(d_in, 3), "duration_out_sec": round(d_out, 3),
                            "ratio": round(ratio, 6), "gain_db": sinfo.get("gain_db"), "reused": reused},
                "split": {"target_sec": target_part_sec, "n": plan.get("n"), "per_part_sec": plan.get("per_part"), "mode": plan.get("mode"),
                          "boundaries": source, "boundary_count": len(boundaries)}}
