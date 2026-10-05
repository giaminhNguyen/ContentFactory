"""Workspace riêng cho từng job (HANDOFF §16): workspace/job_<id>/{source,story,tts,audio,render,temp}."""
from __future__ import annotations

from pathlib import Path

from . import pipeline as P

BASE_DIRS = ("source", "story", "tts", "audio", "render", "temp")


def job_dir(workspace_root: Path, job_id: str) -> Path:
    return Path(workspace_root) / f"job_{job_id}"


def ensure_job_dirs(workspace_root: Path, job_id: str) -> Path:
    d = job_dir(workspace_root, job_id)
    for sub in BASE_DIRS + tuple(s.workdir for s in P.STAGES):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d
