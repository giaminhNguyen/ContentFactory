"""Orchestrator: lấy job từ hàng đợi từng stage, chạy handler, checkpoint, retry, resume.

Bất biến:
- Chỉ DB giữ state. Handler chỉ thấy StageContext (artifact đầu vào đã kiểm + params) và adapter được tiêm.
- Kết quả stage chỉ có hiệu lực khi `store.succeed` commit (artifact + state cùng transaction).
- Tiến trình chết => lease hết hạn => `recover_expired` xếp job lại đúng stage; artifact stage trước không đổi.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from ..contracts import ArtifactRef, CancelToken, ErrorClass, StageContext, StageError, StageResult
from ..fsutil import sha256_file
from ..jobs import manifest as M
from ..jobs import pipeline as P
from ..jobs.db import Claim, JobStore
from ..jobs.workspace import ensure_job_dirs, job_dir
from .config import Config, _merge
from .handlers import HANDLERS
from .log import EventLog
from .registry import build_adapters


class Orchestrator:
    def __init__(self, cfg: Config, adapters: dict | None = None, echo: bool = False) -> None:
        self.cfg = cfg
        self.store = JobStore(cfg.path("db"))
        self.adapters = adapters if adapters is not None else build_adapters(cfg)
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.cancel = CancelToken()
        self.log = EventLog(cfg.path("runtime"), cfg.path("workspace"), echo)
        self.versions = M.read_modules_lock(cfg.root)
        cfg.path("output").mkdir(parents=True, exist_ok=True)

    # -- API ----------------------------------------------------------------------------------
    def submit(self, params: dict, priority: int = 0) -> str:
        merged = _merge(copy.deepcopy(self.cfg["job_defaults"]), copy.deepcopy(params))
        job_id = self.store.create_job(merged, priority)
        ensure_job_dirs(self.cfg.path("workspace"), job_id)
        self.log.emit("job_created", job_id=job_id)
        self._manifest(job_id)
        return job_id

    def retry(self, job_id: str) -> str:
        stage = self.store.retry_failed(job_id)
        self.log.emit("job_retry_requested", job_id=job_id, stage=stage)
        self._manifest(job_id)
        return stage

    def run(self, until_idle: bool = True, stop: threading.Event | None = None) -> None:
        for j in self.store.list_jobs():            # manifest là dẫn xuất: dựng lại nếu crash giữa commit và ghi file
            if j["state"] not in P.TERMINAL:
                self._manifest(j["id"])
        hb_stop = threading.Event()
        threading.Thread(target=self._heartbeat, args=(hb_stop,), daemon=True).start()
        executor = ThreadPoolExecutor(max_workers=int(self.cfg.data.get("max_workers", 8)))
        futures: set[Future] = set()
        self.log.emit("orchestrator_started", owner=self.owner)
        try:
            while not (stop and stop.is_set()):
                futures = {f for f in futures if not f.done()}
                for job_id, action in self.store.recover_expired(self.cfg["retry"]["max_interruptions"]):
                    self.log.emit("lease_recovered", "warning", job_id, action=action)
                    self._manifest(job_id)
                self._schedule(executor, futures)
                if until_idle and not futures and self.store.nonterminal_count() == 0:
                    break
                time.sleep(self.cfg["poll_s"])
        except KeyboardInterrupt:
            pass
        finally:
            self.cancel.set()                       # handler hợp tác trả CANCELLED => job về hàng, không mất retry
            executor.shutdown(wait=True)
            hb_stop.set()
            self.log.emit("orchestrator_stopped", owner=self.owner)

    # -- nội bộ -------------------------------------------------------------------------------
    def _heartbeat(self, stop: threading.Event) -> None:
        while not stop.wait(self.cfg["heartbeat_s"]):
            try:
                self.store.heartbeat(self.owner, self.cfg["lease_s"])
            except Exception as e:                  # DB bận tạm thời: lần sau thử lại
                self.log.emit("heartbeat_error", "warning", error=repr(e))

    def _schedule(self, executor: ThreadPoolExecutor, futures: set[Future]) -> None:
        cap = int(self.cfg.data.get("max_workers", 8))
        for stage in reversed(P.STAGES):            # ưu tiên stage sau để hút pipeline (HANDOFF §14)
            free = cap - len(futures)
            if free <= 0:
                return
            for claim in self.store.claim(stage, free, self.cfg.limit(P.resource_of(stage)),
                                          self.owner, self.cfg["lease_s"]):
                futures.add(executor.submit(self._execute, claim))

    def _manifest(self, job_id: str) -> None:
        jd = job_dir(self.cfg.path("workspace"), job_id)
        if jd.exists():
            M.write(self.store, jd, job_id, self.versions)

    def _execute(self, claim: Claim) -> None:
        stage, job_id = claim.stage, claim.job_id
        log = self.log.bind(job_id, stage.name, claim.attempt)
        try:
            jd = ensure_job_dirs(self.cfg.path("workspace"), job_id)
            inputs = self.store.inputs(job_id, stage.requires)
            self._verify_inputs(stage, inputs, jd)
            key = self._stage_key(stage, claim.params, inputs)
            self.store.set_run_key(claim.run_id, key)
            ctx = StageContext(job_id=job_id, stage=stage.name, attempt=claim.attempt, stage_key=key,
                               workspace=jd, stage_dir=jd / stage.workdir, params=claim.params, inputs=inputs,
                               config={"output_dir": str(self.cfg.path("output"))},
                               cancel=self.cancel, log=log)
            log("stage_started", stage_key=key[:12])
            t0 = time.time()
            result = HANDLERS[stage.name](ctx, **{n: self.adapters[n] for n in stage.adapters})
            arts = self._seal(stage, result, jd)
            if self.store.succeed(claim, self.owner, arts, result.data):
                log("stage_succeeded", seconds=round(time.time() - t0, 3), artifacts=len(arts), data=result.data)
            else:
                log("lease_lost", "warning", note="kết quả bị bỏ vì lease đã hết hạn và job đã được nhận lại")
        except StageError as e:
            self._on_error(claim, e, log)
        except Exception as e:
            log("stage_exception", "error", error=repr(e), traceback=traceback.format_exc())
            self._on_error(claim, StageError(ErrorClass.POLICY, "UNEXPECTED", repr(e)), log)
        finally:
            self._manifest(job_id)

    def _on_error(self, claim: Claim, e: StageError, log) -> None:
        if e.error_class == ErrorClass.CANCELLED:
            self.store.release(claim, self.owner)
            log("stage_released", "warning")
            return
        r = self.cfg["retry"]
        outcome = self.store.fail(claim, self.owner, e, r["max_attempts"], r["backoff_s"])
        log("stage_failed", "error" if outcome == "failed" else "warning", outcome=outcome, error=e.to_dict())

    @staticmethod
    def _stage_key(stage: P.Stage, params: dict, inputs: dict[str, list[ArtifactRef]]) -> str:
        blob = {"stage": stage.name, "params": params,
                "inputs": sorted((k, a["sha256"]) for k, v in inputs.items() for a in v)}
        return hashlib.sha256(json.dumps(blob, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _verify_inputs(stage: P.Stage, inputs: dict[str, list[ArtifactRef]], jd: Path) -> None:
        for kind in stage.requires:
            if not inputs[kind]:
                raise StageError(ErrorClass.POLICY, "MISSING_INPUT", f"thiếu artifact '{kind}' cho stage {stage.name}")
            for a in inputs[kind]:
                p = jd / a["path"]
                if not p.is_file() or p.stat().st_size != a["bytes"]:   # chỉ kiểm size; sha256 đã ghi lúc checkpoint
                    raise StageError(ErrorClass.POLICY, "ARTIFACT_MISSING", a["path"], {"kind": kind})

    @staticmethod
    def _seal(stage: P.Stage, result: StageResult, jd: Path) -> list[ArtifactRef]:
        base, out = jd.resolve(), []
        for d in result.artifacts:
            p = (jd / d.path).resolve()
            try:
                rel = p.relative_to(base).as_posix()
            except ValueError:
                raise StageError(ErrorClass.POLICY, "BAD_ARTIFACT_PATH", d.path) from None
            if d.kind not in stage.produces:
                raise StageError(ErrorClass.POLICY, "UNDECLARED_KIND", f"{stage.name} không khai báo '{d.kind}'")
            if not p.is_file():
                raise StageError(ErrorClass.POLICY, "MISSING_OUTPUT", rel)
            out.append({"path": rel, "kind": d.kind, "sha256": sha256_file(p), "bytes": p.stat().st_size, "meta": d.meta})
        for kind in stage.produces:
            if not any(a["kind"] == kind for a in out):
                raise StageError(ErrorClass.POLICY, "MISSING_OUTPUT", f"stage {stage.name} thiếu artifact '{kind}'")
        return out
