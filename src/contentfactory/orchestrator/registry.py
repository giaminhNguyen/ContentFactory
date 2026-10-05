"""Dựng adapter từ config. Chỉ orchestrator được import module cụ thể; module không import nhau.

Tên adapter trong config["adapters"][<loại>]:
  "fake" | "builtin" | "provider_chain" (source) | "story_branch" (story) | "rule" (planner) | "ffmpeg" (audio) |
  "package.module:ClassName" (adapter ngoài; cấu hình riêng qua config["adapter_config"][<loại>]).
Đổi sang adapter thật chỉ cần sửa config; ví dụ:
  {"adapters": {"source": "provider_chain", "story": "story_branch"}}
`provider_chain` đọc danh sách provider ở config["source"]["providers"] (supervip, ytdlp, local, text).
"""
from __future__ import annotations

import importlib
from pathlib import Path

from ..adapters import fake
from ..audio.processor import FfmpegAudio
from ..adapters.story_branch import StoryBranchAdapter
from ..output.publisher import BuiltinOutputPublisher
from ..render.contentflow import ContentFlowRender
from ..source.chain import ProviderChain
from ..tts.planner import RuleSegmentPlanner
from ..source.providers import LocalSubtitleProvider, PlainTextProvider, SubtitleSupperVipProvider, YtDlpProvider
from .config import Config


def _source_chain(cfg: Config) -> ProviderChain:
    src = cfg.data.get("source", {})
    available = {"supervip": lambda: SubtitleSupperVipProvider(cfg.data.get("supervip", {}), cfg.root),
                 "ytdlp": lambda: YtDlpProvider(cfg.data.get("youtube", {})),
                 "local": LocalSubtitleProvider, "text": PlainTextProvider}
    unknown = [n for n in src.get("providers", []) if n not in available]
    if unknown:
        raise ValueError(f"source.providers có tên không tồn tại: {unknown}; hợp lệ: {sorted(available)}")
    return ProviderChain([available[n]() for n in src.get("providers", [])], cfg.path("runtime") / "cache" / "source",
                         {"languages": src.get("languages", ["vi", "en"]),
                          "allow_translation": src.get("allow_translation", False)})


def _contentflow(cfg: Config) -> ContentFlowRender:
    t = cfg.data.get("tools", {})
    cf = t.get("contentflow", {})
    def p(v, default):
        v = Path(v or default)
        return v if v.is_absolute() else cfg.root / v
    return ContentFlowRender({"root": p(cf.get("root"), "modules/ContentFlow"), "python": cf.get("python"),
                              "base_dir": p(cf.get("base_dir"), "config/contentflow"),
                              "pools_dir": p(cfg.data.get("render", {}).get("pools_dir"), "runtime/pools"),
                              "ffprobe": t.get("ffprobe"), "sync_wait_s": cf.get("sync_wait_s", 3600),
                              "verify_output": cf.get("verify_output", True)})


def _factories(cfg: Config) -> dict:
    sb = cfg.data.get("story_branch", {})
    oh_root = Path(sb.get("oh_story_root") or cfg.root / "modules" / "oh-story-claudecode")
    if not oh_root.is_absolute():
        oh_root = cfg.root / oh_root
    return {
        ("source", "fake"): fake.FakeSource,
        ("source", "provider_chain"): lambda: _source_chain(cfg),
        ("story", "fake"): fake.FakeStory,
        ("story", "story_branch"): lambda: StoryBranchAdapter(sb, oh_root),
        ("tts", "fake"): fake.FakeTTS, ("planner", "rule"): RuleSegmentPlanner, ("audio", "fake"): fake.FakeAudio, ("audio", "ffmpeg"): lambda: FfmpegAudio(cfg.data.get("tools", {})), ("render", "fake"): fake.FakeRender,
        ("publish", "fake"): fake.FakePublish,
        ("render", "contentflow"): lambda: _contentflow(cfg),
        ("output", "builtin"): lambda: BuiltinOutputPublisher(cfg["output"]),
    }


def build_adapters(cfg: Config) -> dict[str, object]:
    factories, out = _factories(cfg), {}
    for kind, name in cfg["adapters"].items():
        if (kind, name) in factories:
            out[kind] = factories[(kind, name)]()
        elif ":" in name:
            mod, cls_name = name.split(":", 1)
            conf = cfg.data.get("adapter_config", {}).get(kind)
            cls = getattr(importlib.import_module(mod), cls_name)
            out[kind] = cls(conf) if conf is not None else cls()
        else:
            raise ValueError(f"adapter {kind}={name!r} không tồn tại")
    return out
