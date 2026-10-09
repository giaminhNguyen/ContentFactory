"""Schema (có version) của các artifact Story Remix. Validator ném `Invalid` với lý do tiếng Việt cụ thể — dùng cả để bảo vệ file đọc lại và làm phản hồi thử lại cho LLM."""
from __future__ import annotations

import re
import unicodedata

from . import similarity as SIM
from .core import Invalid

DNA_VERSION = 1
ROLES = {"protagonist", "deuteragonist", "antagonist", "rival", "foil", "mentor", "ally", "love_interest", "confidant", "comic_relief", "catalyst", "gatekeeper", "wildcard"}


def _s(d: dict, k: str, req: bool = True, where: str = "") -> str:
    """Văn bản: KHÔNG giới hạn độ dài (không cắt). Sửa an toàn bằng code: số → chuỗi, danh sách chuỗi → nối bằng "; "."""
    v = d.get(k)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    elif isinstance(v, list) and v and all(isinstance(x, str) for x in v):
        v = "; ".join(x.strip() for x in v if x.strip())
    if v is None or (isinstance(v, str) and not v.strip()):
        if req:
            raise Invalid(f"{where}{k}: bắt buộc.")
        return ""
    if not isinstance(v, str):
        raise Invalid(f"{where}{k}: phải là văn bản.")
    return v.strip()


def _l(d: dict, k: str, req: bool = True, where: str = "") -> list[str]:
    """Danh sách văn bản: KHÔNG giới hạn số mục/độ dài. Sửa an toàn: một chuỗi → [chuỗi], số → chuỗi, bỏ mục rỗng."""
    v = d.get(k)
    if isinstance(v, str):
        v = [v]
    if v is None:
        v = []
    if not isinstance(v, list) or not all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in v):
        raise Invalid(f"{where}{k}: phải là danh sách văn bản.")
    out = [str(x).strip() for x in v if str(x).strip()]
    if req and not out:
        raise Invalid(f"{where}{k}: cần ít nhất một mục.")
    return out


def _n(d: dict, k: str, lo: float, hi: float, where: str = "") -> float:
    """Số trong [lo, hi]. Sửa an toàn: chuỗi số → số; lệch ra ngoài khoảng → kẹp về biên (lệch thang đo, không đổi ý nghĩa)."""
    v = d.get(k)
    if isinstance(v, str):
        try:
            v = float(v.strip().rstrip("%"))
        except ValueError:
            pass
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise Invalid(f"{where}{k}: phải là số trong [{lo}, {hi}].")
    return min(hi, max(lo, v))


def slug(v) -> str:
    """slot_id an toàn: bỏ dấu, chữ thường, ký tự lạ → _ (sửa bằng code thay vì bắt LLM gọi lại)."""
    t = unicodedata.normalize("NFD", str(v or "")).replace("đ", "d").replace("Đ", "D")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", t)).strip("_")


def cast_names(members: list[dict]) -> dict[str, str]:
    """{tên hiển thị đã chuẩn hoá: character_id} — để sửa bằng code khi LLM ghi tên thay vì id."""
    return {" ".join(SIM.words(m["display_name"])): m["character_id"] for m in members}


def cast_id(v, cast_ids: set[str], names: dict[str, str] | None = None) -> str | None:
    """Sửa an toàn bằng code: LLM hay ghi tên ("Đặng Yến", "Yến") hoặc "ch_x (Tên)" thay vì đúng id. Khớp duy nhất mới nhận."""
    v = str(v or "").strip()
    if v in cast_ids:
        return v
    m = re.search(r"ch_[0-9a-f]{12}", v)
    if m and m.group(0) in cast_ids:
        return m.group(0)
    w = " ".join(SIM.words(v))
    hits = {cid for nm, cid in (names or {}).items() if w and (nm == w or nm.endswith(" " + w))}
    return hits.pop() if len(hits) == 1 else None


def _obj(v, what: str) -> dict:
    if not isinstance(v, dict):
        raise Invalid(f"{what}: phải là object JSON.")
    return v


