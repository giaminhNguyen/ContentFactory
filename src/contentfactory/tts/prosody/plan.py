"""Speech Plan (D-100): văn bản đã chuẩn hóa -> câu -> ranh giới có LOẠI + khoảng nghỉ -> nhóm tổng hợp (synthesis group).

Hai khái niệm tách bạch:
  ranh giới ngôn ngữ (câu, đoạn, thoại, cảnh, dấu phẩy…)  — mỗi câu có `boundary_after` + `pause_after_ms` theo Prosody Profile;
  ranh giới tổng hợp (synthesis group)                     — những câu được gửi cùng MỘT lần gọi TTS để giữ ngữ cảnh/ngữ điệu.
Dấu phẩy/hai chấm/chấm phẩy KHÔNG tự tạo lời gọi TTS mới. Đoạn, đổi lượt thoại, chuyển cảnh và cuối câu khi nhóm đã đủ dài mới đóng nhóm;
khoảng nghỉ chính xác được chèn ở ranh giới giữa các nhóm (stitcher, theo mẫu). Ranh giới câu NẰM TRONG một nhóm do engine tự xử lý (`realized: "engine"`)
trừ khi engine khai báo `supports_exact_break_ms` (khi đó nhóm được giữ dài hơn và manager gửi kèm `breaks`).

Hàm thuần, tất định: cùng văn bản + profile + luật => cùng plan (không ngẫu nhiên). Không gọi LLM; nhãn ngữ nghĩa tùy chọn (`semantic`) do bên ngoài đưa vào.
"""
from __future__ import annotations

import hashlib
import json

from ...contracts import ErrorClass, StageError
from contentfactory.tts.planner import absorb_unspeakable, has_speech
from .profiles import MAX_PAUSE_MS, RULES_VERSION
from .qc import analyze_plan
from .segmenter import _micro, paragraphs, split_long, split_sentences, strip_marks

SCHEMA = 1
SEMANTIC_LABELS = ("dramatic_reveal", "scene_transition", "emphasis", "hesitation", "speaker_turn")
_CUT_KIND = {"semicolon": "semicolon", "colon": "colon", "comma": "comma", "space": "micro", "hard": "micro"}
_CONTEXT_CHARS = 80


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def boundary_key(text: str, occurrence: int) -> str:
    """Khóa ổn định của ranh giới SAU một câu (cho manual override): băm văn bản câu + thứ tự lần xuất hiện. Đổi chữ ở câu đó => khóa mất hiệu lực."""
    return f"{_sha(text)[:10]}.{occurrence}"


def plan_key(text: str, prosody: dict, seg: dict, joiner: str, caps: dict, semantic: dict | None = None) -> str:
    """Khóa nội dung của speech plan: thứ làm plan khác đi. Đổi thumbnail/template/audio KHÔNG nằm ở đây."""
    blob = {"text": strip_marks(text), "scenes": text.count("§§SCENE§§"), "pauses": prosody["pauses"], "overrides": prosody["overrides"],
            "semantic_llm": prosody["semantic_llm"], "seg": seg, "joiner": joiner, "rules": RULES_VERSION,
            "exact_breaks": bool(caps.get("supports_exact_break_ms")), "context": bool(caps.get("supports_context")),
            "semantic": semantic}
    return _sha(json.dumps(blob, sort_keys=True, ensure_ascii=False))


# ---------------------------------------------------------------- bước 1: câu + loại ranh giới
def _rebuild(base: dict, text: str, last: dict) -> dict:
    return {**base, "text": text, "end": last["end"], "micro": [] if base["split_of"] else _micro(text)}


def _units(text: str, max_chars: int) -> list[dict]:
    """Danh sách câu (đã cắt các câu quá dài theo ranh giới ngôn ngữ) kèm thông tin đoạn/thoại/cảnh."""
    units: list[dict] = []
    pars = paragraphs(text)
    for pi, par in enumerate(pars, 1):
        sents = split_sentences(par["text"])
        pieces: list[dict] = []
        for s in sents:
            parts = split_long(s["text"], max_chars) if len(s["text"]) > max_chars else [{"text": s["text"], "cut": None}]
            for k, part in enumerate(parts):
                last = k == len(parts) - 1
                pieces.append({"text": part["text"], "end": s["end"] if last else f"split:{part['cut']}", "micro": s["micro"] if len(parts) == 1 else [],
                               "split_of": s["text"] if len(parts) > 1 else None, "hard_cut": part["cut"] == "hard"})
        pieces = absorb_unspeakable(pieces, max_chars, " ", _rebuild)          # mẩu chỉ dấu câu (`…`, `?!`, `(…)`) gộp vào câu kề, không thành câu riêng
        for k, p in enumerate(pieces):
            p.update(para=pi, dialogue=par["dialogue"], last_in_para=k == len(pieces) - 1, scene_after=par["scene_after"] and k == len(pieces) - 1,
                     next_dialogue=bool(pi < len(pars) and pars[pi]["dialogue"]))
            units.append(p)
    return units


