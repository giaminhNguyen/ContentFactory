"""Schema (có version) của các artifact Story Remix. Validator ném `Invalid` với lý do tiếng Việt cụ thể — dùng cả để bảo vệ file đọc lại và làm phản hồi thử lại cho LLM."""
from __future__ import annotations

import re

from .core import Invalid

DNA_VERSION = 1
ROLES = {"protagonist", "deuteragonist", "antagonist", "rival", "foil", "mentor", "ally", "love_interest", "confidant", "comic_relief", "catalyst", "gatekeeper", "wildcard"}


def _s(d: dict, k: str, mx: int, req: bool = True, where: str = "") -> str:
    v = d.get(k)
    if v is None or v == "":
        if req:
            raise Invalid(f"{where}{k}: bắt buộc.")
        return ""
    if not isinstance(v, str) or len(v.strip()) > mx:
        raise Invalid(f"{where}{k}: phải là văn bản ≤ {mx} ký tự.")
    return v.strip()


def _l(d: dict, k: str, mx: int, ln: int, req: bool = True, where: str = "") -> list[str]:
    v = d.get(k)
    if v is None and not req:
        return []
    if not isinstance(v, list) or (req and not v) or len(v) > mx or not all(isinstance(x, str) and x.strip() and len(x.strip()) <= ln for x in v):
        raise Invalid(f"{where}{k}: phải là danh sách {'(không rỗng) ' if req else ''}tối đa {mx} mục văn bản ≤ {ln} ký tự.")
    return [x.strip() for x in v]


def _n(d: dict, k: str, lo: float, hi: float, where: str = "") -> float:
    v = d.get(k)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise Invalid(f"{where}{k}: phải là số trong [{lo}, {hi}].")
    return v


def _obj(v, what: str) -> dict:
    if not isinstance(v, dict):
        raise Invalid(f"{what}: phải là object JSON.")
    return v


# ---------------------------------------------------------------------------------------------- source DNA
def source_dna(d) -> dict:
    d = _obj(d, "source_dna")
    pc = _obj(d.get("payoff_cadence"), "payoff_cadence")
    return {"version": DNA_VERSION, "genre": _s(d, "genre", 80), "subgenres": _l(d, "subgenres", 6, 60, req=False), "engagement_engine": _s(d, "engagement_engine", 600),
            "reward_types": _l(d, "reward_types", 8, 120), "emotional_promise": _s(d, "emotional_promise", 400), "hook_pattern": _s(d, "hook_pattern", 400),
            "payoff_cadence": {"first_payoff_by_pct": int(_n(pc, "first_payoff_by_pct", 1, 60, "payoff_cadence.")), "payoffs_per_10pct": _n(pc, "payoffs_per_10pct", 0.1, 5, "payoff_cadence.")},
            "escalation_pattern": _s(d, "escalation_pattern", 500), "pacing": _s(d, "pacing", 300), "tone": _s(d, "tone", 200), "pov": _s(d, "pov", 100, req=False),
            "audio_requirements": _l(d, "audio_requirements", 8, 200, req=False), "avoid": _l(d, "avoid", 10, 200, req=False)}


# ---------------------------------------------------------------------------------------------- premise
def _slot(s, where: str) -> dict:
    s = _obj(s, where)
    if s.get("role_code") not in ROLES:
        raise Invalid(f"{where}role_code không hợp lệ ({s.get('role_code')!r}); hợp lệ: {', '.join(sorted(ROLES))}.")
    sid = _s(s, "slot_id", 30, where=where)
    if not re.fullmatch(r"[a-z0-9_]{1,30}", sid):
        raise Invalid(f"{where}slot_id chỉ gồm a-z, 0-9, _.")
    rels = []
    for r in s.get("relationships") or []:
        r = _obj(r, where + "relationships[]")
        rels.append({"with": _s(r, "with", 30, where=where), "type": re.sub(r"\W+", "_", _s(r, "type", 40, where=where).lower()).strip("_") or "related_to"})
    imp = s.get("importance", 2)
    return {"slot_id": sid, "role_code": s["role_code"], "importance": imp if imp in (1, 2, 3) else 2, "traits": _l(s, "traits", 8, 60, req=False, where=where),
            "goal": _s(s, "goal", 300, req=False, where=where), "relationships": rels}


def premise(p, where: str = "") -> dict:
    p = _obj(p, where + "premise")
    slots = p.get("slots")
    if not isinstance(slots, list) or not 2 <= len(slots) <= 8:
        raise Invalid(f"{where}slots: cần 2–8 vai.")
    sl = [_slot(s, f"{where}slots[]. ") for s in slots]
    ids = [s["slot_id"] for s in sl]
    if len(set(ids)) != len(ids):
        raise Invalid(f"{where}slots: slot_id trùng.")
    for s in sl:
        for r in s["relationships"]:
            if r["with"] not in ids or r["with"] == s["slot_id"]:
                raise Invalid(f"{where}slot {s['slot_id']}: quan hệ tới slot không tồn tại ({r['with']}).")
    if not any(s["role_code"] == "protagonist" for s in sl):
        raise Invalid(f"{where}slots: thiếu nhân vật chính (protagonist).")
    plan = p.get("payoff_plan")
    if not isinstance(plan, list) or not 3 <= len(plan) <= 20:
        raise Invalid(f"{where}payoff_plan: cần 3–20 điểm thưởng cảm xúc.")
    pp = []
    for x in plan:
        x = _obj(x, where + "payoff_plan[]")
        pp.append({"beat": _s(x, "beat", 300, where=where), "at_pct": int(_n(x, "at_pct", 1, 100, where)), "type": _s(x, "type", 80, where=where)})
    return {"id": _s(p, "id", 12, where=where), "logline": _s(p, "logline", 400, where=where), "setting": _s(p, "setting", 400, where=where), "central_conflict": _s(p, "central_conflict", 500, where=where),
            "stakes": _s(p, "stakes", 300, where=where), "twist": _s(p, "twist", 400, where=where), "ending": _s(p, "ending", 400, where=where), "hook": _s(p, "hook", 300, where=where),
            "slots": sl, "payoff_plan": sorted(pp, key=lambda x: x["at_pct"])}


