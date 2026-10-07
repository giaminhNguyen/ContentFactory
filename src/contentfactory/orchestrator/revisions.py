"""Impact planner cho cập nhật pipeline/config của một job (Agent Plan Phase 2, D-99).

Người dùng đổi pipeline (stage được yêu cầu) và/hoặc config ngữ nghĩa/params của job; hàm thuần này nói TRƯỚC khi áp dụng stage nào
giữ nguyên, dùng lại, chạy, chạy lại hay bị bỏ khỏi kế hoạch — và job cần lùi về stage nào. Không tạo/đổi gì trong DB.

Hành động mỗi stage (ACTIONS):
  KEEP              đã xong và vẫn nằm trong kế hoạch, kết quả còn hợp lệ
  REUSE             đã có sẵn từ import/from_job
  RUN               sẽ chạy (chưa tới lượt, hoặc trước đó bị bỏ qua vì không được yêu cầu nên phải LÙI về)
  RERUN             đã xong nhưng kết quả không còn đúng (tham số/config của stage đổi, hoặc stage phía trước chạy lại)
  REMOVE_FROM_PLAN  bị bỏ khỏi kế hoạch (artifact đã có được giữ nguyên)
  CURRENT_CONTINUE  đang chạy: hoàn tất đơn vị hiện tại (bản thay đổi chỉ áp dụng sau điểm an toàn)
  BLOCKED           không áp dụng được (vd job đã hoàn tất/hủy)
  OFF               không nằm trong kế hoạch, trước và sau

`stage_key` (jobs/stages.py) là nguồn sự thật về "kết quả còn đúng không": so khóa đã lưu của lần chạy thành công với khóa tính lại trên
config/params mới, nên chỉ khai báo `params_deps`/`config_deps` mới gây RERUN — đổi thumbnail không chạy lại TTS.
"""
from __future__ import annotations

import copy
import json

from ..jobs import pipeline as P
from ..jobs.db import CONTROL_CANCELLED
from ..jobs.plan import plan_spec, spec_from_range
from ..story import guidance as GD
from .config import _merge
from .diagnose import STAGE_LABEL
from .snapshot import apply_patch
from .stages import StageContract, _dig

ACTIONS = ("KEEP", "REUSE", "RUN", "RERUN", "REMOVE_FROM_PLAN", "CURRENT_CONTINUE", "BLOCKED", "OFF")
POLICIES = ("after_current_safe_point", "pause_and_apply")


def depends_on(stage: P.Stage) -> set[str]:
    """Các stage đứng trước mà `stage` tiêu thụ output (requires + input tùy chọn, gồm cả nhánh đóng gói)."""
    kinds = set(stage.requires) | set(stage.optional)
    return {c.name for c in P.STAGES[:P.INDEX[stage.name]] if kinds & set(c.produces)}


def current_pipeline(job: dict, imported: set[str]) -> dict:
    """Pipeline hiện hành của job. Job kiểu mode/start/target cũ được quy về spec tương đương (tất định)."""
    if job.get("pipeline"):
        return job["pipeline"]
    start = job.get("start_stage")
    spec = spec_from_range(start, job.get("target_stage"))
    plan = plan_spec(spec, imported, bool((job["params"].get("input") or {}).get("value")), floor=start)
    lo, hi = P.INDEX[start] if start else 0, P.INDEX[job["target_stage"]] if job.get("target_stage") else len(P.STAGES) - 1
    run = plan.run if not plan.errors else [s.name for s in P.STAGES[lo:hi + 1]]
    return {"version": 2, "requested_stages": spec["requested_stages"], "options": {}, "run": run}


def _blocked(res: dict, why: str, clone: bool = False) -> dict:
    res.update(ok=False, blocked=why, clone_suggested=clone, errors=[why])
    return res


def _stage_row(s: P.Stage, action: str, reason: str, **extra) -> dict:
    return {"id": s.name, "label": STAGE_LABEL[s.name], "action": action, "reason": reason, **extra}


def _dep_changes(st: P.Stage, old_params: dict, new_params: dict, old_snap: dict | None, new_snap: dict | None) -> list[str]:
    """Tên các khai báo phụ thuộc của stage mà giá trị khác nhau (để giải thích vì sao chạy lại)."""
    out: list[str] = []
    for k in (st.params_deps if st.params_deps is not None else sorted(set(old_params) | set(new_params))):
        if _dig(old_params, k) != _dig(new_params, k):
            out.append(f"tham số {k}")
    so, sn = (old_snap or {}).get("semantic", {}), (new_snap or {}).get("semantic", {})
    for k in st.config_deps:
        if so.get(k) != sn.get(k):
            out.append(f"cấu hình {k}")
    for a in st.adapters:
        if (so.get("adapters") or {}).get(a) != (sn.get("adapters") or {}).get(a):
            out.append(f"adapter {a}")
    return out