def _boundary(u: dict, final: bool) -> tuple[str, str]:
    """(loại ranh giới, lý do) SAU câu `u`."""
    if final:
        return "end", "end"
    if u["last_in_para"]:
        if u["scene_after"]:
            return "scene", "auto:scene"
        if u["dialogue"] and u["next_dialogue"]:
            return "dialogue", "auto:dialogue_turn"
        if u["end"] == "ellipsis":
            return "paragraph", "auto:paragraph+ellipsis_end"
        return "paragraph", "auto:paragraph"
    e = u["end"]
    if e.startswith("split:"):
        return _CUT_KIND[e[6:]], f"auto:split_{e[6:]}"
    return {"question": "question", "exclamation": "exclamation", "ellipsis": "ellipsis"}.get(e, "sentence"), f"auto:{e if e != 'none' else 'sentence'}"


def _apply_semantic(u: dict, kind: str, pause: int, label: str | None, pauses: dict) -> tuple[str, int, str | None, bool]:
    """Nhãn ngữ nghĩa (tùy chọn) chỉ ĐỔI LOẠI/độ mạnh của ranh giới; mili-giây vẫn lấy từ bảng profile (không để LLM tự đặt thời gian)."""
    if not label:
        return kind, pause, None, False
    if label == "dramatic_reveal":
        return "reveal", max(pause, pauses["reveal"]), "semantic:dramatic_reveal", True
    if label == "scene_transition":
        return "scene", max(pause, pauses["scene"]), "semantic:scene_transition", True
    if label == "speaker_turn":
        return "dialogue", max(pause, pauses["dialogue"]), "semantic:speaker_turn", True
    if label == "hesitation":
        return kind, min(pause, pauses["ellipsis_hesitation"]), "semantic:hesitation", False
    if label == "emphasis":
        return kind, min(MAX_PAUSE_MS, round(pause * 1.2)), "semantic:emphasis", False
    return kind, pause, None, False


