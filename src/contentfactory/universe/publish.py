"""Publish nguyên tử + idempotent của một truyện đã đạt QA vào Kho nhân vật chính thức, và hoàn tác có kiểm tra an toàn.

- publish_id = băm(story_id + sha256(story.txt)): chạy lại/resume cùng truyện ⇒ KHÔNG làm gì (noop); truyện đổi nội dung ⇒ bản publish cũ của story_id đó được hoàn tác rồi áp bản mới.
- Toàn bộ trong MỘT giao dịch IMMEDIATE: lỗi giữa chừng ⇒ không có gì thay đổi. Không bao giờ ghi gì lên hồ sơ toàn cục của nhân vật ĐÃ CÓ (chỉ thêm lịch sử xuất hiện + quan hệ theo truyện).
- Nhân vật mới (candidates) được chèn với CHÍNH ID đã cấp ở bước casting; nếu trong lúc đó job khác đã publish một nhân vật gần như trùng ⇒ gộp vào nhân vật đó (không tạo bản sao).
"""
from __future__ import annotations

import hashlib
import json
import time

from . import store as S
from .casting import get_cast
from .db import dumps
from .store import Universe, err


def publish_id_for(story_id: str, story_hash: str) -> str:
    return "pb_" + hashlib.sha256(f"{story_id}|{story_hash}".encode()).hexdigest()[:16]


