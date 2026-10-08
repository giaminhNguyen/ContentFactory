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

SCHEMA_VERSION = 2

# Cột thêm ở schema v2 — tách riêng để `ALTER TABLE` cho db đã có (migration không mất dữ liệu).
_V2_COLUMNS = {
    "idle_timeout_s": "REAL NOT NULL DEFAULT 1800",
    "hard_timeout_s": "REAL NOT NULL DEFAULT 14400",
    "circuit_opened_at": "REAL NOT NULL DEFAULT 0",
    "last_error_kind": "TEXT NOT NULL DEFAULT ''",
    "last_error_code": "TEXT NOT NULL DEFAULT ''",
    "last_error_at": "REAL NOT NULL DEFAULT 0",
    "last_success_at": "REAL NOT NULL DEFAULT 0",
}

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
  idle_timeout_s     REAL NOT NULL DEFAULT 1800,
  hard_timeout_s     REAL NOT NULL DEFAULT 14400,
  failure_streak INTEGER NOT NULL DEFAULT 0,
  cooldown_until REAL NOT NULL DEFAULT 0,
  circuit_opened_at REAL NOT NULL DEFAULT 0,
  last_error_kind   TEXT NOT NULL DEFAULT '',
  last_error_code   TEXT NOT NULL DEFAULT '',
  last_error_at     REAL NOT NULL DEFAULT 0,
  last_success_at   REAL NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS worker_health(
  worker_id   TEXT NOT NULL,
  state       TEXT NOT NULL,
  changed_at  REAL NOT NULL,
  reason      TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(worker_id, changed_at)
);
CREATE INDEX IF NOT EXISTS attempts_job ON attempts(job_id, started_at);
CREATE INDEX IF NOT EXISTS attempts_work ON attempts(work_type, started_at);
CREATE INDEX IF NOT EXISTS attempts_worker ON attempts(worker_id, started_at);
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
                v = c.execute("PRAGMA user_version").fetchone()[0]
                if v < 2:
                    have = {r["name"] for r in c.execute("PRAGMA table_info(workers)")}
                    for col, ddl in _V2_COLUMNS.items():
                        if col not in have:                  # db cũ (v1) thiếu cột -> thêm
                            c.execute(f"ALTER TABLE workers ADD COLUMN {col} {ddl}")
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
            idle_timeout_s=r["idle_timeout_s"], hard_timeout_s=r["hard_timeout_s"],
            failure_streak=r["failure_streak"], cooldown_until=r["cooldown_until"],
            circuit_opened_at=r["circuit_opened_at"],
            last_error_kind=r["last_error_kind"], last_error_code=r["last_error_code"],
            last_error_at=r["last_error_at"], last_success_at=r["last_success_at"],
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
                "concurrency,timeout_s,idle_timeout_s,hard_timeout_s,failure_streak,cooldown_until,"
                "circuit_opened_at,last_error_kind,last_error_code,last_error_at,last_success_at,"
                "meta,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name,driver_id=excluded.driver_id,"
                " executable=excluded.executable,status=excluded.status,enabled=excluded.enabled,"
                " source=excluded.source,models=excluded.models,profiles=excluded.profiles,"
                " concurrency=excluded.concurrency,timeout_s=excluded.timeout_s,"
                " idle_timeout_s=excluded.idle_timeout_s,hard_timeout_s=excluded.hard_timeout_s,"
                " failure_streak=excluded.failure_streak,cooldown_until=excluded.cooldown_until,"
                " circuit_opened_at=excluded.circuit_opened_at,"
                " last_error_kind=excluded.last_error_kind,last_error_code=excluded.last_error_code,"
                " last_error_at=excluded.last_error_at,last_success_at=excluded.last_success_at,"
                " meta=excluded.meta,updated_at=excluded.updated_at",
                (w.id, w.name, w.driver_id, w.executable, w.status.value, int(w.enabled), w.source,
                 json.dumps([m.__dict__ for m in w.models], ensure_ascii=False),
                 json.dumps(w.profiles, ensure_ascii=False), w.concurrency, w.timeout_s,
                 w.idle_timeout_s, w.hard_timeout_s,
                 w.failure_streak, w.cooldown_until, w.circuit_opened_at,
                 w.last_error_kind, w.last_error_code, w.last_error_at, w.last_success_at,
                 json.dumps(w.meta, ensure_ascii=False), w.created_at, w.updated_at),
            )
        self._record_health(w)
        return w

    def _record_health(self, w: Worker) -> None:
        """W2.1: ghi transition health (state + reason) khi trạng thái ĐỔI — supply cho health trend/UI."""
        state, reason = w.health()
        last = self._q("SELECT state FROM worker_health WHERE worker_id=? ORDER BY changed_at DESC LIMIT 1",
                       (w.id,))
        if last and last[0]["state"] == state.value:
            return
        with self._tx() as c:
            c.execute("INSERT INTO worker_health(worker_id,state,changed_at,reason) VALUES(?,?,?,?)",
                      (w.id, state.value, time.time(), reason))

    def health_history(self, worker_id: str, limit: int = 20) -> list[dict]:
        """Lịch sử health transition (mới nhất trước) cho UI worker detail."""
        rows = self._q("SELECT * FROM worker_health WHERE worker_id=? ORDER BY changed_at DESC LIMIT ?",
                       (worker_id, limit))
        return [{"state": r["state"], "changed_at": r["changed_at"], "reason": r["reason"]} for r in rows]

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

    def attempts(self, job_id: str = "", work_type: str = "", worker_id: str = "", limit: int = 100) -> list[Attempt]:
        """Lịch sử attempt (mới nhất trước). Không bao giờ xoá: fallback/retry tạo Attempt mới."""
        sql, args = "SELECT * FROM attempts", []
        conds: list[str] = []
        if job_id:
            conds.append("job_id=?")
            args.append(job_id)
        elif work_type:
            conds.append("work_type=?")
            args.append(work_type)
        if worker_id:
            conds.append("worker_id=?")
            args.append(worker_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY started_at DESC, attempt_id DESC LIMIT ?"
        args.append(limit)
        return [self._row_attempt(r) for r in self._q(sql, tuple(args))]

    def attempt(self, attempt_id: str) -> Attempt | None:
        rows = self._q("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))
        return self._row_attempt(rows[0]) if rows else None

    def running_counts(self) -> dict[str, int]:
        """Số attempt đang RUNNING theo worker — dùng cho strategy `least_busy` + probe half-open."""
        return {r["worker_id"]: r["n"] for r in
                self._q("SELECT worker_id, COUNT(*) AS n FROM attempts WHERE state='RUNNING'"
                        " GROUP BY worker_id")}

    def running_attempts(self, limit: int = 200) -> list[Attempt]:
        """W2.5: mọi attempt còn RUNNING (bất kể worker) — để reconciler xử lý orphan sau crash."""
        rows = self._q("SELECT * FROM attempts WHERE state='RUNNING' ORDER BY started_at LIMIT ?", (limit,))
        return [self._row_attempt(r) for r in rows]

    # -- metrics (W2.7) -----------------------------------------------------------------------
    def worker_stats(self) -> dict[str, dict]:
        """Thống kê theo worker từ bảng attempts — trả lời "worker nào đang lỗi nhiều?" không cần raw log."""
        out: dict[str, dict] = {}
        rows = self._q(
            "SELECT worker_id, worker_name,"
            " COUNT(*) AS n,"
            " SUM(CASE WHEN state='SUCCESS' THEN 1 ELSE 0 END) AS ok,"
            " AVG(CASE WHEN ended_at IS NOT NULL THEN ended_at - started_at END) AS avg_dur,"
            " SUM(CASE WHEN error_kind='QUOTA' THEN 1 ELSE 0 END) AS quota,"
            " SUM(CASE WHEN error_kind='AUTH' THEN 1 ELSE 0 END) AS auth,"
            " SUM(CASE WHEN error_kind='TIMEOUT' THEN 1 ELSE 0 END) AS timeout,"
            " SUM(CASE WHEN error_kind='INVALID_OUTPUT' THEN 1 ELSE 0 END) AS invalid"
            " FROM attempts GROUP BY worker_id")
        for r in rows:
            n = r["n"] or 0
            out[r["worker_id"]] = {
                "name": r["worker_name"], "attempts": n,
                "success": int(r["ok"] or 0),
                "success_rate": round(100.0 * (r["ok"] or 0) / n, 1) if n else 0.0,
                "avg_duration_s": round(float(r["avg_dur"] or 0.0), 1),
                "failures": {"QUOTA": int(r["quota"] or 0), "AUTH": int(r["auth"] or 0),
                             "TIMEOUT": int(r["timeout"] or 0), "INVALID_OUTPUT": int(r["invalid"] or 0)},
            }
        return out

    def attempts_per_task(self, limit: int = 200) -> list[dict]:
        """Số attempt mỗi task (job_id + work_type) + có fallback hay không (nhiều worker khác nhau)."""
        rows = self._q(
            "SELECT job_id, work_type, COUNT(*) AS n, COUNT(DISTINCT worker_id) AS workers,"
            " SUM(CASE WHEN state='SUCCESS' THEN 1 ELSE 0 END) AS ok"
            " FROM attempts WHERE job_id != '' GROUP BY job_id, work_type"
            " ORDER BY MAX(started_at) DESC LIMIT ?", (limit,))
        return [{"job_id": r["job_id"], "work_type": r["work_type"], "attempts": r["n"],
                 "workers": r["workers"], "fallback": bool(r["workers"] > 1), "ok": bool(r["ok"] > 0)}
                for r in rows]
