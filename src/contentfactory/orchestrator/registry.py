"""Dựng adapter từ config. Chỉ orchestrator được import module cụ thể; module không import nhau.

Tên adapter: "fake" | "builtin" | "package.module:ClassName" (adapter thật ở các phase sau).
"""
from __future__ import annotations

import importlib

from ..adapters import fake
from ..output.publisher import BuiltinOutputPublisher
from .config import Config

BUILTIN = {
    ("source", "fake"): fake.FakeSource, ("story", "fake"): fake.FakeStory, ("tts", "fake"): fake.FakeTTS,
    ("audio", "fake"): fake.FakeAudio, ("render", "fake"): fake.FakeRender, ("publish", "fake"): fake.FakePublish,
    ("output", "builtin"): BuiltinOutputPublisher,
}


def build_adapters(cfg: Config) -> dict[str, object]:
    out: dict[str, object] = {}
    for kind, name in cfg["adapters"].items():
        if (kind, name) in BUILTIN:
            cls = BUILTIN[(kind, name)]
            out[kind] = cls(cfg["output"]) if kind == "output" else cls()
        elif ":" in name:
            mod, cls_name = name.split(":", 1)
            out[kind] = getattr(importlib.import_module(mod), cls_name)()
        else:
            raise ValueError(f"adapter {kind}={name!r} không tồn tại")
    return out
