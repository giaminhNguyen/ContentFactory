"""Viết chương Story Remix: từng chương một, có bộ nhớ liên tục (story_memory), kiểm tra chất lượng tất định + tối đa N lượt sửa có mục tiêu, checkpoint theo chương.

Writer KHÔNG thấy transcript nguồn: chỉ nhận Story Bible + đại cương chương + dàn nhân vật đã chốt (đúng tên) + bộ nhớ truyện + đoạn cuối chương trước.
Bộ nhớ chỉ ghi sự kiện/trạng thái của TRUYỆN NÀY; không bao giờ đổi danh tính cốt lõi của nhân vật (hồ sơ toàn cục).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..fsutil import atomic_write_json, atomic_write_text
from . import similarity as SIM
from ..contracts import ErrorClass, StageError
from .core import Invalid, Ledger, extract_json, fail, fingerprint
from .stages import SYS

WRITER_VERSION = "1"
MARK = "###MEMORY###"
MINOR_NAMED_ALLOWANCE = 3                          # tối đa số nhân vật PHỤ có tên ngoài dàn trong cả truyện
STATE_KEYS = ("status", "location", "notes")

CHAPTER_PROMPT = """Viết CHƯƠNG {n}/{total} của truyện audio tiếng Việt (ngôn ngữ nội dung: {lang}).
Độ dài mục tiêu ≈ {target} ký tự (trong khoảng {lo}–{hi}). Chỉ viết lời kể + thoại, KHÔNG tiêu đề chương, KHÔNG đánh số, KHÔNG chú thích kỹ thuật hay lời dẫn của người viết.
Văn phong audio: câu ngắn-vừa, nêu rõ ai đang nói, tránh đại từ mơ hồ, mỗi đoạn một ý.{readability}

DÀN NHÂN VẬT (dùng ĐÚNG các tên này; không tự thêm nhân vật chính hay đổi tên):
{cast}

STORY BIBLE: {bible}

ĐẠI CƯƠNG CHƯƠNG NÀY: {chapter}

BỘ NHỚ CỦA TRUYỆN (sự kiện đã xảy ra, trạng thái nhân vật, mạch chưa giải quyết — phải nhất quán): {memory}

ĐOẠN CUỐI CHƯƠNG TRƯỚC (nối tiếp mượt, không nhắc lại): {tail}

Chương này phải thực hiện đúng "goal", các "beats", có điểm thưởng cảm xúc (nếu đại cương có "payoff") và kết bằng hook/cliffhanger.
Chỉ nhân vật PHỤ được đặt tên ngoài dàn (tối đa {minor_left} người còn lại trong cả truyện), và phải báo trong phần bộ nhớ.
SAU lời kể, xuống dòng, ghi đúng dòng {mark} rồi một JSON:
{{"new_facts": [sự kiện then chốt mới xảy ra], "state_changes": [{{"character_id": "ch_...", "status": "...", "location": "...", "notes": "..."}}], "opened": [mạch mới mở], "resolved": [mạch đã giải quyết], "new_named_persons": [{{"name": "...", "minor": true}}]}}"""

REPAIR_PROMPT = """Bản chương {n} dưới đây chưa đạt. Sửa NGẮN GỌN đúng các lỗi nêu, giữ nguyên phần tốt. Vẫn tuân thủ: chỉ lời kể + thoại, dùng đúng tên trong dàn, độ dài ≈ {target} ký tự.
LỖI CẦN SỬA:
{issues}

DÀN NHÂN VẬT:
{cast}

BẢN HIỆN TẠI:
<<<
{text}
>>>
Trả lại toàn bộ chương đã sửa, rồi dòng {mark} và JSON bộ nhớ như cũ ({{"new_facts": [...], "state_changes": [...], "opened": [...], "resolved": [...], "new_named_persons": [...]}})."""


MEMORY_PROMPT = """Chương {n} dưới đây ĐÃ ĐẠT, KHÔNG viết lại. Phần bộ nhớ đi kèm bị từ chối vì: {err}
Chỉ trả về dòng {mark} rồi MỘT JSON bộ nhớ đúng định dạng:
{{"new_facts": [sự kiện then chốt mới xảy ra], "state_changes": [{{"character_id": "ch_...", "status": "...", "location": "...", "notes": "..."}}], "opened": [mạch mới mở], "resolved": [mạch đã giải quyết], "new_named_persons": [{{"name": "...", "minor": true}}]}}
character_id chỉ dùng các id trong dàn; state_changes chỉ gồm character_id, status, location, notes.