def publish_story(uni: Universe, story_id: str, story_hash: str, *, job_id: str | None = None, outcomes: dict[str, str] | None = None, actor: str = "publish") -> dict:
    pid = publish_id_for(story_id, story_hash)
    outcomes = outcomes or {}
    with uni.db.tx() as c:
        prior = uni.db.q("SELECT publish_id, status FROM change_sets WHERE story_id=? ORDER BY created_at", (story_id,))
        same = next((p for p in prior if p["publish_id"] == pid), None)
        if same:
            return {"status": "noop" if same["status"] == "applied" else "reverted_earlier", "publish_id": pid,
                    "note": "Truyện này đã được cập nhật vào kho trước đó." if same["status"] == "applied" else "Bản cập nhật này đã bị hoàn tác; không tự áp dụng lại."}
        done = next((p for p in prior if p["status"] == "applied"), None)
        if done:                                                              # truyện chạy lại/đổi chữ nhưng dàn nhân vật đã vào kho: không có gì mới để ghi
            return {"status": "already_published", "publish_id": done["publish_id"], "note": "Dàn nhân vật của truyện này đã được cập nhật vào kho trước đó."}
        cast = get_cast(uni, story_id)
        if not cast:
            raise err("STORY_NOT_FOUND", f"Truyện {story_id} chưa có dàn nhân vật để publish.")
        if cast["state"] != "staged":
            raise err("CAST_NOT_STAGED", f"Dàn nhân vật của {story_id} đang ở trạng thái {cast['state']}, không publish được.", "Chạy lại bước Truyện để lập dàn mới.")
        now = time.time()
        remap: dict[str, str] = {}
        created, reused, merged, skipped = [], [], {}, []
        cands = {r["candidate_id"]: json.loads(r["profile"]) for r in uni.db.q("SELECT candidate_id, profile FROM character_candidates WHERE story_id=? AND status='staged'", (story_id,))}
        for m in cast["members"]:
            cid = m["character_id"]
            if cid in cands:
                prof = S.clean_profile(cands[cid])
                dup = [d for d in uni.near_duplicates(prof) if d["score"] >= 0.7 and d["status"] != "archived"]
                if dup:
                    remap[cid] = dup[0]["character_id"]
                    merged[cid] = dup[0]["character_id"]
                    c.execute("UPDATE character_candidates SET status='merged' WHERE candidate_id=?", (cid,))
                    continue
                uni.create_character(prof, origin="original_generated", actor=actor, job_id=job_id, publish_id=pid, created_in_story=story_id, allow_duplicate=True, character_id=cid, conn=c)
                c.execute("UPDATE character_candidates SET status='published' WHERE candidate_id=?", (cid,))
                created.append(cid)
            else:
                row = uni.db.one("SELECT status FROM characters WHERE character_id=?", (cid,))
                if not row:
                    skipped.append(cid)
                    remap[cid] = ""
                    continue
                reused.append(cid)
        appearances = 0
        snapshot_by = {m["character_id"]: {"fit": m["fit"], "rationale": m["rationale"], "goal": m["goal"], "origin": m["origin"], "variant_id": m["variant_id"], "slot_id": m["slot_id"]} for m in cast["members"]}
        for m in cast["members"]:
            final = remap.get(m["character_id"], m["character_id"])
            if not final:
                continue
            if final != m["character_id"]:                                   # nhân vật mới bị gộp: dòng cast trỏ sang nhân vật chính thức
                c.execute("UPDATE story_cast SET character_id=? WHERE story_id=? AND character_id=? AND role_code=?", (final, story_id, m["character_id"], m["role_code"]))
            cur = c.execute("INSERT OR IGNORE INTO appearances(appearance_id,story_id,world_id,character_id,role_code,outcome,notes,snapshot,publish_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (f"ap_{pid}_{final}_{m['role_code']}", story_id, cast["world_id"], final, m["role_code"], outcomes.get(m["character_id"], ""), m["rationale"][:300], dumps(snapshot_by[m["character_id"]]), pid, now))
            appearances += cur.rowcount
        for old, new in remap.items():                                       # quan hệ không bao giờ mồ côi: remap hoặc bỏ nếu một đầu bị loại
            if new:
                c.execute("UPDATE story_relationships SET a_id=? WHERE story_id=? AND a_id=?", (new, story_id, old))
                c.execute("UPDATE story_relationships SET b_id=? WHERE story_id=? AND b_id=?", (new, story_id, old))
            else:
                c.execute("DELETE FROM story_relationships WHERE story_id=? AND (a_id=? OR b_id=?)", (story_id, old, old))
        rels = c.execute("UPDATE story_relationships SET status='active' WHERE story_id=? AND status='staged'", (story_id,)).rowcount
        c.execute("UPDATE story_cast SET state='published', updated_at=? WHERE story_id=?", (now, story_id))
        w = uni.db.one("SELECT constraints FROM worlds WHERE story_id=?", (story_id,))
        meta = json.loads(w["constraints"])
        meta.update(state="published", publish_id=pid)
        c.execute("UPDATE worlds SET constraints=? WHERE story_id=?", (dumps(meta), story_id))
        summary = {"created": created, "reused": reused, "merged": merged, "skipped_missing": skipped, "appearances": appearances, "relationships": rels, "world_id": cast["world_id"]}
        c.execute("INSERT INTO change_sets(publish_id,story_id,job_id,status,summary,created_at) VALUES (?,?,?,?,?,?)", (pid, story_id, job_id, "applied", dumps(summary), now))
        uni._commit(c, "story.publish", story_id, actor, "publish", "story", story_id, None, summary, job_id, pid)
    return {"status": "applied", "publish_id": pid, **summary}


def _revert(uni: Universe, c, pid: str, actor: str, reason: str = "user") -> dict:
    cs = uni.db.one("SELECT * FROM change_sets WHERE publish_id=?", (pid,))
    if not cs:
        raise err("CHANGESET_NOT_FOUND", f"Không có bản cập nhật {pid}.")
    if cs["status"] != "applied":
        raise err("CHANGESET_NOT_APPLIED", "Bản cập nhật này đã được hoàn tác.")
    summary = json.loads(cs["summary"])
    blockers = []
    for cid in summary["created"]:
        row = uni.db.one("SELECT revision, display_name, status FROM characters WHERE character_id=?", (cid,))
        if not row:
            continue
        other = uni.db.one("SELECT COUNT(*) AS n FROM appearances WHERE character_id=? AND story_id != ?", (cid, cs["story_id"]))["n"]
        if other:
            blockers.append(f"“{row['display_name']}” đã xuất hiện trong {other} truyện khác")
        elif row["revision"] != 1:
            blockers.append(f"“{row['display_name']}” đã được sửa sau khi tạo")
    if blockers:
        raise err("REVERT_BLOCKED", "Không hoàn tác an toàn được: " + "; ".join(blockers) + ".", "Lưu trữ nhân vật thay vì hoàn tác, hoặc hoàn tác các truyện phụ thuộc trước.", blockers=blockers)
    c.execute("DELETE FROM character_variants WHERE story_id=?", (cs["story_id"],))
    c.execute("UPDATE story_cast SET variant_id=NULL WHERE story_id=?", (cs["story_id"],))
    for cid in summary["created"]:
        before = uni.db.one("SELECT display_name FROM characters WHERE character_id=?", (cid,))
        c.execute("DELETE FROM characters WHERE character_id=?", (cid,))
        c.execute("UPDATE character_candidates SET status='discarded' WHERE candidate_id=?", (cid,))
        uni._commit(c, "character.revert", cid, actor, "revert", "character", cid, before, None, cs["job_id"], pid)
    c.execute("DELETE FROM appearances WHERE publish_id=?", (pid,))
    c.execute("UPDATE story_cast SET state='reverted' WHERE story_id=?", (cs["story_id"],))
    c.execute("UPDATE story_relationships SET status='reverted' WHERE story_id=?", (cs["story_id"],))
    w = uni.db.one("SELECT constraints FROM worlds WHERE story_id=?", (cs["story_id"],))
    if w:
        meta = json.loads(w["constraints"])
        meta["state"] = "reverted"
        c.execute("UPDATE worlds SET constraints=? WHERE story_id=?", (dumps(meta), cs["story_id"]))
    c.execute("UPDATE change_sets SET status='reverted', reverted_at=? WHERE publish_id=?", (time.time(), pid))
    uni._commit(c, "story.revert", cs["story_id"], actor, "revert", "story", cs["story_id"], summary, {"reason": reason}, cs["job_id"], pid)
    return {"publish_id": pid, "story_id": cs["story_id"], "removed_characters": summary["created"], "reason": reason}


def revert_publish(uni: Universe, publish_id: str, actor: str = "user") -> dict:
    with uni.db.tx() as c:
        return _revert(uni, c, publish_id, actor)


def list_changes(uni: Universe, limit: int = 50) -> list[dict]:
    out = []
    for r in uni.db.q("SELECT * FROM change_sets ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)):
        s = json.loads(r["summary"])
        names = {}
        for cid in s["created"]:
            row = uni.db.one("SELECT display_name FROM characters WHERE character_id=?", (cid,))
            names[cid] = row["display_name"] if row else cid
        out.append({"publish_id": r["publish_id"], "story_id": r["story_id"], "job_id": r["job_id"], "status": r["status"], "created_at": r["created_at"], "reverted_at": r["reverted_at"],
                    "created": [{"character_id": k, "display_name": v} for k, v in names.items()], "reused": len(s["reused"]), "merged": len(s["merged"]), "appearances": s["appearances"],
                    "relationships": s["relationships"], "world_id": s["world_id"]})
    return out
