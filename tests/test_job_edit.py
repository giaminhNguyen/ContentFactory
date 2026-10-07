"""Sửa job (core): đổi đích pipeline theo progress floor, giữ job đã xong không tự chạy, xóa job an toàn (không đụng output/)."""
import sqlite3

from contentfactory.contracts import ErrorClass, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.jobs.db import CONTROL_DELETED, CONTROL_PAUSED, CONTROL_RUNNING
from contentfactory.jobs.plan import progress_floor
from tests.support import RootCase, params


def done_state(stage: str) -> str:
    return P.BY_NAME[stage].done_state


def idx(name: str) -> int:
    return P.INDEX[name]


class ProgressFloorTest(RootCase):
    """Hàm thuần: floor suy từ máy trạng thái + lịch sử, không phải từ tên stage."""

    def job(self, state, start=None, failed=None):
        return {"state": state, "start_stage": start, "failed_stage": failed}

    def test_floor_by_state(self):
        runs = lambda *names: [{"stage": n} for n in names]
        self.assertEqual(progress_floor(self.job(P.NEW), []), idx("source"))                              # chưa chạy gì: stage đầu
        self.assertEqual(progress_floor(self.job(P.TTS_RUNNING), runs("source", "story", "tts")), idx("tts"))   # đang chạy = chính nó
        self.assertEqual(progress_floor(self.job(done_state("audio")), runs("source", "story", "tts", "audio")), idx("audio"))
        self.assertEqual(progress_floor(self.job(P.PUBLISHED), []), idx("publish"))
        self.assertEqual(progress_floor(self.job(P.FAILED, failed="render_youtube"), []), idx("render_youtube"))

    def test_floor_respects_start_and_history(self):
        self.assertEqual(progress_floor(self.job(P.STORY_READY, start="tts"), []), idx("tts"))            # queued ở start_stage
        # stage từng bắt đầu rồi bị ngắt/lùi vẫn tính là đã bắt đầu
        self.assertEqual(progress_floor(self.job(P.STORY_READY), [{"stage": "tts"}]), idx("tts"))


