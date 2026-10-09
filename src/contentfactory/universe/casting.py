"""Autocast: chọn nhân vật cho một truyện từ Kho (ưu tiên dùng lại, không ép) và tạo nhân vật MỚI (staged) cho vai còn thiếu.

Thuật toán tất định (không LLM) ⇒ giải thích được và test được:
 1. mỗi (nhân vật, vai) có điểm fit thành phần: tính cách/kỹ năng, thể loại, lịch sử vai, đa dạng (tránh lạm dụng cùng dàn); vi phạm ràng buộc ⇒ loại;
 2. `reuse_strategy=reuse` chỉ cộng nhẹ (tie-break), KHÔNG vượt sàn MIN_FIT: fit kém ⇒ tạo nhân vật mới;
 3. chọn cả DÀN bằng tối ưu hóa toàn cục (không phải top-1 từng vai) rồi kiểm dàn: tương phản chính–phản diện, giọng nói phân biệt, quan hệ không mâu thuẫn,
    cặp đã đồng xuất hiện quá nhiều; lỗi dàn ⇒ cấm cặp vi phạm và chọn lại (tối đa MAX_PASSES);
 4. vai chưa lấp ⇒ factory (LLM ở Phase 4/5; SyntheticFactory cho test) tạo hồ sơ gốc, kiểm trùng với kho + dàn hiện tại, chỉ STAGED (`character_candidates`);
 5. đóng băng dàn vào story_cast (khóa), ghi world riêng (parallel), quan hệ theo truyện. Không ghi gì vào hồ sơ toàn cục. Chạy lại cùng story_id = trả dàn đã đóng băng.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import re
import time
import uuid

from . import store as S
from .db import dumps
from .store import Universe, err, name_key

MIN_FIT = 0.35
MAX_PASSES = 4
MAX_SLOTS = 12
PAIR_STALE = 2                                                   # đã đồng xuất hiện ≥ ngần này truyện thì bị trừ điểm
COMPATIBLE = {"protagonist": {"deuteragonist", "ally", "confidant"}, "deuteragonist": {"protagonist", "ally", "rival"}, "antagonist": {"rival", "gatekeeper", "wildcard"},
              "rival": {"antagonist", "foil", "deuteragonist"}, "foil": {"rival", "ally"}, "mentor": {"gatekeeper", "confidant", "ally"}, "ally": {"confidant", "protagonist", "deuteragonist"},
              "love_interest": {"confidant", "ally"}, "confidant": {"ally", "mentor", "love_interest"}, "comic_relief": {"ally", "wildcard"}, "catalyst": {"wildcard", "gatekeeper"},
              "gatekeeper": {"mentor", "antagonist", "catalyst"}, "wildcard": {"catalyst", "antagonist", "comic_relief"}}
OPPOSED = {("parent_of", "parent_of"), ("employer_of", "employer_of"), ("killed", "killed")}      # hai chiều cùng loại có hướng ⇒ mâu thuẫn
DIRECTED = {"parent_of", "employer_of", "killed", "mentor_of", "saved"}


def _toks(*parts) -> set[str]:
    return {t for p in parts for t in name_key(" ".join(p) if isinstance(p, (list, tuple, set)) else str(p)).split() if len(t) > 2}


def _list(raw, what: str, mx: int, ln: int) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw) or len(raw) > mx or any(len(x) > ln for x in raw):
        raise err("INVALID_CAST_REQUEST", f"{what}: phải là danh sách ≤ {mx} mục văn bản (mỗi mục ≤ {ln} ký tự).")
    return [x.strip() for x in raw if x.strip()]


def normalize_request(raw: dict, roles: set[str]) -> dict:
    """Kiểm yêu cầu casting (từ outline/premise). Sai ⇒ lỗi rõ ràng."""
    if not isinstance(raw, dict) or not isinstance(raw.get("story_id"), str) or not raw["story_id"].strip():
        raise err("INVALID_CAST_REQUEST", "Thiếu story_id.")
    slots = raw.get("slots")
    if not isinstance(slots, list) or not slots or len(slots) > MAX_SLOTS:
        raise err("INVALID_CAST_REQUEST", f"slots phải là danh sách 1–{MAX_SLOTS} vai.")
    out, seen = [], set()
    for i, s in enumerate(slots):
        if not isinstance(s, dict) or not isinstance(s.get("slot_id"), str) or s["slot_id"] in seen:
            raise err("INVALID_CAST_REQUEST", f"slot #{i + 1}: thiếu/trùng slot_id.")
        seen.add(s["slot_id"])
        if s.get("role_code") not in roles:
            raise err("INVALID_CAST_REQUEST", f"slot {s['slot_id']}: role_code không hợp lệ ({s.get('role_code')!r}).", "Vai hợp lệ: " + ", ".join(sorted(roles)))
        imp = s.get("importance", 2)
        if imp not in (1, 2, 3):
            raise err("INVALID_CAST_REQUEST", f"slot {s['slot_id']}: importance phải là 1, 2 hoặc 3.")
        rels = []
        for r in s.get("relationships") or []:
            if not isinstance(r, dict) or not isinstance(r.get("with"), str) or not isinstance(r.get("type"), str):
                raise err("INVALID_CAST_REQUEST", f"slot {s['slot_id']}: relationships phải là [{{with, type}}].")
            rels.append({"with": r["with"], "type": r["type"].strip()[:40], "direction": r.get("direction", "mutual") if r.get("direction") in ("mutual", "out", "in") else "mutual"})
        out.append({"slot_id": s["slot_id"], "role_code": s["role_code"], "importance": imp, "traits": _list(s.get("traits"), f"slot {s['slot_id']} traits", 12, 80),
                    "skills": _list(s.get("skills"), f"slot {s['slot_id']} skills", 12, 80), "must_do": _list(s.get("must_do"), f"slot {s['slot_id']} must_do", 12, 80),
                    "goal": str(s.get("goal") or "")[:2000], "relationships": rels, "variant_facts": s.get("variant_facts") if isinstance(s.get("variant_facts"), dict) else {},
                    "label": str(s.get("label") or "")[:80]})
    ids = {s["slot_id"] for s in out}
    for s in out:
        for r in s["relationships"]:
            if r["with"] not in ids or r["with"] == s["slot_id"]:
                raise err("INVALID_CAST_REQUEST", f"slot {s['slot_id']}: quan hệ tới slot không tồn tại ({r['with']}).")
    strat = raw.get("reuse_strategy", "reuse")
    if strat not in ("reuse", "create_new"):
        raise err("INVALID_CAST_REQUEST", "reuse_strategy phải là reuse hoặc create_new.")
    return {"story_id": raw["story_id"].strip(), "genre": str(raw.get("genre") or "")[:80], "slots": out, "reuse_strategy": strat, "allow_new": bool(raw.get("allow_new", True)),
            "pinned": _list(raw.get("pinned_character_ids"), "pinned_character_ids", 12, 64), "exclude": set(_list(raw.get("exclude_character_ids"), "exclude_character_ids", 200, 64)),
            "canon_mode": "parallel", "world_constraints": raw.get("world_constraints") if isinstance(raw.get("world_constraints"), dict) else {}}


# ---------------------------------------------------------------------------------------------- chấm điểm
def score(ch: dict, slot: dict, genre: str, recent: int) -> dict:
    """Điểm fit 0..1 + chi tiết giải thích. 0 nếu vi phạm ràng buộc cứng."""
    pers = _toks(ch["core_personality"], ch["temperament"], ch["strengths"], ch["motivations"], ch["communication_style"])
    need = _toks(slot["traits"], slot["skills"])
    trait = len(need & pers) / len(need) if need else 0.5
    bound = _toks(ch["boundaries"])
    must = _toks(slot["must_do"])
    if must & bound:
        return {"total": 0.0, "blocked": f"ràng buộc của nhân vật cấm: {', '.join(sorted(must & bound))}", "parts": {}}
    aff = _toks(ch["genre_affinities"])
    g = _toks(genre)
    genre_fit = (1.0 if g & aff else 0.0) if g and aff else 0.5
    hist = ch.get("_roles") or set()
    role = 1.0 if slot["role_code"] in hist else (0.6 if hist & COMPATIBLE.get(slot["role_code"], set()) else 0.4)
    diversity = 1.0 - min(1.0, recent / 5)
    parts = {"traits": round(trait, 3), "genre": round(genre_fit, 3), "role_history": round(role, 3), "diversity": round(diversity, 3)}
    total = 0.45 * trait + 0.2 * genre_fit + 0.15 * role + 0.2 * diversity
    if need and trait < 0.5:
        total = min(total, MIN_FIT - 0.05)                           # vai đòi hỏi đặc điểm mà nhân vật khớp chưa tới một nửa: thể loại/lịch sử vai không cứu được
    return {"total": round(total, 4), "parts": parts, "blocked": ""}


def _rationale(sc: dict, slot: dict, reused: bool) -> str:
    p = sc["parts"]
    bits = [f"tính cách/kỹ năng khớp {int(p['traits'] * 100)}%", f"thể loại {int(p['genre'] * 100)}%"]
    if p["role_history"] >= 0.99:
        bits.append("từng đảm nhận đúng vai này")
    elif p["role_history"] >= 0.6:
        bits.append("từng đóng vai tương thích")
    if p["diversity"] < 0.6:
        bits.append("đã xuất hiện khá nhiều gần đây")
    return ("Dùng lại: " if reused else "") + "; ".join(bits)


def _pair_history(uni: Universe, ids: list[str]) -> dict[tuple[str, str], int]:
    """Số truyện (đã publish) mà từng cặp nhân vật cùng xuất hiện."""
    if len(ids) < 2:
        return {}
    ph = ",".join("?" * len(ids))
    rows = uni.db.q(f"SELECT story_id, character_id FROM appearances WHERE character_id IN ({ph})", tuple(ids))
    by: dict[str, set[str]] = {}
    for r in rows:
        by.setdefault(r["story_id"], set()).add(r["character_id"])
    out: dict[tuple[str, str], int] = {}
    for members in by.values():
        for a, b in itertools.combinations(sorted(members), 2):
            out[(a, b)] = out.get((a, b), 0) + 1
    return out


def _solve(slots: list[dict], cand: dict[tuple[str, str], float], ids: list[str], pair: dict, forced: dict[str, str]) -> dict[str, str]:
    """Gán nhân vật → vai tối đa tổng điểm toàn dàn (DFS có cắt nhánh; ≤ MAX_SLOTS vai). Vai không có ứng viên đủ sàn để trống (⇒ tạo mới)."""
    order = sorted(slots, key=lambda s: (-s["importance"], s["slot_id"]))
    options = {s["slot_id"]: sorted([c for c in ids if cand.get((c, s["slot_id"]), 0) >= MIN_FIT], key=lambda c: -cand[(c, s["slot_id"])])[:6] for s in order}
    best = {"v": -1.0, "a": {}}
    best_possible = {s["slot_id"]: max([cand[(c, s["slot_id"])] for c in options[s["slot_id"]]] + ([cand.get((forced[s["slot_id"]], s["slot_id"]), 1.0)] if s["slot_id"] in forced else []) or [0.0]) * s["importance"]
                     for s in order}

    def dfs(i: int, used: set, cur: dict, val: float) -> None:
        if i == len(order):
            if val > best["v"]:
                best.update(v=val, a=dict(cur))
            return
        if val + sum(best_possible[s["slot_id"]] for s in order[i:]) <= best["v"]:
            return
        s = order[i]
        sid = s["slot_id"]
        if sid in forced:
            opts = [forced[sid]] if forced[sid] not in used else []
        else:
            opts = [c for c in options[sid] if c not in used]
        for c in opts:
            pen = sum(0.05 * s["importance"] for o in cur.values() if pair.get(tuple(sorted((o, c))), 0) >= PAIR_STALE)
            cur[sid] = c
            used.add(c)
            dfs(i + 1, used, cur, val + cand.get((c, sid), 1.0) * s["importance"] - pen)
            used.discard(c)
            del cur[sid]
        dfs(i + 1, used, cur, val)                                   # để trống vai này

    dfs(0, set(), {}, 0.0)
    return best["a"]


def _ensemble_issues(slots: dict[str, dict], assign: dict[str, dict]) -> list[dict]:
    """Lỗi cấp DÀN: [{code, slots:[a,b], message}]. Dùng cả cho nhân vật cũ lẫn mới."""
    issues = []
    by_role: dict[str, list[str]] = {}
    for sid, ch in assign.items():
        by_role.setdefault(slots[sid]["role_code"], []).append(sid)
    for p in by_role.get("protagonist", []):
        for a in by_role.get("antagonist", []):
            x = _toks(assign[p]["core_personality"], assign[p]["motivations"])
            y = _toks(assign[a]["core_personality"], assign[a]["motivations"])
            if x and y and len(x & y) / len(x | y) >= 0.6:
                issues.append({"code": "NO_CONTRAST", "slots": [p, a], "message": "Nhân vật chính và phản diện quá giống nhau về tính cách/động cơ."})
    ids = list(assign)
    for a, b in itertools.combinations(ids, 2):
        va, vb = _toks(assign[a]["communication_style"]), _toks(assign[b]["communication_style"])
        if va and vb and len(va & vb) / len(va | vb) >= 0.8:
            issues.append({"code": "SAME_VOICE", "slots": [a, b], "message": "Hai nhân vật có cách nói chuyện gần như giống hệt."})
        if name_key(assign[a]["display_name"]) == name_key(assign[b]["display_name"]):
            issues.append({"code": "SAME_NAME", "slots": [a, b], "message": "Hai nhân vật trùng tên."})
    directed = {(sid, r["with"], r["type"]) for sid, s in slots.items() for r in s["relationships"] if r["type"] in DIRECTED and r["direction"] != "mutual"}
    for a, b, t in directed:
        if (b, a, t) in directed and a < b:
            issues.append({"code": "CONTRADICTORY_RELATION", "slots": [a, b], "message": f"Quan hệ {t} mâu thuẫn (cả hai chiều)."})
    return issues


def pack_text(text, limit: int, max_items: int = 8) -> list[str]:
    """Chia một đoạn dài thành tối đa `max_items` mục ≤ `limit` ký tự, ngắt ở ranh giới câu/mệnh đề/từ — KHÔNG làm mất chữ nào (hồ sơ nhân vật giới hạn từng mục)."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?;,])\s+", str(text or "").strip()) if p.strip()]
    out, cur = [], ""
    for p in parts:
        for w in p.split(" "):
            while len(w) > limit:                                    # từ dài bất thường: cắt cứng để không vượt giới hạn mục
                if cur:
                    out.append(cur); cur = ""
                out.append(w[:limit]); w = w[limit:]
            if len(cur) + len(w) + 1 > limit and cur:
                out.append(cur); cur = w
            else:
                cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    if len(out) > max_items:                                         # quá nhiều mục: gộp phần dư vào mục cuối chỉ khi còn chỗ; nếu không, báo lỗi rõ thay vì mất chữ
        raise err("CAST_PROFILE_TOO_LONG", f"Văn bản dài {len(str(text))} ký tự không xếp vừa {max_items} mục × {limit} ký tự của hồ sơ nhân vật.", "Rút gọn mô tả vai.")
    return out


