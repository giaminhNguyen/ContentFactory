"""Mẫu cấu hình Story Remix + cấu hình hiệu lực (system < mẫu < job) + ước tính chi phí (Phase 7)."""
import unittest

from contentfactory.contracts import StageError
from contentfactory.orchestrator.service import Service
from contentfactory.story import mode as SM
from contentfactory.story import presets as SP
from contentfactory.story_remix.estimate import estimate
from tests.support import RootCase, params


class PresetTest(RootCase):
    def svc(self):
        self.o = self.orc()
        return Service(self.o)

    def test_save_validates_with_the_job_schema_and_lists(self):
        svc = self.svc()
        info = svc.story_preset_save("Trinh thám u ám", {"story_mode": {"story": {"tone": "u ám", "target_genre": "trinh thám"}, "character_universe": {"reuse_strategy": "create_new"}}})
        [p] = info["presets"]
        self.assertEqual((p["name"], p["story"]["tone"], p["character_universe"]["reuse_strategy"]), ("Trinh thám u ám", "u ám", "create_new"))
        self.assertEqual(p["story"]["auto_select_premise"], True)                                              # đủ giá trị mặc định
        for bad in ({"story_mode": {"story": {"ending": "?"}}}, {"story_mode": {"story": {"quality_repair_max_passes": 99}}}):
            with self.assertRaises(StageError) as e:
                svc.story_preset_save("x", bad)
            self.assertEqual(e.exception.code, "INVALID_STORY_MODE")
        for name in ("", "a/b", "x" * 41, "!bad"):
            with self.assertRaises(StageError) as e:
                svc.story_preset_save(name, {"story_mode": {}})
            self.assertEqual(e.exception.code, "INVALID_PRESET_NAME")
        self.assertEqual(len(svc.story_mode_info()["presets"]), 1)

    def test_default_preset_applies_to_jobs_that_do_not_choose_but_explicit_choice_wins(self):
        svc = self.svc()
        svc.story_preset_save("Mặc định của tôi", {"story_mode": {"story": {"tone": "hài nhẹ"}}, "make_default": True})
        self.assertEqual(svc.story_mode_info()["default_preset"], "Mặc định của tôi")
        j = self.o.submit(params(), mode="STORY_ONLY")                                                         # không chọn gì: chọn Remix MỘT lần ở mẫu rồi chỉ bấm RUN
        sm = self.o.store.get_job(j)["params"]["story_mode"]
        self.assertEqual((sm["mode"], sm["story"]["tone"]), ("story_remix", "hài nhẹ"))
        k = self.o.submit(params(story_mode={"mode": "story_branch"}), mode="STORY_ONLY")
        self.assertNotIn("story_mode", self.o.store.get_job(k)["params"])                                      # chọn rõ Story hiện có thì thắng
        m = self.o.submit(params(story_mode={"mode": "story_remix", "story": {"tone": "u ám"}}), mode="STORY_ONLY")
        self.assertEqual(self.o.store.get_job(m)["params"]["story_mode"]["story"]["tone"], "u ám")
        svc.story_preset_default({"name": None})
        n = self.o.submit(params(), mode="STORY_ONLY")
        self.assertNotIn("story_mode", self.o.store.get_job(n)["params"])                                      # bỏ mặc định ⇒ job cũ y như trước

    def test_default_preset_never_breaks_when_remix_disabled_and_delete_clears_default(self):
        svc = self.svc()
        svc.story_preset_save("p", {"story_mode": {}, "make_default": True})
        self.o.cfg.data["story"]["remix_enabled"] = False
        j = self.o.submit(params(), mode="STORY_ONLY")                                                         # Remix tắt: job thường chạy Story cũ, không bị từ chối
        self.assertNotIn("story_mode", self.o.store.get_job(j)["params"])
        self.o.cfg.data["story"]["remix_enabled"] = True
        info = svc.story_preset_delete("p")
        self.assertEqual((info["presets"], info["default_preset"]), ([], None))
        with self.assertRaises(StageError) as e:
            svc.story_preset_delete("p")
        self.assertEqual(e.exception.code, "PRESET_NOT_FOUND")
        with self.assertRaises(StageError):
            svc.story_preset_default({"name": "không có"})

    def test_corrupt_preset_file_is_ignored_safely(self):
        svc = self.svc()
        svc._presets_file().parent.mkdir(parents=True, exist_ok=True)
        svc._presets_file().write_text("{not json", encoding="utf-8")
        self.assertEqual(svc.story_mode_info()["presets"], [])
        j = self.o.submit(params(), mode="STORY_ONLY")
        self.assertNotIn("story_mode", self.o.store.get_job(j)["params"])

    def test_effective_config_shows_inheritance_system_preset_job(self):
        svc = self.svc()
        svc.story_preset_save("m", {"story_mode": {"story": {"tone": "u ám", "ending": "open"}}})
        eff = svc.story_mode_effective({"preset": "m", "story_mode": {"mode": "story_remix", "story": {"tone": "u ám", "ending": "happy", "target_genre": "kinh dị"}}})
        s = eff["story"]
        self.assertEqual((s["tone"]["source"], s["ending"]["value"], s["ending"]["source"], s["target_genre"]["source"], s["budget_usd"]["source"]), ("preset", "happy", "job", "job", "system"))
        self.assertEqual(eff["character_universe"]["auto_cast"]["source"], "system")
        self.assertEqual(SM.effective({"mode": "story_branch"}, {})["label"], SM.LABELS["story_branch"])


