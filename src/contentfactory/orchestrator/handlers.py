"""Nối tên stage với handler của từng package module."""
from __future__ import annotations

from ..audio import stage as audio_stage
from ..output import stage as output_stage
from ..publish import stage as publish_stage
from ..render import stage as render_stage
from ..source import stage as source_stage
from ..story import stage as story_stage
from ..tts import stage as tts_stage

HANDLERS = {
    "source": source_stage.run,
    "story": story_stage.run,
    "tts": tts_stage.run,
    "audio": audio_stage.run,
    "render_youtube": render_stage.run_youtube,
    "render_tiktok": render_stage.run_tiktok,
    "output": output_stage.run,
    "publish": publish_stage.run,
}
