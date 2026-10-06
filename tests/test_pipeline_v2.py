"""Phase 1 (Agent Plan): Pipeline Planner v2 — spec theo requested_stages + dependency closure, output partial-aware, descriptor cho UI."""
import json
import wave
from pathlib import Path

from contentfactory.adapters.fake import _paragraph
from contentfactory.contracts import StageError
from contentfactory.jobs import pipeline as P
from contentfactory.jobs.plan import plan_job, plan_spec, spec_for_mode, spec_from_range
from contentfactory.orchestrator.service import Service
from tests.support import RootCase, params

ALL = [s.name for s in P.STAGES]


def spec(*stages: str) -> dict:
    return {"version": 2, "requested_stages": list(stages)}


class PlannerV2Test(RootCase):
    # 1. Full tương đương behavior cũ
    def test_full_equals_legacy_plan(self):
        old = plan_job(None, None, set(), True)
        new = plan_spec(spec_for_mode("FULL"), set(), True)
        self.assertEqual((new.run, new.errors), (old.run, old.errors))
        self.assertEqual(new.run, ALL)

    # 2/3. một nhánh render không kéo nhánh kia
    def test_tiktok_only_does_not_include_youtube(self):
        p = plan_spec(spec("render_tiktok"), set(), True)
        self.assertEqual(p.run, ["source", "story", "tts", "audio", "render_tiktok"])
        self.assertNotIn("render_youtube", p.run)
        self.assertEqual(p.states["render_youtube"]["state"], "not_requested")

    def test_youtube_only_does_not_include_tiktok(self):
        p = plan_spec(spec("render_youtube"), set(), True)
        self.assertEqual(p.run, ["source", "story", "tts", "audio", "render_youtube"])
        self.assertNotIn("render_tiktok", p.run)

    # 4. YouTube + publish không ép TikTok
    def test_publish_youtube_does_not_force_tiktok(self):
        p = plan_spec(spec("publish"), set(), True)
        self.assertEqual(p.run, ["source", "story", "tts", "audio", "render_youtube", "output", "publish"])
        self.assertEqual(p.states["publish"]["state"], "selected")
        self.assertEqual(p.states["render_youtube"]["state"], "locked")
        self.assertEqual(p.states["render_youtube"]["by"], ["publish"])

    # 5. gói TikTok không cần artifact YouTube
    def test_tiktok_package_without_youtube(self):
        p = plan_spec(spec("render_tiktok", "output"), set(), True)
        self.assertNotIn("render_youtube", p.run)
        self.assertEqual(p.run[-2:], ["render_tiktok", "output"])
        self.assertFalse(p.errors)

    def test_output_alone_has_nothing_to_package(self):
        p = plan_spec(spec("output"), set(), True)
        self.assertTrue(any("output" in e for e in p.errors))

    # 6. audio_master import => bỏ source/story/tts
    def test_imported_audio_master_skips_upstream(self):
        p = plan_spec(spec("render_youtube"), {"audio_master", "metadata"}, False)
        self.assertEqual(p.run, ["audio", "render_youtube"])
        self.assertEqual(p.states["tts"]["state"], "provided")
        self.assertEqual(p.states["source"]["state"], "provided")        # metadata đã có sẵn
        self.assertFalse(p.errors)

    # 7. thiếu artifact => lỗi xác định trước khi chạy
    def test_missing_artifact_is_deterministic_plan_error(self):
        a = plan_spec(spec("render_youtube"), set(), False)
        b = plan_spec(spec("render_youtube"), set(), False)
        self.assertTrue(a.errors)
        self.assertEqual(a.errors, b.errors)
        self.assertEqual(a.run, b.run)

    # 8. MODES cũ map đúng
    def test_legacy_modes_map_to_specs(self):
        expected = {"SUBTITLE_ONLY": ["source"], "STORY_ONLY": ["story"], "THROUGH_TTS": ["tts"], "TTS_ONLY": ["tts"],
                    "VIDEO_ONLY": ["render_youtube", "render_tiktok"], "FULL": ["render_youtube", "render_tiktok", "output", "publish"]}
        for mode, roots in expected.items():
            self.assertEqual(spec_for_mode(mode)["requested_stages"], roots, mode)
        for mode, (start, target) in P.MODES.items():                # planner mới cho đúng kết quả của planner cũ
            have = {"story_text", "metadata"} if mode == "TTS_ONLY" else {"audio_master", "metadata"} if mode == "VIDEO_ONLY" else set()
            old = plan_job(start, target, have, mode not in ("TTS_ONLY", "VIDEO_ONLY"))
            new = plan_spec(spec_from_range(start, target), have, mode not in ("TTS_ONLY", "VIDEO_ONLY"), floor=start)
            self.assertEqual((old.run, old.skip, old.errors), (new.run, new.skip, new.errors), mode)

    # 9. stage id sai bị từ chối
    def test_invalid_stage_ids_rejected(self):
        self.assertTrue(plan_spec(spec("nonsense"), set(), True).errors)
        self.assertTrue(plan_spec(spec(), set(), True).errors)
        self.assertTrue(plan_spec({"version": 1, "requested_stages": ["tts"]}, set(), True).errors)
        self.assertTrue(plan_spec(spec("tts", "tts"), set(), True).errors == [])        # trùng lặp thì gộp, không lỗi

    def test_plan_is_deterministic_and_order_independent(self):
        a = plan_spec(spec("publish", "render_tiktok"), set(), True)
        b = plan_spec(spec("render_tiktok", "publish"), set(), True)
        self.assertEqual((a.run, a.states, a.errors), (b.run, b.states, b.errors))

    def test_floor_blocks_stages_before_start(self):
        p = plan_spec(spec("render_youtube"), set(), True, floor="audio")
        self.assertTrue(p.errors)                                   # thiếu audio_master mà start_stage chặn tts

    # 10. descriptor khớp thứ tự backend và không lặp đồ thị ở frontend
    def test_descriptor_matches_backend_stage_order(self):
        d = Service(self.orc()).pipeline_descriptor()
        self.assertEqual([s["id"] for s in d["stages"]], ALL)
        self.assertEqual([s["order"] for s in d["stages"]], sorted(s["order"] for s in d["stages"]))
        by = {s["id"]: s for s in d["stages"]}
        for st in P.STAGES:
            self.assertEqual(tuple(by[st.name]["requires"]), st.requires)
            self.assertEqual(tuple(by[st.name]["produces"]), st.produces)
        self.assertEqual(d["modes"]["VIDEO_ONLY"]["requested_stages"], ["render_youtube", "render_tiktok"])
        self.assertEqual(d["version"], 2)

    def test_plan_endpoint_explains_states(self):
        svc = Service(self.orc())
        r = svc.plan_pipeline({"pipeline_spec": spec("render_tiktok"), "input_kind": "youtube_url"})
        self.assertTrue(r["ok"])
        st = {s["id"]: s for s in r["stages"]}
        self.assertEqual(st["render_tiktok"]["state"], "selected")
        self.assertEqual(st["audio"]["state"], "locked")
        self.assertTrue(st["audio"]["reason"])
        self.assertEqual(st["render_youtube"]["state"], "not_requested")
        r = svc.plan_pipeline({"pipeline_spec": spec("render_youtube"), "input_kind": "audio"})
        self.assertEqual({s["id"]: s["state"] for s in r["stages"]}["tts"], "provided")
        bad = svc.plan_pipeline({"pipeline_spec": spec("story"), "input_kind": "audio"})
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["errors"])