# ---------------------------------------------------------------- bước 2: nhóm tổng hợp
def build_speech_plan(text: str, flat: dict, caps: dict, prosody: dict, semantic: dict | None = None) -> dict:
    """`text`: văn bản đã chuẩn hóa (có thể chứa dấu phân cảnh). `flat`: TTS profile hiệu lực (segment, joiner). `prosody`: resolve_prosody().
    `semantic`: {"labels": {thứ_tự_câu(str, từ 1): nhãn}, "source": ...} hoặc None. Trả speech plan (dict JSON-able)."""
    seg, joiner = flat["segment"], flat["joiner"]
    pref, mx, mn = int(seg["preferred_chars"]), int(seg["max_chars"]), int(seg["min_chars"])
    pauses, overrides = prosody["pauses"], prosody["overrides"]
    native = bool(caps.get("supports_exact_break_ms"))
    labels = {int(k): v for k, v in ((semantic or {}).get("labels") or {}).items() if v in SEMANTIC_LABELS}
    units = _units(text, mx)
    warnings: list[str] = []
    seen: dict[str, int] = {}
    n_units = len(units)
    segs: list[dict] = []
    for i, u in enumerate(units):
        kind, reason = _boundary(u, i == n_units - 1)
        pause = 0 if kind == "end" else pauses[kind]
        if reason == "auto:paragraph+ellipsis_end":
            pause = max(pauses["paragraph"], pauses["ellipsis"])
        locked = False
        label = labels.get(i + 1)
        if kind != "end":
            kind, pause, why, brk = _apply_semantic(u, kind, pause, label, pauses)
            reason = why or reason
            locked = brk
        occ = seen[u["text"]] = seen.get(u["text"], 0) + 1
        key = boundary_key(u["text"], occ)
        ov = overrides.get(key)
        manual = bool(ov) and kind != "end"
        if manual:
            pause, reason, locked = int(ov["pause_ms"]), "manual", True
        segs.append({"id": f"s{i + 1:04d}", "key": key, "text": u["text"], "paragraph_id": f"p{u['para']:04d}", "dialogue": u["dialogue"],
                     "boundary_after": kind, "pause_after_ms": int(pause), "source": reason, "locked": locked, "manual_override": manual,
                     "micro": u["micro"], "_para": u["para"], "_hard": u["hard_cut"], "_split": u["split_of"]})
    unmatched = sorted(set(overrides) - {s["key"] for s in segs})
    if unmatched:
        warnings.append(f"{len(unmatched)} manual override không còn khớp câu nào (văn bản đã đổi): {unmatched[:5]}")
    for s in segs:
        s["boundary_before"] = "start"
    for prev, s in zip(segs, segs[1:]):
        s["boundary_before"] = prev["boundary_after"]
    # ---- ghép nhóm: không bao giờ vượt đoạn; đóng nhóm tại ranh giới đoạn/thoại/cảnh/khóa; câu thường chỉ đóng khi vượt preferred_chars
    groups: list[list[dict]] = []
    cur: list[dict] = []

    def width(items: list[dict], extra: str = "") -> int:
        return len(joiner.join([x["text"] for x in items] + ([extra] if extra else [])))

    for s in segs:
        if cur and (cur[-1]["_para"] != s["_para"] or width(cur, s["text"]) > pref or cur[-1]["locked"]):
            groups.append(cur)
            cur = []
        cur.append(s)
    if cur:
        groups.append(cur)
    merged: list[list[dict]] = []
    for g in groups:                                           # không để mẩu cụt: nhóm quá ngắn gộp vào nhóm trước CÙNG đoạn nếu vừa
        if merged and merged[-1][-1]["_para"] == g[0]["_para"] and width(g) < mn and not merged[-1][-1]["locked"] \
                and width(merged[-1] + g) <= mx:
            merged[-1] = merged[-1] + g
        else:
            merged.append(g)
    out_groups: list[dict] = []
    for gi, g in enumerate(merged, 1):
        gid = f"g{gi:04d}"
        last = g[-1]
        breaks, off = [], 0
        for s in g:
            s["synthesis_group"] = gid
            off += len(s["text"])
            if s is not last and native:
                breaks.append({"after_char": off, "ms": s["pause_after_ms"], "boundary": s["boundary_after"]})
            off += len(joiner)
            s["realized"] = "external" if s is last else ("native" if native else "engine")
        out_groups.append({"id": gid, "text": joiner.join(s["text"] for s in g), "segments": [s["id"] for s in g], "paragraph_id": g[0]["paragraph_id"],
                           "boundary_after": last["boundary_after"], "pause_after_ms": last["pause_after_ms"], "chars": width(g),
                           "reason": last["source"], **({"breaks": breaks} if breaks else {})})
    flat_segs = [s for g in merged for s in g]
    for i, s in enumerate(flat_segs):                           # ngữ cảnh lân cận (tùy chọn cho engine hỗ trợ `supports_context`)
        s["previous_context"] = flat_segs[i - 1]["text"][-_CONTEXT_CHARS:] if i else ""
        s["next_context"] = flat_segs[i + 1]["text"][:_CONTEXT_CHARS] if i + 1 < len(flat_segs) else ""
    split_sentences_ = sorted({s["_split"] for s in flat_segs if s["_split"]})
    hard = [s["id"] for s in flat_segs if s["_hard"]]
    if hard:
        warnings.append(f"{len(hard)} câu không có ranh giới ngôn ngữ trong giới hạn engine nên phải cắt cứng: {hard[:5]}")
    for s in flat_segs:
        for k in ("_para", "_hard", "_split"):
            s.pop(k, None)
    plan = {"schema": SCHEMA, "mode": "prosody", "rules_version": RULES_VERSION, "profile": prosody["profile"], "pauses": prosody["pauses"],
            "scale": prosody["scale"], "strategy": "native_breaks" if native else "external_pauses", "joiner": joiner,
            "limits": {"preferred_chars": pref, "max_chars": mx, "min_chars": mn},
            "text_sha256": _sha(strip_marks(text)), "semantic": ({"source": (semantic or {}).get("source"), "labels": len(labels)} if semantic else None),
            "segments": flat_segs, "groups": out_groups, "split_sentences": [{"chars": len(x), "text": x[:120]} for x in split_sentences_],
            "warnings": warnings}
    errs = check_integrity(plan, joiner)
    if errs:
        raise StageError(ErrorClass.POLICY, "SPEECH_PLAN_INVALID", "speech plan không qua kiểm tra nhất quán: " + "; ".join(errs[:3]), {"errors": errs[:10]})
    plan["qc"] = analyze_plan(plan, prosody["qc"])
    return plan


