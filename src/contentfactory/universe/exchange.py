"""Xuất/nhập Excel của Kho nhân vật. DB là nguồn sự thật; Excel chỉ là bản xuất (nhất quán theo MỘT revision) và kênh chỉnh sửa hàng loạt.

Nhập luôn có dry-run: báo từng dòng sẽ tạo/sửa/không đổi/xung đột/lỗi. Dòng sửa phải mang `revision` lúc xuất; DB đã đổi sau đó ⇒ xung đột (không bao giờ ghi đè
âm thầm nội dung mới hơn). Có lỗi (ID sai/không tồn tại/trùng, dữ liệu sai) ⇒ từ chối cả lần nhập. Nhân vật tạo từ Excel là `approved_import` và CHỈ khi người dùng bấm nhập.
"""
from __future__ import annotations

import json
import time
import uuid

from . import store as S
from .store import ID_RX, Universe, clean_profile
from .xlsx import Sheet, XlsxError, read_workbook, write_workbook

CHAR_COLS = ["character_id", "revision", "display_name", "aliases", "core_personality", "temperament", "motivations", "strengths", "flaws", "communication_style",
             "boundaries", "genre_affinities", "visual_cues", "voice_cues", "status", "origin", "locked", "appearances"]
READONLY = {"character_id", "revision", "origin", "locked", "appearances"}
EDITABLE = [k for k in CHAR_COLS if k not in READONLY and k != "status"]
SHEETS = ["README", "Characters", "Role_Types", "Worlds", "Story_Cast", "Relationships", "Appearances", "Candidates", "Change_Log", "Settings"]

README = [
    ["Mục đích", "Bản xuất của Kho nhân vật ContentFactory. CƠ SỞ DỮ LIỆU mới là nơi lưu sự thật; file này để xem và sửa hàng loạt."],
    ["Sheet sửa được", "Chỉ sheet Characters (cột có tiêu đề không phải ID/revision/origin/locked/appearances) và cột status (active|archived). Các sheet khác chỉ để xem."],
    ["Thêm nhân vật", "Thêm dòng mới, ĐỂ TRỐNG character_id. ID sẽ do hệ thống cấp. Danh sách nhiều giá trị: mỗi giá trị một dòng trong ô (Alt+Enter)."],
    ["KHÔNG sửa", "character_id và revision. Nếu nhân vật đã được sửa trong ứng dụng sau khi xuất, dòng đó báo XUNG ĐỘT và không bị ghi đè."],
    ["Nhập lại", "Trong ứng dụng: Kho nhân vật → Nhập Excel → xem trước (dry-run) → xác nhận. Có lỗi thì không nhập gì cả."],
    ["Dòng thời gian", "Mỗi truyện có world_id riêng; vai, quan hệ, sống/chết thuộc truyện đó, không phải hồ sơ toàn cục."],
]


def _cell_list(v) -> str:
    return "\n".join(v) if isinstance(v, list) else str(v or "")


