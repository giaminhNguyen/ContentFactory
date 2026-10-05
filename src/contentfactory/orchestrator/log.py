"""Structured logging: mỗi sự kiện là một dòng JSON, ghi vào log chung và log riêng của job."""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class EventLog:
    def __init__(self, runtime_dir: Path, workspace_root: Path, echo: bool = False) -> None:
        self.global_file = Path(runtime_dir) / "logs" / "orchestrator.jsonl"
        self.global_file.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_root = Path(workspace_root)
        self.echo = echo
        self._lock = threading.Lock()

    def emit(self, event: str, level: str = "info", job_id: str | None = None, **fields) -> None:
        rec = {"ts": datetime.fromtimestamp(time.time(), timezone.utc).isoformat(timespec="milliseconds"),
               "level": level, "event": event, **({"job_id": job_id} if job_id else {}), **fields}
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            with open(self.global_file, "a", encoding="utf-8", newline="\n") as f:
                f.write(line)
            if job_id:
                jd = self.workspace_root / f"job_{job_id}"
                if jd.exists():
                    with open(jd / "job.log.jsonl", "a", encoding="utf-8", newline="\n") as f:
                        f.write(line)
            if self.echo:
                sys.stderr.write(line)

    def bind(self, job_id: str, stage: str, attempt: int):
        def log(event: str, level: str = "info", **fields) -> None:
            self.emit(event, level, job_id, stage=stage, attempt=attempt, **fields)
        return log
