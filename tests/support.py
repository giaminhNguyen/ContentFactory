"""Tiện ích test: root tạm + config chạy nhanh + chạy orchestrator ở tiến trình con để kill thật."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.runner import Orchestrator

REPO = Path(__file__).resolve().parents[1]
FAST = {"poll_s": 0.02, "heartbeat_s": 0.2, "lease_s": 1.0,
        "retry": {"max_attempts": 3, "backoff_s": [0.05, 0.05], "max_interruptions": 5, "jitter": 0, "floor_s": 0},
        "monitor": {"tick_s": 0.02, "base_s": 0.05, "max_s": 0.2}}

BASE_PARAMS = {"input": {"kind": "youtube_url", "value": "https://youtu.be/test"},
               "title": "Truyện ma đêm khuya", "made_for_kids": False,
               "tiktok": {"speed": 2.0, "target_part_sec": 1.5}}


def make_root(extra: dict | None = None) -> Path:
    root = Path(tempfile.mkdtemp(prefix="cf-test-"))
    (root / "config").mkdir()
    cfg = json.loads(json.dumps(FAST))
    if extra:
        cfg.update(extra)
    (root / "config" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    shutil.copyfile(REPO / "modules.lock", root / "modules.lock")
    return root


def params(**over) -> dict:
    p = json.loads(json.dumps(BASE_PARAMS))
    p.update(over)
    return p


def start_runner(root: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    return subprocess.Popen([sys.executable, "-m", "contentfactory", "--root", str(root), "run"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_until(cond, timeout: float = 20.0, what: str = "condition") -> None:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


class RootCase(unittest.TestCase):
    """Mỗi test một root tạm riêng."""

    def setUp(self) -> None:
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.root, True)

    def orc(self) -> Orchestrator:
        return Orchestrator(load_config(self.root))

    def runs(self, orc: Orchestrator, job_id: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in orc.store.stage_runs(job_id):
            out.setdefault(r["stage"], []).append(r["status"])
        return out

    def job_dir(self, job_id: str) -> Path:
        return self.root / "workspace" / f"job_{job_id}"
