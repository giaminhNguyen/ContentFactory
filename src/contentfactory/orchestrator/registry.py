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
from ..adapters.claude_llm import ClaudeCliLLM
from ..adapters.fake_remix import FakeRemixLLM
from ..audio.processor import FfmpegAudio
from ..adapters.story_branch import StoryBranchAdapter
from ..output.publisher import BuiltinOutputPublisher
from ..publish.yt_uploader import YtUploaderPublish
from ..render.contentflow import ContentFlowRender
from ..source.chain import ProviderChain
from ..tts.planner import RuleSegmentPlanner
from ..story.naming import make_titler
from ..story_remix.adapter import StoryRemixAdapter
from ..story_scene_remix.adapter import StorySceneRemixAdapter
from ..story_scene_remix.fake_llm import FakeSceneRemixLLM
from ..universe import Universe, UniverseDB
from ..source.providers import LocalSubtitleProvider, PlainTextProvider, SubtitleSupperVipProvider, YtDlpProvider
from .config import Config
from .remix_universe import UniverseBridge
from .story_router import StoryModeRouter


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


def _remix(cfg: Config) -> StoryRemixAdapter:
    """Story Remix: LLM (fake khi adapter story là fake; ngược lại Claude CLI) + Kho nhân vật (runtime/universe.db, mở lười, dùng chung trong adapter)."""
    rc = cfg.data.get("story_remix") or {}
    kind = rc.get("llm", "auto")
    if kind == "auto":
        kind = "fake" if cfg["adapters"]["story"] == "fake" else "claude_cli"
    llm = FakeRemixLLM() if kind == "fake" else ClaudeCliLLM({**(cfg.data.get("story_branch") or {}), **rc})
    holder: dict = {}

    def universe() -> UniverseBridge:
        if "u" not in holder:
            holder["u"] = UniverseBridge(Universe(UniverseDB(cfg.path("runtime") / "universe.db")))
        return holder["u"]
    return StoryRemixAdapter(llm, universe, publisher=lambda ctx, cast, qa, text, mode: universe().publish(ctx, cast, qa, text, mode))


def _scene_remix(cfg: Config) -> StorySceneRemixAdapter:
    rc = cfg.data.get("story_remix") or {}
    kind = rc.get("llm", "auto")
    if kind == "auto":
        kind = "fake" if cfg["adapters"]["story"] == "fake" else "claude_cli"
    llm = FakeSceneRemixLLM() if kind == "fake" else ClaudeCliLLM({**(cfg.data.get("story_branch") or {}), **rc})
    return StorySceneRemixAdapter(llm, rc.get("price_usd_per_mtok"))


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


def _factories(cfg: Config) -> dict:
    sb = cfg.data.get("story_branch", {})
    oh_root = Path(sb.get("oh_story_root") or cfg.root / "modules" / "oh-story-claudecode")
    if not oh_root.is_absolute():
        oh_root = cfg.root / oh_root
    return {
        ("source", "fake"): fake.FakeSource,
        ("source", "provider_chain"): lambda: _source_chain(cfg),
        ("story", "fake"): lambda: StoryModeRouter(fake.FakeStory(), lambda: _remix(cfg), scene_remix_factory=lambda: _scene_remix(cfg)),
        ("story", "story_branch"): lambda: StoryModeRouter(StoryBranchAdapter(sb, oh_root), lambda: _remix(cfg), make_titler(ClaudeCliLLM({**sb, **(cfg.data.get("story_remix") or {})})), scene_remix_factory=lambda: _scene_remix(cfg)),
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
