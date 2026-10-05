"""Fake adapter cho Phase 1: chạy end-to-end mà không cần engine thật.

Chèn lỗi / độ trễ qua job.params["fake"][<điểm>] (xem `hook`):
  {"sleep_s": 30, "attempt": 1}              ngủ (huỷ được) chỉ ở attempt 1, để test kill/resume
  {"fail_until_attempt": 2, "error_class": "TRANSIENT", "code": "X"}   lỗi cho tới hết attempt 2
  thêm "resource": "network|token|quota|disk|runtime|credential|input", "retry_after_s": N, "resume_after_s": N
  {"fail_while_file": "<path>", ...}          lỗi chừng nào file đó còn tồn tại (test mô phỏng "tài nguyên đã hồi phục")
Điểm: source, story, tts_chunk_<n>, render_youtube, render_tiktok, render_tiktok_part_<n>, publish.
Audio là WAV thật (stdlib `wave`) để QA/ghép/cắt part có ý nghĩa; video/thumbnail chỉ là byte giả.
"""
from __future__ import annotations

import hashlib
import random
import shutil
import time
import wave
from pathlib import Path

from ..contracts import ErrorClass, StageContext, StageError
from ..fsutil import atomic_write, atomic_write_bytes, atomic_write_json, atomic_write_text, sha256_file

RATE = 8000


def hook(ctx: StageContext, point: str) -> None:
    cfg = (ctx.params.get("fake") or {}).get(point)
    if not cfg:
        return
    if cfg.get("sleep_s") and cfg.get("attempt") in (None, ctx.attempt):
        ctx.log("fake_sleep", point=point, seconds=cfg["sleep_s"])
        ctx.cancel.wait(cfg["sleep_s"])
    still_down = bool(cfg.get("fail_while_file")) and Path(cfg["fail_while_file"]).exists()   # tài nguyên do test điều khiển
    if ctx.attempt <= cfg.get("fail_until_attempt", 0) or still_down:
        raise StageError(ErrorClass(cfg.get("error_class", "TRANSIENT")), cfg.get("code", "FAKE_FAILURE"),
                         f"injected at {point}, attempt {ctx.attempt}", resource=cfg.get("resource"),
                         retry_after_s=cfg.get("retry_after_s"),
                         resume_after=time.time() + cfg["resume_after_s"] if cfg.get("resume_after_s") else None)


WORDS = ("đêm khuya gió mưa hẻm nhỏ bước chân xa dần ngọn đèn vàng cánh cửa gỗ tiếng động lạ người đàn ông áo đen "
         "im lặng bóng tối kéo dài lá khô rơi con mèo trắng bờ sông lạnh sương mù tiếng chuông chùa vọng lại").split()


def _paragraph(k: int) -> str:
    """Đoạn giả có nội dung khác nhau thật sự (assembler sẽ loại đoạn gần trùng)."""
    rnd = random.Random(k)
    return f"Đoạn {k}. " + " ".join(rnd.choice(WORDS) for _ in range(45)) + "."


def record_call(ctx: StageContext, name: str) -> None:
    with open(ctx.stage_dir / "calls.log", "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{name} attempt={ctx.attempt}\n")


def _write_wav(path: Path, frames: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames)


