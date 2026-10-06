"""Prosody QC: số liệu + cảnh báo về nhịp đọc. CẢNH BÁO MỀM, không bao giờ làm job lỗi (khác với validator cứng của speech plan).

  analyze_plan   : trước khi tổng hợp — đếm câu/nhóm/ranh giới, phân vị khoảng nghỉ, câu quá dài phải cắt, nhóm quá ngắn (dấu hiệu chia vụn),
                   khoảng nghỉ quá lớn, mọi khoảng nghỉ giống hệt dù có nhiều loại ranh giới (dấu hiệu logic ngữ cảnh hỏng), quá nhiều "chuyển cảnh".
  analyze_audio  : sau khi ghép — khoảng nghỉ đo được so với kế hoạch (lệch quá dung sai = lỗi stitcher/cắt biên), lệch tổng thời lượng.
"""
from __future__ import annotations

from collections import Counter


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    v = sorted(values)
    return int(v[min(len(v) - 1, int(round(q * (len(v) - 1))))])


def analyze_plan(plan: dict, qc: dict | None = None) -> dict:
    qc = qc or {}
    max_pause = int(qc.get("max_pause_ms", 2500))
    tiny_words = int(qc.get("tiny_group_words", 2))
    long_chars = int(qc.get("long_sentence_chars", 300))
    segs, groups = plan["segments"], plan["groups"]
    ext = [int(g["pause_after_ms"]) for g in groups[:-1]]                          # khoảng nghỉ thật sự được chèn (giữa các nhóm)
    kinds = Counter(s["boundary_after"] for s in segs if s["boundary_after"] != "end")
    warns: list[dict] = []

    def warn(code: str, message: str, **extra) -> None:
        warns.append({"code": code, "message": message, **extra})

    tiny = [g["id"] for g in groups if len(g["text"].split()) <= tiny_words]
    if len(groups) >= 3 and len(tiny) / len(groups) > 0.2:
        warn("TOO_MANY_TINY_GROUPS", f"{len(tiny)}/{len(groups)} nhóm tổng hợp chỉ ≤ {tiny_words} từ: engine có thể bị reset ngữ điệu quá nhiều", groups=tiny[:10])
    over = [g["id"] for g in groups[:-1] if g["pause_after_ms"] > max_pause]
    if over:
        warn("PAUSE_ABOVE_MAX", f"{len(over)} khoảng nghỉ > {max_pause} ms", groups=over[:10])
    long_ = [s["id"] for s in segs if len(s["text"]) > long_chars]
    if long_:
        warn("VERY_LONG_SENTENCE", f"{len(long_)} câu dài hơn {long_chars} ký tự (khó đọc tự nhiên)", segments=long_[:10])
    if plan.get("split_sentences"):
        warn("SENTENCE_SPLIT_FOR_LIMIT", f"{len(plan['split_sentences'])} câu vượt giới hạn engine và đã được cắt ở ranh giới ngôn ngữ")
    if plan.get("mode") == "prosody":
        if len(ext) >= 8 and len(set(ext)) == 1 and len(kinds) >= 3:
            warn("UNIFORM_PAUSES", "mọi khoảng nghỉ giữa nhóm giống hệt nhau dù văn bản có nhiều loại ranh giới: có thể phân loại ranh giới đang hỏng")
        para_level = kinds.get("paragraph", 0) + kinds.get("scene", 0) + kinds.get("dialogue", 0)
        if kinds.get("scene", 0) >= 5 and kinds["scene"] / para_level > 0.5:
            warn("TOO_MANY_SCENE_BREAKS", f"{kinds['scene']}/{para_level} ranh giới đoạn là chuyển cảnh: có dòng nào đó bị coi nhầm là phân cảnh?")
    return {"segments": len(segs), "groups": len(groups), "boundaries": dict(kinds), "external_pauses": len(ext),
            "pause_ms": {"p50": _pct(ext, 0.5), "p95": _pct(ext, 0.95), "max": max(ext) if ext else 0},
            "tiny_groups": len(tiny), "warnings": warns}


def analyze_audio(plan_pauses_ms: list[int], joined: dict | None, expected_sec: float | None, actual_sec: float | None,
                  tolerance_ms: float = 60.0) -> dict:
    """Khoảng nghỉ stitcher thực sự áp dụng so với kế hoạch (lệch = bị giới hạn bởi join.pause scale/min/max của audio profile, hoặc stitcher lỗi).
    `joined` = kết quả AudioProcessor.assemble: ưu tiên `pauses[i].requested` (khoảng nghỉ NGHE ĐƯỢC sau khi áp luật), nếu không có thì `timeline[i].gap_after_sec`.
    Chunk cuối dùng đuôi (tail) nên không so. Đo trực tiếp trên file nằm ở test stitcher (silent_runs)."""
    warns: list[dict] = []
    drifts: list[float] = []
    joined = joined or {}
    realized = [float(p["requested"]) for p in joined.get("pauses") or []] or [float(r.get("gap_after_sec", 0.0)) * 1000.0 for r in joined.get("timeline") or []]
    if realized:
        for i, (planned, got) in enumerate(zip(plan_pauses_ms[:-1], realized[:-1])):
            d = abs(got - float(planned))
            drifts.append(d)
            if d > tolerance_ms:
                warns.append({"code": "PAUSE_DRIFT", "message": f"nhóm {i + 1}: khoảng nghỉ thực tế lệch {d:.0f} ms so với kế hoạch {planned} ms (bị giới hạn bởi join.pause hoặc lỗi cắt biên?)", "group": i + 1})
    if expected_sec and actual_sec and abs(actual_sec - expected_sec) > max(0.5, 0.01 * expected_sec):
        warns.append({"code": "DURATION_DRIFT", "message": f"tổng thời lượng {actual_sec:.2f}s khác kỳ vọng {expected_sec:.2f}s", "expected": expected_sec, "actual": actual_sec})
    return {"max_pause_drift_ms": round(max(drifts), 1) if drifts else 0.0, "warnings": warns[:20]}
