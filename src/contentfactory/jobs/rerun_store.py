"""Lưu trữ Manual Rerun (D-113): phiên chạy lại có chọn stage (`rerun_sessions`) + các lần chạy thủ công trong `stage_runs` (cột `rerun_session_id`).

Khác biệt cốt lõi với retry (nằm trong cùng execution, KHÔNG tăng đếm) và với pipeline thường (máy trạng thái của job không đổi):
- một phiên = một lần người dùng bấm "Chạy lại các bước đã chọn"; mỗi job chỉ có TỐI ĐA MỘT phiên đang hoạt động (chỉ mục duy nhất từng phần) và cùng `request_id`
  gửi hai lần trả lại đúng phiên cũ => bấm đúp / hai tab không tạo hai phiên;
- mỗi stage của phiên có định danh thực thi riêng `<session>:<stage>` (xem orchestrator/rerun.py) nên cache/idempotency không phát lại kết quả cũ;
- kết quả chỉ thành "hiện hành" khi `commit_stage` chạy: artifact mới thay artifact cũ của đúng stage đó trong MỘT transaction; lỗi/hủy không động tới artifact cũ;
- `rerun_count` = số phiên khác nhau đã BẮT ĐẦU chạy stage đó (lần chạy đầu và retry/resume không tính).
"""
from __future__ import annotations

import json
import sqlite3
import time

from ..contracts import ArtifactRef, ErrorClass, StageError
from . import pipeline as P