def _read_frames(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def _silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(RATE * seconds)


def _tone(seconds: float) -> bytes:
    """Sóng vuông biên độ nhỏ (không phải im lặng) để QA im lặng/hỏng của TTS Manager có ý nghĩa."""
    half = (3000).to_bytes(2, "little", signed=True) * 40 + (-3000).to_bytes(2, "little", signed=True) * 40
    n = int(RATE * seconds)
    return (half * (n // 80 + 1))[: 2 * n]


HEALTH = {"ok": True, "fake": True}


class FakeSource:
    """SourceAdapter giả: ghi một phụ đề VTT thật nhỏ để Transcript Processor (code thật) chạy trên đó."""

    def acquire(self, src, out_dir: Path, ctx: StageContext):
        hook(ctx, "source")
        record_call(ctx, "source")
        out_dir.mkdir(parents=True, exist_ok=True)
        raw = out_dir / "subtitle_raw.vtt"
        atomic_write_text(raw, "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nBản ghi giả lập.\n")
        return {"source_url": src["value"], "source_type": "youtube", "provider": "fake", "video_id": "abcdefghijk",
                "title": ctx.params.get("title") or "Truyện thử nghiệm", "description": "Mô tả nguồn (không dùng)",
                "language": ctx.params.get("language", "vi"), "raw_subtitle_path": raw, "subtitle_format": "vtt",
                "subtitle_kind": "manual", "has_timestamps": True, "metadata": {}, "status": "ok", "error": None,
                "attempts": [{"provider": "fake", "status": "ok"}], "origin": "network"}

    def health(self) -> dict:
        return HEALTH


class FakeStory:
    def generate(self, bundle, profile: dict, out_dir: Path, ctx: StageContext):
        hook(ctx, "story")
        record_call(ctx, "story")
        n = int(profile.get("paragraphs", 6))
        paras = [_paragraph(k) for k in range(1, n + 1)]
        # 3 section CÓ heading và marker như engine thật hay sinh ra: Story Assembler phải gỡ sạch
        sections, per = [], max(1, n // 3)
        for s in range(3):
            body = paras[s * per:(s + 1) * per] if s < 2 else paras[2 * per:]
            p = out_dir / "sections" / f"section_{s + 1:02d}.md"
            atomic_write_text(p, f"Chương {s + 1}: Mở đầu\n\n" + "\n".join(body) + "\n\n---\n(Còn tiếp)\n")
            sections.append(p)
        return {"sections": sections, "stats": {"paragraphs": n}}

    def health(self) -> dict:
        return HEALTH


class FakeTTS:
    engine_id = "fake"

    def capabilities(self) -> dict:
        return {"max_chars": 600, "languages": ["vi"], "speed": False, "ssml": False, "sample_rate": RATE,
                "engine_version": "fake-1", "output_formats": ["wav"]}

    def synthesize(self, segment, profile: dict, out_path: Path, ctx: StageContext):
        hook(ctx, f"tts_chunk_{segment['index']}")
        record_call(ctx, f"tts_chunk_{segment['index']}")
        secs = max(0.2, len(segment["text"]) / 400)
        atomic_write(out_path, lambda tmp: _write_wav(tmp, _tone(secs)))
        return {"index": segment["index"], "duration_sec": secs}

    def health(self) -> dict:
        return HEALTH


class FakeAudio:
    def qa(self, audio: Path):
        try:
            with wave.open(str(audio), "rb") as w:
                dur = w.getnframes() / w.getframerate()
        except (wave.Error, EOFError, OSError):
            return {"ok": False, "duration_sec": 0.0, "issues": ["UNDECODABLE"]}
        return {"ok": dur > 0, "duration_sec": round(dur, 3), "issues": [] if dur > 0 else ["ZERO_DURATION"]}

    def qa_full(self, audio: Path, expect: dict, ctx: StageContext) -> dict:
        q = self.qa(audio)
        return {"ok": q["ok"], "errors": [{"code": c, "message": c} for c in q["issues"]], "warnings": [], "kind": expect.get("kind"),
                "measures": {"duration_sec": q["duration_sec"]}}

    def assemble(self, chunks, pauses_ms, out: Path, ctx: StageContext) -> dict:
        data, timeline, t = b"", [], 0.0
        for i, (c, p) in enumerate(zip(chunks, pauses_ms), 1):
            fr = _read_frames(c)
            d = len(fr) / 2 / RATE
            timeline.append({"index": i, "start_sec": round(t, 4), "end_sec": round(t + d, 4), "gap_after_sec": p / 1000,
                             "cut_sec": round(t + d + p / 2000, 4)})
            data += fr + _silence(p / 1000)
            t += d + p / 1000
        _write_wav(out, data)
        return {"duration_sec": len(data) / 2 / RATE, "timeline": timeline}

    def master(self, src: Path, out: Path, ctx: StageContext) -> dict:
        data = _read_frames(src)
        atomic_write(out, lambda tmp: _write_wav(tmp, data))
        return {"duration_sec": round(len(data) / 2 / RATE, 3), "fake": True}

    def build_youtube_audio(self, master: Path, watermark: Path | None, out: Path, ctx: StageContext) -> dict:
        data = (_read_frames(watermark) if watermark else b"") + _read_frames(master)
        atomic_write(out, lambda tmp: _write_wav(tmp, data))
        return {"duration_sec": round(len(data) / 2 / RATE, 3), "watermark": bool(watermark)}

    def build_tiktok_parts(self, master: Path, speed: float, target_part_sec: float, out_dir: Path,
                           ctx: StageContext, timeline=None) -> dict:
        # Fake: KHÔNG đổi tốc độ thật, chỉ chia theo độ dài nguồn tương ứng target*speed (xử lý thật: audio.processor.FfmpegAudio).
        data = _read_frames(master)
        step = max(2, int(RATE * target_part_sec * speed) * 2)
        step -= step % 2
        parts = []
        for i, off in enumerate(range(0, len(data), step), 1):
            p = out_dir / f"part_{i:02d}.wav"
            atomic_write(p, lambda tmp, o=off: _write_wav(tmp, data[o:o + step]))
            d = len(data[off:off + step]) / 2 / RATE
            parts.append({"path": p, "index": i, "start_sec": round(off / 2 / RATE, 3), "end_sec": round(off / 2 / RATE + d, 3),
                          "duration_sec": round(d, 3), "boundary": "fake", "forced": False, "mid_sentence": False})
        return {"parts": parts, "warnings": [], "stretch": {"engine": "fake"}, "split": {"boundaries": "fake"}}

    def health(self) -> dict:
        return HEALTH


class FakeRender:
    requires_pool = False                                   # fake không cần source pool

    def prepare_pool(self, pool, ctx=None) -> dict:
        return {"dir": None, "fingerprint": "fake", "reused": True}

    def pool_status(self, pool) -> dict:
        return {"name": pool.get("name"), "ready": True, "syncing": False}

    def version(self) -> str:
        return "fake-render-1"

    def render_video(self, req, ctx: StageContext) -> dict:
        pid = req["profile"]["id"]
        hook(ctx, f"render_{pid}")
        if req.get("part"):
            hook(ctx, f"render_{pid}_part_{req['part']}")
        record_call(ctx, f"render_{pid}:{req['output'].name}")
        sha = sha256_file(req["audio"])[:12]
        atomic_write_bytes(req["output"], f"FAKE-MP4|profile={pid}|audio_sha={sha}\n".encode())
        return {"warnings": []}

    def render_thumbnail(self, req, ctx: StageContext) -> Path:
        record_call(ctx, "thumbnail")
        atomic_write_bytes(req["output"], f"FAKE-JPG|{req['title']}|{req['channel_name']}\n".encode())
        return req["output"]

    def health(self) -> dict:
        return HEALTH


class FakePublish:
    platform = "youtube"

    def publish(self, req, ctx: StageContext):
        hook(ctx, "publish")
        record_call(ctx, "publish")
        vid = "fake-" + hashlib.sha1(req["idempotency_key"].encode()).hexdigest()[:10]   # cùng key => cùng video
        return {"state": "completed", "remote_id": vid, "remote_url": f"https://youtube.invalid/watch?v={vid}",
                "warnings": [], "job_id": "fake-job"}

    def find(self, idempotency_key: str):
        return None

    def health(self) -> dict:
        return HEALTH
