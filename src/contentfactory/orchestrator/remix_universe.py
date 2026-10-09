"""Cầu nối Story Remix ↔ Kho nhân vật (story_remix không được import universe): dựng yêu cầu casting từ ý tưởng đã chọn, lấy hồ sơ cho prompt."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..universe import Universe
from ..universe import casting as CA
from ..universe import publish as PB


class UniverseBridge:
    def __init__(self, uni: Universe) -> None:
        self.u = uni

    def _request(self, story_id: str, premise: dict, genre: str, cu: dict) -> dict:
        return {"story_id": story_id, "genre": genre, "reuse_strategy": cu["reuse_strategy"], "allow_new": cu["allow_new_characters"], "pinned_character_ids": cu["pinned_character_ids"],
                "slots": [{"slot_id": s["slot_id"], "role_code": s["role_code"], "importance": s["importance"], "traits": CA.pack_text(" ; ".join(s["traits"]).replace(" ; ", ", "), 80, 12) if any(len(t) > 80 for t in s["traits"]) or len(s["traits"]) > 12 else s["traits"], "goal": s["goal"],
                           "relationships": [{"with": r["with"], "type": r["type"]} for r in s["relationships"]]} for s in premise["slots"]]}

    def continuity(self, premise: dict, genre: str) -> float:
        """Phần vai có ít nhất một nhân vật đang dùng đủ hợp (chỉ để chấm ý tưởng; không đặt chỗ)."""
        chars = [{**c, "_roles": set()} for c in (self.u.list_characters(status="active", limit=200)["items"])]
        if not chars or not premise["slots"]:
            return 0.5 if not chars else 0.0
        ok = 0
        for s in premise["slots"]:
            slot = {"role_code": s["role_code"], "traits": s["traits"], "skills": [], "must_do": []}
            if any(CA.score(c, slot, genre, 0)["total"] >= CA.MIN_FIT for c in chars):
                ok += 1
        return ok / len(premise["slots"])

    def cast(self, story_id: str, premise: dict, genre: str, cu: dict, job_id: str | None) -> dict:
        return CA.cast_story(self.u, self._request(story_id, premise, genre, cu), job_id=job_id)

    def profiles(self, cast: dict) -> dict[str, dict]:
        out = {}
        for m in cast["members"]:
            r = self.u.db.one("SELECT * FROM characters WHERE character_id=?", (m["character_id"],))
            if r:
                out[m["character_id"]] = self.u.character(m["character_id"])
            else:
                c = self.u.db.one("SELECT profile FROM character_candidates WHERE candidate_id=?", (m["character_id"],))
                out[m["character_id"]] = json.loads(c["profile"]) if c else {}
        return out

    def publish(self, ctx, cast: dict, qa: dict, story_text: str, mode: dict) -> dict:
        """Publish sau QA: nhân vật mới + lịch sử xuất hiện + quan hệ theo truyện, nguyên tử và idempotent (cùng truyện ⇒ noop)."""
        return self.publish_dir(ctx.job_id, Path(ctx.stage_dir), cast, story_text)

    def publish_dir(self, job_id: str, stage_dir: Path, cast: dict, story_text: str) -> dict:
        try:
            mem = json.loads((stage_dir / "remix" / "story_memory.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            mem = {"character_state": {}}
        outcomes = {cid: "; ".join(f"{k}: {v}" for k, v in st.items()) for cid, st in (mem.get("character_state") or {}).items()}
        return {"universe_publish": PB.publish_story(self.u, cast["story_id"], hashlib.sha256(story_text.encode("utf-8")).hexdigest(), job_id=job_id, outcomes=outcomes)}
