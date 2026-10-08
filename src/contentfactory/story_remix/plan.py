"""Lập kế hoạch Story Remix (dừng TRƯỚC khi viết dài): DNA → ý tưởng → chọn → autocast → bible → đại cương → cổng originality/dopamine.

Mỗi bước có checkpoint theo fingerprint (core.Steps) và ghi vào sổ chi phí; kho nhân vật/universe do orchestrator TIÊM (không import). Bước nào dùng nguồn: chỉ DNA và các cổng.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Protocol

from ..fsutil import atomic_write_json
from . import gates as GT
from . import schemas as SC
from . import stages as ST
from .core import Invalid, Ledger, Steps, fail, fingerprint

STEP_VERSION = "1"
MAX_ATTEMPTS = 2


class UniverseLike(Protocol):
    def continuity(self, premise: dict, genre: str) -> float: ...
    def cast(self, story_id: str, premise: dict, genre: str, cu: dict, job_id: str | None) -> dict: ...
    def profiles(self, cast: dict) -> dict[str, dict]: ...


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def chapters_for(profile: dict) -> int:
    target = int(profile.get("target_chars", 40000))
    return int(profile.get("chapters") or math.ceil(target / int(profile.get("chapter_chars", 3000))))


def plan_story(llm, universe: UniverseLike, source_text: str, title: str, lang: str, mode_cfg: dict, profile: dict, out_dir: Path, job_id: str | None, ctx=None, ledger: Ledger | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    st, cu = mode_cfg["story"], mode_cfg["character_universe"]
    ledger = ledger or Ledger(out_dir / "cost_report.json", st.get("budget_usd"))
    steps = Steps(out_dir)
    src_fp = _sha(source_text)
    story_id = f"job-{job_id}" if job_id else "job-local"
    opts_key = {k: st[k] for k in ("target_genre", "tone", "ending", "audio_readability", "blocked_themes", "premise_candidates")}
    n_chapters = chapters_for(profile)
    result: dict = {"story_id": story_id}

    dna = steps.run("dna", fingerprint(src=src_fp, lang=lang, v=STEP_VERSION), "source_dna.json", lambda: ST.analyze_dna(llm, ledger, source_text, lang, ctx), SC.source_dna)
    dna_fp = fingerprint(dna=dna)
    result["dna"] = dna
    feedback = ""
    for attempt in range(MAX_ATTEMPTS):
        suffix = "" if attempt == 0 else f"_{attempt + 1}"
        cands = steps.run(f"premises{suffix}", fingerprint(dna=dna_fp, opts=opts_key, fb=feedback, a=attempt, v=STEP_VERSION), f"premise_candidates{suffix}.json",
                          lambda: ST.generate_premises(llm, ledger, dna, st, lang, ctx, feedback), lambda d: SC.premise_candidates(d, 6))
        sel = ST.select_premise(cands, dna, source_text, lambda p: universe.continuity(p, p["setting"]), cu["reuse_strategy"])
        atomic_write_json(out_dir / "selection_report.json", {**sel, "attempt": attempt + 1})
        if sel["selected"] is None:
            feedback = ST.weakness_feedback(sel)
            if attempt + 1 >= MAX_ATTEMPTS:
                raise fail("PREMISE_TOO_WEAK", f"Mọi ý tưởng đều dưới ngưỡng chất lượng ({ST.MIN_SELECT}) sau {MAX_ATTEMPTS} lần lập; dừng trước khi viết dài (tiết kiệm chi phí).",
                           {"hint": "Xem selection_report.json; thử đổi thể loại/giọng hoặc chạy lại.", "scores": {k: v["total"] for k, v in sel["scores"].items()}})
            continue
        premise = next(p for p in cands["candidates"] if p["id"] == sel["selected"])
        result.update(premise=premise, selection=sel)
        # ---- autocast (kho nhân vật): đóng băng dàn trước khi viết
        if cu["auto_cast"]:
            cast = universe.cast(story_id, premise, st.get("target_genre") or dna["genre"], cu, job_id)
        else:
            raise fail("CAST_DISABLED", "Story Remix cần dàn nhân vật; hiện chưa hỗ trợ tắt ‘Tự chọn nhân vật’ với job tự động.", {"hint": "Bật ‘Tự chọn nhân vật’."})
        atomic_write_json(out_dir / "character_cast.json", cast)
        profiles = universe.profiles(cast)
        cast_fp = fingerprint(cast=cast["fingerprint"], members=[m["character_id"] for m in cast["members"]])
        bible = steps.run(f"bible{suffix}", fingerprint(p=premise["id"], pf=fingerprint(premise=premise), cast=cast_fp, dna=dna_fp, v=STEP_VERSION), "story_bible.json",
                          lambda: ST.make_bible(llm, ledger, premise, cast, profiles, dna, lang, ctx), lambda d: SC.story_bible(d, {m["character_id"] for m in cast["members"]}))
        outline_fp = fingerprint(bible=fingerprint(bible=bible), n=n_chapters, read=st["audio_readability"], v=STEP_VERSION)
        ids = {m["character_id"] for m in cast["members"]}
        prot = next(m["character_id"] for m in cast["members"] if m["role_code"] == "protagonist")
        lo, hi = max(3, n_chapters - 2), n_chapters + 2
        outline = steps.run(f"outline{suffix}", outline_fp, "outline.json", lambda: ST.make_outline(llm, ledger, bible, cast, profiles, dna, n_chapters, lang, ctx),
                            lambda d: SC.outline(d, ids, prot, lo, hi))
        # ---- cổng originality (luôn chạy: an toàn/pháp lý, không tắt được)
        og = steps.run(f"originality{suffix}", fingerprint(o=outline_fp, src=src_fp, rights=st["source_rights"], v=STEP_VERSION), "originality_report.json",
                       lambda: GT.originality_gate(premise, bible, outline, cast, source_text, st["source_rights"], llm, ledger, ctx))
        if og["decision"] == "block":
            feedback = "Bản trước quá giống nguồn — " + "; ".join(e["evidence"] for e in og["evidence"])[:600] + ". Hãy đổi hẳn nhân vật, quan hệ, chuỗi sự kiện và bối cảnh."
            if attempt + 1 >= MAX_ATTEMPTS:
                raise fail("ORIGINALITY_BLOCKED", "Kế hoạch vẫn quá giống nguồn sau khi lập lại; dừng trước khi viết dài.", {"hint": "Xem originality_report.json; thử nguồn/tuỳ chọn khác.", "level": og["level"]})
            continue
        if og["decision"] == "review" and not st.get("review_accepted"):
            raise fail("ORIGINALITY_REVIEW_REQUIRED", "Kế hoạch có điểm giống nguồn ở mức trung bình và quyền sử dụng nguồn chưa rõ: cần bạn xem báo cáo trước khi viết dài.",
                       {"hint": "Mở originality_report.json. Nếu chấp nhận, bật ‘Đã xem báo cáo’ rồi chạy lại; hoặc khai báo quyền sử dụng nguồn.", "evidence": [e["evidence"] for e in og["evidence"]][:5]})
        result.update(cast=cast, bible=bible, outline=outline, originality=og)
        # ---- cổng dopamine (nhịp thưởng) + tối đa N lượt sửa đại cương
        quality = {"version": 1, "outline_gate": bool(st["outline_gate"]), "decision": "skipped", "issues": [], "repairs": 0}
        if st["outline_gate"]:
            repairs = 0
            while True:
                dg = GT.dopamine_gate(outline, dna, st["audio_readability"])
                quality = {**dg, "outline_gate": True, "repairs": repairs}
                if dg["decision"] == "pass":
                    break
                if repairs >= st["quality_repair_max_passes"]:
                    atomic_write_json(out_dir / "quality_report.json", {**quality, "decision": "fail"})
                    raise fail("OUTLINE_GATE_FAILED", "Đại cương chưa đạt nhịp thưởng cảm xúc: " + "; ".join(i["message"] for i in dg["issues"])[:400],
                               {"hint": "Tăng ‘Số lượt sửa lỗi tối đa’ hoặc chạy lại.", "issues": dg["issues"]})
                repairs += 1
                fb = "; ".join(i["message"] for i in dg["issues"])
                outline = ST.make_outline(llm, ledger, bible, cast, profiles, dna, n_chapters, lang, ctx, fb)
                atomic_write_json(out_dir / "outline.json", outline)
                steps.state[f"outline{suffix}"] = {"fp": outline_fp, "file": "outline.json"}
                atomic_write_json(steps.path, steps.state)
            result["outline"] = outline
        atomic_write_json(out_dir / "quality_report.json", quality)
        result["quality"] = quality
        break
    ledger.save()
    result["cost"] = ledger.report()
    result["steps"] = {"ran": steps.ran, "skipped": steps.skipped}
    return result
