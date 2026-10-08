"""Autocast (Phase 3): fit/ensemble, tạo nhân vật mới ở trạng thái staged, đóng băng dàn, world song song, ghim/loại, recast, bất biến dữ liệu."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from contentfactory.universe import Universe, UniverseDB
from contentfactory.universe import casting as C
from tests.test_universe import HUNG, code

DETECTIVE = {"display_name": "Thám Tử Vân", "core_personality": "điềm tĩnh quan sát tinh tế suy luận logic", "strengths": ["quan sát", "suy luận"], "motivations": ["tìm sự thật"],
             "flaws": ["cô độc"], "communication_style": "nói chậm, chọn từ cẩn thận", "genre_affinities": ["trinh thám"]}
THUG = {"display_name": "Cường Dao", "core_personality": "nóng nảy hung hăng bốc đồng", "strengths": ["đánh nhau"], "motivations": ["quyền lực"], "flaws": ["tàn bạo"],
        "communication_style": "câu ngắn, dứt khoát", "genre_affinities": ["hành động"], "boundaries": ["không bao giờ đầu hàng"]}


def req(story_id="s1", **over):
    r = {"story_id": story_id, "genre": "trinh thám", "slots": [
        {"slot_id": "hero", "role_code": "protagonist", "importance": 3, "traits": ["điềm tĩnh", "quan sát"], "goal": "Tìm ra kẻ đứng sau vụ án"},
        {"slot_id": "villain", "role_code": "antagonist", "importance": 3, "traits": ["tham vọng", "tàn nhẫn"], "goal": "Che giấu tội ác",
         "relationships": [{"with": "hero", "type": "enemy_of"}]},
        {"slot_id": "friend", "role_code": "ally", "importance": 1, "traits": ["trung thành"], "relationships": [{"with": "hero", "type": "friend_of"}]}]}
    r.update(over)
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = UniverseDB(Path(self.tmp.name) / "u.db")
        self.addCleanup(self.db.close)
        self.u = Universe(self.db)

    def appear(self, cid, story, role="protagonist"):
        with self.db.tx() as c:
            c.execute("INSERT INTO appearances(appearance_id,story_id,world_id,character_id,role_code,created_at) VALUES (?,?,?,?,?,?)",
                      (f"ap_{cid}_{story}_{role}", story, "w_" + story, cid, role, time.time()))

    def by_role(self, cast):
        return {m["role_code"]: m for m in cast["members"]}


class CastTest(Base):
    def test_empty_universe_creates_smallest_viable_cast_staged_only(self):
        cast = C.cast_story(self.u, req())
        self.assertEqual({m["origin"] for m in cast["members"]}, {"created"})
        self.assertEqual(len(cast["members"]), 3)                                                             # kho rỗng vẫn chạy được, không cần seed
        self.assertEqual(self.u.list_characters(status="")["total"], 0)                                       # LU-006: chưa chạm kho chính thức
        self.assertEqual(self.u.summary()["staged_candidates"], 3)
        ids = [m["character_id"] for m in cast["members"]]
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(i.startswith("ch_") for i in ids))
        self.assertEqual((cast["state"], {m["lock_status"] for m in cast["members"]}, cast["canon_mode"]), ("staged", {"frozen"}, "parallel"))
        self.assertEqual(C.integrity(self.u, "s1"), [])
        self.assertEqual(len(cast["relationships"]), 2)
        self.assertEqual(len({m["display_name"] for m in cast["members"]}), 3)

    def test_reuses_fitting_character_with_explainable_score(self):
        d = self.u.create_character(DETECTIVE)
        cast = C.cast_story(self.u, req())
        hero = self.by_role(cast)["protagonist"]
        self.assertEqual((hero["character_id"], hero["origin"]), (d["character_id"], "reused"))
        self.assertGreaterEqual(hero["fit"], C.MIN_FIT)
        self.assertIn("Dùng lại", hero["rationale"])
        self.assertEqual(set(hero["breakdown"]), {"traits", "genre", "role_history", "diversity"})
        self.assertEqual(self.by_role(cast)["antagonist"]["origin"], "created")                                # chỉ tạo mới khi cần

    def test_incompatible_existing_character_is_not_forced(self):
        self.u.create_character(THUG)                                                                           # LU-002: có nhân vật nhưng không hợp vai chính
        hero = self.by_role(C.cast_story(self.u, req()))["protagonist"]
        self.assertEqual(hero["origin"], "created")
        self.assertNotEqual(hero["display_name"], "Cường Dao")

    def test_reuse_is_preference_not_quota(self):
        self.u.create_character(DETECTIVE)
        weak = {"story_id": "a", "genre": "trinh thám", "slots": [{"slot_id": "hero", "role_code": "protagonist", "importance": 3, "traits": ["điềm tĩnh", "dũng cảm", "hài hước", "giàu có"]}]}
        self.assertEqual(C.cast_story(self.u, weak)["members"][0]["origin"], "created")                         # chỉ khớp 1/4 đặc điểm: dưới sàn ⇒ tạo mới
        easy = {"story_id": "b", "genre": "trinh thám", "slots": [{"slot_id": "hero", "role_code": "protagonist", "traits": ["điềm tĩnh", "quan sát"]}]}
        self.assertEqual(C.cast_story(self.u, easy)["members"][0]["origin"], "reused")
        prefer_new = C.cast_story(self.u, {**easy, "story_id": "c", "reuse_strategy": "create_new"})
        self.assertIn(prefer_new["members"][0]["origin"], ("reused", "created"))                                # ưu tiên mới nâng ngưỡng nhưng nhân vật quá hợp vẫn dùng được

    def test_archived_excluded_and_locked_core_still_usable(self):
        d = self.u.create_character(DETECTIVE)
        d = self.u.set_status(d["character_id"], "archived", 1)
        self.assertEqual(self.by_role(C.cast_story(self.u, req("x")))["protagonist"]["origin"], "created")
        d = self.u.set_status(d["character_id"], "active", d["revision"])
        d = self.u.set_lock(d["character_id"], True, d["revision"])
        self.assertEqual(self.by_role(C.cast_story(self.u, req("y")))["protagonist"]["character_id"], d["character_id"])

    def test_exclude_and_pin(self):
        d = self.u.create_character(DETECTIVE)
        h = self.u.create_character(HUNG)
        cast = C.cast_story(self.u, {**req("p"), "exclude_character_ids": [d["character_id"]]})
        self.assertNotIn(d["character_id"], [m["character_id"] for m in cast["members"]])
        cast = C.cast_story(self.u, {**req("q"), "pinned_character_ids": [h["character_id"]]})
        self.assertIn(h["character_id"], [m["character_id"] for m in cast["members"]])                          # người dùng ghim thì được dùng
        self.assertEqual(code(C.cast_story, self.u, {**req("r"), "pinned_character_ids": ["ch_000000000000"]}), "CAST_PINNED_INVALID")
        self.u.set_status(h["character_id"], "archived", self.u.character(h["character_id"])["revision"])
        self.assertEqual(code(C.cast_story, self.u, {**req("t"), "pinned_character_ids": [h["character_id"]]}), "CAST_PINNED_INVALID")

    def test_ensemble_rerank_when_protagonist_and_antagonist_are_clones(self):
        twin = {**DETECTIVE, "core_personality": "điềm tĩnh quan sát tham vọng tàn nhẫn", "motivations": ["quyền lực"]}
        self.u.create_character({**twin, "display_name": "Song Sinh A", "communication_style": "x1 y1 z1"})
        self.u.create_character({**twin, "display_name": "Bóng Tối B", "communication_style": "x2 y2 z2"}, allow_duplicate=True)
        cast = C.cast_story(self.u, req())
        m = self.by_role(cast)
        self.assertNotEqual(m["protagonist"]["character_id"], m["antagonist"]["character_id"])
        self.assertFalse([i for i in cast["issues"] if i["code"] == "NO_CONTRAST" and i["action"] == "kept"])  # không giữ lại dàn thiếu tương phản

    def test_idempotent_resume_and_changed_request_rebuilds_staged_only(self):
        a = C.cast_story(self.u, req())
        b = C.cast_story(self.u, req())                                                                         # resume/retry: đúng dàn đã đóng băng
        self.assertEqual([m["character_id"] for m in a["members"]], [m["character_id"] for m in b["members"]])
        self.assertEqual(self.u.summary()["staged_candidates"], 3)
        r2 = req()
        r2["slots"][0]["traits"] = ["lì lợm"]
        c = C.cast_story(self.u, r2)
        self.assertNotEqual(c["fingerprint"], a["fingerprint"])
        self.assertEqual(self.u.summary()["staged_candidates"], 3)                                              # dàn cũ staged bị thay, không cộng dồn

    def test_parallel_worlds_share_stable_identity_and_do_not_mutate_it(self):
        d = self.u.create_character(DETECTIVE)
        before = self.u.character(d["character_id"])
        r1 = {"story_id": "A", "genre": "trinh thám", "slots": [{"slot_id": "h", "role_code": "protagonist", "traits": ["điềm tĩnh", "quan sát"], "variant_facts": {"nghề": "cảnh sát"}}]}
        r2 = {"story_id": "B", "genre": "trinh thám", "slots": [{"slot_id": "h", "role_code": "antagonist", "traits": ["điềm tĩnh", "suy luận"], "variant_facts": {"nghề": "kẻ lừa đảo"}}]}
        a, b = C.cast_story(self.u, r1), C.cast_story(self.u, r2)
        self.assertEqual(a["members"][0]["character_id"], b["members"][0]["character_id"])                      # cùng danh tính ổn định
        self.assertEqual((a["members"][0]["role_code"], b["members"][0]["role_code"]), ("protagonist", "antagonist"))
        self.assertNotEqual(a["world_id"], b["world_id"])                                                       # mỗi truyện một dòng thời gian
        self.assertNotEqual(a["members"][0]["variant_id"], b["members"][0]["variant_id"])
        self.assertEqual(self.u.character(d["character_id"]), before)                                          # LU-004/005: hồ sơ toàn cục không đổi
        facts = {v["story_id"]: json.loads(v["facts"]) for v in self.u.character(d["character_id"], detail=True)["variants"]}
        self.assertEqual(facts, {"A": {"nghề": "cảnh sát"}, "B": {"nghề": "kẻ lừa đảo"}})

    def test_many_new_characters_in_one_story_are_distinct(self):
        roles = ["ally", "rival", "mentor", "foil", "catalyst", "wildcard", "gatekeeper", "confidant"]
        cast = C.cast_story(self.u, {"story_id": "big", "genre": "", "slots": [{"slot_id": f"s{i}", "role_code": r, "importance": 1} for i, r in enumerate(roles)]})
        self.assertEqual(len({m["display_name"] for m in cast["members"]}), 8)

    def test_overused_cast_is_penalised(self):
        d = self.u.create_character(DETECTIVE)
        base = {"story_id": "z0", "genre": "trinh thám", "slots": [{"slot_id": "h", "role_code": "protagonist", "traits": ["điềm tĩnh", "quan sát"]}]}
        fresh = C.cast_story(self.u, base)["members"][0]["fit"]
        for i in range(5):
            self.appear(d["character_id"], f"old{i}")
        busy = C.cast_story(self.u, {**base, "story_id": "z1"})["members"][0]["fit"]
        self.assertLess(busy, fresh)                                                                            # tránh lạm dụng cùng nhân vật

    def test_discard_removes_only_staged_story_data(self):
        d = self.u.create_character(DETECTIVE)
        C.cast_story(self.u, req())
        C.discard_story(self.u, "s1")
        self.assertIsNone(C.get_cast(self.u, "s1"))
        self.assertEqual(self.u.summary()["staged_candidates"], 0)
        self.assertEqual(self.u.character(d["character_id"])["revision"], 1)

    def test_recast_once_replace_and_no_orphans(self):
        self.u.create_character(DETECTIVE)
        h = self.u.create_character(HUNG)
        C.cast_story(self.u, req())
        out = C.recast(self.u, "s1", req(), replace={"friend": h["character_id"]})
        self.assertEqual(out["cast_revision"], 2)
        self.assertEqual(self.by_role(out)["ally"]["character_id"], h["character_id"])
        self.assertEqual(C.integrity(self.u, "s1"), [])
        self.assertEqual(code(C.recast, self.u, "s1", req()), "RECAST_LIMIT")
        self.assertEqual(self.u.summary()["staged_candidates"], len([m for m in out["members"] if m["origin"] == "created"]))
        self.assertEqual(code(C.recast, self.u, "nope", req()), "STORY_NOT_FOUND")

    def test_published_cast_cannot_be_recast_or_discarded(self):
        C.cast_story(self.u, req())
        with self.db.tx() as c:
            m = json.loads(self.db.one("SELECT constraints FROM worlds WHERE story_id='s1'")["constraints"])
            m["state"] = "published"
            c.execute("UPDATE worlds SET constraints=? WHERE story_id='s1'", (json.dumps(m),))
        self.assertEqual(code(C.recast, self.u, "s1", req()), "CAST_PUBLISHED")
        C.discard_story(self.u, "s1")
        self.assertIsNotNone(C.get_cast(self.u, "s1"))

    def test_new_characters_disabled_and_incomplete(self):
        self.assertEqual(code(C.cast_story, self.u, {**req(), "allow_new": False}), "CAST_INCOMPLETE")
        self.assertEqual(self.u.summary()["staged_candidates"], 0)

    def test_request_validation(self):
        bad = [{}, {"story_id": "x", "slots": []}, {"story_id": "x", "slots": [{"slot_id": "a", "role_code": "king"}]},
               {"story_id": "x", "slots": [{"slot_id": "a", "role_code": "ally"}, {"slot_id": "a", "role_code": "ally"}]},
               {"story_id": "x", "slots": [{"slot_id": "a", "role_code": "ally", "importance": 9}]},
               {"story_id": "x", "slots": [{"slot_id": "a", "role_code": "ally", "relationships": [{"with": "zz", "type": "t"}]}]},
               {"story_id": "x", "slots": [{"slot_id": "a", "role_code": "ally"}], "reuse_strategy": "nope"}]
        for b in bad:
            self.assertEqual(code(C.cast_story, self.u, b), "INVALID_CAST_REQUEST", b)
        self.assertEqual(self.u.summary()["staged_candidates"], 0)

    def test_contradictory_directed_relations_are_reported(self):
        r = req()
        r["slots"][0]["relationships"] = [{"with": "villain", "type": "parent_of", "direction": "out"}]
        r["slots"][1]["relationships"] = [{"with": "hero", "type": "parent_of", "direction": "out"}]
        self.assertIn("CONTRADICTORY_RELATION", [i["code"] for i in C.cast_story(self.u, r)["issues"]])


class ViewerTest(Base):
    def test_list_alternatives_and_user_replacement(self):
        d = self.u.create_character(DETECTIVE)
        h = self.u.create_character(HUNG)
        C.cast_story(self.u, req())
        [st] = C.list_stories(self.u)
        self.assertEqual((st["story_id"], st["state"], st["members"], st["new"], st["reused"]), ("s1", "staged", 3, 2, 1))
        alts = C.alternatives(self.u, "s1", "friend")
        self.assertEqual({a["character_id"] for a in alts}, {d["character_id"], h["character_id"]})
        self.assertTrue(next(a for a in alts if a["character_id"] == d["character_id"])["in_cast"])                    # đã có trong dàn: UI khóa
        self.assertEqual(code(C.alternatives, self.u, "s1", "zzz"), "INVALID_CAST_REQUEST")
        out = C.replace_member(self.u, "s1", "friend", h["character_id"])
        self.assertEqual(self.by_role(out)["ally"]["character_id"], h["character_id"])
        self.assertEqual(code(C.replace_member, self.u, "s1", "friend", d["character_id"]), "RECAST_LIMIT")             # chỉ một lần

    def test_stored_request_survives_for_alternatives(self):
        C.cast_story(self.u, req())
        self.assertEqual(C.alternatives(self.u, "s1", "hero"), [])                                                       # kho rỗng: không có ứng viên, không lỗi


if __name__ == "__main__":
    unittest.main()
