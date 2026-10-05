"""Stage TTS: story.txt -> TTSManager (normalize, plan, validate, cache, synth từng segment) -> master.wav + tts_manifest.json.

Handler chỉ nối StageContext với TTSManager; mọi logic nằm ở package tts (xem manager.py). Phụ thuộc (adapter TTS,
AudioProcessor, SegmentPlanner) được orchestrator tiêm vào theo tên adapter trong config.
"""
from __future__ import annotations

from ..contracts import AudioProcessor, StageContext, StageResult, TTSAdapter
from .manager import TTSManager
from .planner import SegmentPlanner


def run(ctx: StageContext, tts: TTSAdapter, audio: AudioProcessor, planner: SegmentPlanner) -> StageResult:
    text = ctx.one("story_text").read_text(encoding="utf-8")
    return TTSManager(tts, audio, planner).run(ctx, text, ctx.params.get("tts"))
