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
    """Manifest dẫn xuất từ DB. Bổ sung (Phase 2.9, cùng schema=1): start/target, hold, checkpoint, config snapshot, imports."""
    job = store.get_job(job_id)
    runs, arts = store.stage_runs(job_id), store.artifacts(job_id)
    stages: dict[str, dict] = {}
    for s in P.STAGES:
        r = [x for x in runs if x["stage"] == s.name]
        ok = [x for x in r if x["status"] in ("succeeded", "skipped")]
        last_ok = ok[-1] if ok else None
        data = json.loads(last_ok["data"]) if last_ok and last_ok["data"] else {}
        stages[s.name] = {
            "status": last_ok["status"] if last_ok else (r[-1]["status"] if r else "pending"),
            "attempts": len(r),
            "stage_key": last_ok["stage_key"] if last_ok else None,
            "started": _iso(r[0]["started_at"]) if r else None,
            "ended": _iso(last_ok["ended_at"]) if last_ok else None,
            "data": data,
            "artifacts": [{"path": a["path"], "kind": a["kind"], "sha256": a["sha256"], "bytes": a["bytes"],
                           "meta": json.loads(a["meta"])} for a in arts if a["stage"] == s.name],
        }
        if last_ok and last_ok["status"] == "skipped":
            stages[s.name]["skipped_reason"] = data.get("skipped")
    snap = job.get("config_snapshot") or {}
    hold = None
    if job.get("hold_reason"):
        hold = {"reason": job["hold_reason"], "detail": job["hold_detail"], "since": _iso(job["hold_since"]),
                "resume_after": _iso(job["resume_after"]), "needs_user": job["needs_user"],
                "auto_resumes_without_progress": job["auto_resumes_without_progress"]}
    return {"schema": SCHEMA, "job_id": job_id, "state": job["state"], "failed_stage": job["failed_stage"],
            "last_error": job["last_error"], "params": job["params"], "modules": versions,
            "created": _iso(job["created_at"]), "updated": _iso(job["updated_at"]),
            "start_stage": job.get("start_stage"), "target_stage": job.get("target_stage"),
            "complete": P.is_complete(job["state"], job.get("target_idx")),
            "hold": hold, "auto_resume": job.get("auto_resume"), "progress": job.get("progress"),
            "checkpoint": job.get("checkpoint") or {},
            "config": {"hash": job.get("config_hash"), "revision": job.get("config_revision", 0),
                       "semantic": snap.get("semantic")},
            "imports": [{"path": a["path"], "kind": a["kind"], "sha256": a["sha256"], "meta": json.loads(a["meta"])}
                        for a in arts if a["stage"] == "import"],
            "stages": stages, "nondeterministic": NONDETERMINISTIC}


def write(store: JobStore, workspace_dir: Path, job_id: str, versions: dict[str, str]) -> Path:
    return atomic_write_json(Path(workspace_dir) / "manifest.json", build(store, job_id, versions))
