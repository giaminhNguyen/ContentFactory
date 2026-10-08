"""Ước tính (KHÔNG phải số thật) số lượt gọi LLM + token của một job Story Remix, để người dùng thấy trước và đặt ngân sách.

Công thức đơn giản, công khai và giới hạn rõ: chưa tính thử lại do định dạng sai; USD chỉ có khi cấu hình giá (`story_remix.price_usd_per_mtok`), nếu không là "unknown".
Số thật luôn lấy từ cost_report.json của job (chỉ gồm lượt nhà cung cấp báo)."""
from __future__ import annotations

import math

CHARS_PER_TOKEN = 3.2            # tiếng Việt: ước lượng thô
SOURCE_CAP_CHARS = 120_000       # stages.excerpt: nguồn dài hơn bị lược
REVIEW_SOURCE_CAP = 60_000
PROMPT_OVERHEAD = {"dna": 700, "premises": 1800, "bible": 3000, "outline": 4000, "review": 1500, "chapter": 5200}


def estimate(story: dict, profile: dict, source_chars: int = 60_000, price: dict | None = None) -> dict:
    n = int(profile.get("chapters") or math.ceil(int(profile.get("chapter_chars_total", profile.get("target_chars", 40000))) / int(profile.get("chapter_chars", 3000))))
    cc = int(profile.get("chapter_chars", 3000))
    passes = int(story.get("quality_repair_max_passes", 1))
    cands = int(story.get("premise_candidates", 3))
    src_tok = min(source_chars, SOURCE_CAP_CHARS) / CHARS_PER_TOKEN
    rev_src = min(source_chars, REVIEW_SOURCE_CAP) / CHARS_PER_TOKEN
    ch_out = cc / 2.6 + 300                                            # lời kể + khối bộ nhớ
    steps = [("dna", src_tok + PROMPT_OVERHEAD["dna"], 900, 1, 1), ("premises", PROMPT_OVERHEAD["premises"], cands * 900, 1, 2), ("story_bible", PROMPT_OVERHEAD["bible"], 1500, 1, 1),
             ("outline", PROMPT_OVERHEAD["outline"], n * 180, 1, 1 + passes), ("originality_review", rev_src + n * 250 / CHARS_PER_TOKEN + PROMPT_OVERHEAD["review"], 400, 1, 1),
             ("chapters", PROMPT_OVERHEAD["chapter"], ch_out, n, n * (1 + passes))]
    lo_calls = sum(s[3] for s in steps)
    hi_calls = sum(s[4] for s in steps)
    tin_lo = sum(s[1] * s[3] for s in steps)
    tin_hi = sum(s[1] * s[4] for s in steps)
    tout_lo = sum(s[2] * s[3] for s in steps)
    tout_hi = sum(s[2] * s[4] for s in steps)
    usd = None
    if price and price.get("in") is not None and price.get("out") is not None:
        usd = {"min": round((tin_lo * price["in"] + tout_lo * price["out"]) / 1e6, 2), "max": round((tin_hi * price["in"] + tout_hi * price["out"]) / 1e6, 2)}
    return {"chapters": n, "calls": {"min": lo_calls, "max": hi_calls}, "input_tokens": {"min": int(tin_lo), "max": int(tin_hi)}, "output_tokens": {"min": int(tout_lo), "max": int(tout_hi)},
            "usd": usd, "assumptions": {"source_chars": source_chars, "chars_per_token": CHARS_PER_TOKEN, "repair_passes": passes},
            "note": ("Chi phí USD: không rõ (chưa cấu hình giá/triệu token trong Cài đặt máy). " if usd is None else "") + "Chỉ là ước tính thô, chưa tính thử lại do định dạng sai; số thật ghi ở báo cáo chi phí của job."}