ACTIVE = ("queued", "running")
FINISHED = ("succeeded", "failed", "cancelled")
V8_RUN_COLUMNS = (("rerun_session_id", "TEXT"),)
V8_SQL = (
    """CREATE TABLE IF NOT EXISTS rerun_sessions(
         id TEXT PRIMARY KEY, seq INTEGER UNIQUE NOT NULL, job_id TEXT NOT NULL, number INTEGER NOT NULL, request_id TEXT,
         state TEXT NOT NULL, requested TEXT NOT NULL, stages TEXT NOT NULL, trigger TEXT NOT NULL DEFAULT 'manual',
         created_at REAL NOT NULL, started_at REAL, ended_at REAL, owner TEXT, lease_until REAL, error TEXT, UNIQUE(job_id, number))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS rerun_sessions_request ON rerun_sessions(job_id, request_id) WHERE request_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS rerun_sessions_active ON rerun_sessions(job_id) WHERE state IN ('queued','running')",
    "CREATE INDEX IF NOT EXISTS stage_runs_rerun ON stage_runs(rerun_session_id)",
)


def _err(code: str, msg: str, hint: str = "", **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, msg, {**detail, **({"hint": hint} if hint else {})}, resource="input")


def _session(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["requested"] = json.loads(d["requested"])
    d["stages"] = json.loads(d["stages"])
    d["error"] = json.loads(d["error"]) if d["error"] else None
    return d


class RerunStore:
    def __init__(self, store) -> None:
        self.s = store

    # -- phiên -------------------------------------------------------------------------------------
    def create(self, job_id: str, stages: list[str], request_id: str | None = None, trigger: str = "manual", now: float | None = None) -> tuple[dict, bool]:
        """Tạo phiên `queued` nguyên tử. Trả (phiên, created); cùng (job, request_id) trả phiên cũ. Ném StageError JOB_NOT_FOUND | JOB_BUSY | RERUN_BUSY."""
        now = now or time.time()
        try:
            with self.s._tx() as c:
                if request_id:
                    old = c.execute("SELECT * FROM rerun_sessions WHERE job_id=? AND request_id=?", (job_id, request_id)).fetchone()
                    if old is not None:
                        return _session(old), False
                j = c.execute("SELECT state, control_state, lease_owner FROM jobs WHERE id=?", (job_id,)).fetchone()
                if j is None or j["control_state"] == "DELETED":
                    raise _err("JOB_NOT_FOUND", f"Không có job {job_id}.", "Quay lại danh sách job.")
                if j["state"] in P.BY_RUNNING or j["lease_owner"]:
                    raise _err("JOB_BUSY", "Job đang chạy một bước nên chưa chạy lại được.", "Chờ bước hiện tại xong hoặc tạm dừng job rồi thử lại.")
                if c.execute("SELECT 1 FROM rerun_sessions WHERE job_id=? AND state IN ('queued','running')", (job_id,)).fetchone():
                    raise _err("RERUN_BUSY", "Job đang có một lượt chạy lại chưa xong.", "Chờ lượt đó kết thúc rồi chọn lại.")
                seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM rerun_sessions").fetchone()[0]
                number = c.execute("SELECT COALESCE(MAX(number),0)+1 FROM rerun_sessions WHERE job_id=?", (job_id,)).fetchone()[0]
                sid = f"R{seq:06d}"
                c.execute("INSERT INTO rerun_sessions(id,seq,job_id,number,request_id,state,requested,stages,trigger,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (sid, seq, job_id, number, request_id, "queued", json.dumps(stages), json.dumps({s: {"state": "pending", "attempts": 0} for s in stages}), trigger, now))
                self.s._note(c, job_id, j["state"], now, f"manual rerun #{number} ({sid}) queued: {', '.join(stages)}")
                c.execute("UPDATE jobs SET updated_at=? WHERE id=?", (now, job_id))
                return _session(c.execute("SELECT * FROM rerun_sessions WHERE id=?", (sid,)).fetchone()), True
        except sqlite3.IntegrityError:                                          # hai request cùng lúc: chỉ mục duy nhất chọn đúng một bên thắng
            old = self.by_request(job_id, request_id) if request_id else None
            if old is not None:
                return old, False
            raise _err("RERUN_BUSY", "Job đang có một lượt chạy lại chưa xong.", "Chờ lượt đó kết thúc rồi chọn lại.") from None

    def get(self, sid: str) -> dict | None:
        rows = self.s._q("SELECT * FROM rerun_sessions WHERE id=?", (sid,))
        return _session(rows[0]) if rows else None

    def by_request(self, job_id: str, request_id: str) -> dict | None:
        rows = self.s._q("SELECT * FROM rerun_sessions WHERE job_id=? AND request_id=?", (job_id, request_id))
        return _session(rows[0]) if rows else None

    def sessions(self, job_id: str) -> list[dict]:
        return [_session(r) for r in self.s._q("SELECT * FROM rerun_sessions WHERE job_id=? ORDER BY number DESC", (job_id,))]

    def active(self, job_id: str) -> dict | None:
        rows = self.s._q("SELECT * FROM rerun_sessions WHERE job_id=? AND state IN ('queued','running')", (job_id,))
        return _session(rows[0]) if rows else None

    def active_job_ids(self) -> set[str]:
        return {r["job_id"] for r in self.s._q("SELECT job_id FROM rerun_sessions WHERE state IN ('queued','running')")}

    def active_count(self) -> int:
        return int(self.s._q("SELECT COUNT(*) FROM rerun_sessions s JOIN jobs j ON j.id=s.job_id WHERE s.state IN ('queued','running') "
                             "AND j.control_state='RUNNING'")[0][0])

    def claim(self, limit: int, owner: str, lease_s: float, now: float | None = None) -> list[dict]:
        """Nhận phiên `queued` của job không bị Tạm dừng/Hủy/Xóa."""
        now = now or time.time()
        out = []
        with self.s._tx() as c:
            rows = c.execute("SELECT s.id FROM rerun_sessions s JOIN jobs j ON j.id=s.job_id WHERE s.state='queued' AND j.control_state='RUNNING' "
                             "ORDER BY s.seq LIMIT ?", (max(0, limit),)).fetchall()
            for r in rows:
                c.execute("UPDATE rerun_sessions SET state='running', owner=?, lease_until=?, started_at=COALESCE(started_at, ?) WHERE id=? AND state='queued'",
                          (owner, now + lease_s, now, r["id"]))
                out.append(_session(c.execute("SELECT * FROM rerun_sessions WHERE id=?", (r["id"],)).fetchone()))
        return out

    def heartbeat(self, owner: str, lease_s: float, now: float | None = None) -> int:
        now = now or time.time()
        with self.s._tx() as c:
            return c.execute("UPDATE rerun_sessions SET lease_until=? WHERE owner=? AND state='running'", (now + lease_s, owner)).rowcount

    def recover_expired(self, now: float | None = None) -> list[str]:
        """Phiên đang `running` mà lease hết hạn (tiến trình chết): xếp lại `queued`; lần chạy dở của stage được đánh dấu `interrupted` và stage về `pending`.
        Cùng định danh thực thi => publish resume đúng upload cũ, không đăng hai lần."""
        now = now or time.time()
        done = []
        with self.s._tx() as c:
            for r in c.execute("SELECT * FROM rerun_sessions WHERE state='running' AND lease_until < ?", (now,)).fetchall():
                d = _session(r)
                for st, info in d["stages"].items():
                    if info["state"] == "running":
                        info["state"] = "pending"
                c.execute("UPDATE stage_runs SET status='interrupted', ended_at=? WHERE rerun_session_id=? AND status='running'", (now, d["id"]))
                c.execute("UPDATE rerun_sessions SET state='queued', owner=NULL, lease_until=NULL, stages=? WHERE id=?", (json.dumps(d["stages"]), d["id"]))
                self.s._note(c, d["job_id"], c.execute("SELECT state FROM jobs WHERE id=?", (d["job_id"],)).fetchone()["state"], now,
                             f"manual rerun #{d['number']} ({d['id']}) interrupted -> requeued")
                done.append(d["id"])
        return done

    def set_stage(self, sid: str, stage: str, now: float | None = None, **fields) -> None:
        """Cập nhật trạng thái một stage của phiên (đọc-sửa-ghi trong transaction) + chạm updated_at của job để giao diện cập nhật."""
        now = now or time.time()
        with self.s._tx() as c:
            r = c.execute("SELECT stages, job_id FROM rerun_sessions WHERE id=?", (sid,)).fetchone()
            if r is None:
                return
            st = json.loads(r["stages"])
            st[stage] = {**st.get(stage, {}), **fields}
            c.execute("UPDATE rerun_sessions SET stages=? WHERE id=?", (json.dumps(st, ensure_ascii=False), sid))
            c.execute("UPDATE jobs SET updated_at=? WHERE id=?", (now, r["job_id"]))

    def finish(self, sid: str, state: str, error: dict | None = None, now: float | None = None) -> None:
        now = now or time.time()
        with self.s._tx() as c:
            r = c.execute("SELECT job_id, number FROM rerun_sessions WHERE id=?", (sid,)).fetchone()
            if r is None:
                return
            c.execute("UPDATE rerun_sessions SET state=?, ended_at=?, owner=NULL, lease_until=NULL, error=? WHERE id=?",
                      (state, now, json.dumps(error, ensure_ascii=False) if error else None, sid))
            js = c.execute("SELECT state FROM jobs WHERE id=?", (r["job_id"],)).fetchone()
            if js:
                self.s._note(c, r["job_id"], js["state"], now, f"manual rerun #{r['number']} ({sid}) {state}")
                c.execute("UPDATE jobs SET updated_at=? WHERE id=?", (now, r["job_id"]))

    def requeue(self, sid: str, now: float | None = None) -> None:
        """Dừng có chủ đích (Tạm dừng/shutdown): phiên về hàng đợi, các stage đã xong giữ nguyên, stage đang chạy về `pending`."""
        now = now or time.time()
        with self.s._tx() as c:
            r = c.execute("SELECT * FROM rerun_sessions WHERE id=?", (sid,)).fetchone()
            if r is None or r["state"] != "running":
                return
            d = _session(r)
            for info in d["stages"].values():
                if info["state"] == "running":
                    info["state"] = "pending"
            c.execute("UPDATE rerun_sessions SET state='queued', owner=NULL, lease_until=NULL, stages=? WHERE id=?", (json.dumps(d["stages"]), sid))
            c.execute("UPDATE jobs SET updated_at=? WHERE id=?", (now, d["job_id"]))

    # -- lần chạy thủ công của một stage ---------------------------------------------------------------
    def begin_run(self, sid: str, job_id: str, stage: P.Stage, owner: str, resource_limit: int, now: float | None = None) -> tuple[int, int] | None:
        """Mở một lần chạy (stage_runs.status=running, gắn phiên) nếu job rảnh và tài nguyên dùng chung còn chỗ; None = chưa có chỗ (chờ rồi thử lại).
        Trả (run_id, attempt). Đếm chỗ tính cả job đang chạy stage bình thường lẫn các lần chạy thủ công khác trên cùng tài nguyên."""
        now = now or time.time()
        res = P.resource_of(stage)
        res_stages = [s for s in P.STAGES if P.resource_of(s) == res]
        with self.s._tx() as c:
            j = c.execute("SELECT state, lease_owner, control_state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if j is None or j["control_state"] in ("DELETED", "CANCELLED"):
                raise StageError(ErrorClass.CANCELLED, "CANCELLED", "job đã bị hủy/xóa")
            if j["state"] in P.BY_RUNNING or j["lease_owner"]:
                raise _err("JOB_BUSY", "Job đang chạy một bước khác.", "Chờ bước đó xong rồi chạy lại.")
            running = c.execute(f"SELECT COUNT(*) FROM jobs WHERE state IN ({','.join('?' * len(res_stages))})", [s.running_state for s in res_stages]).fetchone()[0]
            running += c.execute(f"SELECT COUNT(*) FROM stage_runs WHERE status='running' AND rerun_session_id IS NOT NULL AND stage IN ({','.join('?' * len(res_stages))})",
                                 [s.name for s in res_stages]).fetchone()[0]
            if running >= resource_limit:
                return None
            attempt = c.execute("SELECT COUNT(*) FROM stage_runs WHERE job_id=? AND stage=?", (job_id, stage.name)).fetchone()[0] + 1
            run_id = c.execute("INSERT INTO stage_runs(job_id,stage,attempt,status,owner,started_at,rerun_session_id) VALUES(?,?,?,?,?,?,?)",
                               (job_id, stage.name, attempt, "running", owner, now, sid)).lastrowid
            return run_id, attempt

    def end_run(self, run_id: int, status: str, error: dict | None = None, data: dict | None = None, now: float | None = None) -> None:
        with self.s._tx() as c:
            c.execute("UPDATE stage_runs SET status=?, ended_at=?, error=COALESCE(?, error), data=COALESCE(?, data) WHERE id=? AND status='running'",
                      (status, now or time.time(), json.dumps(error, ensure_ascii=False) if error else None, json.dumps(data, ensure_ascii=False) if data is not None else None, run_id))

    def commit_stage(self, job_id: str, stage: P.Stage, run_id: int, artifacts: list[ArtifactRef], data: dict, now: float | None = None) -> bool:
        """Điểm hiệu lực của một stage chạy thủ công: artifact mới THAY artifact cũ của chính stage này + lần chạy thành công, trong MỘT transaction.
        Không đụng state/lease của job. False nếu job đã bị xóa/hủy giữa chừng (kết quả bị bỏ, artifact cũ còn nguyên)."""
        now = now or time.time()
        with self.s._tx() as c:
            j = c.execute("SELECT state, control_state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if j is None or j["control_state"] in ("DELETED", "CANCELLED"):
                return False
            if artifacts:
                c.execute("DELETE FROM artifacts WHERE job_id=? AND stage=?", (job_id, stage.name))
            for a in artifacts:
                c.execute("INSERT INTO artifacts(job_id,stage,run_id,kind,path,sha256,bytes,meta,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                          (job_id, stage.name, run_id, a["kind"], a["path"], a["sha256"], a["bytes"], json.dumps(a["meta"], ensure_ascii=False), now))
            c.execute("UPDATE stage_runs SET status='succeeded', ended_at=?, data=? WHERE id=?", (now, json.dumps(data, ensure_ascii=False), run_id))
            c.execute("UPDATE jobs SET updated_at=? WHERE id=?", (now, job_id))
        return True

    def counts(self, job_id: str) -> dict[str, int]:
        """stage -> số phiên chạy lại thủ công khác nhau đã bắt đầu stage đó (retry trong cùng phiên không tính)."""
        return {r["stage"]: int(r["n"]) for r in self.s._q(
            "SELECT stage, COUNT(DISTINCT rerun_session_id) AS n FROM stage_runs WHERE job_id=? AND rerun_session_id IS NOT NULL GROUP BY stage", (job_id,))}