class UpdateTargetTest(RootCase):
    def domain(self, orc, jid, target):
        with self.assertRaises(StageError) as cm:
            orc.update_target(jid, target)
        self.assertEqual(cm.exception.error_class, ErrorClass.POLICY)
        return cm.exception.code

    def test_queued_job_target_can_change_and_invalid_is_rejected(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="STORY_ONLY")
        r = orc.update_target(jid, "tts")
        self.assertEqual((r["result"], r["old_target"], r["new_target"], r["held"]), ("changed", "story", "tts", False))
        j = orc.store.get_job(jid)
        self.assertEqual((j["target_stage"], j["target_idx"]), ("tts", idx("tts")))
        self.assertEqual(j["config_snapshot"]["target_stage"], "tts")                                      # snapshot khớp đích mới
        self.assertEqual(self.domain(orc, jid, "nonsense"), "PIPELINE_TARGET_INVALID")
        self.assertEqual(orc.store.get_job(jid)["target_stage"], "tts")                                   # lỗi không đổi gì

    def test_repeated_request_is_idempotent(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="STORY_ONLY")
        orc.update_target(jid, "tts")
        n = len(orc.store.transitions(jid))
        self.assertEqual(orc.update_target(jid, "tts")["result"], "unchanged")
        self.assertEqual(len(orc.store.transitions(jid)), n)                                              # không ghi lịch sử thừa

    def test_running_job_target_changes_without_touching_the_running_stage(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="STORY_ONLY")                                                     # đích cũ = story
        orc.run()
        orc.update_target(jid, "publish")
        orc.resume(jid)                                                                                    # job đã xong được giữ: “Chạy tiếp”
        claim = orc.store.claim(P.BY_NAME["tts"], 1, 5, "owner-x", 60)[0]                                 # job đang chạy TTS
        before = orc.store.get_job(jid)
        # mở rộng: không restart, không đổi state/lease
        self.assertEqual(orc.update_target(jid, "output")["result"], "changed")
        # rút ngắn nhưng vẫn >= tiến độ: cho phép
        self.assertEqual(orc.update_target(jid, "audio")["result"], "changed")
        # đích = đúng stage đang chạy: hoàn tất stage rồi dừng
        self.assertEqual(orc.update_target(jid, "tts")["result"], "changed")
        after = orc.store.get_job(jid)
        self.assertEqual((after["state"], after["lease_owner"], after["lease_until"]), (before["state"], before["lease_owner"], before["lease_until"]))
        self.assertEqual(self.domain(orc, jid, "story"), "PIPELINE_TARGET_BEFORE_PROGRESS")               # lùi qua tiến độ: từ chối
        self.assertEqual(orc.store.get_job(jid)["target_stage"], "tts")
        orc.store.release(claim, "owner-x")
        orc.run()                                                                                          # chạy nốt tới đích mới rồi dừng
        self.assertEqual(orc.store.get_job(jid)["state"], done_state("tts"))
        self.assertEqual(self.runs(orc, jid)["source"], ["succeeded"])                                    # không chạy lại stage cũ

    def test_shortening_target_still_finishes_current_stage_then_stops(self):
        orc = self.orc()
        jid = orc.submit(params())                                                                         # đích cũ = publish
        orc.update_target(jid, "story")
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], done_state("story"))
        self.assertNotIn("tts", self.runs(orc, jid))

    def test_no_rollback_before_progress_for_completed_jobs(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="THROUGH_TTS")
        orc.run()
        orc.update_target(jid, "audio")
        orc.store.set_control(jid, CONTROL_RUNNING)
        orc.run()                                                                                          # đã xong tới audio
        for t in ("source", "story", "tts"):
            self.assertEqual(self.domain(orc, jid, t), "PIPELINE_TARGET_BEFORE_PROGRESS")
        self.assertEqual(orc.store.get_job(jid)["target_stage"], "audio")
        self.assertEqual(orc.update_target(jid, "audio")["result"], "unchanged")                          # == floor: no-op hợp lệ

    def test_completed_job_extension_is_saved_but_does_not_run_until_resumed(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="THROUGH_TTS")
        orc.run()
        before = self.runs(orc, jid)
        r = orc.update_target(jid, "render_youtube")
        self.assertEqual((r["result"], r["held"]), ("changed", True))
        j = orc.store.get_job(jid)
        self.assertEqual((j["control_state"], j["pause_origin"], j["target_stage"]), (CONTROL_PAUSED, "EDIT", "render_youtube"))
        orc.run()                                                                                          # Lưu KHÔNG tự chạy
        self.assertEqual(self.runs(orc, jid), before)
        self.assertEqual(orc.store.get_job(jid)["state"], done_state("tts"))
        orc.resume(jid)                                                                                    # “Chạy tiếp”
        orc.run()
        after = self.runs(orc, jid)
        for s in ("source", "story", "tts"):
            self.assertEqual(after[s], before[s])                                                          # dùng lại, không chạy lại
        self.assertEqual(after["audio"], ["succeeded"])
        self.assertEqual(after["render_youtube"], ["succeeded"])
        self.assertEqual(orc.store.get_job(jid)["state"], done_state("render_youtube"))

    def test_batch_resume_cannot_release_an_edit_hold(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="THROUGH_TTS")
        orc.run()
        orc.update_target(jid, "audio")
        self.assertEqual(orc.store.set_control(jid, CONTROL_RUNNING, "BATCH"), "not_owner")
        self.assertEqual(orc.store.get_job(jid)["control_state"], CONTROL_PAUSED)

    def test_set_target_extension_keeps_auto_run_for_dedupe_flow(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="THROUGH_TTS")
        orc.run()
        orc.set_target(jid, "audio")                                                                       # đường cũ (dedupe/CLI): chạy tiếp ngay
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], done_state("audio"))

    def test_held_and_failed_jobs_keep_their_hold_and_history(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="STORY_ONLY")
        orc.run()
        orc.update_target(jid, "publish")
        with sqlite3.connect(orc.store.path) as c:
            c.execute("UPDATE jobs SET hold_reason='PAUSED_NETWORK', hold_detail='x' WHERE id=?", (jid,))
        runs = len(orc.store.stage_runs(jid))
        orc.update_target(jid, "render_youtube")
        j = orc.store.get_job(jid)
        self.assertEqual((j["hold_reason"], j["target_stage"]), ("PAUSED_NETWORK", "render_youtube"))      # không tự gỡ hold
        self.assertEqual(len(orc.store.stage_runs(jid)), runs)                                             # lịch sử còn nguyên
        with sqlite3.connect(orc.store.path) as c:                                                         # FAILED ở render_youtube: không lùi trước stage lỗi
            c.execute("UPDATE jobs SET state='FAILED', failed_stage='render_youtube', hold_reason=NULL WHERE id=?", (jid,))
        self.assertEqual(self.domain(orc, jid, "audio"), "PIPELINE_TARGET_BEFORE_PROGRESS")
        self.assertEqual(orc.update_target(jid, "output")["result"], "changed")
        self.assertEqual(orc.store.get_job(jid)["failed_stage"], "render_youtube")

    def test_cas_conflict_and_unknown_job(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="STORY_ONLY")
        res = orc.store.update_target(jid, "tts", expect_state=P.TTS_RUNNING)                              # state đã khác lúc tính kế hoạch
        self.assertEqual(res["result"], "conflict")
        self.assertEqual(orc.store.get_job(jid)["target_stage"], "story")
        self.assertEqual(self.domain(orc, "999999", "tts"), "JOB_NOT_FOUND")

    def test_cancelled_job_cannot_change_target(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="STORY_ONLY")
        orc.cancel_job(jid)
        self.assertEqual(self.domain(orc, jid, "tts"), "JOB_CANCELLED")

    def test_target_needing_missing_input_is_rejected_before_any_change(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="TTS_ONLY", inputs={"story_text": self.story()})
        self.assertEqual(self.domain(orc, jid, "story"), "PIPELINE_TARGET_BEFORE_PROGRESS")

    def test_custom_pipeline_job_keeps_requested_stages_and_gets_new_spec(self):
        orc = self.orc()
        jid = orc.submit(params(), pipeline={"requested_stages": ["story"]})
        orc.run()
        r = orc.update_target(jid, "tts")
        self.assertEqual(r["result"], "changed")
        j = orc.store.get_job(jid)
        self.assertEqual(j["pipeline"]["requested_stages"], ["story", "tts"])
        self.assertEqual((j["target_stage"], j["pipeline"]["run"][-1]), ("tts", "tts"))
        self.assertEqual(j["control_state"], CONTROL_PAUSED)                                              # job đã xong: giữ, chờ Chạy tiếp

    def story(self):
        from contentfactory.adapters.fake import _paragraph
        p = self.root / "story.txt"
        p.write_text("\n\n".join(_paragraph(k) for k in range(1, 7)), encoding="utf-8")
        return str(p)