def affinity_tag(genre: str, limit: int = 40) -> str:
    """`genre_affinities` của hồ sơ nhân vật là THẺ ngắn (≤ 40 ký tự); thể loại DNA có thể là cả cụm mô tả. Lấy mệnh đề đầu, rồi cắt ở ranh giới từ (chỉ cho thẻ này, không đổi thể loại của truyện)."""
    g = re.split(r"\s*[,;:–—|/]\s*|\s+-\s+", (genre or "").strip(), 1)[0].strip()
    if len(g) > limit:
        g = g[:limit].rsplit(" ", 1)[0].strip()
    return g


# ---------------------------------------------------------------------------------------------- factory tạo nhân vật mới
class SyntheticFactory:
    """Factory tất định cho test/fixture: sinh hồ sơ gốc khác biệt theo (story_id, slot, lần thử). Bản LLM ở Phase 4/5 cùng chữ ký."""
    FIRST = ["Minh", "Thảo", "Quân", "Vy", "Khoa", "Ngọc", "Bách", "Hà", "Tuấn", "Linh", "Phát", "Yến", "Sơn", "Chi", "Đạt", "Mai"]
    LAST = ["Trần", "Lê", "Phạm", "Võ", "Đặng", "Bùi", "Đỗ", "Hồ", "Ngô", "Dương"]
    TRAITS = ["kiên nhẫn", "liều lĩnh", "tỉ mỉ", "thẳng thắn", "kín đáo", "tham vọng", "mềm lòng", "đa nghi", "lạc quan", "cố chấp", "tinh nghịch", "nghiêm khắc",
              "sâu sắc", "bốc đồng", "khiêm tốn", "kiêu hãnh", "chu đáo", "ngạo mạn", "trầm lặng", "hoài nghi"]
    STYLES = ["nói chậm, chọn từ cẩn thận", "nói nhanh, hay ngắt lời", "câu ngắn, dứt khoát", "hay dùng ví von", "mỉa mai nhẹ nhàng", "ôn tồn, hay hỏi ngược lại",
              "thẳng như ruột ngựa", "lịch sự quá mức", "thì thầm, ít khi cao giọng"]

    def __call__(self, slot: dict, ctx: dict, attempt: int) -> dict:
        seed = int(hashlib.sha256(f"{ctx['story_id']}|{slot['slot_id']}|{attempt}".encode()).hexdigest(), 16)
        def r(n: int, k: int) -> int:
            return (seed >> (k * 8)) % n
        name = f"{self.LAST[r(len(self.LAST), 1)]} {self.FIRST[r(len(self.FIRST), 2)]}"
        t = [self.TRAITS[(r(len(self.TRAITS), 3) + 5 * i + attempt) % len(self.TRAITS)] for i in range(3)]
        need = list(slot["traits"])
        core = ", ".join(need + t) + f" ({slot['role_code']})"
        strengths = need[:3] + [t[1]]
        if len(core) > 1000:                                         # tính cách quá dài: core giữ phần ngắn, TOÀN BỘ đặc điểm của vai chuyển sang điểm mạnh (không bỏ chữ nào)
            core, strengths = ", ".join(t) + f" ({slot['role_code']})", need + [t[1]]
        return {"display_name": name if not slot.get("label") else slot["label"], "core_personality": core, "temperament": t[0],
                "motivations": pack_text(slot["goal"], 200) or [f"mục tiêu của vai {slot['role_code']}"], "strengths": [x for s_ in strengths for x in pack_text(s_, 200, 8)], "flaws": [t[2]],
                "communication_style": self.STYLES[(r(len(self.STYLES), 4) + attempt) % len(self.STYLES)], "genre_affinities": [ctx["genre"]] if ctx["genre"] else []}