def export_workbook(uni: Universe) -> bytes:
    """Xuất trong MỘT giao dịch đọc ⇒ mọi sheet cùng một revision (ghi ở sheet Settings)."""
    db = uni.db
    with db.snapshot():
        rev = uni.revision()
        chars = db.q("SELECT c.*, (SELECT COUNT(*) FROM appearances a WHERE a.character_id=c.character_id) AS appc FROM characters c WHERE c.status != 'staged' ORDER BY c.created_at, c.character_id")
        rows = []
        for r in chars:
            ch = S._row({k: v for k, v in r.items() if k != "appc"})
            rows.append([ch["character_id"], ch["revision"], ch["display_name"], *(_cell_list(ch[k]) for k in EDITABLE[1:]), ch["status"], ch["origin"], "yes" if ch["locked"] else "", r["appc"]])
        roles = db.q("SELECT role_code,label,description FROM role_types ORDER BY role_code")
        worlds = db.q("SELECT world_id,story_id,canon_mode,genre FROM worlds ORDER BY created_at")
        cast = db.q("SELECT story_id,world_id,character_id,role_code,state,is_new,goal,arc,fit_notes FROM story_cast ORDER BY story_id, role_code")
        rels = db.q("SELECT rel_id,story_id,world_id,a_id,b_id,type,direction,status FROM story_relationships ORDER BY story_id")
        apps = db.q("SELECT story_id,world_id,character_id,role_code,outcome,publish_id,created_at FROM appearances ORDER BY created_at")
        cands = [{**c, "display_name": json.loads(c["profile"]).get("display_name", "")} for c in db.q("SELECT candidate_id,story_id,job_id,profile,status FROM character_candidates ORDER BY created_at")]
        log = db.q("SELECT rev,ts,actor,action,entity,entity_id,job_id,publish_id FROM audit_events ORDER BY id DESC LIMIT 1000")
    ids = {c["character_id"] for c in chars}
    sheets = [
        Sheet("README", ["Mục", "Nội dung"], README, [22, 110]),
        Sheet("Characters", CHAR_COLS, rows, [16, 9, 22, 22, 50, 20, 40, 34, 34, 34, 40, 24, 30, 30, 11, 18, 8, 11],
              lists={CHAR_COLS.index("status"): ["active", "archived"]}),
        Sheet("Role_Types", ["role_code", "label", "description"], [[r["role_code"], r["label"], r["description"]] for r in roles], [18, 26, 70]),
        Sheet("Worlds", ["world_id", "story_id", "canon_mode", "genre"], [[w[k] for k in ("world_id", "story_id", "canon_mode", "genre")] for w in worlds], [22, 22, 14, 20]),
        Sheet("Story_Cast", ["story_id", "world_id", "character_id", "role_code", "state", "is_new", "goal", "arc", "fit_notes"],
              [[c[k] for k in ("story_id", "world_id", "character_id", "role_code", "state", "is_new", "goal", "arc", "fit_notes")] for c in cast], [22, 22, 16, 16, 10, 8, 40, 40, 40]),
        Sheet("Relationships", ["rel_id", "story_id", "world_id", "a_id", "b_id", "type", "direction", "status"],
              [[r[k] for k in ("rel_id", "story_id", "world_id", "a_id", "b_id", "type", "direction", "status")] for r in rels], [16, 22, 22, 16, 16, 18, 10, 10]),
        Sheet("Appearances", ["story_id", "world_id", "character_id", "role_code", "outcome", "publish_id", "created_at"],
              [[a["story_id"], a["world_id"], a["character_id"], a["role_code"], a["outcome"], a["publish_id"], time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(a["created_at"]))] for a in apps], [22, 22, 16, 16, 30, 22, 20]),
        Sheet("Candidates", ["candidate_id", "story_id", "job_id", "display_name", "status"], [[c[k] for k in ("candidate_id", "story_id", "job_id", "display_name", "status")] for c in cands], [18, 22, 10, 26, 12]),
        Sheet("Change_Log", ["rev", "time_utc", "actor", "action", "entity", "entity_id", "job_id", "publish_id"],
              [[a["rev"], time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(a["ts"])), a["actor"], a["action"], a["entity"], a["entity_id"], a["job_id"], a["publish_id"]] for a in log], [8, 20, 10, 12, 12, 16, 10, 22]),
        Sheet("Settings", ["key", "value"], [["universe_revision", rev], ["export_id", uuid.uuid4().hex[:12]], ["exported_at_utc", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())],
                                             ["character_count", len(ids)], ["canon_mode_default", "parallel"], ["schema", "universe-xlsx-1"]], [24, 40]),
    ]
    return write_workbook(sheets)


def _file_info(book: dict) -> dict:
    kv = {r[0]: r[1] for r in book.get("Settings", [])[1:] if len(r) >= 2}
    try:
        rev = int(kv.get("universe_revision", ""))
    except ValueError:
        rev = None
    return {"revision": rev, "export_id": kv.get("export_id"), "exported_at": kv.get("exported_at_utc"), "schema": kv.get("schema")}