class DeleteJobTest(RootCase):
    def test_delete_completed_job_hides_it_and_keeps_output(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="THROUGH_TTS")
        orc.run()
        keep = self.root / "output" / "Kenh" / "du-an" / "video.mp4"
        keep.parent.mkdir(parents=True)
        keep.write_bytes(b"user-asset")
        self.assertTrue(self.job_dir(jid).exists())
        self.assertEqual(orc.delete_job(jid), "deleted")
        self.assertEqual(orc.store.get_job(jid)["control_state"], CONTROL_DELETED)
        self.assertNotIn(jid, [j["id"] for j in orc.store.list_jobs()])
        self.assertNotIn(jid, [j["id"] for j in orc.store.job_index()])
        self.assertFalse(self.job_dir(jid).exists())                                                      # workspace nội bộ đã dọn
        self.assertEqual(keep.read_bytes(), b"user-asset")                                                # output của người dùng còn nguyên
        self.assertEqual(orc.delete_job(jid), "already")                                                  # lặp lại: idempotent
        self.assertEqual(orc.delete_job("999999"), "gone")

    def test_deleted_queued_job_is_never_claimed(self):
        orc = self.orc()
        jid = orc.submit(params())
        other = orc.submit(params(), mode="STORY_ONLY")
        orc.delete_job(jid)
        orc.run()
        self.assertEqual(self.runs(orc, jid), {})
        self.assertEqual(orc.store.get_job(other)["state"], done_state("story"))
        self.assertEqual(orc.store.set_control(jid, CONTROL_PAUSED), "deleted")

    def test_delete_running_job_aborts_cooperatively_and_cleans_after_lease_release(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="STORY_ONLY")
        orc.run()
        orc.update_target(jid, "publish")
        orc.resume(jid)                                                                                    # job đã xong được giữ: “Chạy tiếp”
        claim = orc.store.claim(P.BY_NAME["tts"], 1, 5, "owner-x", 60)[0]
        part = self.job_dir(jid) / "tts" / "chunk.wav.part"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"x")
        self.assertEqual(orc.store.job_signal(jid), None)
        self.assertEqual(orc.delete_job(jid), "deleted")
        self.assertEqual(orc.store.job_signal(jid), "abort")                                              # worker thấy tín hiệu abort
        self.assertTrue(self.job_dir(jid).exists())                                                       # còn lease: chưa dọn workspace
        self.assertNotIn(jid, [j["id"] for j in orc.store.list_jobs()])
        orc._finalize_deleted()
        self.assertTrue(self.job_dir(jid).exists())
        orc.store.release(claim, "owner-x")                                                               # stage dừng ở điểm an toàn, nhả lease
        orc._finalize_deleted()
        self.assertFalse(self.job_dir(jid).exists())
        self.assertEqual(orc.store.get_job(jid)["lease_owner"], None)
        orc.run()
        self.assertEqual(len(self.runs(orc, jid).get("tts", [])), 1)                                      # không bị nhận lại

    def test_delete_releases_unpublished_sequence(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="STORY_ONLY")
        orc.sequence.reserve("default", jid)
        orc.delete_job(jid)
        self.assertIsNone(orc.sequence.get(jid))