# ---------------------------------------------------------------------------------------------- casting
def _fingerprint(req: dict) -> str:
    j = {k: (sorted(v) if isinstance(v, set) else v) for k, v in req.items()}
    return hashlib.sha256(json.dumps(j, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def get_cast(uni: Universe, story_id: str) -> dict | None:
    w = uni.db.one("SELECT * FROM worlds WHERE story_id=?", (story_id,))
    if not w:
        return None
    meta = json.loads(w["constraints"])
    members = uni.db.q("SELECT * FROM story_cast WHERE story_id=? ORDER BY rowid", (story_id,))
    rels = uni.db.q("SELECT * FROM story_relationships WHERE story_id=? ORDER BY rel_id", (story_id,))
    cands = {c["candidate_id"]: json.loads(c["profile"]) for c in uni.db.q("SELECT candidate_id, profile FROM character_candidates WHERE story_id=? AND status IN ('staged','published')", (story_id,))}
    out = []
    for m in members:
        prof = cands.get(m["character_id"])
        if prof is None:
            r = uni.db.one("SELECT display_name FROM characters WHERE character_id=?", (m["character_id"],))
            prof = {"display_name": r["display_name"] if r else m["character_id"]}
        fit = meta.get("fit", {}).get(m["character_id"], {})
        out.append({"character_id": m["character_id"], "display_name": m["local_alias"] or prof["display_name"], "role_code": m["role_code"], "variant_id": m["variant_id"],
                    "origin": "created" if m["is_new"] else "reused", "state": m["state"], "lock_status": m["lock_status"], "goal": m["goal"], "arc": m["arc"],
                    "slot_id": meta.get("slots", {}).get(m["character_id"]), "fit": fit.get("score"), "breakdown": fit.get("parts"), "rationale": m["fit_notes"]})
    return {"story_id": story_id, "world_id": w["world_id"], "canon_mode": w["canon_mode"], "genre": w["genre"], "universe_revision": meta.get("universe_revision"),
            "cast_revision": meta.get("cast_revision", 1), "fingerprint": meta.get("fingerprint"), "strategy": meta.get("strategy"), "members": out,
            "relationships": [{k: r[k] for k in ("rel_id", "a_id", "b_id", "type", "direction", "status", "evidence")} for r in rels],
            "state": meta.get("state", "staged"), "issues": meta.get("issues", [])}


def integrity(uni: Universe, story_id: str) -> list[str]:
    """Quan hệ trỏ tới nhân vật không có trong dàn (mồ côi). Phải luôn rỗng."""
    cast = get_cast(uni, story_id)
    if not cast:
        return []
    ids = {m["character_id"] for m in cast["members"]}
    return [r["rel_id"] for r in cast["relationships"] if r["a_id"] not in ids or r["b_id"] not in ids]


def cast_story(uni: Universe, raw_request: dict, factory=None, *, job_id: str | None = None, actor: str = "autocast") -> dict:
    """Chọn + đóng băng dàn nhân vật cho truyện. Cùng story_id & cùng yêu cầu ⇒ trả đúng dàn đã đóng băng (resume/retry không tạo thêm)."""
    factory = factory or SyntheticFactory()
    req = normalize_request(raw_request, uni.role_codes())
    fp = _fingerprint(req)
    existing = get_cast(uni, req["story_id"])
    if existing:
        if existing["state"] == "reverted":
            discard_story(uni, req["story_id"])                      # truyện từng bị hoàn tác: lập dàn mới thay vì kẹt ở dàn cũ
        elif existing["fingerprint"] == fp or existing["state"] != "staged":
            return existing
        discard_story(uni, req["story_id"])                          # yêu cầu đổi khi chưa publish: lập lại dàn (dàn cũ staged bị bỏ)
    return _build(uni, req, fp, factory, job_id, actor, cast_revision=1, raw=raw_request)


def _build(uni: Universe, req: dict, fp: str, factory, job_id, actor: str, cast_revision: int, keep: dict[str, str] | None = None, raw: dict | None = None) -> dict:
    slots = {s["slot_id"]: s for s in req["slots"]}
    pool = []
    for ch in uni.db.q("SELECT * FROM characters WHERE status='active'"):
        if ch["character_id"] in req["exclude"]:
            continue
        c = S._row(ch)
        c["_roles"] = {r["role_code"] for r in uni.db.q("SELECT DISTINCT role_code FROM appearances WHERE character_id=?", (c["character_id"],))}
        c["_recent"] = uni.db.one("SELECT COUNT(*) AS n FROM (SELECT DISTINCT story_id FROM appearances WHERE character_id=? ORDER BY created_at DESC LIMIT 5)", (c["character_id"],))["n"]
        pool.append(c)
    byid = {c["character_id"]: c for c in pool}
    bonus = 0.08 if req["reuse_strategy"] == "reuse" else -0.08
    cand: dict[tuple[str, str], float] = {}
    detail: dict[tuple[str, str], dict] = {}
    for c in pool:
        for sid, s in slots.items():
            sc = score(c, s, req["genre"], c["_recent"])
            detail[(c["character_id"], sid)] = sc
            # bonus ưu tiên chỉ áp khi đã đạt sàn: không bao giờ biến một lựa chọn kém thành chấp nhận được
            cand[(c["character_id"], sid)] = sc["total"] + (bonus if sc["total"] >= MIN_FIT else 0.0) if not sc["blocked"] else 0.0
    ids = [c["character_id"] for c in pool]
    pair = _pair_history(uni, ids)
    forced: dict[str, str] = dict(keep or {})
    notes: list[dict] = []
    for pid in req["pinned"]:                                        # ghim: nhân vật phải tồn tại + active; gán vào vai hợp nhất còn trống
        ch = byid.get(pid)
        if not ch:
            raise err("CAST_PINNED_INVALID", f"Nhân vật ghim {pid} không tồn tại hoặc không ở trạng thái đang dùng.", "Bỏ ghim hoặc khôi phục nhân vật.")
        free = [sid for sid in slots if sid not in forced]
        ok = [sid for sid in free if not detail[(pid, sid)]["blocked"]]
        if not ok:
            raise err("CAST_PINNED_INVALID", f"Nhân vật ghim “{ch['display_name']}” không hợp vai nào còn trống.", "Bỏ ghim hoặc thêm vai.")
        target = max(ok, key=lambda sid: detail[(pid, sid)]["total"])
        forced[target] = pid
        cand[(pid, target)] = max(cand[(pid, target)], 1.0)
    banned: set[tuple[str, str]] = set()
    assign: dict[str, str] = {}
    for _ in range(MAX_PASSES):
        for k in banned:
            cand[k] = 0.0
        assign = _solve(list(slots.values()), cand, ids, pair, forced)
        issues = _ensemble_issues(slots, {sid: byid[c] for sid, c in assign.items()})
        issues = [i for i in issues if all(s in assign for s in i["slots"]) and not all(assign[s] in forced.values() for s in i["slots"])]   # chỉ sửa được lỗi giữa nhân vật đã chọn; cặp do NGƯỜI DÙNG ghim: giữ nhưng báo
        if not issues:
            break
        notes += [{**i, "action": "re-ranked"} for i in issues]
        for i in issues:
            victim = min(i["slots"], key=lambda sid: (slots[sid]["importance"], cand.get((assign[sid], sid), 0)))
            if assign.get(victim) and assign[victim] not in forced.values():
                banned.add((assign[victim], victim))
    final_issues = _ensemble_issues(slots, {sid: byid[c] for sid, c in assign.items()})
    # ---- tạo nhân vật mới cho vai còn thiếu
    created: dict[str, tuple[str, dict]] = {}
    missing = [sid for sid in slots if sid not in assign]
    if missing and not req["allow_new"]:
        raise err("CAST_INCOMPLETE", f"Không đủ nhân vật phù hợp cho vai: {', '.join(slots[s]['role_code'] for s in missing)} và đang tắt việc tạo nhân vật mới.", "Bật “Cho phép tạo nhân vật mới”.")
    ctx = {"story_id": req["story_id"], "genre": affinity_tag(req["genre"])}
    taken = [byid[c] for c in assign.values()]
    for sid in sorted(missing, key=lambda s: -slots[s]["importance"]):
        prof, last_err = None, ""
        for attempt in range(12):                                   # kho đầy dần: tên/tính cách dễ trùng hơn ⇒ thử nhiều lần (không tốn AI)
            try:
                cand_prof = S.clean_profile(factory(slots[sid], ctx, attempt))
            except Exception as e:                                   # noqa: BLE001
                if hasattr(e, "code"):
                    last_err = f"{getattr(e, 'code', '')}: {getattr(e, 'message', e)}"
                    continue
                raise
            dup = bool(uni.near_duplicates(cand_prof)) or any(name_key(t["display_name"]) == name_key(cand_prof["display_name"]) or _jacc(t, cand_prof) >= 0.7
                                                              for t in taken + [p for _, p in created.values()])
            if not dup:
                prof = cand_prof
                break
        if prof is None:
            why = f" Hồ sơ bị từ chối: {last_err[:200]}" if last_err else ""
            raise err("CAST_DUPLICATE", f"Không tạo được nhân vật mới đủ khác biệt cho vai {slots[sid]['role_code']}.{why}", "Thử lại hoặc thêm đặc điểm riêng cho vai.")
        created[sid] = (S.new_id(), prof)
    issues_new = _ensemble_issues(slots, {**{sid: byid[c] for sid, c in assign.items()}, **{sid: p for sid, (_, p) in created.items()}})
    # ---- ghi (một giao dịch): world + cast đóng băng + quan hệ + candidates staged
    now = time.time()
    world_id = "w_" + hashlib.sha256(req["story_id"].encode()).hexdigest()[:12]
    rev = uni.revision()
    fit_meta, slot_of = {}, {}
    with uni.db.tx() as c:
        c.execute("INSERT OR REPLACE INTO worlds(world_id,story_id,canon_mode,genre,constraints,created_at) VALUES (?,?,?,?,?,?)",
                  (world_id, req["story_id"], "parallel", req["genre"], "{}", now))
        by_slot: dict[str, str] = {}
        for sid, cid in assign.items():
            sc = detail[(cid, sid)]
            by_slot[sid] = cid
            vid = _variant(c, cid, world_id, req["story_id"], slots[sid], now)
            fit_meta[cid] = {"score": sc["total"], "parts": sc["parts"]}
            slot_of[cid] = sid
            c.execute("INSERT INTO story_cast(story_id,world_id,character_id,variant_id,role_code,fit_notes,goal,arc,lock_status,state,is_new,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (req["story_id"], world_id, cid, vid, slots[sid]["role_code"], _rationale(sc, slots[sid], True), slots[sid]["goal"], "", "frozen", "staged", 0, now))
        for sid, (cid, prof) in created.items():
            by_slot[sid] = cid
            vid = _variant(c, cid, world_id, req["story_id"], slots[sid], now)
            fit_meta[cid] = {"score": None, "parts": None}
            slot_of[cid] = sid
            c.execute("INSERT INTO character_candidates(candidate_id,story_id,job_id,profile,status,created_at) VALUES (?,?,?,?,?,?)", (cid, req["story_id"], job_id, dumps(prof), "staged", now))
            c.execute("INSERT INTO story_cast(story_id,world_id,character_id,variant_id,role_code,fit_notes,goal,arc,lock_status,state,is_new,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (req["story_id"], world_id, cid, vid, slots[sid]["role_code"], "Nhân vật mới: không có nhân vật sẵn có đủ phù hợp (fit < " + str(MIN_FIT) + ") hoặc đang ưu tiên tạo mới.",
                       slots[sid]["goal"], "", "frozen", "staged", 1, now))
        for sid, s in slots.items():
            for r in s["relationships"]:
                c.execute("INSERT INTO story_relationships(rel_id,story_id,world_id,a_id,b_id,type,direction,status,timeline,evidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          ("rl_" + uuid.uuid4().hex[:10], req["story_id"], world_id, by_slot[sid], by_slot[r["with"]], r["type"], r["direction"], "staged", "", "kế hoạch của truyện"))
        meta = {"universe_revision": rev, "fingerprint": fp, "strategy": req["reuse_strategy"], "state": "staged", "cast_revision": cast_revision, "request": raw, "fit": fit_meta, "slots": slot_of,
                "issues": [{**i, "action": "kept"} for i in (final_issues if not missing else issues_new)] + notes}
        c.execute("UPDATE worlds SET constraints=? WHERE world_id=?", (dumps(meta), world_id))
        uni._commit(c, "story.cast", req["story_id"], actor, "cast", "story", req["story_id"], None, {"members": len(by_slot), "new": len(created), "reused": len(assign)}, job_id)
    return get_cast(uni, req["story_id"])


def _jacc(a: dict, b: dict) -> float:
    x, y = _toks(a["core_personality"], a["motivations"], a["flaws"]), _toks(b["core_personality"], b["motivations"], b["flaws"])
    return len(x & y) / len(x | y) if x and y else 0.0


def _variant(c, cid: str, world_id: str, story_id: str, slot: dict, now: float) -> str | None:
    """AU của nhân vật trong truyện này (nghề, hoàn cảnh…): lưu thành variant, KHÔNG sửa hồ sơ cốt lõi."""
    if not slot.get("variant_facts"):
        return None
    vid = "va_" + uuid.uuid4().hex[:10]
    c.execute("INSERT INTO character_variants(variant_id,character_id,world_id,story_id,facts,deviations,created_at) VALUES (?,?,?,?,?,?,?)",
              (vid, cid, world_id, story_id, dumps(slot["variant_facts"]), "{}", now))
    return vid


def discard_story(uni: Universe, story_id: str) -> None:
    """Bỏ dàn CHƯA publish (job lỗi/hủy/lập lại): xóa cast, quan hệ, candidates staged, variant, world. Không đụng kho chính thức."""
    w = uni.db.one("SELECT * FROM worlds WHERE story_id=?", (story_id,))
    if not w or json.loads(w["constraints"]).get("state") == "published":
        return
    with uni.db.tx() as c:
        for t in ("story_cast", "story_relationships", "character_variants"):
            c.execute(f"DELETE FROM {t} WHERE story_id=?", (story_id,))
        c.execute("DELETE FROM character_candidates WHERE story_id=? AND status='staged'", (story_id,))
        c.execute("DELETE FROM worlds WHERE story_id=?", (story_id,))


def recast(uni: Universe, story_id: str, raw_request: dict, replace: dict[str, str] | None = None, factory=None, job_id: str | None = None) -> dict:
    """Lập lại dàn MỘT lần có kiểm soát (outline đổi lớn hoặc người dùng thay một vai). `replace` {slot_id: character_id} giữ/ép nhân vật cho vai.
    Dàn đã publish hoặc đã recast rồi ⇒ từ chối. Không bao giờ để quan hệ mồ côi (xây lại từ yêu cầu mới)."""
    cur = get_cast(uni, story_id)
    if not cur:
        raise err("STORY_NOT_FOUND", f"Truyện {story_id} chưa có dàn nhân vật.")
    if cur["state"] != "staged":
        raise err("CAST_PUBLISHED", "Truyện đã publish: không thể đổi dàn nhân vật.", "Tạo truyện mới nếu cần dàn khác.")
    if cur["cast_revision"] >= 2:
        raise err("RECAST_LIMIT", "Chỉ được lập lại dàn nhân vật một lần cho mỗi truyện.")
    req = normalize_request({**raw_request, "story_id": story_id}, uni.role_codes())
    keep = {}
    for sid, cid in (replace or {}).items():
        if sid not in {s["slot_id"] for s in req["slots"]}:
            raise err("INVALID_CAST_REQUEST", f"Không có vai {sid}.")
        ch = uni.db.one("SELECT status FROM characters WHERE character_id=?", (cid,))
        if not ch or ch["status"] != "active":
            raise err("CAST_PINNED_INVALID", f"Nhân vật {cid} không tồn tại hoặc không đang dùng.")
        keep[sid] = cid
    fp = _fingerprint(req)
    discard_story(uni, story_id)
    out = _build(uni, req, fp, factory or SyntheticFactory(), job_id, "recast", cast_revision=2, keep=keep, raw={**raw_request, "story_id": story_id})
    left = integrity(uni, story_id)
    if left:
        raise err("CAST_INTEGRITY", f"Quan hệ mồ côi: {left}")
    return out


def list_stories(uni: Universe) -> list[dict]:
    out = []
    for w in uni.db.q("SELECT story_id, world_id, genre, constraints, created_at FROM worlds ORDER BY created_at DESC"):
        meta = json.loads(w["constraints"])
        n = uni.db.one("SELECT COUNT(*) AS n, COALESCE(SUM(is_new),0) AS new FROM story_cast WHERE story_id=?", (w["story_id"],))
        out.append({"story_id": w["story_id"], "world_id": w["world_id"], "genre": w["genre"], "state": meta.get("state", "staged"), "cast_revision": meta.get("cast_revision", 1),
                    "members": n["n"], "new": n["new"], "reused": n["n"] - n["new"], "created_at": w["created_at"]})
    return out


def alternatives(uni: Universe, story_id: str, slot_id: str, limit: int = 8) -> list[dict]:
    """Nhân vật đang dùng xếp theo độ hợp với một vai của truyện (để người dùng tuỳ chọn thay). Chỉ khi dàn còn staged."""
    cast = get_cast(uni, story_id)
    if not cast:
        raise err("STORY_NOT_FOUND", f"Truyện {story_id} chưa có dàn nhân vật.")
    raw = json.loads(uni.db.one("SELECT constraints FROM worlds WHERE story_id=?", (story_id,))["constraints"]).get("request")
    req = normalize_request(raw, uni.role_codes())
    slot = next((s for s in req["slots"] if s["slot_id"] == slot_id), None)
    if not slot:
        raise err("INVALID_CAST_REQUEST", f"Không có vai {slot_id}.")
    taken = {m["character_id"] for m in cast["members"]}
    out = []
    for ch in uni.db.q("SELECT * FROM characters WHERE status='active'"):
        c = S._row(ch)
        c["_roles"] = {r["role_code"] for r in uni.db.q("SELECT DISTINCT role_code FROM appearances WHERE character_id=?", (c["character_id"],))}
        recent = uni.db.one("SELECT COUNT(*) AS n FROM (SELECT DISTINCT story_id FROM appearances WHERE character_id=? ORDER BY created_at DESC LIMIT 5)", (c["character_id"],))["n"]
        sc = score(c, slot, req["genre"], recent)
        out.append({"character_id": c["character_id"], "display_name": c["display_name"], "score": sc["total"], "breakdown": sc["parts"], "blocked": sc["blocked"],
                    "in_cast": c["character_id"] in taken, "ok": sc["total"] >= MIN_FIT and not sc["blocked"]})
    return sorted(out, key=lambda d: -d["score"])[:limit]


def replace_member(uni: Universe, story_id: str, slot_id: str, character_id: str) -> dict:
    """Người dùng (tuỳ chọn) thay nhân vật của một vai: dùng đúng cơ chế recast MỘT lần có kiểm soát."""
    meta = uni.db.one("SELECT constraints FROM worlds WHERE story_id=?", (story_id,))
    raw = json.loads(meta["constraints"]).get("request") if meta else None
    if not raw:
        raise err("STORY_NOT_FOUND", f"Truyện {story_id} chưa có dàn nhân vật.")
    return recast(uni, story_id, raw, replace={slot_id: character_id})
