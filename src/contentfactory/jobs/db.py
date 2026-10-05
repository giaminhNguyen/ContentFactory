"""SQLite job store: nguồn sự thật cho state, lease, stage run, artifact, hold, checkpoint, lịch sử chuyển trạng thái.

Mọi thay đổi state đi qua `_move` (kiểm tra bảng chuyển hợp lệ + compare-and-swap theo state hiện tại)
và nằm trong một transaction cùng với artifact/stage_run tương ứng => checkpoint nguyên tử:
hoặc stage được ghi nhận xong cùng artifact, hoặc không gì cả.

Schema có phiên bản (`PRAGMA user_version`). SCHEMA là bản v0 (Phase 1) và KHÔNG đổi: DB cũ và DB mới đều đi qua cùng
một đường migration (`_migrate`), nên đường nâng cấp được dùng và test thật. Migration chỉ THÊM (cột/bảng), chạy trong một
transaction, và sao lưu DB trước nếu đã có job (D-50).
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..contracts import ArtifactRef, ErrorClass, StageError
from . import pipeline as P
from .policy import RetryPolicy, outcome_for

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

SCHEMA_VERSION = 1
# v1 (Phase 2.9): điều khiển job (start/target stage), hold + auto resume, checkpoint, config snapshot, resource monitor
V1_JOB_COLUMNS = (
    ("start_stage", "TEXT"), ("target_stage", "TEXT"), ("target_idx", "INTEGER"),
    ("hold_reason", "TEXT"), ("hold_detail", "TEXT"), ("hold_since", "REAL"), ("resume_after", "REAL"),
    ("hold_sig", "TEXT"), ("auto_resumes_without_progress", "INTEGER NOT NULL DEFAULT 0"),
    ("needs_user", "INTEGER NOT NULL DEFAULT 0"), ("auto_resume", "INTEGER"),
    ("config_snapshot", "TEXT"), ("config_hash", "TEXT"), ("config_revision", "INTEGER NOT NULL DEFAULT 0"),
    ("checkpoint", "TEXT"), ("progress", "TEXT"),
)
V1_SQL = (
    "CREATE INDEX IF NOT EXISTS jobs_hold ON jobs(hold_reason)",
    """CREATE TABLE IF NOT EXISTS resource_status(
         resource TEXT PRIMARY KEY, ok INTEGER NOT NULL, detail TEXT, checked_at REAL, next_check_at REAL,
         retry_after REAL, failures INTEGER NOT NULL DEFAULT 0)""",
)

_RUNNING_STATES = tuple(s.running_state for s in P.STAGES)


@dataclass
class Claim:
    job_id: str
    stage: P.Stage
    attempt: int
    run_id: int
    params: dict
    snapshot: dict | None = None        # config snapshot của job (None với job tạo trước migration)
    target_idx: int | None = None


class LostLease(Exception):
    """Job không còn thuộc về owner này (lease hết hạn và bị nhận lại)."""


def _sig(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


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
        self._migrate()

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

    # -- migration --------------------------------------------------------------------------
    def schema_version(self) -> int:
        return self._q("PRAGMA user_version")[0][0]

    def _migrate(self) -> None:
        if self.schema_version() >= SCHEMA_VERSION:
            return
        c = self._connect()
        try:
            old = c.execute("PRAGMA user_version").fetchone()[0]
            if c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] > 0:        # DB đang có dữ liệu: sao lưu nhất quán trước
                bak = Path(f"{self.path}.bak-v{old}")
                if not bak.exists():
                    dst = sqlite3.connect(bak)
                    try:
                        c.backup(dst)
                    finally:
                        dst.close()
            c.execute("BEGIN IMMEDIATE")                                         # tuần tự hóa nhiều tiến trình khởi động cùng lúc
            try:
                if c.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                    have = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
                    for name, decl in V1_JOB_COLUMNS:
                        if name not in have:
                            c.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
                    for sql in V1_SQL:
                        c.execute(sql)
                    c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
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

    @staticmethod
    def _note(c: sqlite3.Connection, job_id: str, state: str, now: float, note: str, stage: str | None = None) -> None:
        """Ghi vào lịch sử một sự kiện KHÔNG đổi state (hold, resume, đổi target, ...)."""
        c.execute("INSERT INTO transitions(ts,job_id,from_state,to_state,stage,note) VALUES(?,?,?,?,?,?)",
                  (now, job_id, state, state, stage, note))

    # -- jobs -------------------------------------------------------------------------------
    def create_job(self, params: dict, priority: int = 0, now: float | None = None, *, state: str = P.NEW,
                   start_stage: str | None = None, target_stage: str | None = None,
                   auto_resume: bool | None = None, snapshot: dict | None = None, config_hash: str | None = None) -> str:
        now = now or time.time()
        target_idx = P.INDEX[target_stage] if target_stage else None
        with self._tx() as c:
            seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM jobs").fetchone()[0]
            job_id = f"{seq:06d}"
            c.execute("INSERT INTO jobs(id,seq,created_at,updated_at,state,params,priority,start_stage,target_stage,"
                      "target_idx,auto_resume,config_snapshot,config_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (job_id, seq, now, now, state, json.dumps(params, ensure_ascii=False), priority, start_stage,
                       target_stage, target_idx, None if auto_resume is None else int(auto_resume),
                       json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None, config_hash))
            c.execute("INSERT INTO transitions(ts,job_id,from_state,to_state,note) VALUES(?,?,NULL,?,?)",
                      (now, job_id, state, "created"))
        return job_id

    @staticmethod
    def _job(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["params"] = json.loads(d["params"])
        d["last_error"] = json.loads(d["last_error"]) if d["last_error"] else None
        d["checkpoint"] = json.loads(d["checkpoint"]) if d.get("checkpoint") else {}
        d["config_snapshot"] = json.loads(d["config_snapshot"]) if d.get("config_snapshot") else None
        d["auto_resume"] = None if d.get("auto_resume") is None else bool(d["auto_resume"])
        d["needs_user"] = bool(d.get("needs_user"))
        return d

    def get_job(self, job_id: str) -> dict | None:
        rows = self._q("SELECT * FROM jobs WHERE id=?", (job_id,))
        return self._job(rows[0]) if rows else None

    def list_jobs(self) -> list[dict]:
        return [self._job(r) for r in self._q("SELECT * FROM jobs ORDER BY seq")]

    def discard_job(self, job_id: str) -> None:
        """Dọn dẹp một job vừa tạo mà khâu khởi tạo (import artifact) thất bại; không dùng cho job đã chạy."""
        with self._tx() as c:
            for t in ("artifacts", "stage_runs", "transitions"):
                c.execute(f"DELETE FROM {t} WHERE job_id=?", (job_id,))
            c.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    @staticmethod
    def is_active(job: dict) -> bool:
        """Còn việc để chạy ngay: chưa terminal, chưa đạt target, chưa bị giữ."""
        return not (job["state"] in P.TERMINAL or job.get("hold_reason") or P.is_complete(job["state"], job.get("target_idx")))

    def nonterminal_count(self) -> int:
        """Số job còn ACTIVE (chạy được ngay hoặc đang chạy). Job đã đạt target, FAILED hoặc đang bị giữ không tính."""
        return sum(self.is_active(j) for j in self.list_jobs())

    def held_jobs(self) -> list[dict]:
        return [self._job(r) for r in self._q("SELECT * FROM jobs WHERE hold_reason IS NOT NULL ORDER BY seq")]

    # -- scheduling -------------------------------------------------------------------------
    def claim(self, stage: P.Stage, limit: int, resource_limit: int, owner: str,
              lease_s: float, now: float | None = None) -> list[Claim]:
        """Nhận tối đa `limit` job đang xếp hàng ở `stage` (không bị giữ, stage chưa vượt target), không vượt
        `resource_limit` job đang chạy trên cùng tài nguyên (đếm trong cùng transaction nên đúng cả khi có nhiều orchestrator)."""
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
                "SELECT id, params, config_snapshot, target_idx FROM jobs WHERE state=? AND hold_reason IS NULL "
                "AND (not_before IS NULL OR not_before<=?) AND (target_idx IS NULL OR target_idx>=?) "
                "ORDER BY priority DESC, seq LIMIT ?", (stage.queue_state, now, P.INDEX[stage.name], n)).fetchall()
            for r in rows:
                attempt = c.execute("SELECT COUNT(*) FROM stage_runs WHERE job_id=? AND stage=?",
                                    (r["id"], stage.name)).fetchone()[0] + 1
                self._move(c, r["id"], stage.queue_state, stage.running_state, now, stage.name, attempt,
                           "claimed", lease_owner=owner, lease_until=now + lease_s, not_before=None)
                run_id = c.execute(
                    "INSERT INTO stage_runs(job_id,stage,attempt,status,owner,started_at) VALUES(?,?,?,?,?,?)",
                    (r["id"], stage.name, attempt, "running", owner, now)).lastrowid
                out.append(Claim(r["id"], stage, attempt, run_id, json.loads(r["params"]),
                                 json.loads(r["config_snapshot"]) if r["config_snapshot"] else None, r["target_idx"]))
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
                now: float | None = None, status: str = "succeeded") -> bool:
        """CHECKPOINT: artifact + stage_run + chuyển state trong một transaction.
        `status="skipped"` (artifact đầu ra đã hợp lệ sẵn): không có artifact mới, state vẫn tiến tới done_state."""
        now = now or time.time()
        with self._tx() as c:
            if not self._owned(c, claim, owner):
                return False
            for a in artifacts:
                c.execute("INSERT INTO artifacts(job_id,stage,run_id,kind,path,sha256,bytes,meta,created_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                          (claim.job_id, claim.stage.name, claim.run_id, a["kind"], a["path"], a["sha256"],
                           a["bytes"], json.dumps(a["meta"], ensure_ascii=False), now))
            c.execute("UPDATE stage_runs SET status=?, ended_at=?, data=? WHERE id=?",
                      (status, now, json.dumps(data, ensure_ascii=False), claim.run_id))
            self._move(c, claim.job_id, claim.stage.running_state, claim.stage.done_state, now,
                       claim.stage.name, claim.attempt, status, lease_owner=None, lease_until=None,
                       retry_used=0, not_before=None, failed_stage=None, last_error=None,
                       hold_sig=None, auto_resumes_without_progress=0, needs_user=0)
        return True

    def handle_error(self, claim: Claim, owner: str, err: StageError, policy: RetryPolicy,
                     now: float | None = None, rand=random.random) -> str:
        """Quyết định số phận job sau lỗi stage (D-37, D-40). Trả 'retry' | 'hold' | 'failed' | 'lost'.

        retry : xếp lại hàng sau backoff (có jitter, không nhỏ hơn Retry-After), tiêu một lượt retry
        hold  : lỗi tài nguyên TẠM THỜI: `state` giữ nguyên vị trí (về queue_state), hold_reason được đặt,
                KHÔNG tiêu retry, KHÔNG phải lỗi vĩnh viễn
        failed: FAILED_PERMANENT (state FAILED + failed_stage), chờ người sửa rồi retry thủ công
        """
        now = now or time.time()
        with self._tx() as c:
            if not self._owned(c, claim, owner):
                return "lost"
            j = c.execute("SELECT retry_used, checkpoint, hold_sig, auto_resumes_without_progress FROM jobs WHERE id=?",
                          (claim.job_id,)).fetchone()
            used = j["retry_used"]
            o = outcome_for(err, used, policy, now, rand)
            err_json = json.dumps(err.to_dict(), ensure_ascii=False)
            c.execute("UPDATE stage_runs SET status=?, ended_at=?, error=? WHERE id=?",
                      ("held" if o.action == "hold" else "failed", now, err_json, claim.run_id))
            s = claim.stage
            if o.action == "retry":
                self._move(c, claim.job_id, s.running_state, s.queue_state, now, s.name, claim.attempt, o.note,
                           lease_owner=None, lease_until=None, retry_used=used + 1, not_before=now + o.delay)
            elif o.action == "hold":
                sig = _sig(json.loads(j["checkpoint"]) if j["checkpoint"] else {})
                # hai lần hold liên tiếp mà checkpoint không tiến thêm => đếm "auto resume không tiến triển"
                count = j["auto_resumes_without_progress"] + 1 if j["hold_sig"] == sig else 0
                self._move(c, claim.job_id, s.running_state, s.queue_state, now, s.name, claim.attempt,
                           f"{o.reason}: {err.code}", lease_owner=None, lease_until=None, not_before=None,
                           hold_reason=o.reason, hold_detail=f"{err.code}: {err.message}"[:500], hold_since=now,
                           resume_after=o.resume_after, hold_sig=sig, auto_resumes_without_progress=count,
                           last_error=err_json)
            else:
                self._move(c, claim.job_id, s.running_state, P.FAILED, now, s.name, claim.attempt, o.note,
                           lease_owner=None, lease_until=None, failed_stage=s.name, last_error=err_json)
            return o.action

    def fail(self, claim: Claim, owner: str, err: StageError, max_attempts: int, backoff_s: list[float],
             now: float | None = None) -> str:
        """API Phase 1 (tương thích): backoff cố định, không jitter. Dùng `handle_error` cho chính sách đầy đủ."""
        pol = RetryPolicy(max_attempts=max_attempts, backoff_s=tuple(backoff_s), jitter=0.0, floor_s=0.0)
        return self.handle_error(claim, owner, err, pol, now)

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
                       retry_used=0, not_before=None, failed_stage=None, last_error=None, hold_reason=None,
                       hold_detail=None, hold_since=None, resume_after=None, needs_user=0,
                       auto_resumes_without_progress=0, hold_sig=None)
        return stage.name

    # -- hold / resume ----------------------------------------------------------------------
    def release_hold(self, job_id: str, now: float | None = None, *, auto: bool = False,
                     max_no_progress: int = 5) -> str:
        """Đưa job bị giữ trở lại hàng đợi. Trả 'released' | 'blocked' | 'not_held'.

        auto=True (Auto Resume): từ chối ('blocked', đặt needs_user) khi đã auto-resume `max_no_progress` lần liên tiếp mà
        checkpoint không tiến thêm: KHÔNG chuyển FAILED và KHÔNG lặp vô hạn. auto=False (người dùng Resume): luôn được,
        và xóa needs_user."""
        now = now or time.time()
        with self._tx() as c:
            j = c.execute("SELECT state, hold_reason, needs_user, auto_resumes_without_progress FROM jobs WHERE id=?",
                          (job_id,)).fetchone()
            if not j or not j["hold_reason"]:
                return "not_held"
            if auto and (j["needs_user"] or j["auto_resumes_without_progress"] >= max_no_progress):
                if not j["needs_user"]:
                    c.execute("UPDATE jobs SET needs_user=1, updated_at=? WHERE id=?", (now, job_id))
                    self._note(c, job_id, j["state"], now, f"auto resume dừng: {j['auto_resumes_without_progress']} lần không tiến triển")
                return "blocked"
            c.execute("UPDATE jobs SET hold_reason=NULL, hold_detail=NULL, hold_since=NULL, resume_after=NULL, "
                      "updated_at=?" + ("" if auto else ", needs_user=0, auto_resumes_without_progress=0") +
                      " WHERE id=?", (now, job_id))
            self._note(c, job_id, j["state"], now, f"resumed ({'auto' if auto else 'manual'}) from {j['hold_reason']}")
        return "released"

    def update_hold_detail(self, job_id: str, detail: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE jobs SET hold_detail=? WHERE id=? AND hold_reason IS NOT NULL", (detail[:500], job_id))

    # -- checkpoint, import, cấu hình -------------------------------------------------------
    def set_checkpoint(self, job_id: str, stage: str, info: dict, now: float | None = None) -> None:
        now = now or time.time()
        with self._tx() as c:
            r = c.execute("SELECT checkpoint FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not r:
                return
            cp = json.loads(r["checkpoint"]) if r["checkpoint"] else {}
            cp[stage] = {**info, "updated": now}
            done, total = info.get("done"), info.get("total")
            label = f"{stage} {done}/{total}" if total else (f"{stage} {done}" if done is not None else stage)
            c.execute("UPDATE jobs SET checkpoint=?, progress=? WHERE id=?",
                      (json.dumps(cp, ensure_ascii=False), label + (f" {info['detail']}" if info.get("detail") else ""), job_id))

    def add_artifacts(self, job_id: str, stage: str, artifacts: list[ArtifactRef], now: float | None = None) -> None:
        """Đăng ký artifact không do một stage_run sinh ra (stage giả `import`)."""
        now = now or time.time()
        with self._tx() as c:
            for a in artifacts:
                c.execute("INSERT INTO artifacts(job_id,stage,run_id,kind,path,sha256,bytes,meta,created_at) "
                          "VALUES(?,?,NULL,?,?,?,?,?,?)",
                          (job_id, stage, a["kind"], a["path"], a["sha256"], a["bytes"],
                           json.dumps(a["meta"], ensure_ascii=False), now))

    def set_target(self, job_id: str, target_stage: str, now: float | None = None) -> None:
        now = now or time.time()
        with self._tx() as c:
            j = c.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            c.execute("UPDATE jobs SET target_stage=?, target_idx=?, updated_at=? WHERE id=?",
                      (target_stage, P.INDEX[target_stage], now, job_id))
            self._note(c, job_id, j["state"], now, f"target_stage -> {target_stage}")

    def set_auto_resume(self, job_id: str, value: bool, now: float | None = None) -> None:
        now = now or time.time()
        with self._tx() as c:
            j = c.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            c.execute("UPDATE jobs SET auto_resume=?, updated_at=? WHERE id=?", (int(value), now, job_id))
            self._note(c, job_id, j["state"], now, f"auto_resume -> {value}")

    def set_snapshot(self, job_id: str, snapshot: dict, config_hash: str, revision: int, now: float | None = None) -> None:
        now = now or time.time()
        with self._tx() as c:
            j = c.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            c.execute("UPDATE jobs SET config_snapshot=?, config_hash=?, config_revision=?, updated_at=? WHERE id=?",
                      (json.dumps(snapshot, ensure_ascii=False), config_hash, revision, now, job_id))
            self._note(c, job_id, j["state"], now, f"config revision {revision} ({config_hash[:8]})")

    # -- Resource Monitor -------------------------------------------------------------------
    def put_resource_status(self, resource: str, ok: bool, detail: str, checked_at: float, next_check_at: float,
                            retry_after: float | None, failures: int) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO resource_status(resource,ok,detail,checked_at,next_check_at,retry_after,failures) "
                      "VALUES(?,?,?,?,?,?,?) ON CONFLICT(resource) DO UPDATE SET ok=excluded.ok, detail=excluded.detail, "
                      "checked_at=excluded.checked_at, next_check_at=excluded.next_check_at, "
                      "retry_after=excluded.retry_after, failures=excluded.failures",
                      (resource, int(ok), detail, checked_at, next_check_at, retry_after, failures))

    def get_resource_status(self, resource: str) -> dict | None:
        rows = self._q("SELECT * FROM resource_status WHERE resource=?", (resource,))
        return {**dict(rows[0]), "ok": bool(rows[0]["ok"])} if rows else None

    def list_resource_status(self) -> list[dict]:
        return [{**dict(r), "ok": bool(r["ok"])} for r in self._q("SELECT * FROM resource_status ORDER BY resource")]

    # -- đọc để hiển thị / manifest ----------------------------------------------------------
    def stage_runs(self, job_id: str) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM stage_runs WHERE job_id=? ORDER BY id", (job_id,))]

    def artifacts(self, job_id: str) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM artifacts WHERE job_id=? ORDER BY id", (job_id,))]

    def transitions(self, job_id: str) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM transitions WHERE job_id=? ORDER BY id", (job_id,))]
