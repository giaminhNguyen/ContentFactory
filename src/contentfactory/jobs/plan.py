"""Lập kế hoạch job (Pipeline Planner v2): từ *pipeline spec* (các stage người dùng/hệ thống YÊU CẦU) và các artifact đã có
(import / from_job) suy ra stage nào chạy.

Pipeline spec (version 2): {"version": 2, "requested_stages": [<stage id>...], "options": {}}. Các stage được yêu cầu là GỐC của kế hoạch;
KHÔNG stage nào được kéo vào chỉ vì nó là `deliverable` hay vì nó nằm trước đích (D-36 cũ).

Thuật toán (đóng kín phụ thuộc): đi NGƯỢC từ stage cuối về stage đầu theo *kind* artifact.
  unresolved = kind mà các stage đã được chọn (phía sau) cần mà chưa có sẵn
  stage i cần chạy nếu nó là gốc, hoặc nó sản sinh kind nào đó trong `unresolved`; khi đó
  unresolved = (unresolved - produces) | (requires - đã có)
Kind đã có sẵn (provided) thì không cần stage sản sinh nó. Kind còn thiếu sau cùng => lỗi plan TRƯỚC khi chạy.
Stage có `packages` (gói output): chỉ nhánh có nguồn (stage sản sinh nằm trong kế hoạch, hoặc kind được cung cấp) mới được đóng gói,
và phải có ít nhất một nhánh — không đóng gói rỗng.

`start_stage` (kiểu cũ) là cận dưới: không stage nào trước nó được chạy. `plan_job(start, target, ...)` giữ API cũ và chạy qua đúng
planner này (`spec_from_range`): gốc = target + các stage `deliverable` trong [start, target] — cho kết quả y hệt planner cũ.
Hàm thuần: chỉ dùng bảng stage.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import pipeline as P

SPEC_VERSION = 2


@dataclass
class Plan:
    start_idx: int
    target_idx: int
    run: list[str] = field(default_factory=list)       # stage cần chạy (có thể vẫn bị skip lúc chạy nếu output hợp lệ)
    skip: list[str] = field(default_factory=list)      # stage trong khoảng nhưng không chạy (đã có sẵn / không được yêu cầu)
    errors: list[str] = field(default_factory=list)
    requested: list[str] = field(default_factory=list)  # gốc của kế hoạch (đã chuẩn hóa, theo thứ tự pipeline)
    # stage -> {"state": selected|locked|provided|not_requested, "by": [stage cần nó], "kinds": [kind đang được cần]}
    states: dict[str, dict] = field(default_factory=dict)
    reuse: list[str] = field(default_factory=list)    # stage trong kế hoạch mà MỌI output đã được cung cấp sẵn (sẽ chỉ reuse)

    @property
    def start_stage(self) -> str:
        return P.STAGES[self.start_idx].name

    @property
    def target_stage(self) -> str:
        return P.STAGES[self.target_idx].name


# ---------------------------------------------------------------------------------------------------- spec
def normalize_spec(spec) -> tuple[list[str], list[str]]:
    """(requested_stages theo thứ tự pipeline, lỗi). Trùng lặp được gộp; id sai / rỗng / version lạ là lỗi."""
    if not isinstance(spec, dict):
        return [], ["pipeline_spec phải là object {version, requested_stages}"]
    errors: list[str] = []
    if spec.get("version", SPEC_VERSION) != SPEC_VERSION:
        errors.append(f"pipeline_spec.version không hỗ trợ: {spec.get('version')!r} (hỗ trợ {SPEC_VERSION})")
    req = spec.get("requested_stages")
    if not isinstance(req, (list, tuple)) or not req:
        errors.append("pipeline_spec.requested_stages phải là danh sách stage không rỗng")
        return [], errors
    bad = sorted({str(x) for x in req if not isinstance(x, str) or x not in P.INDEX})
    if bad:
        errors.append(f"stage không hợp lệ: {bad}; hợp lệ: {[s.name for s in P.STAGES]}")
    if errors:
        return [], errors
    return sorted(set(req), key=P.INDEX.__getitem__), []


def spec_from_range(start_stage: str | None, target_stage: str | None) -> dict:
    """Quy (start_stage, target_stage) kiểu cũ về spec: gốc = target + stage deliverable nằm trong [start, target]."""
    t = P.INDEX[target_stage] if target_stage else len(P.STAGES) - 1
    floor = P.INDEX[start_stage] if start_stage else 0
    roots = [s.name for i, s in enumerate(P.STAGES) if floor <= i <= t and (i == t or s.deliverable)]
    return {"version": SPEC_VERSION, "requested_stages": roots, "options": {}}


def spec_for_mode(mode: str) -> dict:
    """Spec của một MODES cũ (FULL, SUBTITLE_ONLY, ...)."""
    start, target = P.MODES[mode]
    return spec_from_range(start, target)


# ---------------------------------------------------------------------------------------------------- planner
def plan_spec(spec: dict, provided: set[str], has_input: bool, floor: str | None = None) -> Plan:
    requested, errors = normalize_spec(spec)
    if floor is not None and floor not in P.INDEX:
        errors.append(f"start_stage không hợp lệ: {floor!r}; hợp lệ: {[s.name for s in P.STAGES]}")
    if errors:
        return Plan(0, len(P.STAGES) - 1, errors=errors)
    n = len(P.STAGES)
    f = P.INDEX[floor] if floor else 0
    roots = {P.INDEX[r] for r in requested}
    if min(roots) < f:
        return Plan(f, max(roots), errors=[f"start_stage {floor!r} đứng sau stage được yêu cầu {P.STAGES[min(roots)].name!r}"])

    unresolved: set[str] = set()
    needed: set[int] = set()
    for i in range(n - 1, f - 1, -1):
        stage = P.STAGES[i]
        if i in roots or unresolved & set(stage.produces):
            needed.add(i)
            unresolved = (unresolved - set(stage.produces)) | (set(stage.requires) - provided)
    errors = []
    # Nhánh đóng gói: có nguồn khi stage sản sinh nằm trong kế hoạch hoặc kind được cung cấp sẵn.
    for i in sorted(needed):
        stage = P.STAGES[i]
        if not stage.packages:
            continue
        live = [prod for prod, kinds in stage.packages if P.INDEX[prod] in needed or set(kinds) <= provided]
        if not live:
            want = " hoặc ".join(prod for prod, _ in stage.packages)
            errors.append(f"stage {stage.name!r} không có gì để đóng gói: hãy chọn {want} (hoặc cung cấp video có sẵn)")
    if 0 in needed and not has_input and 0 >= f:
        errors.append("stage 'source' cần chạy nhưng job không có params.input (URL/file/văn bản nguồn)")
    if unresolved:
        where = f"start_stage={floor!r}" if floor else "pipeline"
        errors.append(f"thiếu artifact đầu vào cho {where}: {sorted(unresolved)} (hãy import hoặc dùng from_job)")

    states: dict[str, dict] = {}
    for i, stage in enumerate(P.STAGES):
        consumers = [P.STAGES[j] for j in sorted(needed) if j > i and set(P.STAGES[j].requires) & set(stage.produces)]
        by = [c.name for c in consumers]
        kinds = sorted({k for c in consumers for k in c.requires if k in stage.produces})
        if i in roots:
            states[stage.name] = {"state": "selected", "by": by, "kinds": kinds}
        elif i in needed:
            states[stage.name] = {"state": "locked", "by": by, "kinds": kinds}
        elif set(stage.produces) & provided:
            states[stage.name] = {"state": "provided", "by": by, "kinds": sorted(set(stage.produces) & provided)}
        else:
            states[stage.name] = {"state": "not_requested", "by": [], "kinds": []}
    target = max(needed)
    start = f if floor else min(needed)
    run = [P.STAGES[i].name for i in range(start, target + 1) if i in needed]
    skip = [P.STAGES[i].name for i in range(start, target + 1) if i not in needed]
    reuse = [P.STAGES[i].name for i in sorted(needed) if set(P.STAGES[i].produces) <= provided]
    return Plan(start, target, run, skip, errors, requested, states, reuse)


def plan_job(start_stage: str | None, target_stage: str | None, provided: set[str], has_input: bool) -> Plan:
    """API kiểu cũ (start_stage/target_stage). Chạy qua `plan_spec`."""
    errors: list[str] = []
    for label, name in (("start_stage", start_stage), ("target_stage", target_stage)):
        if name is not None and name not in P.INDEX:
            errors.append(f"{label} không hợp lệ: {name!r}; hợp lệ: {[s.name for s in P.STAGES]}")
    if errors:
        return Plan(0, len(P.STAGES) - 1, errors=errors)
    t = P.INDEX[target_stage] if target_stage else len(P.STAGES) - 1
    s0 = P.INDEX[start_stage] if start_stage else None
    if s0 is not None and s0 > t:
        return Plan(s0, t, errors=[f"start_stage {start_stage!r} đứng sau target_stage {P.STAGES[t].name!r}"])
    return plan_spec(spec_from_range(start_stage, target_stage), provided, has_input, floor=start_stage)