class EstimateTest(RootCase):
    def test_estimate_is_rough_public_and_never_invents_prices(self):
        e = estimate({"quality_repair_max_passes": 1, "premise_candidates": 3}, {}, 60_000)
        self.assertEqual((e["chapters"], e["usd"]), (14, None))
        self.assertIn("không rõ", e["note"])
        self.assertLess(e["calls"]["min"], e["calls"]["max"])
        self.assertGreater(e["input_tokens"]["max"], e["input_tokens"]["min"])
        priced = estimate({"quality_repair_max_passes": 1, "premise_candidates": 3}, {}, 60_000, {"in": 3, "out": 15})
        self.assertLess(priced["usd"]["min"], priced["usd"]["max"])
        zero = estimate({"quality_repair_max_passes": 0, "premise_candidates": 3}, {}, 60_000)
        self.assertLess(zero["calls"]["max"], e["calls"]["max"])                                               # trần số lượt sửa giới hạn chi phí
        long = estimate({"quality_repair_max_passes": 1, "premise_candidates": 3}, {}, 900_000)
        self.assertEqual(long["input_tokens"]["min"] - e["input_tokens"]["min"] > 0, True)
        self.assertEqual(estimate({"quality_repair_max_passes": 1, "premise_candidates": 3}, {"target_chars": 18000}, 60_000)["chapters"], 6)

    def test_service_estimate_validates_and_reads_price_config(self):
        o = self.orc()
        svc = Service(o)
        out = svc.story_mode_estimate({"story_mode": {"story": {"quality_repair_max_passes": 2}}, "source_chars": 30000})
        self.assertEqual((out["assumptions"]["repair_passes"], out["usd"]), (2, None))
        o.cfg.data["story_remix"]["price_usd_per_mtok"] = {"in": 3, "out": 15}
        self.assertIsNotNone(svc.story_mode_estimate({})["usd"])
        for bad in ({"source_chars": 5}, {"source_chars": True}, {"story_mode": {"story": {"ending": "?"}}}):
            with self.assertRaises(StageError):
                svc.story_mode_estimate(bad)

    def test_job_view_carries_estimate_next_to_actual(self):
        o = self.orc()
        j = o.submit(params(story_mode={"mode": "story_remix"}), mode="STORY_ONLY")
        p = Service(o).remix_plan(j)
        self.assertIn("estimate", p)
        self.assertGreater(p["estimate"]["calls"]["max"], 0)
        self.assertIsNone(p["cost"])


if __name__ == "__main__":
    unittest.main()