def check_integrity(plan: dict, joiner: str) -> list[str]:
    """Kiểm tra ngay khi tạo plan: không rỗng; mọi nhóm có chữ/số; nhóm phủ đúng thứ tự, không lặp/mất segment; id liên tục; text nhóm = ghép text segment;
    offset `breaks` tăng dần, nằm trong nhóm, đúng ranh giới segment."""
    errs: list[str] = []
    segs = {s["id"]: s for s in plan["segments"]}
    if not plan["groups"]:
        return ["EMPTY_PLAN: không có nhóm tổng hợp nào"]
    if [i for g in plan["groups"] for i in g["segments"]] != [s["id"] for s in plan["segments"]] or len(segs) != len(plan["segments"]):
        errs.append("groups không phủ đúng thứ tự, không lặp, không mất segment")
        return errs
    for n, g in enumerate(plan["groups"], 1):
        texts = [segs[i]["text"] for i in g["segments"]]
        if g["id"] != f"g{n:04d}":
            errs.append(f"id nhóm không liên tục ở vị trí {n}: {g['id']}")
        if not has_speech(g["text"]):
            errs.append(f"nhóm {g['id']} không có chữ/số để đọc")
        if g["text"] != joiner.join(texts):
            errs.append(f"text nhóm {g['id']} không bằng phần ghép các segment")
        prev, upto = 0, 0
        for b in g.get("breaks", []):
            upto += 1
            want = len(joiner.join(texts[:upto]))
            if b["after_char"] != want or not prev < b["after_char"] < len(g["text"]):
                errs.append(f"break của nhóm {g['id']} sai offset: {b['after_char']} (cần {want}, nhóm dài {len(g['text'])})")
            prev = b["after_char"]
    return errs


def sentence_texts(text: str, max_chars: int) -> list[str]:
    """Các câu theo đúng thứ tự mà plan dùng (cho bộ gán nhãn ngữ nghĩa tùy chọn)."""
    return [u["text"] for u in _units(text, max_chars)]


def segments_of(plan: dict) -> list[dict]:
    """Segment cho TTSManager/AudioProcessor (index, text, pause_after_ms) — mỗi nhóm tổng hợp là một segment; `breaks` đi kèm khi engine hỗ trợ."""
    out = []
    for i, g in enumerate(plan["groups"], 1):
        seg = {"index": i, "text": g["text"], "pause_after_ms": int(g["pause_after_ms"])}
        if g.get("breaks"):
            seg["breaks"] = g["breaks"]
        out.append(seg)
    return out


def legacy_plan(segments: list[dict], flat: dict, report: dict | None = None) -> dict:
    """Speech plan "tương thích" cho job không dùng prosody (đường cũ): chỉ phản ánh segment/pause mà planner cũ đã chọn, để mọi job đều có artifact và QC."""
    para = int(flat["pause_ms"]["paragraph"])
    groups, segs = [], []
    for i, s in enumerate(segments, 1):
        kind = "end" if i == len(segments) else "paragraph" if s["pause_after_ms"] >= para > 0 else "sentence"
        gid = f"g{i:04d}"
        segs.append({"id": f"s{i:04d}", "key": boundary_key(s["text"], 1), "text": s["text"], "boundary_after": kind, "pause_after_ms": s["pause_after_ms"],
                     "synthesis_group": gid, "source": "legacy", "locked": False, "manual_override": False, "realized": "external"})
        groups.append({"id": gid, "text": s["text"], "segments": [f"s{i:04d}"], "boundary_after": kind, "pause_after_ms": s["pause_after_ms"],
                       "chars": len(s["text"]), "reason": "legacy"})
    plan = {"schema": SCHEMA, "mode": "legacy", "rules_version": None, "profile": None, "pauses": flat["pause_ms"], "strategy": "legacy",
            "segments": segs, "groups": groups, "warnings": [], "planner": (report or {}).get("used")}
    plan["qc"] = analyze_plan(plan, {})
    return plan
