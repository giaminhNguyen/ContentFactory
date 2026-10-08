"""Dựng adapter từ config. Chỉ orchestrator được import module cụ thể; module không import nhau.

Tên adapter trong config["adapters"][<loại>]:
  "fake" | "builtin" | "provider_chain" (source) | "story_branch" (story) | "rule" (planner) | "ffmpeg" (audio) | "contentflow" (render) | "yt_uploader" (publish) |
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
from ..publish.yt_uploader import YtUploaderPublish
from ..render.contentflow import ContentFlowRender
from ..source.chain import ProviderChain
from ..tts.planner import RuleSegmentPlanner
from ..source.providers import LocalSubtitleProvider, PlainTextProvider, SubtitleSupperVipProvider, YtDlpProvider
from ..workers.drivers.claude_cli import ClaudeCliDriver
from ..workers.manager import WorkerManager
from ..workers.registry import WorkerRegistry
from ..workers.runner import DriverRunner, WorkerRunner
from ..workers.store import WorkerStore
from .config import Config
from .validation import validate_kind


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
                              "user_root": p(cf.get("user_root"), "contentflow_user"), "ffprobe": t.get("ffprobe"), "sync_wait_s": cf.get("sync_wait_s", 3600),
                              "verify_output": cf.get("verify_output", True)})


def _story_runner(cfg: Config, sb: dict):
    """Runner cho StoryBranchAdapter (W1: story.write đi qua Worker Runtime).

    Đã cấu hình routing `story.write` trong workers.db -> WorkerManager (retry/fallback/cooldown
    + validation gate); chưa -> chạy thẳng ClaudeCliDriver một CLI như trước đây.
    """
    driver = ClaudeCliDriver(sb)
    reg = WorkerRegistry(WorkerStore(cfg.path("runtime") / "workers.db"), drivers={"claude_cli": driver})
    if "story.write" not in reg.routing():
        return DriverRunner(driver)
    mgr = WorkerManager(reg, cfg.path("runtime") / "workspace",
                        validate=lambda path, work_type: validate_kind(work_type, path))
    return WorkerRunner(mgr)


def _factories(cfg: Config) -> dict:
    sb = cfg.data.get("story_branch", {})
    oh_root = Path(sb.get("oh_story_root") or cfg.root / "modules" / "oh-story-claudecode")
    if not oh_root.is_absolute():
        oh_root = cfg.root / oh_root
    return {
        ("source", "fake"): fake.FakeSource,
        ("source", "provider_chain"): lambda: _source_chain(cfg),
        ("story", "fake"): fake.FakeStory,
        ("story", "story_branch"): lambda: StoryBranchAdapter(sb, oh_root, runner=_story_runner(cfg, sb)),
        ("tts", "fake"): fake.FakeTTS, ("planner", "rule"): RuleSegmentPlanner, ("audio", "fake"): fake.FakeAudio, ("audio", "ffmpeg"): lambda: FfmpegAudio(cfg.data.get("tools", {})), ("render", "fake"): fake.FakeRender,
        ("publish", "fake"): fake.FakePublish,
        ("render", "contentflow"): lambda: _contentflow(cfg),
        ("publish", "yt_uploader"): lambda: YtUploaderPublish({**cfg.data.get("tools", {}).get("yt_uploader", {}),
                                                              "ffmpeg": cfg.data.get("tools", {}).get("ffmpeg")}),
        ("output", "builtin"): lambda: BuiltinOutputPublisher(cfg["output"]),
    }


def build_adapter(cfg: Config, kind: str, factories: dict | None = None):
    name = cfg["adapters"][kind]
    factories = factories or _factories(cfg)
    if (kind, name) in factories:
        return factories[(kind, name)]()
    if ":" in name:
        mod, cls_name = name.split(":", 1)
        conf = cfg.data.get("adapter_config", {}).get(kind)
        cls = getattr(importlib.import_module(mod), cls_name)
        return cls(conf) if conf is not None else cls()
    raise ValueError(f"adapter {kind}={name!r} không tồn tại")


def build_adapters(cfg: Config) -> dict[str, object]:
    factories = _factories(cfg)
    return {kind: build_adapter(cfg, kind, factories) for kind in cfg["adapters"]}
