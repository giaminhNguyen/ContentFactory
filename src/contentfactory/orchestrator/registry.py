"""Dựng adapter từ config. Chỉ orchestrator được import module cụ thể; module không import nhau.

Tên adapter trong config["adapters"][<loại>]:
  "fake" | "builtin" | "youtube" (source) | "story_branch" (story) | "package.module:ClassName" (adapter của phase sau).
Đổi sang adapter thật chỉ cần sửa config; ví dụ:
  {"adapters": {"source": "youtube", "story": "story_branch"}}
"""
from __future__ import annotations

import importlib
from pathlib import Path

from ..adapters import fake
from ..adapters.story_branch import StoryBranchAdapter
from ..output.publisher import BuiltinOutputPublisher
from ..source.processor import YouTubeSourceProcessor
from .config import Config


def _factories(cfg: Config) -> dict:
    sb = cfg.data.get("story_branch", {})
    oh_root = Path(sb.get("oh_story_root") or cfg.root / "modules" / "oh-story-claudecode")
    if not oh_root.is_absolute():
        oh_root = cfg.root / oh_root
    return {
        ("source", "fake"): fake.FakeSource,
        ("source", "youtube"): lambda: YouTubeSourceProcessor(cfg.data.get("youtube", {}), cfg.path("runtime") / "cache"),
        ("story", "fake"): fake.FakeStory,
        ("story", "story_branch"): lambda: StoryBranchAdapter(sb, oh_root),
        ("tts", "fake"): fake.FakeTTS, ("audio", "fake"): fake.FakeAudio, ("render", "fake"): fake.FakeRender,
        ("publish", "fake"): fake.FakePublish,
        ("output", "builtin"): lambda: BuiltinOutputPublisher(cfg["output"]),
    }


def build_adapters(cfg: Config) -> dict[str, object]:
    factories, out = _factories(cfg), {}
    for kind, name in cfg["adapters"].items():
        if (kind, name) in factories:
            out[kind] = factories[(kind, name)]()
        elif ":" in name:
            mod, cls_name = name.split(":", 1)
            out[kind] = getattr(importlib.import_module(mod), cls_name)()
        else:
            raise ValueError(f"adapter {kind}={name!r} không tồn tại")
    return out
