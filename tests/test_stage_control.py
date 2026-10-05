"""Phase 2.9: start_stage/target_stage, artifact đưa từ ngoài vào, skip khi artifact hợp lệ, stage_key theo khai báo."""
import json
import shutil
import wave
from pathlib import Path

from contentfactory.adapters.fake import _paragraph
from contentfactory.contracts import ErrorClass, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.jobs.plan import plan_job
from contentfactory.orchestrator.stages import StageContract
from tests.support import RootCase, params


def done_state(stage: str) -> str:
    return P.BY_NAME[stage].done_state


class StageControlTest(RootCase):
    def story_file(self) -> Path:
        p = self.root / "story.txt"
        p.write_text("\n\n".join(_paragraph(k) for k in range(1, 7)), encoding="utf-8")
        return p

    def wav_file(self) -> Path:
        p = self.root / "master.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 8000 * 4)
        return p

    # -- use case 1/2: chỉ subtitle, chỉ story ------------------------------------------------
    def test_source_only_and_story_only_stop_at_target(self):
        orc = self.orc()
        a = orc.submit(params(), mode="SUBTITLE_ONLY")
        b = orc.submit(params(), mode="STORY_ONLY")
        orc.run()
        self.assertEqual(orc.store.get_job(a)["state"], done_state("source"))
        self.assertEqual(orc.store.get_job(b)["state"], done_state("story"))
        self.assertEqual(set(self.runs(orc, a)), {"source"})
        self.assertEqual(set(self.runs(orc, b)), {"source", "story"})
        self.assertTrue(P.is_complete(orc.store.get_job(b)["state"], orc.store.get_job(b)["target_idx"]))
        self.assertEqual(orc.store.nonterminal_count(), 0)             # đạt target = không còn việc, tiến trình thoát

    # -- use case: Story artifact -> target Audio (story.txt -> TTS) ----------------------------
    def test_story_artifact_to_audio_target_runs_only_needed_stages(self):
        orc = self.orc()
        jid = orc.submit(params(), inputs={"story_text": str(self.story_file())}, target_stage="audio")
        j = orc.store.get_job(jid)
        self.assertEqual((j["start_stage"], j["target_stage"]), ("tts", "audio"))
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], done_state("audio"))
        self.assertEqual(set(self.runs(orc, jid)), {"tts", "audio"})   # không source/story
        m = json.loads((self.job_dir(jid) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([i["kind"] for i in m["imports"]], ["story_text"])
        self.assertTrue(m["complete"])

    def test_tts_only(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="TTS_ONLY", inputs={"story_text": str(self.story_file())})
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], done_state("tts"))
        self.assertEqual(set(self.runs(orc, jid)), {"tts"})

    # -- use case: Audio artifact -> target Video ----------------------------------------------
    def test_audio_artifact_to_video_target(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="VIDEO_ONLY",
                         inputs={"audio_master": str(self.wav_file()), "metadata": {"title": "Tiêu đề thủ công"}})
        self.assertEqual(orc.store.get_job(jid)["start_stage"], "audio")
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual(j["state"], done_state("render_tiktok"))
        self.assertEqual(set(self.runs(orc, jid)), {"audio", "render_youtube", "render_tiktok"})
        kinds = {a["kind"] for a in orc.store.artifacts(jid)}
        self.assertTrue({"video_youtube", "thumbnail", "video_tiktok"} <= kinds)

    # -- never force upstream when valid artifacts exist ---------------------------------------
    def test_valid_provided_artifact_is_skipped_not_rerun(self):
        orc = self.orc()
        jid = orc.submit(params(), start_stage="story", target_stage="tts",
                         inputs={"story_text": str(self.story_file())})
        orc.run()
        self.assertEqual(self.runs(orc, jid)["story"], ["skipped"])
        self.assertFalse((self.job_dir(jid) / "story" / "calls.log").exists())   # handler story KHÔNG được gọi
        self.assertEqual(orc.store.get_job(jid)["state"], done_state("tts"))
        m = json.loads((self.job_dir(jid) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["stages"]["story"]["skipped_reason"], "provided")

    def test_from_job_reuses_artifacts_and_skips_upstream(self):
        orc = self.orc()
        first = orc.submit(params(), mode="STORY_ONLY")
        orc.run()
        second = orc.submit(params(), mode="THROUGH_TTS", from_job={"job_id": first, "kinds": ["story_text"]})
        self.assertEqual(orc.store.get_job(second)["start_stage"], "tts")
        orc.run()
        self.assertEqual(set(self.runs(orc, second)), {"tts"})
        self.assertEqual(orc.store.get_job(second)["state"], done_state("tts"))

    def test_extend_target_does_not_rerun_completed_stages(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="THROUGH_TTS")
        orc.run()
        before = self.runs(orc, jid)
        orc.set_target(jid, "audio")
        orc.run()
        after = self.runs(orc, jid)
        for s in ("source", "story", "tts"):
            self.assertEqual(after[s], before[s])                       # stage đã xong không chạy lại
        self.assertEqual(after["audio"], ["succeeded"])
        self.assertEqual(orc.store.get_job(jid)["state"], done_state("audio"))

    def test_full_pipeline_unchanged(self):
        orc = self.orc()
        jid = orc.submit(params())
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        self.assertTrue(all(v == ["succeeded"] for v in self.runs(orc, jid).values()))

    # -- spec không hợp lệ bị từ chối ngay, không để lại job nửa vời ------------------------------
    def test_invalid_specs_rejected_without_creating_job(self):
        orc = self.orc()
        bad = [dict(mode="TTS_ONLY"),                                  # thiếu story_text
               dict(start_stage="tts", target_stage="story", inputs={"story_text": str(self.story_file())}),
               dict(target_stage="nonsense"),
               dict(inputs={"nonsense_kind": str(self.story_file())}),
               dict(inputs={"story_text": str(self.root / "missing.txt")}, mode="TTS_ONLY")]
        for kw in bad:
            with self.assertRaises(StageError, msg=str(kw)):
                orc.submit(params(), **kw)
        empty = self.root / "empty.txt"
        empty.write_text("", encoding="utf-8")
        with self.assertRaises(StageError):
            orc.submit(params(), mode="TTS_ONLY", inputs={"story_text": str(empty)})
        self.assertEqual(orc.store.list_jobs(), [])

    def test_planner_tables(self):
        p = plan_job(None, "tts", set(), True)
        self.assertEqual(p.run, ["source", "story", "tts"])
        p = plan_job(None, None, set(), True)
        self.assertEqual(p.run, [s.name for s in P.STAGES])
        self.assertTrue(plan_job("story", "tts", set(), True).errors)  # story cần transcript mà không có và start không phải source
        self.assertTrue(plan_job("tts", "story", {"story_text"}, True).errors)

    # -- stage_key chỉ phụ thuộc vào khai báo ------------------------------------------------------
    def test_stage_key_depends_only_on_declared_deps(self):
        c = StageContract(P.BY_NAME["tts"])
        ins = {"story_text": [{"path": "a", "kind": "story_text", "sha256": "x", "bytes": 1, "meta": {}}]}
        base = c.stage_key({"tts": {"voice": "a"}, "made_for_kids": False}, None, ins)
        self.assertEqual(base, c.stage_key({"tts": {"voice": "a"}, "made_for_kids": True}, None, ins))   # tham số không liên quan
        self.assertNotEqual(base, c.stage_key({"tts": {"voice": "b"}, "made_for_kids": False}, None, ins))
        ins2 = {"story_text": [{**ins["story_text"][0], "sha256": "y"}]}
        self.assertNotEqual(base, c.stage_key({"tts": {"voice": "a"}}, None, ins2))                      # input đổi

    def test_changed_provided_artifact_is_not_skipped_after_edit(self):
        """File import bị sửa sau khi tạo job => sha256 không khớp => không còn coi là hợp lệ để skip."""
        orc = self.orc()
        jid = orc.submit(params(), start_stage="story", target_stage="tts", inputs={"story_text": str(self.story_file())})
        imp = next(a for a in orc.store.artifacts(jid) if a["stage"] == "import")
        (self.job_dir(jid) / imp["path"]).write_text("khác hẳn", encoding="utf-8")
        orc.run()
        j = orc.store.get_job(jid)
        self.assertNotEqual(self.runs(orc, jid).get("story"), ["skipped"])
        self.assertIsNotNone(j["hold_reason"] or j["failed_stage"])    # không âm thầm dùng file hỏng
