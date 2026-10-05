"""Pause Engine: biến metadata pause (pause_after_ms của từng segment, do TTS planner đặt) thành khoảng im lặng THỰC SỰ chèn vào,
theo luật của profile (`join.pause`). Hàm thuần, tất định.

Mỗi chunk đã được cắt biên chỉ còn `keep_ms` im lặng ở mỗi đầu, nên giữa hai chunk liền nhau luôn có sẵn 2×keep_ms.
`compensate_edge` trừ phần này đi để khoảng nghỉ nghe được đúng bằng giá trị yêu cầu (không cộng dồn).
"""
from __future__ import annotations


def plan_pauses(pauses_ms: list[int | float], rules: dict, keep_ms: float) -> list[dict]:
    """[{requested, inserted}] cho từng chunk (pause SAU chunk đó). Chunk cuối dùng `tail_ms` thay vì giá trị yêu cầu."""
    out = []
    n = len(pauses_ms)
    for i, p in enumerate(pauses_ms):
        last = i == n - 1
        req = float(rules.get("tail_ms", 0)) if last else float(p or 0)
        if req > 0 and not last:
            req = min(float(rules["max_ms"]), max(float(rules["min_ms"]), req * float(rules["scale"])))
        elif req > 0:
            req = min(float(rules["max_ms"]), req)
        natural = keep_ms if last else 2 * keep_ms                       # im lặng sẵn có ở biên (cuối cùng chỉ có đuôi chunk cuối)
        ins = max(0.0, req - natural) if rules.get("compensate_edge", True) and req > 0 else req
        out.append({"requested": req, "inserted": round(ins, 3)})
    return out