def _split(v: str) -> list[str]:
    return [x.strip() for x in str(v).replace("\r", "").split("\n") if x.strip()]


def analyze(uni: Universe, data: bytes) -> dict:
    try:
        book = read_workbook(data)
    except XlsxError as e:
        raise S.err("INVALID_XLSX", str(e), "Chọn file .xlsx do ứng dụng xuất.") from None
    sheet = book.get("Characters")
    if not sheet:
        raise S.err("INVALID_XLSX", "Không thấy sheet “Characters”.", "Dùng file do ứng dụng xuất ra.")
    header = [h.strip() for h in sheet[0]]
    missing = [c for c in ("character_id", "revision", "display_name") if c not in header]
    if missing:
        raise S.err("INVALID_XLSX", f"Thiếu cột: {', '.join(missing)}.", "Không đổi/xóa tiêu đề cột.")
    idx = {h: i for i, h in enumerate(header)}

    def get(row, k):
        return str(row[idx[k]]).strip() if k in idx and idx[k] < len(row) else ""
    out, seen_ids, seen_names = [], set(), {}
    info = _file_info(book)
    current = uni.revision()
    for n, row in enumerate(sheet[1:], start=2):
        if not any(str(c).strip() for c in row):
            continue
        r = {"row": n, "id": get(row, "character_id"), "name": get(row, "display_name"), "action": "", "message": "", "changes": []}
        out.append(r)
        try:
            cid = r["id"]
            raw = {k: (_split(get(row, k)) if k in S.LIST_KEYS else get(row, k)) for k in EDITABLE if k in idx}
            prof = clean_profile(raw, partial=bool(cid))
            status = get(row, "status") or None
            if status and status not in ("active", "archived"):
                raise S.err("INVALID_CHARACTER", f"status phải là active hoặc archived (đang là {status!r}).")
            if not cid:
                if not prof.get("display_name"):
                    raise S.err("INVALID_CHARACTER", "Dòng mới cần có tên hiển thị.")
                key = S.name_key(prof["display_name"])
                if key in seen_names:
                    raise S.err("DUPLICATE_CHARACTER", f"Trùng tên với dòng {seen_names[key]} trong cùng file.")
                seen_names[key] = n
                dup = uni.near_duplicates(clean_profile(raw))
                if dup:
                    raise S.err("DUPLICATE_CHARACTER", f"Giống nhân vật có sẵn {dup[0]['display_name']} ({dup[0]['character_id']}): {dup[0]['reason']}.")
                r.update(action="create", profile=clean_profile(raw), status=status)
                continue
            if not ID_RX.match(cid):
                raise S.err("INVALID_ID", f"character_id không hợp lệ: {cid!r} (dạng ch_ + 12 ký tự hex).")
            if cid in seen_ids:
                raise S.err("DUPLICATE_ID", f"character_id {cid} xuất hiện nhiều lần trong file.")
            seen_ids.add(cid)
            try:
                cur = uni.character(cid)
            except Exception:                                                              # noqa: BLE001
                raise S.err("UNKNOWN_ID", f"Không có nhân vật {cid} trong kho.") from None
            diff = {k: v for k, v in prof.items() if cur[k] != v}
            sdiff = status if status and status != cur["status"] else None
            if not diff and not sdiff:
                r["action"] = "unchanged"
                continue
            try:
                file_rev = int(get(row, "revision"))
            except ValueError:
                raise S.err("INVALID_ROW", "Thiếu/sai cột revision.", "Không sửa cột revision.") from None
            r["changes"] = sorted(diff) + (["status"] if sdiff else [])
            if file_rev != cur["revision"]:
                r.update(action="conflict", message=f"Nhân vật đã đổi sau khi xuất (revision file {file_rev}, hiện tại {cur['revision']}). Xuất lại để lấy bản mới.", current_revision=cur["revision"])
                continue
            if cur["status"] == "archived" and sdiff != "active":
                raise S.err("CHARACTER_ARCHIVED", "Nhân vật đang lưu trữ: đặt status=active trước khi sửa.")
            if cur["locked"] and set(diff) & S.CORE_KEYS:
                raise S.err("CHARACTER_LOCKED", f"Nhân vật bị khóa cốt lõi, không sửa: {', '.join(sorted(set(diff) & S.CORE_KEYS))}.")
            r.update(action="update", fields=diff, status=sdiff, revision=cur["revision"])
        except Exception as e:                                                             # noqa: BLE001
            if not hasattr(e, "code"):
                raise
            r.update(action="error", code=e.code, message=e.message)
    counts = {a: sum(1 for r in out if r["action"] == a) for a in ("create", "update", "unchanged", "conflict", "error")}
    stale = info["revision"] is not None and info["revision"] < current
    return {"file": info, "current_revision": current, "stale": stale, "rows": out, "counts": counts,
            "can_apply": counts["error"] == 0 and (counts["create"] + counts["update"] > 0),
            "needs_resolution": counts["conflict"] > 0}