# ---------------------------------------------------------------------------------------------- source DNA
def source_dna(d) -> dict:
    d = _obj(d, "source_dna")
    pc = _obj(d.get("payoff_cadence"), "payoff_cadence")
    return {"version": DNA_VERSION, "genre": _s(d, "genre"), "subgenres": _l(d, "subgenres", req=False), "engagement_engine": _s(d, "engagement_engine"),
            "reward_types": _l(d, "reward_types"), "emotional_promise": _s(d, "emotional_promise"), "hook_pattern": _s(d, "hook_pattern"),
            "payoff_cadence": {"first_payoff_by_pct": int(_n(pc, "first_payoff_by_pct", 1, 60, "payoff_cadence.")), "payoffs_per_10pct": _n(pc, "payoffs_per_10pct", 0.1, 5, "payoff_cadence.")},
            "escalation_pattern": _s(d, "escalation_pattern"), "pacing": _s(d, "pacing"), "tone": _s(d, "tone"), "pov": _s(d, "pov", req=False),
            "audio_requirements": _l(d, "audio_requirements", req=False), "avoid": _l(d, "avoid", req=False)}


# ---------------------------------------------------------------------------------------------- premise
def _slot(s, where: str) -> dict:
    s = _obj(s, where)
    role = str(s.get("role_code") or "").strip().lower()
    if role not in ROLES:
        raise Invalid(f"{where}role_code không hợp lệ ({s.get('role_code')!r}); hợp lệ: {', '.join(sorted(ROLES))}.")
    sid = slug(_s(s, "slot_id", where=where))
    if not sid:
        raise Invalid(f"{where}slot_id: bắt buộc (a-z, 0-9, _).")
    rels = []
    for r in s.get("relationships") or []:
        r = _obj(r, where + "relationships[]")
        rels.append({"with": slug(_s(r, "with", where=where)), "type": re.sub(r"\W+", "_", _s(r, "type", where=where).lower()).strip("_") or "related_to"})
    imp = s.get("importance", 2)
    return {"slot_id": sid, "role_code": role, "importance": imp if imp in (1, 2, 3) else 2, "traits": _l(s, "traits", req=False, where=where),
            "goal": _s(s, "goal", req=False, where=where), "relationships": rels}


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
    if not isinstance(plan, list) or len(plan) < 3:
        raise Invalid(f"{where}payoff_plan: cần ít nhất 3 điểm thưởng cảm xúc.")
    pp = []
    for x in plan:
        x = _obj(x, where + "payoff_plan[]")
        pp.append({"beat": _s(x, "beat", where=where), "at_pct": int(_n(x, "at_pct", 1, 100, where)), "type": _s(x, "type", where=where)})
    return {"id": _s(p, "id", where=where), "logline": _s(p, "logline", where=where), "setting": _s(p, "setting", where=where), "central_conflict": _s(p, "central_conflict", where=where),
            "stakes": _s(p, "stakes", where=where), "twist": _s(p, "twist", where=where), "ending": _s(p, "ending", where=where), "hook": _s(p, "hook", where=where),
            "slots": sl, "payoff_plan": sorted(pp, key=lambda x: x["at_pct"])}


def premise_candidates(d, want: int) -> dict:
    d = _obj(d, "premise_candidates")
    c = d.get("candidates")
    if not isinstance(c, list) or len(c) < 2:
        raise Invalid("candidates: cần ít nhất 2 ý tưởng khác nhau.")
    out, first_err = [], None
    for i, p in enumerate(c[:want + 2]):
        try:
            out.append(premise(p, f"candidates[{i}]. "))
        except Invalid as e:                                         # một ý tưởng hỏng không bắt cả lượt gọi lại: giữ các ý tưởng hợp lệ nếu còn ≥ 2
            first_err = first_err or e
    if len(out) < 2:
        raise first_err or Invalid("candidates: cần ít nhất 2 ý tưởng hợp lệ.")
    if len({p["id"] for p in out}) != len(out):                      # id trùng: đánh số lại bằng code (id chỉ là nhãn nội bộ)
        for i, p in enumerate(out, 1):
            p["id"] = f"P{i}"
    return {"version": 1, "candidates": out}


