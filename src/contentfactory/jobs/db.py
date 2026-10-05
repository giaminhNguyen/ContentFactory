"""SQLite job store: nguồn sự thật cho state, lease, stage run, artifact, lịch sử chuyển trạng thái.

Mọi thay đổi state đi qua `_move` (kiểm tra bảng chuyển hợp lệ + compare-and-swap theo state hiện tại)
và nằm trong một transaction cùng với artifact/stage_run tương ứng => checkpoint nguyên tử:
hoặc stage được ghi nhận xong cùng artifact, hoặc không gì cả.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..contracts import ArtifactRef, ErrorClass, StageError
from . import pipeline as P

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, seq INTEGER UNIQUE NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  state TEXT NOT NULL, params TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
  not_before REAL, lease_owner TEXT, lease_until REAL,
  retry_used INTEGER NOT NULL DEFAULT 0, failed_stage TEXT, last_error TEXT);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, not_before);
CREATE TABLE IF NOT EXISTS stage_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, stage TEXT NOT NULL,
  attempt INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT, stage_key TEXT,
  started_at REAL NOT NULL, ended_at REAL, error TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS stage_runs_job ON stage_runs(job_id, stage, id);
CREATE TABLE IF NOT EXISTS artifacts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, stage TEXT NOT NULL, run_id INTEGER,
  kind TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
  meta TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(job_id, path));
CREATE TABLE IF NOT EXISTS transitions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, job_id TEXT NOT NULL,
  from_state TEXT, to_state TEXT NOT NULL, stage TEXT, attempt INTEGER, note TEXT);
"""

_RUNNING_STATES = tuple(s.running_state for s in P.STAGES)


@dataclass
class Claim:
    job_id: str
    stage: P.Stage
    attempt: int
    run_id: int
    params: dict


