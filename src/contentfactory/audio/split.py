"""Intelligent split cho phần TikTok: cắt narration (đã tăng tốc) thành part ~target giây, ưu tiên ranh giới tự nhiên.

Không ép đúng `target`. Số part và độ dài đích của từng part:
  - tổng ≤ target×max_ratio                      : một part;
  - phần dư r = tổng − k×target ≥ min_last_ratio×target : k part ~target + một part cuối ngắn hơn (đúng HANDOFF §11: "part cuối có thể ngắn hơn");
  - phần dư nhỏ hơn thế                          : không sinh part cuối vụn — dồn dư vào các part (tổng/k mỗi part) nếu còn trong
                                                    max_ratio, ngược lại chia đều thành k+1 part.
Tại mỗi điểm cắt chọn ranh giới trong cửa sổ cho phép có chi phí thấp nhất:

    chi_phí = |t - lý_tưởng| - bonus[loại]      (loại: scene > paragraph > sentence > silence)

Cửa sổ còn bị giới hạn sao cho phần còn lại vẫn chia vừa các part sau. Ranh giới `cut` (hết một segment giữa câu) KHÔNG dùng khi còn ranh
giới khác trong cửa sổ đã nới; nếu buộc phải dùng hoặc không có ranh giới nào thì part được đánh dấu `forced`/`mid_sentence` kèm cảnh báo.
Hàm thuần: nhận danh sách ranh giới, trả kế hoạch. Ranh giới: [{"t": giây, "kind": scene|paragraph|sentence|silence|cut}] (t = giữa khoảng nghỉ).
"""
from __future__ import annotations

KINDS = ("scene", "paragraph", "sentence", "silence", "cut")


def plan_split(total: float, boundaries: list[dict], target: float, min_ratio: float = 0.85, max_ratio: float = 1.15,
               bonus_sec: dict | None = None, min_last_ratio: float = 0.4, min_part_sec: float = 0.0) -> dict:
    """`min_part_sec`: sàn TUYỆT ĐỐI cho part cuối (giây) — phải ≥ ngưỡng audio QA, nếu không với `target` rất nhỏ planner có thể cho ra part mà QA coi là rỗng."""
    bonus = {"scene": 90.0, "paragraph": 45.0, "sentence": 15.0, "silence": 5.0, "cut": 0.0, **(bonus_sec or {})}
    warns: list[str] = []
    if total <= 0:
        return {"parts": [], "warnings": ["audio rỗng"]}
    if total <= target * max_ratio:
        return {"parts": [{"start": 0.0, "end": total, "boundary": "end", "forced": False, "mid_sentence": False}],
                "warnings": warns, "n": 1, "per_part": total, "mode": "single"}
    k = int(total // target)
    rest = total - k * target
    tail_min = max(0.5 * min_last_ratio * target, min_part_sec)     # part cuối ngắn nhất chấp nhận được (chế độ tail)
    if k >= 1 and rest >= max(min_last_ratio * target, min_part_sec):
        mode, n, ideal_len = "tail", k + 1, target
    else:
        per = total / max(1, k)
        n = max(1, k) if per <= target * max_ratio else k + 1
        mode, ideal_len = "even", total / n
    if n == 1:
        return {"parts": [{"start": 0.0, "end": total, "boundary": "end", "forced": False, "mid_sentence": False}],
                "warnings": warns, "n": 1, "per_part": total, "mode": mode}
    L = ideal_len
    last_lo = tail_min if mode == "tail" else L * min_ratio
    bs = sorted(({"t": float(b["t"]), "kind": b["kind"]} for b in boundaries if 0 < b["t"] < total), key=lambda b: b["t"])
    parts, start = [], 0.0
    for idx in range(n - 1):
        rem = n - idx - 1                                       # số part còn lại SAU part này (≥ 1)
        ideal = start + L
        lo_feas = total - rem * L * max_ratio                   # phần còn lại phải vừa đủ chỗ cho các part sau
        hi_feas = total - ((rem - 1) * L * min_ratio + last_lo)
        chosen, forced = None, False
        for widen in (1.0, 1.5, 2.0):
            lo = max(start + L * (1 - (1 - min_ratio) * widen), lo_feas)
            hi = min(start + L * (1 + (max_ratio - 1) * widen), hi_feas)
            cands = [b for b in bs if lo <= b["t"] <= hi and b["t"] > start and b["kind"] != "cut"]
            if cands:
                chosen = min(cands, key=lambda b: abs(b["t"] - ideal) - bonus.get(b["kind"], 0.0))
                if widen > 1:
                    warns.append(f"part {idx + 1}: phải nới cửa sổ cắt ×{widen} mới có ranh giới tự nhiên")
                break
        if chosen is None:                                      # không có ranh giới tự nhiên: thử đến ranh giới giữa câu, rồi cắt cứng
            lo, hi = max(start + L * min_ratio, lo_feas), min(start + L * max_ratio, hi_feas)
            cands = [b for b in bs if lo <= b["t"] <= hi and b["t"] > start]
            if cands:
                chosen = min(cands, key=lambda b: abs(b["t"] - ideal))
                warns.append(f"part {idx + 1}: chỉ có ranh giới giữa câu trong cửa sổ cắt")
            else:
                t = min(max(ideal, lo_feas, start + 1e-3), hi_feas if hi_feas > start else ideal)
                chosen, forced = {"t": t, "kind": "cut"}, True
                warns.append(f"part {idx + 1}: không có ranh giới nào trong cửa sổ, cắt cứng tại {t:.2f}s")
        parts.append({"start": start, "end": chosen["t"], "boundary": chosen["kind"], "forced": forced,
                      "mid_sentence": chosen["kind"] == "cut"})
        start = chosen["t"]
    parts.append({"start": start, "end": total, "boundary": "end", "forced": False, "mid_sentence": False})
    return {"parts": parts, "warnings": warns, "n": n, "per_part": L, "mode": mode}
