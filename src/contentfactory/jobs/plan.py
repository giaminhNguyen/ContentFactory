"""Lập kế hoạch job: từ `start_stage`, `target_stage` và các artifact đã có (import / from_job) suy ra stage nào chạy.

Thuật toán (D-36, D-51): đi NGƯỢC từ target_stage theo *kind* artifact.
  unresolved = kind mà target cần mà chưa có sẵn
  với mỗi stage đứng trước (từ sau ra trước): nếu nó sản sinh kind nào đó trong `unresolved` (hoặc là stage
  `deliverable`: output package, publish) thì stage đó CẦN CHẠY,
  unresolved = (unresolved - produces) | (requires - đã có)
Nên chỉ stage thật sự cần mới chạy; stage có output đã sẵn (import) bị bỏ qua.
`start_stage` tường minh là cận dưới: không stage nào trước nó được chạy, nên nếu vẫn thiếu kind thì từ chối ngay lúc
tạo job (không đợi chạy mới biết). Không có `start_stage`: tự chọn stage cần chạy sớm nhất.
Hàm thuần: chỉ dùng bảng stage.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import pipeline as P


@dataclass
class Plan:
    start_idx: int
    target_idx: int
    run: list[str] = field(default_factory=list)       # stage cần chạy (có thể vẫn bị skip lúc chạy nếu output hợp lệ)
    skip: list[str] = field(default_factory=list)      # stage trong khoảng nhưng output đã có sẵn
    errors: list[str] = field(default_factory=list)

    @property
    def start_stage(self) -> str:
        return P.STAGES[self.start_idx].name

    @property
    def target_stage(self) -> str:
        return P.STAGES[self.target_idx].name


def plan_job(start_stage: str | None, target_stage: str | None, provided: set[str], has_input: bool) -> Plan:
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

    unresolved = set(P.STAGES[t].requires) - provided
    needed = {t}
    floor = s0 if s0 is not None else 0
    for i in range(t - 1, floor - 1, -1):
        stage = P.STAGES[i]
        if stage.deliverable or unresolved & set(stage.produces):
            needed.add(i)
            unresolved = (unresolved - set(stage.produces)) | (set(stage.requires) - provided)
    missing = sorted(unresolved)
    source_needed = 0 in needed
    if source_needed and not has_input and 0 >= floor:
        errors.append("stage 'source' cần chạy nhưng job không có params.input (URL/file/văn bản nguồn)")
    if missing:
        where = f"start_stage={start_stage!r}" if start_stage else "pipeline"
        errors.append(f"thiếu artifact đầu vào cho {where}: {missing} (hãy import hoặc dùng from_job)")
    start = s0 if s0 is not None else (min(needed) if needed else t)
    run = [P.STAGES[i].name for i in range(start, t + 1) if i in needed]
    skip = [P.STAGES[i].name for i in range(start, t + 1) if i not in needed]
    return Plan(start, t, run, skip, errors)
