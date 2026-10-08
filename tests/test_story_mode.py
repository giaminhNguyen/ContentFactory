"""Chế độ truyện (Story hiện có | Story Remix): schema typed, tương thích ngược, từ chối khi chưa khả dụng, API, Settings."""
import json
import unittest
from unittest import mock

from contentfactory.contracts import StageError
from contentfactory.orchestrator.service import Service
from contentfactory.orchestrator.service_admin import AdminService
from contentfactory.story import mode as SM
from tests.support import RootCase, params

FULL = {"mode": "story_remix", "story": {"tone": "u ám"}, "character_universe": {"auto_cast": True}}


class SchemaTest(unittest.TestCase):
    def test_defaults_match_user_chosen_universe_config(self):
        cu = SM.defaults()["character_universe"]
        self.assertEqual({k: cu[k] for k in ("auto_cast", "reuse_strategy", "canon_mode", "auto_update_after_qa")},
                         {"auto_cast": True, "reuse_strategy": "reuse", "canon_mode": "parallel", "auto_update_after_qa": True})
        st = SM.defaults()["story"]
        self.assertEqual((st["auto_select_premise"], st["outline_gate"], st["quality_repair_max_passes"]), (True, True, 1))

    def test_missing_or_legacy_mode_stores_nothing(self):
        self.assertEqual(SM.parse(None), {"mode": "story_branch"})
        self.assertEqual(SM.parse({"mode": "story_branch", "story": {"tone": "x"}}), {"mode": "story_branch"})   # giữ cấu hình khi chuyển qua lại, mode cũ bỏ qua
        self.assertIsNone(SM.resolve_for_job(None, {}))
        self.assertEqual(SM.of_job({}), {"mode": "story_branch"})
        self.assertIsNone(SM.stage_key_extra({}))                                                              # khóa stage của job cũ không đổi

    def test_remix_fills_defaults_and_validates_strictly(self):
        m = SM.parse(FULL)
        self.assertEqual(m["story"]["tone"], "u ám")
        self.assertTrue(m["story"]["auto_select_premise"])
        self.assertEqual(m["character_universe"]["canon_mode"], "parallel")
        bad = [{"mode": "weird"}, {"mode": "story_remix", "x": 1}, {"mode": "story_remix", "story": {"nope": 1}},
               {"mode": "story_remix", "story": {"quality_repair_max_passes": 9}}, {"mode": "story_remix", "story": {"quality_repair_max_passes": True}},
               {"mode": "story_remix", "story": {"ending": "sad"}}, {"mode": "story_remix", "story": {"tone": "x" * 81}},
               {"mode": "story_remix", "story": {"auto_select_premise": "yes"}}, {"mode": "story_remix", "story": {"blocked_themes": ["a", ""]}},
               {"mode": "story_remix", "story": {"budget_usd": 0}}, {"mode": "story_remix", "character_universe": {"canon_mode": "shared"}},
               {"mode": "story_remix", "character_universe": "x"}, "str"]
        for b in bad:
            with self.assertRaises(StageError, msg=b) as e:
                SM.parse(b)
            self.assertEqual(e.exception.code, "INVALID_STORY_MODE")
        self.assertEqual(SM.parse({"mode": "story_remix", "story": {"blocked_themes": [" a ", "a", "b"]}})["story"]["blocked_themes"], ["a", "b"])   # khử trùng, không ép kiểu thầm lặng
        self.assertIsNone(SM.parse({"mode": "story_remix", "story": {"budget_usd": None}})["story"]["budget_usd"])

    def test_settings_defaults_feed_character_universe(self):
        m = SM.parse({"mode": "story_remix"}, {"character_universe": {"auto_update_after_qa": False}})
        self.assertFalse(m["character_universe"]["auto_update_after_qa"])
        e = SM.effective({"mode": "story_remix", "character_universe": {"auto_cast": False}}, {"character_universe": {"auto_update_after_qa": False}})
        self.assertEqual(e["character_universe"]["auto_cast"], {"value": False, "source": "job"})
        self.assertEqual(e["character_universe"]["auto_update_after_qa"]["source"], "system")

    def test_describe_is_the_single_schema_for_ui(self):
        d = SM.describe({})
        self.assertEqual([m["id"] for m in d["modes"]], ["story_branch", "story_remix"])
        self.assertTrue(d["modes"][0]["available"])
        self.assertEqual(d["default_mode"], "story_branch")
        self.assertEqual({f["key"] for f in d["schema"]["character_universe"]}, {k for k in SM.defaults()["character_universe"]})
        json.dumps(d)                                                                                          # tuple -> list: serialize được

    def test_key_extra_ignores_provenance_only_fields(self):
        a = SM.stage_key_extra({"story_mode": SM.parse(FULL)})
        b = SM.stage_key_extra({"story_mode": SM.parse({**FULL, "story": {"tone": "u ám", "rights_ack": True, "budget_usd": 50, "source_provenance": "x"}})})
        c = SM.stage_key_extra({"story_mode": SM.parse({**FULL, "story": {"tone": "vui"}})})
        self.assertEqual(a, b)                                                                                 # RM-008: field chỉ lưu vết/ngân sách không làm vô hiệu bước đắt
        self.assertNotEqual(a, c)


