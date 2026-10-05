"""Manifest của job (HANDOFF §18). DẪN XUẤT từ DB nên mất cũng dựng lại được; DB mới là nguồn sự thật."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..fsutil import atomic_write_json
from . import pipeline as P
from .db import JobStore

SCHEMA = 1
# Những thứ mà một lần chạy lại KHÔNG đảm bảo ra kết quả y hệt (DECISIONS D-08).
NONDETERMINISTIC = ["render.background_selection"]


def read_modules_lock(root: Path) -> dict[str, str]:
    f = Path(root) / "modules.lock"
    out: dict[str, str] = {}
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 4 and not line.lstrip().startswith("#"):
                out[parts[0]] = parts[3]
    return out


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def build(store: JobStore, job_id: str, versions: dict[str, str]) -> dict:
    job = store.get_job(job_id)
    runs, arts = store.stage_runs(job_id), store.artifacts(job_id)
    stages: dict[str, dict] = {}
    for s in P.STAGES:
        r = [x for x in runs if x["stage"] == s.name]
        ok = [x for x in r if x["status"] == "succeeded"]
        stages[s.name] = {
            "status": "succeeded" if ok else (r[-1]["status"] if r else "pending"),
            "attempts": len(r),
            "stage_key": ok[-1]["stage_key"] if ok else None,
            "started": _iso(r[0]["started_at"]) if r else None,
            "ended": _iso(ok[-1]["ended_at"]) if ok else None,
            "data": json.loads(ok[-1]["data"]) if ok and ok[-1]["data"] else {},
            "artifacts": [{"path": a["path"], "kind": a["kind"], "sha256": a["sha256"], "bytes": a["bytes"],
                           "meta": json.loads(a["meta"])} for a in arts if a["stage"] == s.name],
        }
    return {"schema": SCHEMA, "job_id": job_id, "state": job["state"], "failed_stage": job["failed_stage"],
            "last_error": job["last_error"], "params": job["params"], "modules": versions,
            "created": _iso(job["created_at"]), "updated": _iso(job["updated_at"]),
            "stages": stages, "nondeterministic": NONDETERMINISTIC}


def write(store: JobStore, workspace_dir: Path, job_id: str, versions: dict[str, str]) -> Path:
    return atomic_write_json(Path(workspace_dir) / "manifest.json", build(store, job_id, versions))
