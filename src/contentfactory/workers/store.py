"""Lưu trữ Worker Runtime: sqlite riêng `runtime/workers.db`.

Khác với `jobs/db.py` (bảng trong `contentfactory.db`), `workers` là package ISOLATED nên
KHÔNG import `jobs.db` được, và tách DB riêng tránh đụng version migration của pipeline.
Làm theo cùng mẫu: WAL + `busy_timeout` + khoá `BEGIN IMMEDIATE` khi migrate.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import (
    Attempt,
    AttemptState,
    PoolStrategy,
    Worker,
    WorkerModel,
    WorkerPool,
    WorkerStatus,
)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers(
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  driver_id     TEXT NOT NULL,
  executable    TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'DETECTED',
  enabled       INTEGER NOT NULL DEFAULT 1,
  source        TEXT NOT NULL DEFAULT 'manual',
  models        TEXT NOT NULL DEFAULT '[]',
  profiles      TEXT NOT NULL DEFAULT '{}',
  concurrency   INTEGER NOT NULL DEFAULT 1,
  timeout_s     REAL NOT NULL DEFAULT 3600,
  failure_streak INTEGER NOT NULL DEFAULT 0,
  cooldown_until REAL NOT NULL DEFAULT 0,
  meta          TEXT NOT NULL DEFAULT '{}',
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pools(
  name         TEXT PRIMARY KEY,
  display_name TEXT NOT NULL DEFAULT '',
  strategy     TEXT NOT NULL DEFAULT 'priority',
  members      TEXT NOT NULL DEFAULT '[]',
  enabled      INTEGER NOT NULL DEFAULT 1,
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS routing(
  work_type     TEXT PRIMARY KEY,
  pool          TEXT NOT NULL DEFAULT '',
  model_profile TEXT NOT NULL DEFAULT '',
  policy        TEXT NOT NULL DEFAULT '{}',
  updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts(
  attempt_id    TEXT PRIMARY KEY,
  work_type     TEXT NOT NULL,
  worker_id     TEXT NOT NULL,
  worker_name   TEXT NOT NULL DEFAULT '',
  driver_id     TEXT NOT NULL DEFAULT '',
  model_id      TEXT,
  pool          TEXT NOT NULL DEFAULT '',
  job_id        TEXT NOT NULL DEFAULT '',
  stage         TEXT NOT NULL DEFAULT '',
  state         TEXT NOT NULL,
  started_at    REAL NOT NULL,
  ended_at      REAL,
  error_kind    TEXT NOT NULL DEFAULT '',
  error_code    TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  workspace     TEXT NOT NULL DEFAULT '',
  validation    TEXT NOT NULL DEFAULT '[]',
  promoted      INTEGER NOT NULL DEFAULT 0,
  session_id    TEXT,
  cost_usd      REAL NOT NULL DEFAULT 0,
  meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS attempts_job ON attempts(job_id, started_at);
CREATE INDEX IF NOT EXISTS attempts_work ON attempts(work_type, started_at);
"""


