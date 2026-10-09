"""Đo độ giống với nguồn BẰNG SỐ ĐO CỤ THỂ (không phán đoán pháp lý): n-gram từ, tên riêng lặp lại, cửa sổ gần-trùng.
Chỉ các cổng kiểm tra và bước phân tích DNA được đọc nguồn; bộ sinh ý tưởng/đại cương/viết chương KHÔNG bao giờ nhận transcript."""
from __future__ import annotations

import re

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)
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
    """Tên riêng (cụm Viết Hoa liên tiếp ≤ 3 từ; từ đơn đầu câu không tính) xuất hiện ≥ min_count lần. Heuristic: transcript ASR thường không viết hoa ⇒ có thể bỏ sót (báo là giới hạn)."""
    text = source or ""
    toks = [(m.group(0), m.start()) for m in _TOKEN.finditer(text)]
    counts: dict[str, int] = {}
    i = 0
    while i < len(toks):
        w, pos = toks[i]
        if not w[0].isupper():
            i += 1
            continue
        j = i
        while j + 1 < len(toks) and j - i < 2 and toks[j + 1][0][0].isupper() and not re.search(r"[.!?…]\s*$", text[toks[j][1] + len(toks[j][0]):toks[j + 1][1]]):
            j += 1
        before = text[:pos].rstrip()
        at_start = not before or before[-1] in ".!?…" or "\n" in text[len(before):pos]
        seq = [t[0] for t in toks[i:j + 1]]
        if not (at_start and len(seq) == 1) and not (len(seq) == 1 and seq[0] in _STOP_CAPS):
            nm = " ".join(seq)
            counts[nm] = counts.get(nm, 0) + 1
        i = j + 1
    # Transcript ASR thường viết hoa đầu câu mà không có dấu chấm ⇒ từ thường ("Cậu", "Không", "Lúc") trông như tên riêng. Tên riêng thật gần như LUÔN viết hoa:
    # giữ lại chỉ khi mọi từ của nó hiếm khi xuất hiện ở dạng chữ thường trong chính văn bản (lỗi này chỉ lộ khi chạy với nguồn thật).
    cap_n: dict[str, int] = {}
    low_n: dict[str, int] = {}
    for w, _ in toks:
        (cap_n if w[0].isupper() else low_n)[w.lower()] = (cap_n if w[0].isupper() else low_n).get(w.lower(), 0) + 1

    def proper(nm: str) -> bool:
        return all(low_n.get(w.lower(), 0) * 10 <= cap_n.get(w.lower(), 0) for w in nm.split())
    return sorted(n for n, c in counts.items() if c >= min_count and proper(n))


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