DÀN NHÂN VẬT:
{cast}

CHƯƠNG {n}:
<<<
{text}
>>>"""


class MemoryInvalid(Invalid):
    """Lời kể đạt nhưng phần bộ nhớ sai: giữ lời kể, chỉ hỏi lại bộ nhớ (rẻ hơn nhiều so với viết lại cả chương)."""
    def __init__(self, msg: str, text: str) -> None:
        super().__init__(msg)
        self.text = text


def empty_memory() -> dict:
    return {"version": 1, "facts": [], "character_state": {}, "unresolved": [], "timeline": [], "minor_persons": [], "progress": {"last_chapter": 0}}


def split_output(raw: str) -> tuple[str, dict]:
    if MARK not in raw:
        raise Invalid(f"thiếu dòng {MARK} và JSON bộ nhớ sau lời kể.")
    text, mem = raw.split(MARK, 1)
    text = re.sub(r"^\s*```[a-z]*\s*|\s*```\s*$", "", text.strip())
    try:
        upd = extract_json(mem)
    except Invalid:
        raise Invalid("JSON bộ nhớ sau dòng " + MARK + " không hợp lệ.") from None
    if not isinstance(upd, dict):
        raise Invalid("bộ nhớ phải là object JSON.")
    return text.strip(), upd


def _strs(v, what: str) -> list[str]:
    """Danh sách chuỗi, KHÔNG giới hạn số mục/độ dài (bộ nhớ tự giới hạn khi hiển thị). Sửa an toàn: một chuỗi → [chuỗi], số → chuỗi."""
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in v):
        raise Invalid(f"{what}: phải là danh sách chuỗi.")
    return [str(x).strip() for x in v if str(x).strip()]


def _cast_id(v, cast_ids: set[str], names: dict[str, str]) -> str | None:
    """Sửa an toàn bằng code: LLM hay ghi tên ("Đặng Yến", "Yến") hoặc "ch_x (Tên)" thay vì đúng id. Khớp duy nhất mới nhận."""
    v = str(v or "").strip()
    if v in cast_ids:
        return v
    m = re.search(r"ch_[0-9a-f]{12}", v)
    if m and m.group(0) in cast_ids:
        return m.group(0)
    w = " ".join(SIM.words(v))
    hits = {cid for nm, cid in names.items() if w and (nm == w or nm.endswith(" " + w))}
    return hits.pop() if len(hits) == 1 else None


def check_update(upd: dict, cast_ids: set[str], names: dict[str, str] | None = None) -> dict:
    """`names`: {tên hiển thị đã chuẩn hoá: character_id}. Trạng thái của người ngoài dàn bị bỏ qua (bộ nhớ chỉ theo dõi dàn); khóa danh tính cốt lõi bị bỏ (bất biến)."""
    out = {"new_facts": _strs(upd.get("new_facts"), "new_facts"), "opened": _strs(upd.get("opened"), "opened"), "resolved": _strs(upd.get("resolved"), "resolved"), "state_changes": [], "new_named_persons": []}
    for s in upd.get("state_changes") or []:
        if not isinstance(s, dict):
            raise Invalid("state_changes: mỗi mục phải là object {character_id, status, location, notes}.")
        cid = _cast_id(s.get("character_id"), cast_ids, names or {})
        if cid is None:
            continue
        out["state_changes"].append({"character_id": cid, **{k: str(s[k]) for k in STATE_KEYS if isinstance(s.get(k), (str, int, float)) and str(s[k]).strip()}})
    for p in upd.get("new_named_persons") or []:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"].strip():
            raise Invalid("new_named_persons: mỗi mục cần {name, minor}.")
        out["new_named_persons"].append({"name": p["name"].strip(), "minor": bool(p.get("minor", False))})
    return out


def merge_memory(mem: dict, upd: dict, chapter: int) -> dict:
    m = json.loads(json.dumps(mem))
    m["facts"] = (m["facts"] + upd["new_facts"])[-200:]
    for s in upd["state_changes"]:
        m["character_state"].setdefault(s["character_id"], {}).update({k: v for k, v in s.items() if k != "character_id"})
    low = lambda x: " ".join(SIM.words(x))                                           # noqa: E731
    resolved = {low(r) for r in upd["resolved"]}
    m["unresolved"] = [u for u in m["unresolved"] if low(u) not in resolved] + [o for o in upd["opened"] if low(o) not in {low(u) for u in m["unresolved"]}]
    m["timeline"].append({"chapter": chapter, "events": upd["new_facts"][:3]})
    for p in upd["new_named_persons"]:
        if low(p["name"]) not in {low(x["name"]) for x in m["minor_persons"]}:
            m["minor_persons"].append({**p, "first_chapter": chapter})
    m["progress"]["last_chapter"] = chapter
    return m


def memory_view(mem: dict) -> str:
    return json.dumps({"facts": mem["facts"][-30:], "character_state": mem["character_state"], "unresolved": mem["unresolved"][-12:], "minor_persons": [p["name"] for p in mem["minor_persons"]]}, ensure_ascii=False)


# ---------------------------------------------------------------------------------------------- QA từng chương
def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?…])\s+|\n+", text) if s.strip()]


def source_brand_marks(title: str, channel: str = "") -> list[str]:
    """Dấu hiệu nhận diện KÊNH GỐC cần loại khỏi truyện mới: tên kênh, phần đầu tên video trước "Số/-/|" (vd "Anh Ben Travel"), chữ trong 【】/[] và sau "|" cuối."""
    from ..story.naming import brand_marks
    head = re.split(r"\s+(?:Số|số|-|–|\|)\s*", (title or "").strip(), 1)[0].strip()
    marks = [channel.strip(), head, *brand_marks(title)]
    out = []
    for m in marks:
        m = re.sub(r"\s*-\s*Videos$", "", m).strip()
        if len(m) >= 4 and m.lower() not in {x.lower() for x in out}:
            out.append(m)
    return out


def brand_hit(text: str, marks: list[str]) -> str | None:
    """Dấu hiệu kênh gốc còn sót trong văn bản (so khớp không phân biệt hoa/thường và không phụ thuộc khoảng trắng)."""
    flat = re.sub(r"\s+", "", text).lower()
    return next((m for m in marks if re.sub(r"\s+", "", m).lower() in flat), None)


def qa_chapter(text: str, chapter: dict, members: list[dict], upd: dict, mem_before: dict, target: int, readability: str, marks: list[str] | None = None) -> list[dict]:
    """Lỗi tất định của một chương: [{code, severity: block|warn, message}]. Không dùng LLM (rẻ, lặp lại được)."""
    issues = []
    n = len(text)
    if n < 0.4 * target:
        issues.append({"code": "TOO_SHORT", "severity": "block", "message": f"Chương chỉ {n} ký tự, quá ngắn so với mục tiêu ≈ {target}."})
    elif n < 0.6 * target:
        issues.append({"code": "SHORT", "severity": "warn", "message": f"Chương {n} ký tự, ngắn hơn mục tiêu ≈ {target}."})
    elif n > 1.7 * target:
        issues.append({"code": "TOO_LONG", "severity": "warn", "message": f"Chương {n} ký tự, dài hơn nhiều so với mục tiêu ≈ {target}."})
    hit = brand_hit(text, marks or [])
    if hit:
        issues.append({"code": "SOURCE_BRAND_TRACE", "severity": "block", "message": f"Còn dấu vết kênh/watermark của nguồn cũ trong lời kể: “{hit}”. Xoá hẳn, không thay bằng biến thể."})
    by_id = {m["character_id"]: m for m in members}
    low = " " + " ".join(SIM.words(text)) + " "
    missing = [by_id[c]["display_name"] for c in chapter["cast"] if c in by_id and " " + " ".join(SIM.words(by_id[c]["display_name"])) + " " not in low]
    if missing:
        issues.append({"code": "CAST_NOT_NAMED", "severity": "warn", "message": f"Nhân vật theo đại cương không xuất hiện bằng tên trong chương: {', '.join(missing)}."})
    known = {" ".join(SIM.words(m["display_name"])) for m in members}
    extra = [p["name"] for p in upd["new_named_persons"] if " ".join(SIM.words(p["name"])) not in known]
    already = len(mem_before["minor_persons"])
    over = [p for p in upd["new_named_persons"] if " ".join(SIM.words(p["name"])) not in known and not p["minor"]]
    if over:
        issues.append({"code": "UNAPPROVED_MAIN_CHARACTER", "severity": "block", "message": f"Tạo nhân vật có tên ngoài dàn ở vai không phải phụ: {', '.join(p['name'] for p in over)}. Chỉ được dùng nhân vật trong dàn."})
    elif already + len(extra) > MINOR_NAMED_ALLOWANCE:
        issues.append({"code": "TOO_MANY_MINOR", "severity": "warn", "message": f"Quá nhiều nhân vật phụ có tên ({already + len(extra)} > {MINOR_NAMED_ALLOWANCE}); gộp vào nhân vật trong dàn hoặc để vô danh."})
    names = SIM.proper_names(text, min_count=3)
    known_tokens = {t for k in known for t in k.split()} | {t for x in mem_before["minor_persons"] + upd["new_named_persons"] for t in SIM.words(x["name"])}
    stray = [nm for nm in names if not set(SIM.words(nm)) <= known_tokens]                    # tên (kể cả ghép/viết tắt một phần) có từ không thuộc dàn/nhân vật phụ đã khai
    sents = _sentences(text)
    if sents:
        lens = [len(SIM.words(s)) for s in sents]
        cap = 20 if readability == "high" else 28
        if sum(lens) / len(lens) > cap:
            issues.append({"code": "LONG_SENTENCES", "severity": "warn", "message": f"Câu trung bình {sum(lens) / len(lens):.0f} từ (> {cap}); khó nghe bằng audio."})
    if re.search(r"^\s*(#{1,6}\s|chương\s+\d+|第\d+章)", text, re.I | re.M):
        issues.append({"code": "HEADING_IN_TEXT", "severity": "warn", "message": "Có dòng tiêu đề/đánh số chương trong lời kể."})
    if stray and len(stray) >= 2:
        issues.append({"code": "STRAY_NAMES", "severity": "warn", "message": f"Tên riêng xuất hiện nhiều lần nhưng không thuộc dàn/nhân vật phụ đã khai: {', '.join(stray[:4])}."})
    return issues


def _cast_lines(members: list[dict], profiles: dict) -> str:
    return "\n".join(f"- {m['character_id']} | {m['display_name']} | vai {m['role_code']} | {profiles.get(m['character_id'], {}).get('core_personality', '')[:160]} | cách nói: {profiles.get(m['character_id'], {}).get('communication_style', '')[:100]}" for m in members)


# ---------------------------------------------------------------------------------------------- vòng viết
def write_chapters(llm, ledger: Ledger, out_dir: Path, bible: dict, outline: dict, cast: dict, profiles: dict, dna: dict, lang: str, target_chars: int, readability: str,
                   repair_passes: int, ctx=None, marks: list[str] | None = None) -> dict:
    """Viết mọi chương theo thứ tự; resume theo checkpoint từng chương. Trả {sections, memory, chapters[{n, chars, repairs, issues}], ran, skipped}."""
    ch_dir, mem_dir = out_dir / "chapters", out_dir / "memory"
    ch_dir.mkdir(parents=True, exist_ok=True)
    mem_dir.mkdir(parents=True, exist_ok=True)
    members = cast["members"]
    ids = {m["character_id"] for m in members}
    names = {" ".join(SIM.words(m["display_name"])): m["character_id"] for m in members}
    total = len(outline["chapters"])
    mem = empty_memory()
    bible_fp, cast_fp = fingerprint(b=bible), fingerprint(c=[m["character_id"] for m in members])
    sections, report, ran, skipped = [], [], [], []
    tail = ""
    for ch in outline["chapters"]:
        n = ch["n"]
        text_f, meta_f, mem_f = ch_dir / f"ch_{n:03d}.md", ch_dir / f"ch_{n:03d}.meta.json", mem_dir / f"after_{n:03d}.json"
        fp = fingerprint(ch=ch, bible=bible_fp, cast=cast_fp, prior=fingerprint(m=mem), read=readability, t=target_chars, lang=lang, v=WRITER_VERSION)
        try:
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = None
        if meta and meta.get("fp") == fp and text_f.is_file() and mem_f.is_file():
            try:
                mem = json.loads(mem_f.read_text(encoding="utf-8"))
                tail = text_f.read_text(encoding="utf-8")[-800:]
                sections.append(text_f)
                report.append(meta["report"])
                skipped.append(n)
                if ctx:
                    ctx.progress(n, total, "chương đã có (dùng lại)")
                continue
            except (OSError, ValueError):
                pass
        if ctx:
            ctx.cancel.check()
            ctx.progress(n - 1, total, f"đang viết chương {n}/{total}")
        minor_left = max(0, MINOR_NAMED_ALLOWANCE - len(mem["minor_persons"]))
        prompt = CHAPTER_PROMPT.format(n=n, total=total, lang=lang, target=target_chars, lo=int(target_chars * 0.7), hi=int(target_chars * 1.4), mark=MARK, minor_left=minor_left,
                                       readability=("\nĐộ dễ nghe CAO: câu ≤ 20 từ, nêu rõ người nói ở mỗi lời thoại." if readability == "high" else ""), cast=_cast_lines(members, profiles),
                                       bible=json.dumps({k: bible[k] for k in ("title", "setting", "world_rules", "themes")}, ensure_ascii=False), chapter=json.dumps(ch, ensure_ascii=False),
                                       memory=memory_view(mem), tail=tail or "(đây là chương đầu)")
        state: dict = {}

        def parse(raw: str) -> tuple[str, dict]:
            try:
                text, upd = split_output(raw)
            except Invalid as e:
                text = re.sub(r"^\s*```[a-z]*\s*|\s*```\s*$", "", raw.split(MARK, 1)[0].strip()).strip()
                if len(text) >= 120:
                    raise MemoryInvalid(str(e), text) from None
                raise
            if len(text) < 120:
                raise Invalid("lời kể quá ngắn (< 120 ký tự).")
            try:
                return text, check_update(upd, ids, names)
            except Invalid as e:
                raise MemoryInvalid(str(e), text) from None

        def parse_memory(text: str, raw: str) -> tuple[str, dict]:
            mem_raw = raw.split(MARK, 1)[1] if MARK in raw else raw
            upd = extract_json(mem_raw)
            if not isinstance(upd, dict):
                raise Invalid("bộ nhớ phải là object JSON.")
            return text, check_update(upd, ids, names)

        mem_prompt = lambda text, err: MEMORY_PROMPT.format(n=n, err=err, mark=MARK, cast=_cast_lines(members, profiles), text=text)   # noqa: E731
        text, upd = _ask(llm, ledger, f"chapter_{n}", prompt, parse, ctx, parse_memory, mem_prompt)
        repairs, issues = 0, qa_chapter(text, ch, members, upd, mem, target_chars, readability, marks)
        while issues and repairs < repair_passes and any(i["severity"] in ("block", "warn") for i in issues):
            repairs += 1
            rp = REPAIR_PROMPT.format(n=n, target=target_chars, issues="\n".join(f"- {i['message']}" for i in issues), cast=_cast_lines(members, profiles), text=text, mark=MARK)
            text, upd = _ask(llm, ledger, f"chapter_{n}_repair", rp, parse, ctx, parse_memory, mem_prompt)
            issues = qa_chapter(text, ch, members, upd, mem, target_chars, readability, marks)
        blocking = [i for i in issues if i["severity"] == "block"]
        if blocking:
            atomic_write_text(text_f.with_suffix(".rejected.md"), text)
            raise fail("CHAPTER_QA_FAILED", f"Chương {n} không đạt kiểm tra bắt buộc sau {repairs} lượt sửa: " + "; ".join(i["message"] for i in blocking)[:400],
                       {"hint": "Tăng ‘Số lượt sửa lỗi tối đa’ rồi chạy lại: các chương trước được giữ.", "chapter": n, "issues": issues})
        mem = merge_memory(mem, upd, n)
        rep = {"n": n, "chars": len(text), "repairs": repairs, "issues": issues}
        atomic_write_text(text_f, text + "\n")
        atomic_write_json(mem_f, mem)
        atomic_write_json(meta_f, {"fp": fp, "report": rep})
        atomic_write_json(out_dir / "story_memory.json", mem)
        sections.append(text_f)
        report.append(rep)
        ran.append(n)
        tail = text[-800:]
    atomic_write_json(out_dir / "story_memory.json", mem)
    return {"sections": sections, "memory": mem, "chapters": report, "ran": ran, "skipped": skipped}


def _ask(llm, ledger: Ledger, step: str, prompt: str, parse, ctx, parse_memory=None, mem_prompt=None):
    """Tối đa 3 lượt. Lời kể đạt mà bộ nhớ sai (MemoryInvalid) ⇒ giữ lời kể, các lượt sau chỉ hỏi lại bộ nhớ."""
    err, kept = "", None
    for attempt in range(1, 4):
        if ctx is not None:
            ctx.cancel.check()
        ledger.guard()
        p = mem_prompt(kept, err) if kept is not None else prompt + (f"\n\nLẦN TRƯỚC BỊ TỪ CHỐI: {err}\nTrả lại đúng định dạng." if err else "")
        res = llm.complete(p, system=SYS.replace("MỘT đối tượng JSON hợp lệ", "văn bản theo đúng định dạng yêu cầu"), step=step, ctx=ctx)
        ledger.record(step, res, attempt)
        try:
            return parse_memory(kept, res.get("text", "")) if kept is not None else parse(res.get("text", ""))
        except MemoryInvalid as e:
            err = str(e)[:400]
            if parse_memory is not None:
                kept = e.text
        except Invalid as e:
            err = str(e)[:400]
    raise StageError(ErrorClass.TRANSIENT, "REMIX_LLM_INVALID", f"Bước {step}: LLM không trả đúng định dạng sau 3 lần ({err}).", {"hint": "Chạy lại; các chương đã xong được giữ.", "step": step})


# ---------------------------------------------------------------------------------------------- QA cuối truyện
def final_qa(chapters: list[dict], members: list[dict], text: str, memory: dict, marks: list[str] | None = None) -> dict:
    """Kiểm cuối sau khi ghép story.txt: mọi nhân vật trong dàn có mặt; tỷ lệ chương còn cảnh báo; mạch chưa giải quyết. `accepted` quyết định việc publish kho nhân vật."""
    low = " " + " ".join(SIM.words(text)) + " "
    absent = [m["display_name"] for m in members if " " + " ".join(SIM.words(m["display_name"])) + " " not in low]
    warned = [c["n"] for c in chapters if any(i["severity"] == "warn" for i in c["issues"])]
    blocking = [c["n"] for c in chapters if any(i["severity"] == "block" for i in c["issues"])]
    problems = []
    hit = brand_hit(text, marks or [])
    if hit:
        problems.append({"code": "SOURCE_BRAND_TRACE", "message": f"Truyện còn dấu vết kênh/watermark của nguồn cũ: “{hit}”."})
    if absent:
        problems.append({"code": "CAST_ABSENT", "message": f"Nhân vật trong dàn không xuất hiện trong truyện: {', '.join(absent)}."})
    if blocking:
        problems.append({"code": "BLOCKING_CHAPTERS", "message": f"Chương còn lỗi bắt buộc: {blocking}."})
    if chapters and len(warned) / len(chapters) > 0.3:
        problems.append({"code": "TOO_MANY_WARNINGS", "message": f"{len(warned)}/{len(chapters)} chương còn cảnh báo chất lượng (> 30%)."})
    return {"version": 1, "accepted": not problems, "problems": problems, "metrics": {"chapters": len(chapters), "chapters_with_warnings": warned, "total_chars": len(text), "unresolved_threads": memory.get("unresolved", [])[:10],
                                                                                  "repairs": sum(c["repairs"] for c in chapters)}}
