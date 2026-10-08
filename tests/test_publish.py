"""Publish Kho nhân vật (Phase 6): nguyên tử, idempotent, gộp trùng, phạm vi theo truyện, hoàn tác an toàn, đồng thời, mô phỏng 100 truyện."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from contentfactory.universe import Universe, UniverseDB
from contentfactory.universe import casting as C
from contentfactory.universe import publish as PB
from tests.test_casting import DETECTIVE, req
from tests.test_universe import code


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = UniverseDB(Path(self.tmp.name) / "u.db")
        self.addCleanup(self.db.close)
        self.u = Universe(self.db)

    def cast_and_publish(self, story_id="s1", h="h1", **kw):
        C.cast_story(self.u, req(story_id, **kw))
        return PB.publish_story(self.u, story_id, h, job_id="7")

    def chars(self, status=""):
        return self.u.list_characters(status=status, limit=200)["items"]


class PublishTest(Base):
    def test_publish_adds_new_characters_history_and_relationships_with_stable_ids(self):
        d = self.u.create_character(DETECTIVE)
        cast = C.cast_story(self.u, req("s1"))
        staged_ids = {m["character_id"] for m in cast["members"] if m["origin"] == "created"}
        before = self.u.character(d["character_id"])
        res = PB.publish_story(self.u, "s1", "hash-1", job_id="7", outcomes={d["character_id"]: "status: sống"})
        self.assertEqual((res["status"], len(res["created"]), len(res["reused"]), res["appearances"], res["relationships"]), ("applied", 2, 1, 3, 2))
        self.assertEqual(set(res["created"]), staged_ids)                                                       # ID đã cấp ở casting được giữ nguyên
        self.assertEqual({c["character_id"] for c in self.chars("active")}, staged_ids | {d["character_id"]})
        new = self.u.character(res["created"][0])
        self.assertEqual((new["origin"], new["status"], new["revision"], new["created_in_story"]), ("original_generated", "active", 1, "s1"))
        self.assertEqual(self.u.character(d["character_id"]), before)                                          # nhân vật cũ không bị sửa hồ sơ
        app = self.u.character(d["character_id"], detail=True)["appearances"]
        self.assertEqual((app[0]["story_id"], app[0]["role_code"], app[0]["outcome"]), ("s1", "protagonist", "status: sống"))
        cast = C.get_cast(self.u, "s1")
        self.assertEqual((cast["state"], {m["state"] for m in cast["members"]}, {r["status"] for r in cast["relationships"]}), ("published", {"published"}, {"active"}))
        self.assertEqual(C.integrity(self.u, "s1"), [])
        s = self.u.summary()
        self.assertEqual((s["stories"], s["staged_candidates"], s["reused_appearances"], s["new_character_appearances"]), (1, 0, 0, 2))
        self.assertIsNotNone(s["last_publish_at"])
        pub = [a for a in self.u.audit() if a["publish_id"] == res["publish_id"]]
        self.assertTrue({"create", "publish"} <= {a["action"] for a in pub})                                   # có audit gắn publish_id

    def test_failed_or_unpublished_story_leaves_catalog_untouched(self):
        C.cast_story(self.u, req("fail"))
        self.assertEqual(self.chars(), [])
        C.discard_story(self.u, "fail")                                                                         # job lỗi/hủy
        self.assertEqual((self.chars(), self.u.summary()["staged_candidates"]), ([], 0))

    def test_publish_is_idempotent_for_resume_retry_and_rewritten_story(self):
        self.cast_and_publish()
        n, rev = len(self.chars()), self.u.revision()
        again = PB.publish_story(self.u, "s1", "h1", job_id="7")
        self.assertEqual(again["status"], "noop")                                                              # LU-007
        later = PB.publish_story(self.u, "s1", "h2", job_id="7")                                               # truyện viết lại nhưng dàn đã vào kho
        self.assertEqual(later["status"], "already_published")
        self.assertEqual((len(self.chars()), self.u.revision(), self.u.summary()["stories"]), (n, rev, 1))
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM appearances")["n"], 3)

    def test_atomic_failure_changes_nothing(self):
        C.cast_story(self.u, req("s1"))
        orig = self.u._commit
        self.u._commit = lambda c, kind, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")) if kind == "story.publish" else orig(c, kind, *a, **k)
        with self.assertRaises(RuntimeError):
            PB.publish_story(self.u, "s1", "h1")
        self.u._commit = orig
        self.assertEqual((self.chars(), self.db.one("SELECT COUNT(*) AS n FROM change_sets")["n"], C.get_cast(self.u, "s1")["state"]), ([], 0, "staged"))
        self.assertEqual(self.u.summary()["staged_candidates"], 3)                                             # vẫn staged, thử lại được
        self.assertEqual(PB.publish_story(self.u, "s1", "h1")["status"], "applied")

    def test_outcomes_and_relationships_are_scoped_per_story(self):
        d = self.u.create_character(DETECTIVE)
        single = lambda sid, role: {"story_id": sid, "genre": "trinh thám", "slots": [{"slot_id": "h", "role_code": role, "traits": ["điềm tĩnh", "quan sát"]}]}      # noqa: E731
        C.cast_story(self.u, single("A", "protagonist"))
        PB.publish_story(self.u, "A", "ha", outcomes={d["character_id"]: "status: đã chết"})                   # chết ở truyện A…
        C.cast_story(self.u, single("B", "antagonist"))
        PB.publish_story(self.u, "B", "hb")
        app = {a["story_id"]: a for a in self.u.character(d["character_id"], detail=True)["appearances"]}
        self.assertEqual(app["A"]["outcome"], "status: đã chết")
        self.assertEqual((app["B"]["outcome"], app["B"]["role_code"]), ("", "antagonist"))                     # …không lan sang truyện B (LU-005)
        self.assertEqual(self.u.character(d["character_id"])["status"], "active")
        s = self.u.summary()
        self.assertEqual(s["reused_appearances"], 0)                                                           # nhân vật có sẵn (không do truyện nào tạo) không tính vào “dùng lại từ truyện khác”

    def test_duplicate_new_characters_from_concurrent_stories_are_merged_not_cloned(self):
        same = lambda slot, ctx, attempt: {"display_name": "Trần Hạo", "core_personality": "kiên nhẫn tỉ mỉ cẩn trọng", "motivations": ["tìm sự thật"], "flaws": ["đa nghi"]}      # noqa: E731
        one = lambda sid: {"story_id": sid, "genre": "x", "slots": [{"slot_id": "h", "role_code": "protagonist", "relationships": []}, {"slot_id": "a", "role_code": "antagonist", "relationships": [{"with": "h", "type": "enemy_of"}]}]}   # noqa: E731
        def fac(slot, ctx, attempt):
            p = same(slot, ctx, attempt)
            return {**p, "display_name": "Trần Hạo" if slot["slot_id"] == "h" else "Võ Quyết", "core_personality": p["core_personality"] if slot["slot_id"] == "h" else "tàn nhẫn tham vọng lạnh lùng", "motivations": ["tìm sự thật"] if slot["slot_id"] == "h" else ["quyền lực"], "flaws": p["flaws"] if slot["slot_id"] == "h" else ["kiêu ngạo"]}
        C.cast_story(self.u, one("A"), factory=fac)
        C.cast_story(self.u, one("B"), factory=fac)                                                             # hai job cùng lập dàn khi kho còn trống ⇒ cùng nhân vật mới
        ra, rb = PB.publish_story(self.u, "A", "ha"), PB.publish_story(self.u, "B", "hb")
        self.assertEqual((len(ra["created"]), len(rb["created"]), len(rb["merged"])), (2, 0, 2))
        self.assertEqual(len(self.chars()), 2)                                                                  # LU-003: không có bản sao
        cb = C.get_cast(self.u, "B")
        self.assertEqual(C.integrity(self.u, "B"), [])
        self.assertEqual({m["character_id"] for m in cb["members"]}, {c["character_id"] for c in self.chars()})
        self.assertEqual(self.u.summary()["reused_appearances"], 2)                                             # truyện B dùng lại nhân vật do truyện A tạo

    def test_concurrent_publishes_of_different_stories_do_not_lose_updates(self):
        for i in range(4):
            C.cast_story(self.u, {"story_id": f"c{i}", "genre": "", "slots": [{"slot_id": "h", "role_code": "protagonist", "traits": [f"đặc điểm{i}x"]}, {"slot_id": "a", "role_code": "ally"}]})
        res, errs = [], []

        def go(i):
            try:
                res.append(PB.publish_story(self.u, f"c{i}", f"h{i}"))
            except Exception as e:                                                                              # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual({r["status"] for r in res}, {"applied"})
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM change_sets WHERE status='applied'")["n"], 4)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM appearances")["n"], 8)
        revs = [r["rev"] for r in self.db.q("SELECT rev FROM universe_revisions")]
        self.assertEqual(revs, sorted(set(revs)))


class RevertTest(Base):
    def test_revert_restores_state_and_blocks_reapplying_the_same_publish(self):
        res = self.cast_and_publish()
        out = PB.revert_publish(self.u, res["publish_id"])
        self.assertEqual(set(out["removed_characters"]), set(res["created"]))
        self.assertEqual((self.chars(), self.db.one("SELECT COUNT(*) AS n FROM appearances")["n"]), ([], 0))
        self.assertEqual(self.u.summary()["stories"], 0)
        self.assertEqual(C.get_cast(self.u, "s1")["state"], "reverted")
        self.assertEqual(code(PB.revert_publish, self.u, res["publish_id"]), "CHANGESET_NOT_APPLIED")
        self.assertEqual(PB.publish_story(self.u, "s1", "h1")["status"], "reverted_earlier")                    # retry không tự áp dụng lại điều người dùng đã hoàn tác
        again = C.cast_story(self.u, req("s1"))                                                                  # chạy lại Story ⇒ dàn mới, không kẹt dàn cũ
        self.assertEqual(again["state"], "staged")
        self.assertEqual(PB.publish_story(self.u, "s1", "h2")["status"], "applied")
        ch = PB.list_changes(self.u)
        self.assertEqual([c["status"] for c in ch], ["applied", "reverted"])
        self.assertTrue(any(a["action"] == "revert" for a in self.u.audit()))                                   # LU-012: có audit hoàn tác

    def test_revert_blocked_when_dependents_or_edits_exist(self):
        ra = self.cast_and_publish("A", "ha")
        hero = next(c for c in self.chars() if c["character_id"] in ra["created"])
        self.u.update_character(hero["character_id"], {"temperament": "đã sửa"}, hero["revision"])
        self.assertEqual(code(PB.revert_publish, self.u, ra["publish_id"]), "REVERT_BLOCKED")                   # đã sửa sau khi tạo
        self.assertEqual(len(self.chars()), 3)

    def test_revert_blocked_when_another_story_reuses_a_created_character(self):
        ra = self.cast_and_publish("A", "ha")
        C.cast_story(self.u, {"story_id": "B", "genre": "trinh thám", "slots": [{"slot_id": "h", "role_code": "protagonist", "traits": ["điềm tĩnh", "quan sát"], "importance": 3}]})
        hero_a = next(m for m in C.get_cast(self.u, "A")["members"] if m["role_code"] == "protagonist")
        cb = C.get_cast(self.u, "B")
        if cb["members"][0]["character_id"] != hero_a["character_id"]:
            self.skipTest("fit không chọn lại nhân vật của A trong fixture này")
        PB.publish_story(self.u, "B", "hb")
        e = code(PB.revert_publish, self.u, ra["publish_id"])
        self.assertEqual(e, "REVERT_BLOCKED")
        self.assertEqual(len(self.chars()), 3)
        rb = next(c for c in PB.list_changes(self.u) if c["story_id"] == "B")
        PB.revert_publish(self.u, rb["publish_id"])                                                              # hoàn tác truyện phụ thuộc trước ⇒ rồi mới hoàn tác A
        PB.revert_publish(self.u, ra["publish_id"])
        self.assertEqual(self.chars(), [])


class SimulationTest(Base):
    def test_hundred_stories_do_not_flood_the_universe_with_duplicates(self):
        genres = ["trinh thám", "kinh dị", "tình cảm"]
        traits = [["điềm tĩnh", "quan sát"], ["liều lĩnh", "bốc đồng"], ["tham vọng", "tàn nhẫn"], ["trung thành", "chu đáo"]]
        for i in range(100):
            sid = f"sim{i}"
            slots = [{"slot_id": "h", "role_code": "protagonist", "importance": 3, "traits": traits[i % 2], "goal": "g"}, {"slot_id": "v", "role_code": "antagonist", "importance": 3, "traits": traits[2]},
                     {"slot_id": "a", "role_code": "ally", "importance": 1, "traits": traits[3], "relationships": [{"with": "h", "type": "friend_of"}]}]
            C.cast_story(self.u, {"story_id": sid, "genre": genres[i % 3], "slots": slots})
            PB.publish_story(self.u, sid, f"h{i}")
        chars = self.chars()
        keys = [S_key(c["display_name"]) for c in chars]
        self.assertEqual(len(keys), len(set(keys)))                                                             # không trùng tên
        self.assertLess(len(chars), 100)                                                                        # LU-010: dùng lại thực sự xảy ra, kho không phình theo số truyện
        s = self.u.summary()
        self.assertEqual(s["stories"], 100)
        self.assertGreater(s["reused_appearances"], 150)
        self.assertEqual(self.u.summary()["staged_candidates"], 0)


def S_key(n):
    from contentfactory.universe.store import name_key
    return name_key(n)


if __name__ == "__main__":
    unittest.main()