def preview_import(uni: Universe, data: bytes) -> dict:
    rep = analyze(uni, data)
    for r in rep["rows"]:
        r.pop("profile", None)
        r.pop("fields", None)
    if rep["counts"]["error"]:
        rep["message"] = "Có lỗi: không thể nhập. Sửa các dòng báo lỗi rồi thử lại."
    elif rep["counts"]["conflict"]:
        rep["message"] = "Có dòng xung đột: chỉ nhập được các dòng còn lại nếu bạn chọn bỏ qua xung đột."
    elif rep["stale"]:
        rep["message"] = "File được xuất từ revision cũ hơn; các dòng không xung đột vẫn nhập an toàn."
    return rep


def apply_import(uni: Universe, data: bytes, actor: str = "user", skip_conflicts: bool = False) -> dict:
    """Nhập nguyên tử: phân tích lại ngay trong giao dịch ghi (không dựa vào kết quả xem trước cũ)."""
    with uni.db.tx() as c:
        rep = analyze(uni, data)
        if rep["counts"]["error"]:
            raise S.err("IMPORT_HAS_ERRORS", "File có dòng lỗi nên không nhập gì cả.", "Xem trước để biết dòng nào lỗi.", report=_slim(rep))
        if rep["counts"]["conflict"] and not skip_conflicts:
            raise S.err("IMPORT_CONFLICTS", "Có nhân vật đã đổi sau khi xuất. Chọn bỏ qua xung đột hoặc xuất lại.", report=_slim(rep))
        created, updated = [], []
        for r in rep["rows"]:
            if r["action"] == "create":
                ch = uni.create_character(r["profile"], origin="approved_import", actor=actor, conn=c)
                if r.get("status") == "archived":
                    uni.set_status(ch["character_id"], "archived", ch["revision"], actor, conn=c)
                created.append(ch["character_id"])
            elif r["action"] == "update":
                rev = r["revision"]
                if r.get("status") == "active":                     # khôi phục trước rồi mới sửa nội dung
                    rev = uni.set_status(r["id"], "active", rev, actor, conn=c)["revision"]
                if r["fields"]:
                    rev = uni.update_character(r["id"], r["fields"], rev, actor, conn=c)["revision"]
                if r.get("status") == "archived":
                    uni.set_status(r["id"], "archived", rev, actor, conn=c)
                updated.append(r["id"])
    return {"created": created, "updated": updated, "skipped_conflicts": rep["counts"]["conflict"], "unchanged": rep["counts"]["unchanged"], "revision": uni.revision()}


def _slim(rep: dict) -> dict:
    return {**rep, "rows": [{k: v for k, v in r.items() if k not in ("profile", "fields")} for r in rep["rows"]]}
