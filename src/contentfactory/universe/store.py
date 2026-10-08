"""Kho nhân vật sống (Living Character Universe): hồ sơ nhân vật bền vững, ID ổn định, revision + khóa lạc quan, audit đầy đủ.

Nguyên tắc: danh tính CỐT LÕI của nhân vật là toàn cục và ổn định; mọi thứ thuộc về MỘT truyện (vai, quan hệ, sống/chết, tình cảm, biến cố) nằm ở bảng
theo `story_id`/`world_id` (xem casting.py/publish.py) — KHÔNG bao giờ ghi ngược lên hồ sơ toàn cục.
Mọi ghi đi qua `_commit` (tăng revision toàn kho + audit trong CÙNG giao dịch).
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
import uuid

from ..contracts import ErrorClass, StageError
from .db import UniverseDB, dumps

ID_RX = re.compile(r"^ch_[0-9a-f]{12}$")
ORIGINS = ("original_generated", "user_created", "approved_import")
STATUSES = ("active", "archived", "staged")

# (khóa, kiểu, giới hạn, nhãn). Kiểu: name | text | list
FIELDS = [
    ("display_name", "name", 80, "Tên hiển thị"),
    ("aliases", "list", (10, 80), "Tên khác"),
    ("core_personality", "text", 1000, "Tính cách cốt lõi"),
    ("temperament", "text", 200, "Khí chất"),
    ("motivations", "list", (8, 200), "Động cơ"),
    ("strengths", "list", (8, 200), "Điểm mạnh"),
    ("flaws", "list", (8, 200), "Điểm yếu"),
    ("communication_style", "text", 300, "Cách nói chuyện"),
    ("boundaries", "list", (8, 200), "Ràng buộc / giới hạn (không bao giờ làm)"),
    ("genre_affinities", "list", (10, 40), "Thể loại hợp"),
    ("visual_cues", "text", 300, "Gợi ý hình ảnh"),
    ("voice_cues", "text", 300, "Gợi ý giọng nói"),
]
FIELD_KEYS = [f[0] for f in FIELDS]
LIST_KEYS = {f[0] for f in FIELDS if f[1] == "list"}
CORE_KEYS = {"core_personality", "temperament", "motivations", "strengths", "flaws", "communication_style", "boundaries"}   # khóa (locked) chặn sửa những trường này


def err(code: str, msg: str, hint: str = "", **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, msg, {"hint": hint, **detail}, resource="input")


def name_key(s: str) -> str:
    """Khóa so khớp tên: không phân biệt hoa/thường, dấu tiếng Việt, ký tự phân cách."""
    s = unicodedata.normalize("NFD", str(s).lower().replace("đ", "d"))
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in s if unicodedata.category(c) != "Mn")).strip()


def _tokens(*parts) -> set[str]:
    return {t for p in parts for t in name_key(" ".join(p) if isinstance(p, list) else p).split() if len(t) > 2}


def new_id() -> str:
    return "ch_" + uuid.uuid4().hex[:12]


def clean_profile(raw: dict, *, partial: bool = False) -> dict:
    """Chuẩn hóa + kiểm hồ sơ. Sai kiểu/quá dài ⇒ lỗi rõ ràng (không cắt/ép âm thầm). partial=True: chỉ kiểm các khóa có mặt."""
    if not isinstance(raw, dict):
        raise err("INVALID_CHARACTER", "Hồ sơ nhân vật phải là object.")
    unknown = sorted(set(raw) - set(FIELD_KEYS))
    if unknown:
        raise err("INVALID_CHARACTER", f"Trường không hỗ trợ: {', '.join(unknown)}.")
    out: dict = {}
    for key, typ, lim, label in FIELDS:
        if key not in raw:
            if not partial:
                out[key] = [] if typ == "list" else ""
            continue
        v = raw[key]
        if typ == "list":
            mx, ln = lim
            if v is None:
                v = []
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise err("INVALID_CHARACTER", f"{label}: phải là danh sách văn bản.")
            items = []
            for x in (re.sub(r"\s+", " ", x).strip() for x in v):
                if x and x not in items:
                    items.append(x)
            if len(items) > mx or any(len(x) > ln for x in items):
                raise err("INVALID_CHARACTER", f"{label}: tối đa {mx} mục, mỗi mục ≤ {ln} ký tự.")
            out[key] = items
        else:
            if v is None:
                v = ""
            if not isinstance(v, str):
                raise err("INVALID_CHARACTER", f"{label}: phải là văn bản.")
            v = v.replace("\r\n", "\n").replace("\r", "\n").strip() if typ == "text" else re.sub(r"\s+", " ", v).strip()
            if len(v) > lim:
                raise err("INVALID_CHARACTER", f"{label}: dài {len(v)} ký tự, tối đa {lim}.")
            out[key] = v
    if not partial and not out["display_name"]:
        raise err("INVALID_CHARACTER", "Tên hiển thị là bắt buộc.")
    if "display_name" in out and not out["display_name"]:
        raise err("INVALID_CHARACTER", "Tên hiển thị là bắt buộc.")
    return out


def _row(r: dict) -> dict:
    out = dict(r)
    for k in LIST_KEYS:
        out[k] = json.loads(out[k])
    out["locked"] = bool(out["locked"])
    out.pop("name_key", None)
    return out


class Universe:
    def __init__(self, db: UniverseDB) -> None:
        self.db = db

    # ---- revision / audit -------------------------------------------------------------------
    def revision(self) -> int:
        r = self.db.one("SELECT COALESCE(MAX(rev),0) AS r FROM universe_revisions")
        return int(r["r"])

    def _commit(self, c, kind: str, ref: str | None, actor: str, action: str, entity: str, entity_id: str | None, before, after,
                job_id: str | None = None, publish_id: str | None = None) -> int:
        now = time.time()
        rev = c.execute("INSERT INTO universe_revisions(ts,kind,ref,publish_id) VALUES (?,?,?,?)", (now, kind, ref, publish_id)).lastrowid
        c.execute("INSERT INTO audit_events(ts,actor,action,entity,entity_id,before,after,job_id,publish_id,rev) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (now, actor, action, entity, entity_id, None if before is None else dumps(before), None if after is None else dumps(after), job_id, publish_id, rev))
        return rev

    # ---- đọc ---------------------------------------------------------------------------------
    def roles(self) -> list[dict]:
        return self.db.q("SELECT role_code, label, description, builtin FROM role_types ORDER BY builtin DESC, role_code")

    def role_codes(self) -> set[str]:
        return {r["role_code"] for r in self.roles()}

    def character(self, character_id: str, *, detail: bool = False) -> dict:
        r = self.db.one("SELECT * FROM characters WHERE character_id=?", (character_id,))
        if not r:
            raise err("CHARACTER_NOT_FOUND", f"Không có nhân vật {character_id}.")
        out = _row(r)
        if detail:
            out["appearances"] = self.db.q("SELECT story_id, world_id, role_code, outcome, notes, created_at, publish_id FROM appearances WHERE character_id=? ORDER BY created_at DESC",
                                           (character_id,))
            out["variants"] = self.db.q("SELECT variant_id, world_id, story_id, facts, deviations FROM character_variants WHERE character_id=?", (character_id,))
            out["audit"] = [{**a, "before": json.loads(a["before"]) if a["before"] else None, "after": json.loads(a["after"]) if a["after"] else None}
                            for a in self.db.q("SELECT ts, actor, action, before, after, job_id, publish_id, rev FROM audit_events WHERE entity='character' AND entity_id=? ORDER BY id DESC LIMIT 50",
                                               (character_id,))]
        return out

    def list_characters(self, q: str = "", status: str = "", role: str = "", genre: str = "", limit: int = 50, offset: int = 0) -> dict:
        where, args = [], []
        if status:
            if status not in STATUSES:
                raise err("INVALID_FILTER", f"Trạng thái không hợp lệ: {status}.")
            where.append("c.status=?")
            args.append(status)
        else:
            where.append("c.status != 'staged'")
        if q.strip():
            k = name_key(q)
            where.append("(c.name_key LIKE ? OR c.aliases LIKE ? OR c.core_personality LIKE ? OR c.character_id=?)")
            args += [f"%{k}%", f"%{q.strip()}%", f"%{q.strip()}%", q.strip()]
        if role:
            where.append("EXISTS (SELECT 1 FROM appearances a WHERE a.character_id=c.character_id AND a.role_code=?)")
            args.append(role)
        if genre:
            where.append("c.genre_affinities LIKE ?")
            args.append(f"%{genre}%")
        w = " AND ".join(where)
        total = self.db.one(f"SELECT COUNT(*) AS n FROM characters c WHERE {w}", tuple(args))["n"]
        rows = self.db.q(f"""SELECT c.*, (SELECT COUNT(*) FROM appearances a WHERE a.character_id=c.character_id) AS appearance_count,
                             (SELECT GROUP_CONCAT(DISTINCT a.role_code) FROM appearances a WHERE a.character_id=c.character_id) AS roles
                             FROM characters c WHERE {w} ORDER BY c.updated_at DESC, c.character_id LIMIT ? OFFSET ?""", tuple(args) + (limit, offset))
        items = []
        for r in rows:
            roles = (r.pop("roles") or "").split(",") if r.get("roles") else []
            cnt = r.pop("appearance_count")
            items.append({**_row(r), "appearance_count": cnt, "roles": [x for x in roles if x]})
        return {"items": items, "total": total, "revision": self.revision()}

    def summary(self) -> dict:
        by = {r["status"]: r["n"] for r in self.db.q("SELECT status, COUNT(*) AS n FROM characters GROUP BY status")}
        stories = self.db.one("SELECT COUNT(DISTINCT story_id) AS n FROM appearances")["n"]
        reuse = self.db.one("""SELECT COUNT(*) AS n FROM appearances a JOIN characters c USING(character_id)
                               WHERE c.created_in_story IS NOT NULL AND c.created_in_story != a.story_id""")["n"]
        new = self.db.one("""SELECT COUNT(*) AS n FROM appearances a JOIN characters c USING(character_id)
                             WHERE c.created_in_story = a.story_id""")["n"]
        last = self.db.one("SELECT created_at FROM change_sets WHERE status='applied' ORDER BY created_at DESC LIMIT 1")
        conflicts = self.db.one("SELECT COUNT(*) AS n FROM audit_events WHERE action='conflict'")["n"]
        staged = self.db.one("SELECT COUNT(*) AS n FROM character_candidates WHERE status='staged'")["n"]
        return {"revision": self.revision(), "active": by.get("active", 0), "archived": by.get("archived", 0), "staged_candidates": staged, "stories": stories,
                "reused_appearances": reuse, "new_character_appearances": new, "last_publish_at": last["created_at"] if last else None, "write_conflicts": conflicts}

    def audit(self, limit: int = 100, entity_id: str | None = None) -> list[dict]:
        if entity_id:
            rows = self.db.q("SELECT * FROM audit_events WHERE entity_id=? ORDER BY id DESC LIMIT ?", (entity_id, limit))
        else:
            rows = self.db.q("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,))
        return [{**a, "before": json.loads(a["before"]) if a["before"] else None, "after": json.loads(a["after"]) if a["after"] else None} for a in rows]

    # ---- trùng lặp ---------------------------------------------------------------------------
    def near_duplicates(self, profile: dict, exclude_id: str | None = None, threshold: float = 0.7) -> list[dict]:
        """Nhân vật gần giống (cùng tên/bí danh, hoặc tính cách+động cơ trùng nhiều). Dùng khi tạo thủ công, nhập Excel và khi AI tạo nhân vật mới."""
        key = name_key(profile.get("display_name", ""))
        names = {key, *(name_key(a) for a in profile.get("aliases", []))} - {""}
        mine = _tokens(profile.get("core_personality", ""), profile.get("motivations", []), profile.get("flaws", []))
        out = []
        for r in self.db.q("SELECT * FROM characters"):
            if r["character_id"] == exclude_id:
                continue
            theirs_names = {r["name_key"], *(name_key(a) for a in json.loads(r["aliases"]))}
            if names & theirs_names:
                out.append({"character_id": r["character_id"], "display_name": r["display_name"], "score": 1.0, "reason": "trùng tên/bí danh", "status": r["status"]})
                continue
            theirs = _tokens(r["core_personality"], json.loads(r["motivations"]), json.loads(r["flaws"]))
            if mine and theirs:
                s = len(mine & theirs) / len(mine | theirs)
                if s >= threshold:
                    out.append({"character_id": r["character_id"], "display_name": r["display_name"], "score": round(s, 3), "reason": "tính cách/động cơ gần như trùng", "status": r["status"]})
        return sorted(out, key=lambda d: -d["score"])

    # ---- ghi ---------------------------------------------------------------------------------
    def _run(self, conn, fn):
        """Chạy `fn(c)` trong giao dịch riêng, hoặc trong giao dịch của người gọi (nhập Excel/publish gộp nhiều thao tác thành MỘT giao dịch)."""
        if conn is not None:
            return fn(conn)
        with self.db.tx() as c:
            return fn(c)

    def create_character(self, raw: dict, *, origin: str = "user_created", actor: str = "user", status: str = "active", job_id: str | None = None,
                         publish_id: str | None = None, created_in_story: str | None = None, allow_duplicate: bool = False, character_id: str | None = None, conn=None) -> dict:
        if origin not in ORIGINS or status not in ("active", "staged"):
            raise err("INVALID_CHARACTER", "origin/status không hợp lệ.")
        prof = clean_profile(raw)
        cid = character_id or new_id()
        if not ID_RX.match(cid):
            raise err("INVALID_CHARACTER", f"ID không hợp lệ: {cid}.")
        now = time.time()

        def work(c) -> dict:
            if not allow_duplicate:                                                                                       # kiểm TRONG giao dịch ghi: hai job cùng tạo một nhân vật không lọt cả hai
                dup = [d for d in self.near_duplicates(prof) if d["score"] >= 0.7]
                if dup:
                    raise err("DUPLICATE_CHARACTER", f"Đã có nhân vật giống “{prof['display_name']}” ({dup[0]['display_name']}, {dup[0]['character_id']}).",
                              "Dùng lại nhân vật đó hoặc đổi tên/tính cách.", duplicates=dup)
            c.execute("INSERT INTO characters(character_id,display_name,name_key,aliases,core_personality,temperament,motivations,strengths,flaws,communication_style,boundaries,"
                      "genre_affinities,visual_cues,voice_cues,origin,status,locked,revision,created_in_story,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,?,?,?)",
                      (cid, prof["display_name"], name_key(prof["display_name"]), dumps(prof["aliases"]), prof["core_personality"], prof["temperament"], dumps(prof["motivations"]),
                       dumps(prof["strengths"]), dumps(prof["flaws"]), prof["communication_style"], dumps(prof["boundaries"]), dumps(prof["genre_affinities"]),
                       prof["visual_cues"], prof["voice_cues"], origin, status, created_in_story, now, now))
            self._commit(c, "character.create", cid, actor, "create", "character", cid, None, {**prof, "origin": origin, "status": status}, job_id, publish_id)
            return {}
        if conn is not None:
            work(conn)
        else:
            with self.db.tx() as c:
                work(c)
        return self.character(cid)

    def _guard(self, c, cur: dict, expected_revision: int | None, actor: str) -> None:
        if expected_revision is None:
            raise err("INVALID_CHARACTER", "Thiếu revision hiện tại của nhân vật.", "Tải lại hồ sơ rồi sửa.")
        if int(expected_revision) != cur["revision"]:
            c.execute("INSERT INTO audit_events(ts,actor,action,entity,entity_id,before,after) VALUES (?,?,?,?,?,?,?)",          # ghi xung đột (bảng điều khiển đếm)
                      (time.time(), actor, "conflict", "character", cur["character_id"], dumps({"expected": expected_revision}), dumps({"actual": cur["revision"]})))
            e = err("REVISION_CONFLICT", f"“{cur['display_name']}” đã được người/job khác sửa (revision {cur['revision']}, bạn đang xem {expected_revision}).",
                      "Tải lại hồ sơ để xem thay đổi mới rồi sửa lại.", current=cur)
            e.commit_anyway = True
            raise e

    def update_character(self, character_id: str, fields: dict, expected_revision: int | None, actor: str = "user", conn=None) -> dict:
        patch = clean_profile(fields, partial=True)

        def work(c) -> None:
            cur = self.character(character_id)
            self._guard(c, cur, expected_revision, actor)
            if cur["status"] == "archived":
                raise err("CHARACTER_ARCHIVED", f"“{cur['display_name']}” đang được lưu trữ.", "Khôi phục nhân vật trước khi sửa.")
            changed = {k: v for k, v in patch.items() if cur[k] != v}
            if not changed:
                return
            if cur["locked"] and set(changed) & CORE_KEYS:
                raise err("CHARACTER_LOCKED", f"“{cur['display_name']}” đang bị khóa cốt lõi: không sửa {', '.join(sorted(set(changed) & CORE_KEYS))}.",
                          "Mở khóa trước nếu thật sự muốn đổi danh tính cốt lõi (mọi truyện sau sẽ thấy thay đổi).")
            if "display_name" in changed or "aliases" in changed:
                merged = {**cur, **changed}
                dup = [d for d in self.near_duplicates({"display_name": merged["display_name"], "aliases": merged["aliases"]}, exclude_id=character_id) if d["score"] >= 1.0]
                if dup:
                    raise err("DUPLICATE_CHARACTER", f"Tên trùng với nhân vật {dup[0]['display_name']} ({dup[0]['character_id']}).", "Chọn tên khác.", duplicates=dup)
            sets, args = [], []
            for k, v in changed.items():
                sets.append(f"{k}=?")
                args.append(dumps(v) if k in LIST_KEYS else v)
            if "display_name" in changed:
                sets.append("name_key=?")
                args.append(name_key(changed["display_name"]))
            c.execute(f"UPDATE characters SET {', '.join(sets)}, revision=revision+1, updated_at=? WHERE character_id=?", (*args, time.time(), character_id))
            self._commit(c, "character.update", character_id, actor, "update", "character", character_id, {k: cur[k] for k in changed}, changed)
        self._run(conn, work)
        return self.character(character_id)

    def set_status(self, character_id: str, status: str, expected_revision: int | None, actor: str = "user", conn=None) -> dict:
        if status not in ("active", "archived"):
            raise err("INVALID_CHARACTER", "Chỉ chuyển giữa active và archived.")

        def work(c) -> None:
            cur = self.character(character_id)
            self._guard(c, cur, expected_revision, actor)
            if cur["status"] == "staged":
                raise err("CHARACTER_STAGED", "Nhân vật đang chờ truyện đạt QA, chưa thuộc kho chính thức.")
            if cur["status"] == status:
                return
            c.execute("UPDATE characters SET status=?, revision=revision+1, updated_at=? WHERE character_id=?", (status, time.time(), character_id))
            self._commit(c, "character.status", character_id, actor, "archive" if status == "archived" else "restore", "character", character_id, {"status": cur["status"]}, {"status": status})
        self._run(conn, work)
        return self.character(character_id)

    def set_lock(self, character_id: str, locked: bool, expected_revision: int | None, actor: str = "user") -> dict:
        with self.db.tx() as c:
            cur = self.character(character_id)
            self._guard(c, cur, expected_revision, actor)
            if cur["locked"] == bool(locked):
                return cur
            c.execute("UPDATE characters SET locked=?, revision=revision+1, updated_at=? WHERE character_id=?", (1 if locked else 0, time.time(), character_id))
            self._commit(c, "character.lock", character_id, actor, "lock" if locked else "unlock", "character", character_id, {"locked": cur["locked"]}, {"locked": bool(locked)})
        return self.character(character_id)
