"""Facade Kho nhân vật cho giao diện: chỉ nối HTTP với `universe.*` (không chứa logic nghiệp vụ). Lỗi là StageError có mã + gợi ý tiếng Việt."""
from __future__ import annotations

from ..contracts import ErrorClass, StageError
from ..universe import casting as CA
from ..universe import exchange as EX
from ..universe import store as S
from .service_templates import Raw

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _int(v, what: str) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
        raise StageError(ErrorClass.POLICY, "INVALID_CHARACTER", f"{what} phải là số nguyên.", resource="input")
    return int(v)


class UniverseService:
    def __init__(self, orc) -> None:
        self.orc = orc

    @property
    def u(self):
        return self.orc.universe

    def summary(self) -> dict:
        fields = [{"key": k, "type": t, "limit": list(lim) if isinstance(lim, tuple) else lim, "label": lab, "core": k in S.CORE_KEYS} for k, t, lim, lab in S.FIELDS]
        return {**self.u.summary(), "roles": self.u.roles(), "fields": fields}

    def list(self, q: dict) -> dict:
        def one(k, d=""):
            return (q.get(k) or [d])[0]
        return self.u.list_characters(one("q"), one("status"), one("role"), one("genre"), max(1, min(200, int(one("limit", 50)))), max(0, int(one("offset", 0))))

    def detail(self, cid: str) -> dict:
        return {"character": self.u.character(cid, detail=True), "revision": self.u.revision()}

    def create(self, body: dict) -> dict:
        prof = {k: v for k, v in body.items() if k != "allow_duplicate"}
        return {"character": self.u.create_character(prof, origin="user_created", allow_duplicate=bool(body.get("allow_duplicate")))}

    def update(self, cid: str, body: dict) -> dict:
        return {"character": self.u.update_character(cid, body.get("fields") or {}, _int(body.get("revision"), "revision"))}

    def status(self, cid: str, status: str, body: dict) -> dict:
        return {"character": self.u.set_status(cid, status, _int(body.get("revision"), "revision"))}

    def lock(self, cid: str, locked: bool, body: dict) -> dict:
        return {"character": self.u.set_lock(cid, locked, _int(body.get("revision"), "revision"))}

    def audit(self, q: dict) -> dict:
        return {"events": self.u.audit(min(300, int((q.get("limit") or ["100"])[0])), (q.get("character") or [None])[0])}

    def export(self) -> Raw:
        return Raw(EX.export_workbook(self.u), XLSX)

    def import_preview(self, data: bytes) -> dict:
        return EX.preview_import(self.u, data)

    def import_apply(self, data: bytes, skip_conflicts: bool) -> dict:
        return EX.apply_import(self.u, data, skip_conflicts=skip_conflicts)

    # ---- dàn nhân vật theo truyện (autocast)
    def stories(self) -> dict:
        return {"stories": CA.list_stories(self.u)}

    def story(self, story_id: str) -> dict:
        cast = CA.get_cast(self.u, story_id)
        if not cast:
            raise StageError(ErrorClass.POLICY, "STORY_NOT_FOUND", f"Truyện {story_id} chưa có dàn nhân vật.", resource="input")
        return {"cast": cast, "orphans": CA.integrity(self.u, story_id), "can_replace": cast["state"] == "staged" and cast["cast_revision"] < 2}

    def alternatives(self, story_id: str, q: dict) -> dict:
        return {"alternatives": CA.alternatives(self.u, story_id, (q.get("slot") or [""])[0])}

    def replace(self, story_id: str, body: dict) -> dict:
        CA.replace_member(self.u, story_id, str(body.get("slot_id") or ""), str(body.get("character_id") or ""))
        return self.story(story_id)
