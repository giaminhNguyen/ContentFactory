"""Hàm TẤT ĐỊNH của Remix bám sự việc (không gọi LLM, không I/O): tách cảnh, gộp bản đồ truyện, kiểm kế hoạch, làm sạch + kiểm đầu ra từng cảnh, QA cục bộ.

Nguyên tắc constraint-first: mọi lỗi đoán trước được (marker kỹ thuật, tiêu đề chương, cụm cũ còn sót, độ dài, dấu vết kênh gốc, đoạn lặp) bị bắt và — khi an toàn —
sửa bằng code NGAY SAU KHI sinh; chỉ lỗi về nghĩa mới cần LLM. Dùng lại regex của Story Assembler/validator để không có hai bộ luật lệch nhau.
"""
from __future__ import annotations

import math
import re
import unicodedata

from ..contracts import ErrorClass, StageError
from ..story import assembler as ASM
from ..story.validate import sanitize_prose, validate_story_text
from ..story_remix.core import Invalid
from ..story_remix.writer import brand_hit

SPLIT_TARGET, SPLIT_MAX, GROUP = 2200, 3300, 6          # ký tự mỗi khối nguồn / trần khối / số cảnh mỗi lượt lập bản đồ
LEVEL2_WARN_RATIO = 0.25                                  # ngưỡng CẢNH BÁO tham khảo, không chặn sửa chữa cần thiết
MAX_SPREAD = 0.65                                         # >65% cảnh bị ảnh hưởng (truyện ≥12 cảnh) ⇒ chọn thay thế nhẹ hơn
MAX_CHANGES = 40
LEN_LO, LEN_HI = {1: 0.6, 2: 0.5}, {1: 1.6, 2: 1.9}      # tỉ lệ độ dài cảnh viết lại / cảnh nguồn
BEATS = ("hook", "twist", "climax", "payoff", "setup", "none")
ALLOWED_RIGHTS = ("own", "licensed", "permitted")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(s or ""))).strip().casefold()


def contains(text: str, term: str) -> bool:
    t = norm(term)
    return len(t) >= 2 and t in norm(text)


def sid(i: int) -> str:
    return f"s{i + 1:03d}"


def norm_id(x, n: int) -> str | None:
    """'s1' / 'S001' / 1 / 's 001' → 's001'; ngoài [1..n] ⇒ None (sửa bằng code các biến thể định dạng vô hại)."""
    m = re.fullmatch(r"\s*s?\s*0*(\d{1,5})\s*", str(x).lower())
    return sid(int(m.group(1)) - 1) if m and 1 <= int(m.group(1)) <= n else None