class WorkerStore:
    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(100):
            c = self._connect()
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.executescript(SCHEMA)
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() or attempt == 99:
                    raise
            finally:
                c.close()
            time.sleep(0.05 + 0.02 * (attempt % 5))
        self._migrate()

    # -- plumbing ------------------------------------------------------------------------
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

    def schema_version(self) -> int:
        return self._q("PRAGMA user_version")[0][0]

    def _migrate(self) -> None:
        if self.schema_version() >= SCHEMA_VERSION:
            return
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                if c.execute("PRAGMA user_version").fetchone()[0] < 1:
                    c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        finally:
            c.close()

    # -- workers -------------------------------------------------------------------------
    @staticmethod
    def _row_worker(r: sqlite3.Row) -> Worker:
        return Worker(
            id=r["id"], name=r["name"], driver_id=r["driver_id"], executable=r["executable"],
            status=WorkerStatus(r["status"]), enabled=bool(r["enabled"]), source=r["source"],
            models=[WorkerModel(**m) for m in json.loads(r["models"])],
            profiles=json.loads(r["profiles"]), concurrency=r["concurrency"], timeout_s=r["timeout_s"],
            failure_streak=r["failure_streak"], cooldown_until=r["cooldown_until"],
            meta=json.loads(r["meta"]), created_at=r["created_at"], updated_at=r["updated_at"],
        )

    def worker(self, worker_id: str) -> Worker | None:
        rows = self._q("SELECT * FROM workers WHERE id=?", (worker_id,))
        return self._row_worker(rows[0]) if rows else None

    def workers(self) -> list[Worker]:
        return [self._row_worker(r) for r in self._q("SELECT * FROM workers ORDER BY created_at, name")]

    def save_worker(self, w: Worker) -> Worker:
        with self._tx() as c:
            c.execute(
                "INSERT INTO workers(id,name,driver_id,executable,status,enabled,source,models,profiles,"
                "concurrency,timeout_s,failure_streak,cooldown_until,meta,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name,driver_id=excluded.driver_id,"
                " executable=excluded.executable,status=excluded.status,enabled=excluded.enabled,"
                " source=excluded.source,models=excluded.models,profiles=excluded.profiles,"
                " concurrency=excluded.concurrency,timeout_s=excluded.timeout_s,"
                " failure_streak=excluded.failure_streak,cooldown_until=excluded.cooldown_until,"
                " meta=excluded.meta,updated_at=excluded.updated_at",
                (w.id, w.name, w.driver_id, w.executable, w.status.value, int(w.enabled), w.source,
                 json.dumps([m.__dict__ for m in w.models], ensure_ascii=False),
                 json.dumps(w.profiles, ensure_ascii=False), w.concurrency, w.timeout_s,
                 w.failure_streak, w.cooldown_until, json.dumps(w.meta, ensure_ascii=False),
                 w.created_at, w.updated_at),
            )
        return w

    def delete_worker(self, worker_id: str) -> bool:
        with self._tx() as c:
            cur = c.execute("DELETE FROM workers WHERE id=?", (worker_id,))
            return cur.rowcount > 0

    # -- pools ---------------------------------------------------------------------------
    @staticmethod
    def _row_pool(r: sqlite3.Row) -> WorkerPool:
        return WorkerPool(
            name=r["name"], display_name=r["display_name"], strategy=PoolStrategy(r["strategy"]),
            members=json.loads(r["members"]), enabled=bool(r["enabled"]),
            created_at=r["created_at"], updated_at=r["updated_at"],
        )

    def pool(self, name: str) -> WorkerPool | None:
        rows = self._q("SELECT * FROM pools WHERE name=?", (name,))
        return self._row_pool(rows[0]) if rows else None

    def pools(self) -> list[WorkerPool]:
        return [self._row_pool(r) for r in self._q("SELECT * FROM pools ORDER BY created_at, name")]

    def save_pool(self, p: WorkerPool) -> WorkerPool:
        with self._tx() as c:
            c.execute(
                "INSERT INTO pools(name,display_name,strategy,members,enabled,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET display_name=excluded.display_name,"
                " strategy=excluded.strategy,members=excluded.members,enabled=excluded.enabled,"
                " updated_at=excluded.updated_at",
                (p.name, p.display_name, p.strategy.value, json.dumps(p.members), int(p.enabled),
                 p.created_at, p.updated_at),
            )
        return p

    def delete_pool(self, name: str) -> bool:
        with self._tx() as c:
            cur = c.execute("DELETE FROM pools WHERE name=?", (name,))
            return cur.rowcount > 0

    # -- routing -------------------------------------------------------------------------
    def routing(self) -> dict[str, dict]:
        """{work_type: {"pool": str, "model_profile": str, "policy": dict}}"""
        return {
            r["work_type"]: {"pool": r["pool"], "model_profile": r["model_profile"],
                             "policy": json.loads(r["policy"])}
            for r in self._q("SELECT * FROM routing ORDER BY work_type")
        }

    def save_routing(self, work_type: str, cfg: dict) -> dict:
        pool = str(cfg.get("pool", ""))
        profile = str(cfg.get("model_profile", ""))
        policy = json.dumps(cfg.get("policy", {}), ensure_ascii=False)
        with self._tx() as c:
            c.execute(
                "INSERT INTO routing(work_type,pool,model_profile,policy,updated_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(work_type) DO UPDATE SET pool=excluded.pool,model_profile=excluded.model_profile,"
                " policy=excluded.policy,updated_at=excluded.updated_at",
                (work_type, pool, profile, policy, time.time()),
            )
        return {"pool": pool, "model_profile": profile, "policy": json.loads(policy)}

    def delete_routing(self, work_type: str) -> bool:
        with self._tx() as c:
            cur = c.execute("DELETE FROM routing WHERE work_type=?", (work_type,))
            return cur.rowcount > 0

    # -- attempts ------------------------------------------------------------------------
    @staticmethod
    def _row_attempt(r: sqlite3.Row) -> Attempt:
        return Attempt(
            attempt_id=r["attempt_id"], work_type=r["work_type"], worker_id=r["worker_id"],
            worker_name=r["worker_name"], driver_id=r["driver_id"], model_id=r["model_id"],
            pool=r["pool"], job_id=r["job_id"], stage=r["stage"], state=AttemptState(r["state"]),
            started_at=r["started_at"], ended_at=r["ended_at"], error_kind=r["error_kind"],
            error_code=r["error_code"], error_message=r["error_message"], workspace=r["workspace"],
            validation=json.loads(r["validation"]), promoted=bool(r["promoted"]),
            session_id=r["session_id"], cost_usd=r["cost_usd"], meta=json.loads(r["meta"]),
        )

    def add_attempt(self, a: Attempt) -> Attempt:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO attempts(attempt_id,work_type,worker_id,worker_name,driver_id,"
                "model_id,pool,job_id,stage,state,started_at,ended_at,error_kind,error_code,error_message,"
                "workspace,validation,promoted,session_id,cost_usd,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.attempt_id, a.work_type, a.worker_id, a.worker_name, a.driver_id, a.model_id, a.pool,
                 a.job_id, a.stage, a.state.value, a.started_at, a.ended_at, a.error_kind, a.error_code,
                 a.error_message, a.workspace, json.dumps(a.validation, ensure_ascii=False),
                 int(a.promoted), a.session_id, a.cost_usd, json.dumps(a.meta, ensure_ascii=False)),
            )
        return a

    def finish_attempt(self, a: Attempt) -> Attempt:
        return self.add_attempt(a)

    def attempts(self, job_id: str = "", work_type: str = "", limit: int = 100) -> list[Attempt]:
        """Lịch sử attempt (mới nhất trước). Không bao giờ xoá: fallback/retry tạo Attempt mới."""
        sql, args = "SELECT * FROM attempts", []
        if job_id:
            sql, args = sql + " WHERE job_id=?", [job_id]
        elif work_type:
            sql, args = sql + " WHERE work_type=?", [work_type]
        sql += " ORDER BY started_at DESC, attempt_id DESC LIMIT ?"
        args.append(limit)
        return [self._row_attempt(r) for r in self._q(sql, tuple(args))]

    def attempt(self, attempt_id: str) -> Attempt | None:
        rows = self._q("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        return self._row_attempt(rows[0]) if rows else None

    def running_counts(self) -> dict[str, int]:
        """Số attempt đang RUNNING theo worker — dùng cho strategy `least_busy`."""
        return {r["worker_id"]: r["n"] for r in
                self._q("SELECT worker_id, COUNT(*) AS n FROM attempts WHERE state='RUNNING'"
                        " GROUP BY worker_id")}