class LostLease(Exception):
    """Job không còn thuộc về owner này (lease hết hạn và bị nhận lại)."""


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = self._connect()
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
        finally:
            c.close()

    # -- plumbing ---------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        return c

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        c = self._connect()
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()

    @staticmethod
    def _move(c: sqlite3.Connection, job_id: str, src: str, dst: str, now: float,
              stage: str | None = None, attempt: int | None = None, note: str | None = None,
              **set_cols: object) -> None:
        if not P.allowed(src, dst):
            raise ValueError(f"illegal transition {src} -> {dst}")
        cols = {"state": dst, "updated_at": now, **set_cols}
        sql = "UPDATE jobs SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=? AND state=?"
        if c.execute(sql, (*cols.values(), job_id, src)).rowcount != 1:
            raise LostLease(f"{job_id}: expected state {src}")
        c.execute("INSERT INTO transitions(ts,job_id,from_state,to_state,stage,attempt,note) VALUES(?,?,?,?,?,?,?)",
                  (now, job_id, src, dst, stage, attempt, note))

    # -- jobs -------------------------------------------------------------------------------
    def create_job(self, params: dict, priority: int = 0, now: float | None = None) -> str:
        now = now or time.time()
        with self._tx() as c:
            seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM jobs").fetchone()[0]
            job_id = f"{seq:06d}"
            c.execute("INSERT INTO jobs(id,seq,created_at,updated_at,state,params,priority) VALUES(?,?,?,?,?,?,?)",
                      (job_id, seq, now, now, P.NEW, json.dumps(params, ensure_ascii=False), priority))
            c.execute("INSERT INTO transitions(ts,job_id,from_state,to_state,note) VALUES(?,?,NULL,?,?)",
                      (now, job_id, P.NEW, "created"))
        return job_id

    @staticmethod
    def _job(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["params"] = json.loads(d["params"])
        d["last_error"] = json.loads(d["last_error"]) if d["last_error"] else None
        return d

    def get_job(self, job_id: str) -> dict | None:
        rows = self._q("SELECT * FROM jobs WHERE id=?", (job_id,))
        return self._job(rows[0]) if rows else None

    def list_jobs(self) -> list[dict]:
        return [self._job(r) for r in self._q("SELECT * FROM jobs ORDER BY seq")]

    def nonterminal_count(self) -> int:
        return self._q("SELECT COUNT(*) FROM jobs WHERE state NOT IN (?,?)", (P.PUBLISHED, P.FAILED))[0][0]

    # -- scheduling -------------------------------------------------------------------------
    def claim(self, stage: P.Stage, limit: int, resource_limit: int, owner: str,
              lease_s: float, now: float | None = None) -> list[Claim]:
        """Nhận tối đa `limit` job đang xếp hàng ở `stage`, không vượt `resource_limit` job đang chạy
        trên cùng tài nguyên (đếm trong cùng transaction nên đúng cả khi có nhiều orchestrator)."""
        now = now or time.time()
        res = P.resource_of(stage)
        res_states = [s.running_state for s in P.STAGES if P.resource_of(s) == res]
        out: list[Claim] = []
        with self._tx() as c:
            running = c.execute(
                f"SELECT COUNT(*) FROM jobs WHERE state IN ({','.join('?' * len(res_states))})", res_states
            ).fetchone()[0]
            n = min(limit, resource_limit - running)
            if n <= 0:
                return []
            rows = c.execute(
                "SELECT id, params FROM jobs WHERE state=? AND (not_before IS NULL OR not_before<=?) "
                "ORDER BY priority DESC, seq LIMIT ?", (stage.queue_state, now, n)).fetchall()
            for r in rows:
                attempt = c.execute("SELECT COUNT(*) FROM stage_runs WHERE job_id=? AND stage=?",
                                    (r["id"], stage.name)).fetchone()[0] + 1
                self._move(c, r["id"], stage.queue_state, stage.running_state, now, stage.name, attempt,
                           "claimed", lease_owner=owner, lease_until=now + lease_s, not_before=None)
                run_id = c.execute(
                    "INSERT INTO stage_runs(job_id,stage,attempt,status,owner,started_at) VALUES(?,?,?,?,?,?)",
                    (r["id"], stage.name, attempt, "running", owner, now)).lastrowid
                out.append(Claim(r["id"], stage, attempt, run_id, json.loads(r["params"])))
        return out

    def heartbeat(self, owner: str, lease_s: float, now: float | None = None) -> int:
        now = now or time.time()
        with self._tx() as c:
            return c.execute(
                f"UPDATE jobs SET lease_until=? WHERE lease_owner=? AND state IN ({','.join('?' * len(_RUNNING_STATES))})",
                (now + lease_s, owner, *_RUNNING_STATES)).rowcount

    def recover_expired(self, max_interruptions: int, now: float | None = None) -> list[tuple[str, str]]:
        """Job đang ở running_state mà lease đã hết hạn (tiến trình chết) => xếp lại hàng."""
        now = now or time.time()
        acted: list[tuple[str, str]] = []
        with self._tx() as c:
            rows = c.execute(
                f"SELECT id, state FROM jobs WHERE lease_until < ? AND state IN ({','.join('?' * len(_RUNNING_STATES))})",
                (now, *_RUNNING_STATES)).fetchall()
            for r in rows:
                stage = P.BY_RUNNING[r["state"]]
                c.execute("UPDATE stage_runs SET status='interrupted', ended_at=? "
                          "WHERE job_id=? AND stage=? AND status='running'", (now, r["id"], stage.name))
                last = [x[0] for x in c.execute(
                    "SELECT status FROM stage_runs WHERE job_id=? AND stage=? ORDER BY id DESC LIMIT ?",
                    (r["id"], stage.name, max_interruptions))]
                if len(last) >= max_interruptions and all(s == "interrupted" for s in last):
                    err = StageError(ErrorClass.RESOURCE, "INTERRUPTED_REPEATEDLY",
                                     f"stage {stage.name} bị ngắt {max_interruptions} lần liên tiếp").to_dict()
                    self._move(c, r["id"], r["state"], P.FAILED, now, stage.name, note="interrupted_repeatedly",
                               failed_stage=stage.name, last_error=json.dumps(err), lease_owner=None, lease_until=None)
                    acted.append((r["id"], "failed"))
                else:
                    self._move(c, r["id"], r["state"], stage.queue_state, now, stage.name, note="lease_expired",
                               lease_owner=None, lease_until=None)
                    acted.append((r["id"], "requeued"))
        return acted

    # -- stage lifecycle --------------------------------------------------------------------
    def set_run_key(self, run_id: int, stage_key: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE stage_runs SET stage_key=? WHERE id=?", (stage_key, run_id))

    def inputs(self, job_id: str, kinds: tuple[str, ...]) -> dict[str, list[ArtifactRef]]:
        out: dict[str, list[ArtifactRef]] = {k: [] for k in kinds}
        for r in self._q("SELECT kind,path,sha256,bytes,meta FROM artifacts WHERE job_id=? ORDER BY id", (job_id,)):
            if r["kind"] in out:
                out[r["kind"]].append({"path": r["path"], "kind": r["kind"], "sha256": r["sha256"],
                                       "bytes": r["bytes"], "meta": json.loads(r["meta"])})
        return out

    def _owned(self, c: sqlite3.Connection, claim: Claim, owner: str) -> bool:
        r = c.execute("SELECT state, lease_owner FROM jobs WHERE id=?", (claim.job_id,)).fetchone()
        return bool(r) and r["state"] == claim.stage.running_state and r["lease_owner"] == owner

    def succeed(self, claim: Claim, owner: str, artifacts: list[ArtifactRef], data: dict,
                now: float | None = None) -> bool:
        """CHECKPOINT: artifact + stage_run + chuyển state trong một transaction."""
        now = now or time.time()
        with self._tx() as c:
            if not self._owned(c, claim, owner):
                return False
            for a in artifacts:
                c.execute("INSERT INTO artifacts(job_id,stage,run_id,kind,path,sha256,bytes,meta,created_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                          (claim.job_id, claim.stage.name, claim.run_id, a["kind"], a["path"], a["sha256"],
                           a["bytes"], json.dumps(a["meta"], ensure_ascii=False), now))
            c.execute("UPDATE stage_runs SET status='succeeded', ended_at=?, data=? WHERE id=?",
                      (now, json.dumps(data, ensure_ascii=False), claim.run_id))
            self._move(c, claim.job_id, claim.stage.running_state, claim.stage.done_state, now,
                       claim.stage.name, claim.attempt, "succeeded", lease_owner=None, lease_until=None,
                       retry_used=0, not_before=None, failed_stage=None, last_error=None)
        return True

    def fail(self, claim: Claim, owner: str, err: StageError, max_attempts: int, backoff_s: list[float],
             now: float | None = None) -> str:
        """Trả 'retry' (xếp lại hàng sau backoff), 'failed' (FAILED, chờ retry thủ công) hoặc 'lost'."""
        now = now or time.time()
        with self._tx() as c:
            if not self._owned(c, claim, owner):
                return "lost"
            used = c.execute("SELECT retry_used FROM jobs WHERE id=?", (claim.job_id,)).fetchone()[0]
            c.execute("UPDATE stage_runs SET status='failed', ended_at=?, error=? WHERE id=?",
                      (now, json.dumps(err.to_dict(), ensure_ascii=False), claim.run_id))
            if err.error_class == ErrorClass.TRANSIENT and used + 1 < max_attempts:
                delay = backoff_s[min(used, len(backoff_s) - 1)] if backoff_s else 0
                self._move(c, claim.job_id, claim.stage.running_state, claim.stage.queue_state, now,
                           claim.stage.name, claim.attempt, f"retry in {delay}s: {err.code}",
                           lease_owner=None, lease_until=None, retry_used=used + 1, not_before=now + delay)
                return "retry"
            self._move(c, claim.job_id, claim.stage.running_state, P.FAILED, now, claim.stage.name,
                       claim.attempt, f"failed: {err.code}", lease_owner=None, lease_until=None,
                       failed_stage=claim.stage.name, last_error=json.dumps(err.to_dict(), ensure_ascii=False))
            return "failed"

    def release(self, claim: Claim, owner: str, now: float | None = None) -> bool:
        """Dừng có chủ đích (shutdown): trả job về hàng, không tính vào retry."""
        now = now or time.time()
        with self._tx() as c:
            if not self._owned(c, claim, owner):
                return False
            c.execute("UPDATE stage_runs SET status='cancelled', ended_at=? WHERE id=?", (now, claim.run_id))
            self._move(c, claim.job_id, claim.stage.running_state, claim.stage.queue_state, now,
                       claim.stage.name, claim.attempt, "released", lease_owner=None, lease_until=None)
        return True

    def retry_failed(self, job_id: str, now: float | None = None) -> str:
        """Retry thủ công: chỉ đưa job về hàng đợi của đúng stage đã lỗi; artifact các stage trước giữ nguyên."""
        now = now or time.time()
        with self._tx() as c:
            r = c.execute("SELECT state, failed_stage FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not r or r["state"] != P.FAILED:
                raise ValueError(f"job {job_id} không ở trạng thái FAILED")
            stage = P.BY_NAME[r["failed_stage"]]
            self._move(c, job_id, P.FAILED, stage.queue_state, now, stage.name, note="manual_retry",
                       retry_used=0, not_before=None, failed_stage=None, last_error=None)
        return stage.name

    # -- đọc để hiển thị / manifest ----------------------------------------------------------
    def stage_runs(self, job_id: str) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM stage_runs WHERE job_id=? ORDER BY id", (job_id,))]

    def artifacts(self, job_id: str) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM artifacts WHERE job_id=? ORDER BY id", (job_id,))]

    def transitions(self, job_id: str) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM transitions WHERE job_id=? ORDER BY id", (job_id,))]