# ---------------------------------------------------------------------------------------------- tách cảnh
def split_scenes(source: str) -> list[str]:
    """Các khối văn xuôi liền kề theo thứ tự, KHÔNG sửa chữ: ưu tiên ranh giới đoạn gốc, đoạn quá dài cắt ở ranh giới câu (ASR hay ra đoạn khổng lồ).
    Giữ nguyên chỗ nối: cùng đoạn gốc nối bằng khoảng trắng, khác đoạn nối bằng dòng trống. shortcut: heuristic độ dài, không phải mô hình ranh giới cảnh."""
    source = source.strip()
    if not source:
        return []
    atoms: list[tuple[str, str]] = []                     # (văn bản, chuỗi nối với atom trước)
    for para in (s.strip() for s in re.split(r"\n\s*\n|\n", source)):
        if not para:
            continue
        sep = "\n\n"
        pieces = [para] if len(para) <= SPLIT_MAX else re.split(r"(?<=[.!?…。！？])\s+(?=[\w\"“‘'\-–—])", para)
        for piece in pieces:
            while len(piece) > SPLIT_MAX:                # không dấu câu: cắt ở khoảng trắng gần SPLIT_TARGET (chữ Hán không có khoảng trắng: cắt cứng)
                cut = piece.rfind(" ", SPLIT_TARGET // 2, SPLIT_TARGET)
                cut = cut if cut != -1 else SPLIT_TARGET
                atoms.append((piece[:cut].strip(), sep))
                piece, sep = piece[cut:].strip(), " "
            if piece:
                atoms.append((piece, sep))
                sep = " "
    chunks: list[str] = []
    cur, size = "", 0
    for text, sep in atoms:
        if cur and size + len(text) > SPLIT_TARGET:
            chunks.append(cur)
            cur, size = "", 0
        cur = text if not cur else cur + sep + text
        size += len(text)
    if cur:
        chunks.append(cur)
    return chunks


def pov_of(source: str) -> str:
    """Ngôi kể (heuristic đếm đại từ; ghi rõ là heuristic trong bản đồ)."""
    w = re.findall(r"\w+", norm(source))
    first = sum(1 for x in w if x in ("tôi", "mình", "tớ"))
    return "ngôi thứ nhất ('tôi')" if w and first / len(w) > 0.004 else "ngôi thứ ba"


# ---------------------------------------------------------------------------------------------- bản đồ truyện
def _strs(v, cap: int, n: int) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    out: list[str] = []
    for x in v:
        x = str(x).strip()[:cap]
        if x and norm(x) not in {norm(o) for o in out}:
            out.append(x)
    return out[:n]


def check_map_group(d, ids: list[str]) -> dict:
    """Một lượt lập bản đồ: đủ + đúng id cảnh; trường lạ/thừa bị cắt về cỡ an toàn bằng code (không bắt LLM làm lại vì độ dài)."""
    if not isinstance(d, dict) or not isinstance(d.get("scenes"), list):
        raise Invalid("source_map cần object {scenes:[...]}.")
    by: dict[str, dict] = {}
    for x in d["scenes"]:
        k = norm_id(x.get("id"), 99999) if isinstance(x, dict) else None
        if k:
            by.setdefault(k, x)
    miss = [i for i in ids if i not in by]
    if miss:
        raise Invalid(f"Thiếu cảnh: {', '.join(miss)}. Phải trả đủ đúng các id {', '.join(ids)}.")
    out = []
    for i in ids:
        x = by[i]
        beat = str(x.get("beat") or "none").strip().lower()
        out.append({"id": i, "event": str(x.get("event") or "").strip()[:450], "cause": str(x.get("cause") or "").strip()[:300],
                    "effect": str(x.get("effect") or "").strip()[:300], "emotional_role": str(x.get("emotional_role") or "").strip()[:200],
                    "characters": _strs(x.get("characters"), 60, 10), "objects": _strs(x.get("objects"), 60, 10), "beat": beat if beat in BEATS else "none"})
    return {"scenes": out}


def global_map(scenes: list[str], per_scene: list[dict]) -> dict:
    """Bản đồ toàn truyện = gộp tất định từ các lượt map: nhân vật, vật/bí mật xuất hiện lại, hook/twist/cao trào, ngôi kể, kết thúc."""
    chars: dict[str, list[str]] = {}
    objs: dict[str, list[str]] = {}
    for s in per_scene:
        for c in s["characters"]:
            chars.setdefault(c, []).append(s["id"])
        for o in s["objects"]:
            objs.setdefault(o, []).append(s["id"])
    # chi tiết "xuất hiện lại" = có mặt trong ≥2 cảnh theo văn bản nguồn (đếm bằng code, không tin lời LLM)
    recurring = {o: [sid(i) for i, t in enumerate(scenes) if contains(t, o)] for o in objs}
    n = len(per_scene)
    return {"scene_count": n, "pov": pov_of(" ".join(scenes)), "pov_is_heuristic": True,
            "characters": {c: {"scenes": ids, "count": len(ids)} for c, ids in sorted(chars.items(), key=lambda kv: -len(kv[1]))[:20]},
            "recurring_objects": {o: ids for o, ids in recurring.items() if len(ids) >= 2},
            "hook_scene": next((s["id"] for s in per_scene if s["beat"] == "hook"), sid(0)),
            "beats": [{"id": s["id"], "beat": s["beat"]} for s in per_scene if s["beat"] != "none"],
            "ending_scenes": [sid(i) for i in range(max(0, n - 2), n)]}


# ---------------------------------------------------------------------------------------------- kế hoạch
def check_plan(d, scenes: list[str]) -> dict:
    """Kế hoạch thay đổi toàn cục. Lỗi định dạng/nội dung ⇒ Invalid (sửa gọn một lượt); không có phương án nhẹ ⇒ StageError (Hard Stop, không viết âm thầm thành truyện khác)."""
    n = len(scenes)
    if not isinstance(d, dict):
        raise Invalid("Kế hoạch phải là object {changes:[...]}.")
    note = str(d.get("needs_level3") or "").strip()
    raw = d.get("changes")
    if (raw is None or raw == []) and note:
        raise StageError(ErrorClass.POLICY, "REMIX_NEEDS_LEVEL3", "Không có phương án thay chi tiết/cảnh nào đủ nhẹ; cần thay cả tuyến sự kiện (cấp 3) — không tự động chạy.",
                         {"reason": note[:600], "hint": "Chọn nguồn khác hoặc dùng Story Remix; hoặc tự quyết định thay tuyến rồi tạo job mới."}, resource="input")
    if not isinstance(raw, list) or not raw:
        raise Invalid("Chưa có thay đổi cụ thể (changes rỗng). Nếu thật sự chỉ thay được cả tuyến sự kiện, trả {changes:[], needs_level3:\"lý do\"}.")
    if len(raw) > MAX_CHANGES:
        raise Invalid(f"Quá nhiều thay đổi ({len(raw)} > {MAX_CHANGES}); chọn ít thay đổi lớn nhất đủ dùng.")
    changes, seen = [], set()
    for k, c in enumerate(raw, 1):
        if not isinstance(c, dict):
            raise Invalid(f"Thay đổi #{k} phải là object.")
        cid = str(c.get("id") or f"c{k:03d}").strip()
        old, new = str(c.get("old") or "").strip(), str(c.get("new") or "").strip()
        if cid in seen:
            raise Invalid(f"Mã thay đổi {cid} bị trùng.")
        if not old or not new or norm(old) == norm(new):
            raise Invalid(f"{cid}: old/new phải khác nhau và không rỗng.")
        try:
            level = int(c.get("level", 1))
        except (TypeError, ValueError):
            raise Invalid(f"{cid}: level phải là 1 hoặc 2.") from None
        if level == 3:
            raise Invalid(f"{cid}: cấp 3 (thay tuyến sự kiện) không được tự động; thay bằng phương án cấp 1/2 nhẹ hơn.")
        if level not in (1, 2):
            raise Invalid(f"{cid}: level phải là 1 hoặc 2.")
        ids = [i for i in (norm_id(x, n) for x in (c.get("scene_ids") if isinstance(c.get("scene_ids"), list) else [c.get("scene_ids")])) if i]
        found = [sid(i) for i, t in enumerate(scenes) if contains(t, old)]
        if level == 1 and not found:
            raise Invalid(f"{cid}: cụm old \"{old[:60]}\" không có trong nguồn. old phải TRÍCH NGUYÊN VĂN từ nguồn (đúng như xuất hiện trong cảnh).")
        ids = list(dict.fromkeys([*ids, *found]))          # sửa bằng code: gộp cảnh LLM nêu + mọi cảnh thật sự chứa cụm old
        if not ids:
            raise Invalid(f"{cid}: scene_ids rỗng/không hợp lệ (id dạng s001..s{n:03d}).")
        why2 = str(c.get("why_level2") or "").strip()
        if level == 2 and len(why2) < 10:
            raise Invalid(f"{cid}: cấp 2 phải có why_level2 nêu vì sao thay chi tiết (cấp 1) không đủ hợp lý.")
        seen.add(cid)
        changes.append({"id": cid, "old": old, "new": new, "level": level, "scene_ids": ids, "why": str(c.get("why") or "").strip()[:300], **({"why_level2": why2[:300]} if level == 2 else {})})
    plan = {"changes": changes, "global_rules": _strs(d.get("global_rules"), 200, 10), "warnings": []}
    impacted = affected_scenes(scenes, changes)
    if n >= 12 and len(impacted) > int(n * MAX_SPREAD):
        raise Invalid(f"Thay đổi lan sang {len(impacted)}/{n} cảnh (>{int(MAX_SPREAD * 100)}%); chọn chi tiết thay thế ít lan toả hơn.")
    l2 = sum(c["level"] == 2 for c in changes)
    if l2 > max(1, int(len(changes) * LEVEL2_WARN_RATIO)):
        plan["warnings"].append(f"Cấp 2 chiếm {l2}/{len(changes)} thay đổi (> {int(LEVEL2_WARN_RATIO * 100)}%): xem lại có thể thay bằng cấp 1.")
    return plan


def affected_scenes(scenes: list[str], changes: list[dict]) -> list[int]:
    """Cảnh bị ảnh hưởng = cảnh kế hoạch nêu ∪ mọi cảnh còn chứa cụm cũ (đồ vật/chi tiết xuất hiện lại)."""
    out: set[int] = set()
    for c in changes:
        out.update(int(s[1:]) - 1 for s in c["scene_ids"])
        out.update(i for i, t in enumerate(scenes) if contains(t, c["old"]))
    return sorted(out)


def relevant_changes(scene_id: str, text: str, changes: list[dict]) -> list[dict]:
    return [c for c in changes if scene_id in c["scene_ids"] or contains(text, c["old"])]


# ---------------------------------------------------------------------------------------------- làm sạch + kiểm đầu ra
clean_prose = sanitize_prose                         # dùng chung với Story Remix + Assembler (story/validate.py)


def _words(s: str) -> set[str]:
    return set(re.findall(r"\w+", norm(s)))


def check_scene(source: str, text: str, relevant: list[dict], marks: list[str], level: int = 1) -> list[dict]:
    """Lỗi tất định của MỘT cảnh viết lại: [{code, message}]. Không dùng LLM."""
    n, ns = len(text), len(source)
    issues: list[dict] = []
    if not text.strip():
        return [{"code": "EMPTY", "message": "Cảnh rỗng."}]
    if n < LEN_LO[level] * ns:
        issues.append({"code": "TOO_SHORT", "message": f"Cảnh chỉ {n} ký tự (nguồn {ns}): bị rút gọn/mất nội dung. Khôi phục phần bị bỏ từ nguồn; KHÔNG chèn chữ thừa."})
    elif n > LEN_HI[level] * ns:
        issues.append({"code": "TOO_LONG", "message": f"Cảnh dài {n} ký tự (nguồn {ns}): thêm quá nhiều. Giữ độ dài tương đương nguồn, không thêm tình tiết."})
    for c in relevant:
        if not contains(source, c["old"]):
            continue                                       # cảnh chỉ nằm trong kế hoạch (phụ thuộc), không chứa cụm cũ
        if contains(text, c["old"]) and not contains(c["new"], c["old"]):
            issues.append({"code": "OLD_REMAINS", "message": f"Còn cụm cũ \"{c['old']}\" (kế hoạch {c['id']} thay bằng \"{c['new']}\")."})
        elif not contains(text, c["new"]) and len(_words(c["new"]) & _words(text)) < 0.6 * max(1, len(_words(c["new"]))):
            issues.append({"code": "NEW_MISSING", "message": f"Chưa thấy chi tiết mới \"{c['new']}\" của kế hoạch {c['id']}."})
    hit = brand_hit(text, marks)
    if hit:
        issues.append({"code": "SOURCE_BRAND_TRACE", "message": f"Còn dấu vết kênh/watermark nguồn: “{hit}”. Xoá hẳn."})
    bad = validate_story_text(text)
    if bad:
        issues.append({"code": "FORMAT", "message": "Định dạng không hợp lệ cho TTS: " + ", ".join(bad)})
    return issues


# ---------------------------------------------------------------------------------------------- QA tất định toàn truyện
def det_qa(changed: list[str], changes: list[dict], marks: list[str], max_removed: float = 0.35) -> list[dict]:
    """Kiểm tất định TOÀN truyện sau khi ghép: cụm cũ còn sót, dấu vết kênh, định dạng, đoạn lặp giữa các cảnh, cảnh rỗng/mất, và (nếu Assembler sẽ xoá quá nhiều) cảnh gây ra."""
    issues: list[dict] = []
    seen: dict[str, str] = {}
    for i, t in enumerate(changed):
        s = sid(i)
        if not t.strip():
            issues.append({"scene_id": s, "code": "EMPTY", "problem": "Cảnh bị mất nội dung."})
            continue
        for c in changes:
            if contains(t, c["old"]) and not contains(c["new"], c["old"]):
                issues.append({"scene_id": s, "code": "OLD_REMAINS", "problem": f"Còn cụm cũ \"{c['old']}\" (đã chốt thay bằng \"{c['new']}\")."})
        hit = brand_hit(t, marks)
        if hit:
            issues.append({"scene_id": s, "code": "SOURCE_BRAND_TRACE", "problem": f"Còn dấu vết kênh/watermark nguồn “{hit}”."})
        bad = [b for b in validate_story_text(t) if b in ("TECHNICAL_MARKER", "REPEATED_PARAGRAPHS")]
        if bad:
            issues.append({"scene_id": s, "code": "FORMAT", "problem": "Có marker kỹ thuật/đoạn lặp: " + ", ".join(bad)})
        for p in (x.strip() for x in re.split(r"\n\s*\n", t)):
            k = ASM._norm(p)
            if len(k) >= ASM.MIN_DUP_CHARS:
                if k in seen and seen[k] != s:
                    issues.append({"scene_id": s, "code": "REPEATED_CONTENT", "problem": f"Đoạn lặp lại nguyên văn đoạn của cảnh {seen[k]}: “{p[:80]}”."})
                seen.setdefault(k, s)
    if not issues:
        try:
            ASM.assemble(changed, max_removed)
        except StageError as e:
            if e.code != "ASSEMBLER_REMOVED_TOO_MUCH":
                raise
            culprits = (e.detail or {}).get("culprits") or []
            issues.extend({"scene_id": sid(c["section"]), "code": "ASSEMBLER_REMOVES", "problem": f"Assembler sẽ loại {c['removed_ratio']:.0%} nội dung riêng của cảnh này (tiêu đề/marker/lặp)."} for c in culprits)
            if not issues:
                raise
    return issues


# ---------------------------------------------------------------------------------------------- QA ngữ nghĩa: đầu vào/đầu ra
def check_qa(d, n: int) -> dict:
    """Kết quả QA của LLM: {issues:[{scene_id, problem}]}. Id dạng khác được chuẩn hoá, id không tồn tại bị bỏ (không retry vì chuyện định dạng), tối đa 8 vấn đề, mỗi cảnh gộp một."""
    if not isinstance(d, dict) or not isinstance(d.get("issues"), list):
        raise Invalid("QA cần object {issues:[...]}.")
    by: dict[str, list[str]] = {}
    for x in d["issues"]:
        k = norm_id(x.get("scene_id"), n) if isinstance(x, dict) else None
        p = str(x.get("problem") or "").strip() if isinstance(x, dict) else ""
        if k and p:
            by.setdefault(k, []).append(p[:400])
    return {"issues": [{"scene_id": k, "code": "SEMANTIC", "problem": " | ".join(v)[:600]} for k, v in list(by.items())[:8]]}


def merge_issues(issues: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for x in issues:
        out.setdefault(x["scene_id"], []).append(x)
    return out


# ---------------------------------------------------------------------------------------------- ước tính chi phí (thô, công khai giả định)
def estimate(source_chars: int, repair_passes: int = 1, price: dict | None = None, titler: bool = True) -> dict:
    n = max(1, math.ceil(source_chars / (SPLIT_TARGET * 0.8)))
    groups = math.ceil(n / GROUP)
    ct = 3.2                                              # ký tự/token (tiếng Việt, thô)
    scene_tok = SPLIT_TARGET * 0.8 / ct
    steps = [("source_map", groups, groups, GROUP * scene_tok + 700, GROUP * 140),
             ("remix_plan", 1, 2, n * 150 + 900, 700),
             ("scene_rewrite", 1, max(1, int(n * MAX_SPREAD)) * 2, scene_tok + 1300, scene_tok * 1.1),     # tối đa: mỗi cảnh bị ảnh hưởng + 1 lượt thử lại
             ("continuity_qa", 1, 1 + repair_passes, 3000 + n * 40, 250),
             ("repair", 0, repair_passes * 4, scene_tok * 2 + 900, scene_tok * 1.1)]
    if titler:
        steps.append(("title", 1, 3, 1500, 60))
    lo_calls, hi_calls = sum(s[1] for s in steps), sum(s[2] for s in steps)
    tin = [sum(s[1] * s[3] for s in steps), sum(s[2] * s[3] for s in steps)]
    tout = [sum(s[1] * s[4] for s in steps), sum(s[2] * s[4] for s in steps)]
    usd = None
    if price and price.get("in") is not None and price.get("out") is not None:
        usd = {k: round((tin[j] * price["in"] + tout[j] * price["out"]) / 1e6, 2) for j, k in enumerate(("min", "max"))}
    return {"scenes": n, "calls": {"min": lo_calls, "max": hi_calls}, "input_tokens": {"min": int(tin[0]), "max": int(tin[1])},
            "output_tokens": {"min": int(tout[0]), "max": int(tout[1])}, "usd": usd, "chapters": n,
            "assumptions": {"source_chars": source_chars, "chars_per_token": ct, "repair_passes": repair_passes,
                            "note_min": "min = chỉ viết lại 1 cảnh, không sửa; max = 65% cảnh bị ảnh hưởng, mỗi cảnh thử lại 1 lần, dùng hết lượt sửa"},
            "note": ("Chi phí USD: không rõ (chưa cấu hình giá/triệu token trong Cài đặt máy). " if usd is None else "") + "Chỉ là ước tính thô; số thật ghi ở cost_report.json của job."}

