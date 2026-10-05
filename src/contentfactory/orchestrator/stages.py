"""Contract của một stage (HANDOFF §15A, MODULE_CONTRACTS §11.3), bọc `jobs.pipeline.Stage` bằng hành vi cần validator:

  required_inputs / produced_outputs   khai báo theo KIND artifact
  can_run(available)                   đủ đầu vào chưa
  validate_inputs / validate_outputs   kiểm file + validator theo kind
  stage_key                            dấu vân tay: input sha256 + tham số/config mà stage KHAI BÁO phụ thuộc
  skip_reason                          output đã hợp lệ và còn khớp stage_key => không chạy lại
  retry / resume                       chính sách retry (jobs/policy.py) và điểm resume (`checkpoint`, ctx.progress)
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..contracts import ArtifactRef, ErrorClass, StageError
from ..fsutil import sha256_file
from ..jobs import pipeline as P
from ..jobs.db import JobStore
from .validation import validate_kind


def _dig(d: dict, dotted: str):
    """params_deps cho phép đường dẫn có dấu chấm (vd "audio.join") để stage chỉ phụ thuộc đúng phần của mình."""
    cur = d
    for part in dotted.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


class StageContract:
    def __init__(self, stage: P.Stage) -> None:
        self.stage = stage

    @property
    def required_inputs(self) -> tuple[str, ...]:
        return self.stage.requires

    @property
    def produced_outputs(self) -> tuple[str, ...]:
        return self.stage.produces

    @property
    def resume(self) -> str:
        return self.stage.checkpoint

    # -- can_run ----------------------------------------------------------------------------
    def can_run(self, available: set[str]) -> tuple[bool, list[str]]:
        missing = [k for k in self.stage.requires if k not in available]
        return not missing, missing

    # -- validate_inputs --------------------------------------------------------------------
    def validate_inputs(self, inputs: dict[str, list[ArtifactRef]], workspace: Path) -> None:
        """Raise StageError: thiếu/mất file => lỗi TÀI NGUYÊN (resource=input, job bị giữ chờ người cung cấp lại);
        file còn nhưng nội dung không qua validator => POLICY vĩnh viễn."""
        for kind in self.stage.requires:
            if not inputs.get(kind):
                raise StageError(ErrorClass.POLICY, "MISSING_INPUT", f"thiếu artifact '{kind}' cho stage {self.stage.name}",
                                 {"kind": kind}, resource="input")
            for a in inputs[kind]:
                p = workspace / a["path"]
                if not p.is_file() or p.stat().st_size != a["bytes"]:
                    raise StageError(ErrorClass.POLICY, "ARTIFACT_MISSING", a["path"], {"kind": kind}, resource="input")
                issues = validate_kind(kind, p, a["meta"])
                if issues:
                    raise StageError(ErrorClass.POLICY, "INPUT_INVALID", f"{a['path']}: {','.join(issues)}",
                                     {"kind": kind, "issues": issues})

    # -- validate_outputs -------------------------------------------------------------------
    def validate_outputs(self, artifacts: list[ArtifactRef], workspace: Path) -> None:
        for a in artifacts:
            issues = validate_kind(a["kind"], workspace / a["path"], a["meta"])
            if issues:
                raise StageError(ErrorClass.POLICY, "OUTPUT_INVALID", f"{a['path']}: {','.join(issues)}",
                                 {"kind": a["kind"], "issues": issues})

    # -- stage_key --------------------------------------------------------------------------
    def stage_key(self, params: dict, snapshot: dict | None, inputs: dict[str, list[ArtifactRef]]) -> str:
        st = self.stage
        picked = params if st.params_deps is None else {k: _dig(params, k) for k in st.params_deps}
        sem = (snapshot or {}).get("semantic", {})
        cfg = {k: sem.get(k) for k in st.config_deps}
        cfg["adapters"] = {a: (sem.get("adapters") or {}).get(a) for a in st.adapters}
        blob = {"stage": st.name, "params": picked, "config": cfg,
                "inputs": sorted((k, a["sha256"]) for k, v in inputs.items() for a in v)}
        return hashlib.sha256(json.dumps(blob, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

    # -- skip -------------------------------------------------------------------------------
    def consumed_outputs(self, target_idx: int | None) -> tuple[str, ...]:
        """Output mà job THỰC SỰ cần từ stage này trong khoảng [stage, target]: tất cả nếu là target hoặc deliverable,
        ngược lại chỉ kind mà một stage sau (tới target) tiêu thụ."""
        i = P.INDEX[self.stage.name]
        t = len(P.STAGES) - 1 if target_idx is None else target_idx
        if self.stage.deliverable or i == t:
            return self.stage.produces
        needed = {k for j in range(i + 1, t + 1) for k in P.STAGES[j].requires}
        return tuple(k for k in self.stage.produces if k in needed)

    def skip_reason(self, store: JobStore, job_id: str, workspace: Path, key: str | None,
                    target_idx: int | None) -> str | None:
        """'valid' (output do chính stage này sinh, còn nguyên và stage_key khớp), 'provided' (output được import/from_job)
        hoặc None (phải chạy). Kiểm file thật: tồn tại, size, sha256 và validator theo kind."""
        kinds = self.consumed_outputs(target_idx)
        if not kinds:
            return None
        rows = store.artifacts(job_id)
        runs = {r["id"]: r for r in store.stage_runs(job_id)}
        reason = "provided"
        for kind in kinds:
            mine = [a for a in rows if a["kind"] == kind and a["stage"] in (self.stage.name, "import")]
            if not mine:
                return None
            for a in mine:
                p = workspace / a["path"]
                if not p.is_file() or p.stat().st_size != a["bytes"] or sha256_file(p) != a["sha256"]:
                    return None
                if validate_kind(kind, p, json.loads(a["meta"])):
                    return None
                if a["stage"] == self.stage.name:
                    run = runs.get(a["run_id"])
                    if key is None or not run or run["stage_key"] != key:                 # input/tham số/config đã đổi: không còn hợp lệ
                        return None
                    reason = "valid"
        return reason