def premise_candidates(d, want: int) -> dict:
    d = _obj(d, "premise_candidates")
    c = d.get("candidates")
    if not isinstance(c, list) or len(c) < 2:
        raise Invalid("candidates: cần ít nhất 2 ý tưởng khác nhau.")
    out = [premise(p, f"candidates[{i}]. ") for i, p in enumerate(c[:want + 2])]
    if len({p["id"] for p in out}) != len(out):
        raise Invalid("candidates: id trùng.")
    return {"version": 1, "candidates": out}


# ---------------------------------------------------------------------------------------------- bible / outline
def story_bible(d, cast_ids: set[str]) -> dict:
    d = _obj(d, "story_bible")
    cast = d.get("cast")
    if not isinstance(cast, list):
        raise Invalid("cast: phải là danh sách.")
    got, out_cast = set(), []
    for m in cast:
        m = _obj(m, "cast[]")
        cid = m.get("character_id")
        if cid not in cast_ids:
            raise Invalid(f"cast: character_id {cid!r} không thuộc dàn đã chốt ({', '.join(sorted(cast_ids))}). Không được tự thêm nhân vật chính ngoài dàn.")
        got.add(cid)
        out_cast.append({"character_id": cid, "arc": _s(m, "arc", 500, where="cast[]. "), "secrets": _l(m, "secrets", 5, 200, req=False, where="cast[]. "), "voice_notes": _s(m, "voice_notes", 300, req=False, where="cast[]. ")})
    if got != cast_ids:
        raise Invalid(f"cast: thiếu nhân vật của dàn đã chốt: {', '.join(sorted(cast_ids - got))}.")
    chain = d.get("causal_chain")
    if not isinstance(chain, list) or len(chain) < 3:
        raise Invalid("causal_chain: cần ít nhất 3 mắt xích nhân-quả.")
    cc = []
    for x in chain[:40]:
        x = _obj(x, "causal_chain[]")
        cc.append({"event": _s(x, "event", 300, where="causal_chain[]. "), "cause": _s(x, "cause", 300, where="causal_chain[]. "), "effect": _s(x, "effect", 300, where="causal_chain[]. ")})
    setting = _obj(d.get("setting"), "setting")
    return {"version": 1, "title": _s(d, "title", 120), "premise_id": _s(d, "premise_id", 12), "setting": {"place": _s(setting, "place", 300, where="setting."), "time": _s(setting, "time", 200, req=False, where="setting.")},
            "world_rules": _l(d, "world_rules", 12, 300, req=False), "cast": out_cast, "causal_chain": cc, "themes": _l(d, "themes", 6, 100, req=False), "originality_notes": _l(d, "originality_notes", 8, 300, req=False)}


def outline(d, cast_ids: set[str], protagonist_id: str, chapters_min: int, chapters_max: int) -> dict:
    d = _obj(d, "outline")
    chs = d.get("chapters")
    if not isinstance(chs, list) or not chapters_min <= len(chs) <= chapters_max:
        raise Invalid(f"chapters: cần {chapters_min}–{chapters_max} chương (đang có {len(chs) if isinstance(chs, list) else 'không phải danh sách'}).")
    out, seen = [], set()
    for i, c in enumerate(chs, 1):
        c = _obj(c, f"chapters[{i}]")
        w = f"chapters[{i}]. "
        if c.get("n") != i:
            raise Invalid(f"{w}n phải là {i} (đánh số liên tục từ 1).")
        on = c.get("cast")
        if not isinstance(on, list) or not on or any(x not in cast_ids for x in on):
            raise Invalid(f"{w}cast: danh sách character_id thuộc dàn đã chốt ({', '.join(sorted(cast_ids))}); tên tự do không được chấp nhận.")
        seen.update(on)
        pay = c.get("payoff")
        payoff = None
        if pay is not None:
            pay = _obj(pay, w + "payoff")
            payoff = {"type": _s(pay, "type", 80, where=w + "payoff."), "description": _s(pay, "description", 300, where=w + "payoff.")}
        out.append({"n": i, "title": _s(c, "title", 120, where=w), "goal": _s(c, "goal", 300, where=w), "beats": _l(c, "beats", 8, 400, where=w), "cast": list(dict.fromkeys(on)),
                    "hook": _s(c, "hook", 300, where=w), "payoff": payoff, "cliffhanger": _s(c, "cliffhanger", 300, req=False, where=w)})
    if seen != cast_ids:
        raise Invalid(f"chapters: nhân vật không xuất hiện ở chương nào: {', '.join(sorted(cast_ids - seen))}.")
    pr = sum(1 for c in out if protagonist_id in c["cast"]) / len(out)
    if pr < 0.6:
        raise Invalid(f"chapters: nhân vật chính chỉ xuất hiện ở {int(pr * 100)}% số chương (cần ≥ 60%).")
    return {"version": 1, "total_chapters": len(out), "chapters": out}