def compute_impact(orc, job: dict, *, pipeline: dict | None = None, config_patch: dict | None = None, params_patch: dict | None = None) -> dict:
    """`pipeline` = {"requested_stages": [...]} (hoặc None = giữ nguyên); `config_patch` gộp sâu vào config ngữ nghĩa của job; `params_patch` gộp vào params."""
    store, jid = orc.store, job["id"]
    res: dict = {"job_id": jid, "ok": True, "errors": [], "blocked": None, "clone_suggested": False, "stages": [], "rewind_to": None, "pipeline": None,
                 "summary": {}, "current_running": job["state"] in P.BY_RUNNING,
                 "changes": {"pipeline": pipeline is not None, "config": sorted(config_patch or []), "params": sorted(params_patch or [])}}
    if job.get("control_state") == CONTROL_CANCELLED:
        return _blocked(res, "Job đã bị hủy: không cập nhật được. Dùng “Chạy lại với thay đổi” để tạo job mới.", True)
    if P.is_complete(job["state"], job.get("target_idx")):
        return _blocked(res, "Job đã hoàn tất: kết quả cũ không bị thay đổi. Dùng “Chạy lại với thay đổi” để tạo job mới và dùng lại phần còn hợp lệ.", True)
    if not (pipeline or config_patch or params_patch):
        return _blocked(res, "Chưa có thay đổi nào để áp dụng.")
    if config_patch and not job.get("config_snapshot"):
        return _blocked(res, "Job này không có config snapshot nên không đổi được cấu hình.")
    if (config_patch is not None and not isinstance(config_patch, dict)) or (params_patch is not None and not isinstance(params_patch, dict)):
        return _blocked(res, "config_patch/params_patch phải là object.")
    try:
        new_snap = apply_patch(job["config_snapshot"], config_patch) if config_patch else job.get("config_snapshot")
    except ValueError as e:
        return _blocked(res, str(e))
    old_params = job["params"]
    new_params = _merge(copy.deepcopy(old_params), copy.deepcopy(params_patch)) if params_patch else old_params

    imported = {a["kind"] for a in store.artifacts(jid) if a["stage"] == "import"}
    old = current_pipeline(job, imported)
    old_run = set(old["run"])
    requested = (pipeline or {}).get("requested_stages") if pipeline else old["requested_stages"]
    new_spec = {"version": 2, "requested_stages": requested, "options": old.get("options") or {}}
    plan = plan_spec(new_spec, imported, bool((new_params.get("input") or {}).get("value")))
    if plan.errors:
        res.update(ok=False, errors=plan.errors)
        return res
    new_run = set(plan.run)
    res["pipeline"] = {"version": 2, "requested_stages": plan.requested, "options": new_spec["options"], "run": list(plan.run)}

    state = job["state"]
    running = state in P.BY_RUNNING
    pos = P.INDEX.get(job.get("failed_stage") or "", 0) if state == P.FAILED else P.position(state)
    pos = len(P.STAGES) if pos is None else pos
    last: dict[str, dict] = {}
    for r in store.stage_runs(jid):
        if r["status"] in ("succeeded", "skipped"):
            last[r["stage"]] = r

    changed: set[str] = set()
    rows: list[dict] = []
    for i, s in enumerate(P.STAGES):
        n = s.name
        if n not in new_run:
            rows.append(_stage_row(s, "REMOVE_FROM_PLAN", "Không còn cần cho kết quả bạn chọn; kết quả đã có được giữ nguyên.") if n in old_run
                        else _stage_row(s, "OFF", ""))
            continue
        run_row = last.get(n)
        skipped = (json.loads(run_row["data"] or "{}").get("skipped") if run_row and run_row["status"] == "skipped" else None)
        ups = sorted((depends_on(s) & changed), key=P.INDEX.__getitem__)
        up_text = ", ".join(STAGE_LABEL[u] for u in ups)
        kchg: list[str] = []
        if run_row and run_row["stage_key"] and not skipped:
            inputs = store.inputs(jid, s.requires + s.optional)
            contract = StageContract(s)
            scoped, _ = contract.scope_inputs(inputs, {"run": plan.run})
            if contract.stage_key(new_params, new_snap, scoped, GD.run_extra(run_row)) != run_row["stage_key"]:
                kchg = _dep_changes(s, old_params, new_params, job.get("config_snapshot"), new_snap) or ["đầu vào hoặc phần đóng gói"]
        if running and i == pos:
            then = bool(kchg or ups)
            rows.append(_stage_row(s, "CURRENT_CONTINUE", "Đang chạy: hoàn tất đơn vị hiện tại rồi mới áp dụng thay đổi."
                                   + (" Sau đó sẽ được chạy lại vì thay đổi." if then else ""), then_rerun=then))
            if then:
                changed.add(n)
        elif i < pos:
            if run_row is None or skipped == "not_requested":
                rows.append(_stage_row(s, "RUN", "Trước đó chưa được yêu cầu nên chưa chạy; job sẽ lùi về bước này."))
                changed.add(n)
            elif skipped == "provided":
                rows.append(_stage_row(s, "REUSE", "Dùng lại kết quả đã có sẵn."))
            elif kchg:
                rows.append(_stage_row(s, "RERUN", "Sẽ chạy lại vì đổi " + ", ".join(kchg) + "."))
                changed.add(n)
            elif ups:
                rows.append(_stage_row(s, "RERUN", f"Sẽ chạy lại vì {up_text} chạy lại."))
                changed.add(n)
            else:
                rows.append(_stage_row(s, "KEEP", "Đã xong và vẫn hợp lệ: giữ nguyên."))
        else:
            rows.append(_stage_row(s, "RUN", "Sẽ chạy (dùng lại luôn nếu đã có kết quả hợp lệ)."))
    for r in rows:                                            # vai trò trong kế hoạch mới (để giao diện khóa đúng bước bắt buộc)
        info = plan.states[r["id"]]
        r["role"], r["by"] = info["state"], info["by"]
    behind = [P.INDEX[r["id"]] for r in rows if r["action"] in ("RUN", "RERUN") and P.INDEX[r["id"]] < pos]
    res["rewind_to"] = P.STAGES[min(behind)].name if behind else None
    res["stages"] = rows
    for r in rows:
        res["summary"].setdefault(r["action"], []).append(r["id"])
    return res
