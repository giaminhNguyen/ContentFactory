"""Stage TTS: story.txt -> chunk (từng segment) -> master.wav.

Phase 1 dùng segmenter tối giản (theo đoạn). SegmentPlanner AI + RuleValidator là Phase 4.
Checkpoint trong stage: chunk đã có file (adapter ghi atomic) thì dùng lại, nên resume không tổng hợp lại chunk cũ.
Handler nhận AudioProcessor qua tiêm phụ thuộc (kiểu từ contracts), không import package audio.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..contracts import AudioProcessor, ErrorClass, Segment, StageContext, StageError, StageResult, TTSAdapter
from ..fsutil import atomic_write

DEFAULT_PROFILE = {"max_chars": 600, "pause_ms": {"paragraph": 600}}


def split_segments(text: str, max_chars: int, pause_ms: int) -> list[Segment]:
    segs: list[Segment] = []
    for para in (p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()):
        buf = ""
        for sentence in re.split(r"(?<=[.!?…])\s+", para):
            if buf and len(buf) + 1 + len(sentence) > max_chars:
                segs.append({"index": len(segs) + 1, "text": buf, "pause_after_ms": 0})
                buf = ""
            buf = f"{buf} {sentence}".strip()
        if buf:
            segs.append({"index": len(segs) + 1, "text": buf, "pause_after_ms": pause_ms})
    return segs


def run(ctx: StageContext, tts: TTSAdapter, audio: AudioProcessor) -> StageResult:
    profile = {**DEFAULT_PROFILE, **ctx.params.get("tts", {})}
    text = ctx.one("story_text").read_text(encoding="utf-8")
    segments = split_segments(text, profile["max_chars"], profile["pause_ms"]["paragraph"])
    if not segments:
        raise StageError(ErrorClass.POLICY, "EMPTY_STORY", "story.txt không có segment nào")
    for seg in segments:
        if len(seg["text"]) > tts.capabilities().get("max_chars", 10**9):
            raise StageError(ErrorClass.POLICY, "SEGMENT_TOO_LONG", f"segment {seg['index']}")
    chunk_dir = ctx.stage_dir / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[Path] = []
    reused = 0
    for seg in segments:
        ctx.cancel.check()
        out = chunk_dir / f"{seg['index']:06d}.wav"
        was_reused = out.is_file() and audio.qa(out)["ok"]
        if was_reused:
            reused += 1
        else:
            tts.synthesize(seg, profile, out, ctx)
            if not audio.qa(out)["ok"]:
                raise StageError(ErrorClass.TRANSIENT, "CHUNK_QA_FAILED", f"chunk {seg['index']}")
        chunks.append(out)
        ctx.log("tts_chunk_done", index=seg["index"], total=len(segments), reused=was_reused)
    master = ctx.workspace / "audio" / "master.wav"
    atomic_write(master, lambda tmp: audio.assemble(chunks, [s["pause_after_ms"] for s in segments], tmp, ctx))
    qa = audio.qa(master)
    if not qa["ok"]:
        raise StageError(ErrorClass.TRANSIENT, "MASTER_QA_FAILED", ",".join(qa["issues"]))
    return StageResult([ctx.draft(master, "audio_master", duration_sec=qa["duration_sec"])],
                       {"segments": len(segments), "reused_chunks": reused, "duration_sec": qa["duration_sec"]})
