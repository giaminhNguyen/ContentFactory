"""Đo độ giống với nguồn BẰNG SỐ ĐO CỤ THỂ (không phán đoán pháp lý): n-gram từ, tên riêng lặp lại, cửa sổ gần-trùng.
Chỉ các cổng kiểm tra và bước phân tích DNA được đọc nguồn; bộ sinh ý tưởng/đại cương/viết chương KHÔNG bao giờ nhận transcript."""
from __future__ import annotations

import re

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_NAME = re.compile(r"(?<![.!?…]\s)(?<!^)\b([A-ZÀ-ỸĐ][^\W\d_]+(?:\s+[A-ZÀ-ỸĐ][^\W\d_]+){0,2})\b", re.UNICODE)
_STOP_CAPS = {"Tôi", "Anh", "Chị", "Em", "Ông", "Bà", "Cô", "Chú", "Bác", "Mình", "Hôm", "Nhưng", "Và", "Rồi", "Khi", "Nếu", "Vì", "Sau", "Trước", "Một", "Những", "Các", "Đó", "Đây", "Chương"}


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def shingles(ws: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(ws[i:i + n]) for i in range(len(ws) - n + 1)} if len(ws) >= n else set()


def containment(text: str, source: str, n: int = 4) -> float:
    """Tỷ lệ n-gram của `text` có mặt trong `source` (0..1). Đo mức văn bản mới SAO CHÉP nguồn, không phải ý tưởng giống."""
    a = shingles(words(text), n)
    return len(a & shingles(words(source), n)) / len(a) if a else 0.0


def proper_names(source: str, min_count: int = 2) -> list[str]:
    """Tên riêng (cụm Viết Hoa giữa câu) xuất hiện ≥ min_count lần trong nguồn. Heuristic: transcript ASR thường không viết hoa ⇒ có thể bỏ sót (báo là giới hạn)."""
    counts: dict[str, int] = {}
    for m in _NAME.finditer(source or ""):
        nm = m.group(1).strip()
        if nm.split()[0] in _STOP_CAPS and len(nm.split()) == 1:
            continue
        counts[nm] = counts.get(nm, 0) + 1
    return sorted(n for n, c in counts.items() if c >= min_count)


def reused_names(names: list[str], text: str) -> list[str]:
    low = " " + " ".join(words(text)) + " "
    return [n for n in names if " " + " ".join(words(n)) + " " in low]


def near_windows(beats: list[str], source: str, size: int = 60, threshold: float = 0.5) -> list[dict]:
    """Beat nào của kế hoạch có tập từ giống một cửa sổ nguồn ≥ threshold (Jaccard) — dấu hiệu kể lại sát. Trả [{beat, score}] cao nhất mỗi beat."""
    src = words(source)
    wins = [set(src[i:i + size]) for i in range(0, max(1, len(src) - size + 1), size // 2)] if src else []
    out = []
    for b in beats:
        bw = set(words(b))
        if len(bw) < 6 or not wins:
            continue
        best = max(len(bw & w) / len(bw | w) for w in wins)
        if best >= threshold:
            out.append({"beat": b[:120], "score": round(best, 3)})
    return out