class SubmitTest(RootCase):
    def test_default_job_is_unchanged(self):
        orc = self.orc()
        j = orc.submit(params(), mode="STORY_ONLY")
        self.assertNotIn("story_mode", orc.store.get_job(j)["params"])
        orc.run()
        self.assertEqual(orc.store.get_job(j)["state"], "STORY_READY")

    def test_explicit_legacy_mode_stores_nothing(self):
        orc = self.orc()
        j = orc.submit(params(story_mode={"mode": "story_branch", "story": {"tone": "x"}}), mode="STORY_ONLY")
        self.assertNotIn("story_mode", orc.store.get_job(j)["params"])

    def test_remix_is_rejected_while_unavailable_and_creates_no_job(self):
        orc = self.orc()
        with self.assertRaises(StageError) as e:
            orc.submit(params(story_mode=FULL), mode="STORY_ONLY")
        self.assertEqual(e.exception.code, "REMIX_UNAVAILABLE")
        self.assertEqual(orc.store.list_jobs(), [])

    def test_invalid_config_is_rejected_before_availability(self):
        orc = self.orc()
        with self.assertRaises(StageError) as e:
            orc.submit(params(story_mode={"mode": "story_remix", "story": {"ending": "?"}}), mode="STORY_ONLY")
        self.assertEqual(e.exception.code, "INVALID_STORY_MODE")

    def test_remix_stored_and_in_stage_key_when_available(self):
        with mock.patch.object(SM, "BACKEND_READY", True):
            orc = self.orc()
            j = orc.submit(params(story_mode=FULL), mode="STORY_ONLY")
            p = orc.store.get_job(j)["params"]
            self.assertEqual(p["story_mode"]["mode"], "story_remix")
            self.assertEqual(p["story_mode"]["character_universe"]["reuse_strategy"], "reuse")
            self.assertEqual(SM.stage_key_extra(orc.stage_extra("story", p))["story_mode"]["mode"], "story_remix")
            self.assertIn("story_mode", orc.stage_extra("story", p))
            self.assertEqual(orc.stage_extra("source", p), {})

    def test_unavailable_default_mode_does_not_break_old_jobs(self):
        orc = self.orc()
        orc.cfg.data["story"]["default_mode"] = "story_remix"                                                  # lệch cấu hình: vẫn chạy Story hiện có
        j = orc.submit(params(), mode="STORY_ONLY")
        self.assertNotIn("story_mode", orc.store.get_job(j)["params"])


class ApiTest(RootCase):
    def test_service_exposes_schema_and_effective_config(self):
        svc = Service(self.orc())
        info = svc.story_mode_info()
        self.assertFalse(info["available"])
        self.assertIn("chưa chạy được", info["reason"])
        eff = svc.story_mode_effective({"story_mode": {"mode": "story_remix", "story": {"tone": "x"}}})
        self.assertEqual(eff["story"]["tone"], {"value": "x", "source": "job"})
        self.assertFalse(eff["available"])
        with self.assertRaises(StageError) as e:
            svc.story_mode_effective({"story_mode": {"mode": "story_remix", "story": {"ending": "zzz"}}})
        self.assertEqual(e.exception.code, "INVALID_STORY_MODE")

    def test_settings_cannot_default_to_unavailable_remix(self):
        adm = AdminService(self.orc())
        with self.assertRaises(StageError) as e:
            adm.update_settings({"story.default_mode": "story_remix"})
        self.assertEqual(e.exception.code, "REMIX_UNAVAILABLE")
        adm.update_settings({"story.default_mode": "story_branch", "story.character_universe.auto_update_after_qa": False})
        self.assertFalse(adm.cfg.data["story"]["character_universe"]["auto_update_after_qa"])
        keys = {i["key"] for i in adm.get_settings()["items"]}
        self.assertTrue({"story.default_mode", "story.remix_enabled", "story.character_universe.auto_cast"} <= keys)

    def test_job_detail_reports_mode(self):
        orc = self.orc()
        j = orc.submit(params(), mode="STORY_ONLY")
        self.assertEqual(Service(orc).job_detail(j)["story_mode"], {"mode": "story_branch"})


if __name__ == "__main__":
    unittest.main()