# ---------------------------------------------------------------------------------------------- bible / outline
def story_bible(d, cast_ids: set[str], names: dict[str, str] | None = None) -> dict:
    d = _obj(d, "story_bible")
    cast = d.get("cast")
    if not isinstance(cast, list):
        raise Invalid("cast: phải là danh sách.")
    got, out_cast = set(), []
    for m in cast:
        m = _obj(m, "cast[]")
        cid = cast_id(m.get("character_id"), cast_ids, names)
        if cid is None:
            raise Invalid(f"cast: character_id {m.get('character_id')!r} không thuộc dàn đã chốt ({', '.join(sorted(cast_ids))}). Không được tự thêm nhân vật chính ngoài dàn.")
        got.add(cid)
        out_cast.append({"character_id": cid, "arc": _s(m, "arc", where="cast[]. "), "secrets": _l(m, "secrets", req=False, where="cast[]. "), "voice_notes": _s(m, "voice_notes", req=False, where="cast[]. ")})
    if got != cast_ids:
        raise Invalid(f"cast: thiếu nhân vật của dàn đã chốt: {', '.join(sorted(cast_ids - got))}.")
    chain = d.get("causal_chain")
    if not isinstance(chain, list) or len(chain) < 3:
        raise Invalid("causal_chain: cần ít nhất 3 mắt xích nhân-quả.")
    cc = []
    for x in chain:
        x = _obj(x, "causal_chain[]")
        cc.append({"event": _s(x, "event", where="causal_chain[]. "), "cause": _s(x, "cause", where="causal_chain[]. "), "effect": _s(x, "effect", where="causal_chain[]. ")})
    setting = _obj(d.get("setting"), "setting")
    return {"version": 1, "title": _s(d, "title"), "premise_id": _s(d, "premise_id"), "setting": {"place": _s(setting, "place", where="setting."), "time": _s(setting, "time", req=False, where="setting.")},
            "world_rules": _l(d, "world_rules", req=False), "cast": out_cast, "causal_chain": cc, "themes": _l(d, "themes", req=False), "originality_notes": _l(d, "originality_notes", req=False)}


def outline(d, cast_ids: set[str], protagonist_id: str, chapters_min: int, chapters_max: int, names: dict[str, str] | None = None) -> dict:
    d = _obj(d, "outline")
    chs = d.get("chapters")
    if not isinstance(chs, list) or not chapters_min <= len(chs) <= chapters_max:
        raise Invalid(f"chapters: cần {chapters_min}–{chapters_max} chương (đang có {len(chs) if isinstance(chs, list) else 'không phải danh sách'}).")
    out, seen = [], set()
    for i, c in enumerate(chs, 1):
        c = _obj(c, f"chapters[{i}]")
        w = f"chapters[{i}]. "
        on = c.get("cast")
        if isinstance(on, list):
            on = [cast_id(x, cast_ids, names) or x for x in on]
        if not isinstance(on, list) or not on or any(x not in cast_ids for x in on):
            raise Invalid(f"{w}cast: danh sách character_id thuộc dàn đã chốt ({', '.join(sorted(cast_ids))}); tên tự do không được chấp nhận.")
        seen.update(on)
        pay = c.get("payoff")
        payoff = None
        if pay is not None:
            pay = _obj(pay, w + "payoff")
            payoff = {"type": _s(pay, "type", where=w + "payoff."), "description": _s(pay, "description", where=w + "payoff.")}
        out.append({"n": i, "title": _s(c, "title", where=w), "goal": _s(c, "goal", where=w), "beats": _l(c, "beats", where=w), "cast": list(dict.fromkeys(on)),
                    "hook": _s(c, "hook", where=w), "payoff": payoff, "cliffhanger": _s(c, "cliffhanger", req=False, where=w)})
    if seen != cast_ids:
        raise Invalid(f"chapters: nhân vật không xuất hiện ở chương nào: {', '.join(sorted(cast_ids - seen))}.")
    pr = sum(1 for c in out if protagonist_id in c["cast"]) / len(out)
    if pr < 0.6:
        raise Invalid(f"chapters: nhân vật chính chỉ xuất hiện ở {int(pr * 100)}% số chương (cần ≥ 60%).")
    return {"version": 1, "total_chapters": len(out), "chapters": out}
