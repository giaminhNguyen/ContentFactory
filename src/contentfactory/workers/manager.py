"""WorkerManager: chạy MỘT work type bằng pipeline retry/fallback/cooldown (W1.9–W1.14).

    pick (router) -> execute (driver) -> validate (gate) -> promote (atomic)
         ^                                                        |
         +---- lỗi: retry cùng worker theo policy, hết thì loại  |

Mọi lần thử ghi thành một `Attempt` MỚI (không ghi đè attempt cũ) nên UI dựng được timeline
"tại sao fallback". Worker bị lỗi liên tiếp -> cooldown; QUOTA block ngay; AUTH -> AUTH_REQUIRED.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from ..contracts import CancelToken, ErrorClass, StageError
from ..fsutil import atomic_write
from . import policy as policy_mod
from .drivers.base import ExecRequest, ExecResult
from .errors import WorkerError, WorkerErrorClass
from .models import Attempt, AttemptState, ExecutionTarget, WorkType, Worker, WorkerStatus, new_id
from .registry import WorkerRegistry
from .validation import validate_output


@dataclass
class RunResult:
    """Kết quả một lượt chạy work_type (có thể qua nhiều attempt)."""
    ok: bool
    work_type: str
    attempts: list[Attempt] = field(default_factory=list)
    output_path: Path | None = None     # file canonical sau promote
    error: WorkerError | None = None    # lỗi cuối khi ok=False
    reason: str = ""                    # vì sao dừng (thành công / hết worker / hết lượt)
    text: str = ""                      # text mà lượt chạy thành công trả về (driver dạng hội thoại)
    session_id: str | None = None       # session để tiếp lượt sau (agent CLI)
    cost_usd: float = 0.0

    @property
    def final(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None


class WorkerManager:
    """`validate` là phần contract-specific (orchestrator tiêm `validate_kind`); module này
    giữ logic retry/fallback — không import orchestrator (workers là package ISOLATED)."""

    def __init__(self, registry: WorkerRegistry, workspace_root: Path, *,
                 validate: Callable[[Path, str], list[str]] | None = None,
                 now: Callable[[], float] = time.time) -> None:
        self.reg = registry
        self.workspace_root = Path(workspace_root)
        self.validate = validate
        self.now = now

    # -- chạy ----------------------------------------------------------------------------------
    def run(self, work_type: WorkType, *, prompt: str = "", job_id: str = "", stage: str = "",
            output: Path | None = None, timeout_s: float | None = None,
            cancel: CancelToken | None = None, meta: dict | None = None,
            cwd: Path | None = None) -> RunResult:
        """`cwd`: thư mục LÀM VIỆC dùng chung (adapter có trạng thái trên đĩa như oh-story) —
        không truyền thì chạy trong attempt workspace riêng. Kết quả promote ra `output`."""
        routing = self.reg.routing().get(work_type) or {}
        policy = policy_mod.merge(routing.get("policy"))
        attempts: list[Attempt] = []
        chosen: set[str] = set()
        tries: dict[str, int] = {}                  # số lần đã thử trên từng worker
        used: list[str] = []                        # worker đã hết ngân sách -> loại khỏi pick
        pending: ExecutionTarget | None = None      # retry cùng worker: bỏ qua pick
        last_error: WorkerError | None = None
        reason = ""
        session = (meta or {}).get("session")
        session_owner: str | None = None            # worker đang giữ session (W1.13): worker khác không kế thừa
        fingerprints: dict[str, int] = {}           # W2.6: lỗi lặp lại cùng dạng -> poison task
        no_progress_max = int(policy["max_no_progress"])

        while len(attempts) < int(policy["max_total_attempts"]):
            if cancel is not None and cancel.is_set():
                reason = "đã hủy (cancel token)"
                break
            if pending is not None:
                target = pending
                pending = None
            else:
                target, why = self.reg.pick(work_type, now=self.now(), exclude=tuple(used))
                if target is None:
                    reason = why
                    break
                if target.worker.id not in chosen:
                    if len(chosen) >= int(policy["max_distinct_workers"]):
                        reason = f"đạt giới hạn max_distinct_workers={policy['max_distinct_workers']}"
                        break
                    chosen.add(target.worker.id)

            attempt = self._begin(work_type, target, job_id, stage, timeout_s)
            use_session = session if (session_owner is None or session_owner == target.worker.id) else None
            if use_session is not None and session_owner is None:
                session_owner = target.worker.id          # worker đầu nhận session resume là chủ của nó (W1.13)
            try:
                result = self._execute(target, attempt, prompt, timeout_s, cancel, meta, cwd,
                                       session=use_session)
            except StageError as e:                       # driver ném CANCELLED -> ghi nhận rồi lan ra
                attempts.append(self._finish(attempt, AttemptState.FAILED,
                                             WorkerError.from_stage_error(e), ExecResult(ok=False)))
                raise
            if result.session_id is not None:
                session_owner = target.worker.id          # session giờ thuộc worker này (nếu có session mới/continue)
            if not result.ok:
                err = result.error or WorkerError.from_text(
                    WorkerErrorClass.UNKNOWN, "EXEC_FAILED", raw=result.text)
                attempts.append(self._finish(attempt, AttemptState.FAILED, err, result))
                self._register_failure(target.worker, err, policy)
                reason = f"{target.worker.name}: {err.kind.value} {err.code}"
                last_error = err
                # W2.6: cùng một lỗi lặp lại trên task này mà không tiến triển -> dừng, cần người xem
                fp = f"{work_type}|{err.kind.value}:{err.code}"
                fingerprints[fp] = fingerprints.get(fp, 0) + 1
                if fingerprints[fp] >= no_progress_max:
                    reason = (f"poison task: lặp {fingerprints[fp]} lần không tiến triển "
                              f"({err.kind.value} {err.code})"
                              + (f"; lỗi cuối: {reason}" if reason else ""))
                    break
                pending = self._retry_or_fallback(target, err, policy, tries, used)
                continue

            if result.output_path is not None:
                errors = validate_output(result.output_path, work_type, extra=self.validate)
            else:
                # driver hội thoại không ghi file: text trả về chính là output
                errors = [] if (result.text or "").strip() else ["không có output"]
            if errors:
                err = WorkerError.from_text(WorkerErrorClass.INVALID_OUTPUT, "OUTPUT_INVALID",
                                            message="; ".join(errors))
                attempts.append(self._finish(attempt, AttemptState.INVALID, err, result,
                                             validation=errors))
                self._register_failure(target.worker, err, policy)
                reason = f"{target.worker.name}: output không hợp lệ ({errors[0]})"
                last_error = err
                pending = self._retry_or_fallback(target, err, policy, tries, used)
                continue

            promoted = self._promote(result.output_path, output)
            attempts.append(self._finish(attempt, AttemptState.SUCCESS, None, result))
            self._register_success(target.worker)
            fingerprints = {}                                # W2.6: có tiến triển, reset bộ đếm poison
            return RunResult(ok=True, work_type=work_type, attempts=attempts, output_path=promoted,
                             text=result.text, session_id=result.session_id, cost_usd=result.cost_usd,
                             reason=f"thành công qua {len(attempts)} attempt: {target.worker.name}")

        if len(attempts) >= int(policy["max_total_attempts"]):
            reason = (f"đạt giới hạn max_total_attempts={policy['max_total_attempts']}"
                      + (f" (lỗi cuối: {reason})" if reason else ""))
        return RunResult(ok=False, work_type=work_type, attempts=attempts, error=last_error,
                         reason=reason or "không chạy được")

    # -- bên trong -------------------------------------------------------------------------------
    def _workspace(self, attempt_id: str, job_id: str) -> Path:
        # workspace/<job>/worker_attempts/<attempt_id>/ — attempt cũ không bao giờ ghi đè attempt mới
        return self.workspace_root / (job_id or "task") / "worker_attempts" / attempt_id

    def _begin(self, work_type: WorkType, target: ExecutionTarget, job_id: str, stage: str,
               timeout_s: float | None) -> Attempt:
        ws = self._workspace(new_id("att"), job_id)
        ws.mkdir(parents=True, exist_ok=True)
        w = target.worker
        return Attempt(attempt_id=ws.name, work_type=work_type, worker_id=w.id, worker_name=w.name,
                       driver_id=w.driver_id, model_id=target.model, pool=target.pool, job_id=job_id,
                       stage=stage, state=AttemptState.RUNNING, started_at=self.now(),
                       workspace=str(ws), meta={"timeout_s": timeout_s or w.timeout_s})

    def _execute(self, target: ExecutionTarget, attempt: Attempt, prompt: str,
                 timeout_s: float | None, cancel: CancelToken | None, meta: dict | None,
                 cwd: Path | None = None, session: str | None = None) -> ExecResult:
        w = target.worker
        work_dir = Path(cwd) if cwd is not None else Path(attempt.workspace)
        req = ExecRequest(work_type=attempt.work_type, prompt=prompt, cwd=work_dir,
                          model=target.model, session=session,
                          timeout_s=timeout_s or w.timeout_s,
                          startup_timeout_s=float(w.meta.get("startup_timeout_s", 60.0)),
                          idle_timeout_s=w.idle_timeout_s, hard_timeout_s=w.hard_timeout_s,
                          cancel=cancel or CancelToken(), meta=dict(meta or {}))
        try:
            return self.reg.driver(w.driver_id).execute(req)
        except StageError as e:
            if e.error_class is ErrorClass.CANCELLED:       # hủy job không phải lỗi worker
                raise
            return ExecResult(ok=False, text=e.message, error=WorkerError.from_stage_error(e))
        except NotImplementedError as e:                    # driver chưa dựng lệnh -> lỗi rõ ràng
            return ExecResult(ok=False, text=str(e), error=WorkerError.from_text(
                WorkerErrorClass.UNKNOWN, "DRIVER_UNSUPPORTED", raw=str(e)))
        except Exception as e:                              # mọi lỗi bất ngờ đều thành lỗi có loại
            return ExecResult(ok=False, text=repr(e), error=WorkerError.from_text(
                self.reg.driver(w.driver_id).classify(repr(e)), "DRIVER_EXCEPTION", raw=repr(e)))

    def _finish(self, attempt: Attempt, state: AttemptState, error: WorkerError | None,
                result: ExecResult, validation: list[str] | None = None) -> Attempt:
        done = replace(attempt, state=state, ended_at=self.now(),
                       error_kind=(error.kind.value if error else ""),
                       error_code=(error.code if error else ""),
                       error_message=(error.message if error else ""),
                       validation=list(validation or []),
                       session_id=result.session_id, cost_usd=result.cost_usd,
                       meta={**attempt.meta, **(result.meta or {}), "exit_code": result.exit_code})
        self.reg.store.add_attempt(done)                    # INSERT OR REPLACE cùng attempt_id
        return done

    def _retry_or_fallback(self, target: ExecutionTarget, err: WorkerError, policy: dict,
                           tries: dict[str, int], used: list[str]) -> ExecutionTarget | None:
        """Còn ngân sách retry cho worker này mà nó vẫn routable -> chạy lại nó; không thì loại."""
        wid = target.worker.id
        tries[wid] = tries.get(wid, 0) + 1
        budget = policy_mod.retries_for(policy, err.kind.value)
        if tries[wid] <= budget and self.reg.get(wid).routable(self.now())[0]:
            return ExecutionTarget(worker=self.reg.get(wid), model=target.model, pool=target.pool,
                                   reason=f"retry {tries[wid]}/{budget} sau {err.kind.value}")
        used.append(wid)
        return None

    # -- trạng thái worker (W1.9/W1.10 + W2.1 health/circuit) -----------------------------------
    def _register_failure(self, w: Worker, err: WorkerError, policy: dict) -> None:
        """Streak + cooldown/circuit + AUTH/QUOTA. Không xoá lịch sử attempt.

        W2.2: cooldown = circuit OPEN. Lỗi khi HALF_OPEN (probe thử lại) -> mở lại circuit
        (kéo dài cooldown) — probe có giới hạn, không để provider lia lửa.
        """
        w = self.reg.get(w.id)
        streak = w.failure_streak + 1
        cooldown = 0.0
        if err.kind is WorkerErrorClass.QUOTA:              # block ngay thời điểm đó
            cooldown = self.now() + float(policy["cooldown_s"])
        elif w.circuit_opened_at and (
                w.cooldown_until <= self.now()):            # HALF_OPEN probe hỏng -> mở lại circuit
            cooldown = self.now() + float(policy["cooldown_s"])
        elif streak >= int(policy["cooldown_after"]):
            cooldown = self.now() + float(policy["cooldown_s"])
        kw: dict = {"failure_streak": streak,
                    "last_error_kind": err.kind.value, "last_error_code": err.code,
                    "last_error_at": self.now()}
        if cooldown:
            kw["cooldown_until"] = max(cooldown, w.cooldown_until)
            kw["circuit_opened_at"] = w.circuit_opened_at or self.now()
        if err.kind is WorkerErrorClass.AUTH:
            kw["status"] = WorkerStatus.AUTH_REQUIRED
        self.reg.store.save_worker(w.with_updates(**kw))

    def _register_success(self, w: Worker) -> None:
        w = self.reg.get(w.id)
        if (w.failure_streak or w.cooldown_until or w.circuit_opened_at or w.last_error_kind
                or w.last_error_at or w.last_error_code):
            self.reg.store.save_worker(w.with_updates(
                failure_streak=0, cooldown_until=0.0, circuit_opened_at=0.0,
                last_error_kind="", last_error_code="", last_error_at=0.0, last_success_at=self.now()))

    # -- reconciler W2.5 -------------------------------------------------------------------------
    def reconcile_orphans(self) -> list[Attempt]:
        """Sau khi app crash giữa attempt: không để RUNNING vĩnh viễn.

        Attempt workspace chỉ được promote qua atomic_write SAU validation gate nên canonical
        không thể nửa chừng/corrupt — attempt đang RUNNING lúc crash đều là orphan hợp lệ.
        """
        orphans: list[Attempt] = []
        for a in self.reg.store.running_attempts():
            done = replace(a, state=AttemptState.FAILED, ended_at=self.now(),
                           error_kind="UNKNOWN", error_code="ORPHANED",
                           error_message="restart giữa attempt (orphan)")
            self.reg.store.add_attempt(done)
            orphans.append(done)
        return orphans

    # -- promote ------------------------------------------------------------------------------------
    @staticmethod
    def _promote(src: Path | None, dst: Path | None) -> Path | None:
        """Chỉ file đã qua validation gate mới được ghi vào canonical (atomic: ghi tạm rồi replace)."""
        if src is None:
            return None
        if dst is None:
            return src
        src = Path(src)
        atomic_write(Path(dst), lambda tmp: shutil.copy2(src, tmp))
        return Path(dst)