class PipelineRunTest(RootCase):
    def wav_file(self) -> Path:
        p = self.root / "master.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 8000 * 4)
        return p

    def story_file(self) -> Path:
        p = self.root / "story.txt"
        p.write_text("\n\n".join(_paragraph(k) for k in range(1, 7)), encoding="utf-8")
        return p

    def kinds(self, orc, jid) -> set[str]:
        return {a["kind"] for a in orc.store.artifacts(jid)}

    def test_tiktok_only_job_never_renders_youtube(self):
        orc = self.orc()
        jid = orc.submit(params(), pipeline=spec("render_tiktok"))
        j = orc.store.get_job(jid)
        self.assertEqual((j["start_stage"], j["target_stage"]), ("source", "render_tiktok"))
        orc.run()
        j = orc.store.get_job(jid)
        self.assertTrue(P.is_complete(j["state"], j["target_idx"]))
        runs = self.runs(orc, jid)
        self.assertEqual(runs["render_youtube"], ["skipped"])             # đi qua máy trạng thái nhưng không render
        self.assertEqual(runs["render_tiktok"], ["succeeded"])
        k = self.kinds(orc, jid)
        self.assertIn("video_tiktok", k)
        self.assertFalse({"video_youtube", "thumbnail"} & k)
        m = json.loads((self.job_dir(jid) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["stages"]["render_youtube"]["skipped_reason"], "not_requested")

    def test_youtube_only_job_with_package_has_no_tiktok(self):
        orc = self.orc()
        jid = orc.submit(params(), pipeline=spec("render_youtube", "output"))
        orc.run()
        j = orc.store.get_job(jid)
        self.assertTrue(P.is_complete(j["state"], j["target_idx"]))
        self.assertEqual(self.runs(orc, jid)["render_tiktok"], ["skipped"])
        k = self.kinds(orc, jid)
        self.assertTrue({"video_youtube", "thumbnail", "output_package"} <= k)
        self.assertNotIn("video_tiktok", k)
        pkg = next(a for a in orc.store.artifacts(jid) if a["kind"] == "output_package")
        proj = json.loads((Path(json.loads((self.job_dir(jid) / pkg["path"]).read_text(encoding="utf-8"))["project_dir"]) / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(proj["tiktok"]["count"], 0)
        self.assertTrue(proj["youtube"]["video"])

    def test_publish_youtube_without_tiktok(self):
        orc = self.orc()
        jid = orc.submit(params(), pipeline=spec("publish"))
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        self.assertEqual(self.runs(orc, jid)["render_tiktok"], ["skipped"])
        self.assertNotIn("video_tiktok", self.kinds(orc, jid))

    def test_tiktok_package_only(self):
        orc = self.orc()
        jid = orc.submit(params(), pipeline=spec("render_tiktok", "output"))
        orc.run()
        self.assertEqual(self.runs(orc, jid)["render_youtube"], ["skipped"])
        k = self.kinds(orc, jid)
        self.assertTrue({"video_tiktok", "output_package"} <= k)
        self.assertFalse({"video_youtube", "thumbnail"} & k)

    def test_imported_audio_job_runs_only_needed_stages(self):
        orc = self.orc()
        jid = orc.submit(params(), pipeline=spec("render_tiktok"),
                         inputs={"audio_master": str(self.wav_file()), "metadata": {"title": "Tiêu đề thủ công"}})
        j = orc.store.get_job(jid)
        self.assertEqual(j["start_stage"], "audio")
        orc.run()
        runs = self.runs(orc, jid)
        self.assertEqual(runs["audio"], ["succeeded"])
        self.assertNotIn("source", runs)
        self.assertEqual(runs["render_youtube"], ["skipped"])

    def test_invalid_pipeline_rejected_without_job(self):
        orc = self.orc()
        for bad in (spec("nonsense"), spec(), spec("render_youtube")):    # sai id / rỗng / thiếu input (không URL, không import)
            kw = {"pipeline": bad}
            p = params()
            if bad == spec("render_youtube"):
                p["input"] = {}
                p.pop("title")
            with self.assertRaises(StageError, msg=str(bad)):
                orc.submit(p, **kw)
        self.assertEqual(orc.store.list_jobs(), [])

    def test_pipeline_cannot_mix_with_legacy_range(self):
        with self.assertRaises(StageError):
            self.orc().submit(params(), pipeline=spec("tts"), mode="FULL")

    def test_pipeline_spec_persisted_with_job(self):
        orc = self.orc()
        jid = orc.submit(params(), pipeline=spec("render_tiktok"))
        pl = orc.store.get_job(jid)["pipeline"]
        self.assertEqual(pl["requested_stages"], ["render_tiktok"])
        self.assertEqual(pl["run"], ["source", "story", "tts", "audio", "render_tiktok"])
        self.assertEqual(orc.store.get_job(orc.submit(params(), mode="FULL"))["pipeline"], None)   # job kiểu cũ giữ nguyên hành vi

    def test_service_custom_pipeline_creates_job_and_plan(self):
        svc = Service(self.orc())
        pv = svc.preview_run({"input": {"value": "https://youtu.be/abcdefghijk"}, "channel": "default",
                              "pipeline": {"mode": "custom", "requested_stages": ["render_tiktok"]}, "kids": False})
        self.assertIsNotNone(pv["plan"])
        states = {s["name"]: s["state"] for s in pv["plan"]["stages"]}
        self.assertEqual(states["render_youtube"], "off")
        self.assertEqual(states["render_tiktok"], "run")
        r = svc.create_run({"input": {"value": "https://youtu.be/abcdefghijk"}, "channel": "default", "kids": False,
                            "pipeline": {"mode": "custom", "requested_stages": ["render_tiktok"]}})
        self.assertEqual(svc.orc.store.get_job(r["job_id"])["pipeline"]["requested_stages"], ["render_tiktok"])
