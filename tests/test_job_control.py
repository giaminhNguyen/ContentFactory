"""Agent Plan Phase 2: Tạm dừng an toàn / tiếp tục / hủy (khác hold tài nguyên) + pipeline revision (impact, áp dụng tại điểm an toàn, restart)."""
import json
import time

from contentfactory.contracts import CancelToken, ErrorClass, JobCancelToken, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.jobs.db import CONTROL_CANCELLED, CONTROL_PAUSED, CONTROL_RUNNING, JobStore
from contentfactory.orchestrator.monitor import CallableProbe
from contentfactory.orchestrator.runner import Orchestrator
from tests.support import RootCase, params, wait_until
from tests.test_pause_resume import Loop


def spec(*stages: str) -> dict:
    return {"version": 2, "requested_stages": list(stages)}


def sleeper(point: str, seconds: float = 20) -> dict:
    """Chèn một điểm ngủ (hủy/tạm dừng được) chỉ ở lần thử đầu: lần chạy lại sau Tạm dừng không ngủ nữa."""
    return {point: {"sleep_s": seconds, "attempt": 1}}


class ControlCase(RootCase):
    def calls(self, jid, stage: str) -> int:
        f = self.job_dir(jid) / stage / "calls.log"
        return len(f.read_text(encoding="utf-8").splitlines()) if f.is_file() else 0

    def wait_released(self, orc, jid, stage: str, timeout: float = 15.0):
        """Job đã nhả stage (không còn lease) và về hàng đợi của `stage`."""
        wait_until(lambda: orc.store.get_job(jid)["state"] == P.BY_NAME[stage].queue_state and orc.store.get_job(jid)["lease_owner"] is None,
                   timeout, f"{jid} released to {stage}")


class JobTokenTest(RootCase):
    def test_pause_signal_stops_only_at_boundaries_abort_stops_inflight(self):
        sig = {"v": None}
        tok = JobCancelToken(CancelToken(), lambda: sig["v"], interval=0.0)
        tok.check()
        sig["v"] = "pause"
        self.assertFalse(tok.is_set())                      # tiến trình con đang chạy KHÔNG bị giết
        with self.assertRaises(StageError) as cm:
            tok.check()                                     # nhưng ranh giới đơn vị thì dừng
        self.assertEqual(cm.exception.error_class, ErrorClass.CANCELLED)
        with self.assertRaises(StageError):
            tok.wait(5)
        sig["v"] = "abort"
        self.assertTrue(tok.is_set())
        sig["v"] = None
        tok.check()                                         # tiếp tục trước khi tới ranh giới: không dừng
        parent = CancelToken()
        t2 = JobCancelToken(parent, None)
        parent.set()
        self.assertTrue(t2.is_set())
        self.assertEqual(t2.reason, "shutdown")


class PauseTest(ControlCase):
    def test_pause_before_stage_blocks_claim_then_resume_completes(self):
        orc = self.orc()
        jid = orc.submit(params())
        self.assertEqual(orc.pause_job(jid), "changed")
        self.assertEqual(orc.pause_job(jid), "unchanged")                  # idempotent
        orc.run()                                                          # job tạm dừng không giữ tiến trình lại và không được nhận
        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["control_state"], j["pause_origin"]), (P.NEW, CONTROL_PAUSED, "USER"))
        self.assertEqual(orc.store.stage_runs(jid), [])
        self.assertEqual(orc.resume(jid), "unpaused")
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)

    def test_pause_during_tts_loop_resumes_from_checkpoint_without_redoing_chunks(self):
        orc = self.orc()
        jid = orc.submit(params(fake=sleeper("tts_chunk_4")), mode="THROUGH_TTS")
        with Loop(orc):
            wait_until(lambda: self.calls(jid, "tts") >= 3, 20, "3 chunks done")
            self.assertEqual(orc.pause_job(jid), "changed")
            self.wait_released(orc, jid, "tts")
            before = (self.job_dir(jid) / "tts" / "calls.log").read_text(encoding="utf-8").splitlines()
            time.sleep(0.4)
            j = orc.store.get_job(jid)
            self.assertEqual((j["control_state"], j["state"]), (CONTROL_PAUSED, P.STORY_READY))     # không nhận lại stage khi đang tạm dừng
            self.assertEqual(self.runs(orc, jid)["tts"], ["cancelled"])
            self.assertEqual(j["retry_used"], 0)                                                   # Tạm dừng không tiêu ngân sách retry
            self.assertEqual(orc.resume(jid), "unpaused")
            wait_until(lambda: P.is_complete(orc.store.get_job(jid)["state"], orc.store.get_job(jid)["target_idx"]), 30, "job done")
        calls = (self.job_dir(jid) / "tts" / "calls.log").read_text(encoding="utf-8").splitlines()
        for n in (1, 2, 3):
            self.assertEqual(sum(1 for c in calls if c.startswith(f"tts_chunk_{n} ")), 1, f"chunk {n} bị tổng hợp lại")
        self.assertGreater(len(calls), len(before))

    def test_pause_during_tiktok_parts_keeps_finished_parts(self):
        orc = self.orc()
        jid = orc.submit(params(fake=sleeper("render_tiktok_part_2")), pipeline=spec("render_tiktok"))
        with Loop(orc):
            part1 = self.job_dir(jid) / "render" / "tiktok" / "part_01.mp4"
            wait_until(part1.is_file, 30, "part 1 rendered")
            stamp = part1.stat().st_mtime_ns
            orc.pause_job(jid)
            self.wait_released(orc, jid, "render_tiktok")
            self.assertEqual(orc.store.get_job(jid)["control_state"], CONTROL_PAUSED)
            orc.resume(jid)
            wait_until(lambda: P.is_complete(orc.store.get_job(jid)["state"], orc.store.get_job(jid)["target_idx"]), 30, "job done")
        self.assertEqual(part1.stat().st_mtime_ns, stamp)                       # part đã xong không dựng lại

    def test_auto_resume_cannot_resume_manual_pause_and_manual_resume_keeps_resource_hold(self):
        flag = self.root / "down.flag"
        flag.write_text("x")
        orc = self.orc()
        orc.monitor.probes["network"] = CallableProbe("network", lambda: (not flag.exists(), "flag"))
        down = {"story": {"error_class": "RESOURCE", "resource": "network", "code": "NET_DOWN", "fail_while_file": str(flag)}}
        jid = orc.submit(params(fake=down), auto_resume=True)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["hold_reason"], P.PAUSED_NETWORK)
        orc.pause_job(jid)
        with Loop(orc):
            flag.unlink()                                                      # tài nguyên đã hồi phục
            time.sleep(1.0)
            j = orc.store.get_job(jid)
            self.assertEqual((j["control_state"], j["state"]), (CONTROL_PAUSED, P.SOURCE_READY))   # Auto Resume KHÔNG override Tạm dừng
            self.assertEqual(self.runs(orc, jid)["story"], ["held"])
            self.assertEqual(orc.resume(jid), "unpaused")
            wait_until(lambda: orc.store.get_job(jid)["state"] == P.PUBLISHED, 30, "auto resume sau khi bỏ tạm dừng")

    def test_manual_resume_does_not_clear_a_resource_hold(self):
        flag = self.root / "down.flag"
        flag.write_text("x")
        orc = self.orc()
        orc.monitor.probes["network"] = CallableProbe("network", lambda: (not flag.exists(), "flag"))
        jid = orc.submit(params(fake={"story": {"error_class": "RESOURCE", "resource": "network", "fail_while_file": str(flag)}}), auto_resume=False)
        orc.run()
        orc.pause_job(jid)
        self.assertEqual(orc.resume(jid), "unpaused")
        j = orc.store.get_job(jid)
        self.assertEqual((j["control_state"], j["hold_reason"]), (CONTROL_RUNNING, P.PAUSED_NETWORK))   # gỡ pause, hold còn nguyên
        self.assertEqual(orc.resume(jid), "still_down")                        # hold chỉ được gỡ khi tài nguyên thật sự sẵn sàng

    def test_pause_is_noop_for_finished_job_and_user_pause_beats_batch_pause(self):
        orc = self.orc()
        done = orc.submit(params(), mode="SUBTITLE_ONLY")
        orc.run()
        self.assertEqual(orc.pause_job(done), "complete")
        self.assertEqual(orc.store.get_job(done)["control_state"], CONTROL_RUNNING)
        jid = orc.submit(params())
        orc.pause_job(jid, "BATCH")
        self.assertEqual(orc.store.set_control(jid, CONTROL_RUNNING, "BATCH"), "changed")
        orc.pause_job(jid, "BATCH")
        orc.pause_job(jid, "USER")
        self.assertEqual(orc.store.get_job(jid)["pause_origin"], "USER")
        self.assertEqual(orc.store.set_control(jid, CONTROL_RUNNING, "BATCH"), "not_owner")       # batch resume không tự chạy job người dùng đã dừng
        self.assertEqual(orc.store.get_job(jid)["control_state"], CONTROL_PAUSED)


class CancelTest(ControlCase):
    def test_cancel_is_separate_final_and_never_auto_resumes(self):
        flag = self.root / "down.flag"
        flag.write_text("x")
        orc = self.orc()
        orc.monitor.probes["network"] = CallableProbe("network", lambda: (not flag.exists(), "flag"))
        jid = orc.submit(params(fake={"story": {"error_class": "RESOURCE", "resource": "network", "fail_while_file": str(flag)}}), auto_resume=True)
        orc.run()
        self.assertEqual(orc.cancel_job(jid), "changed")
        self.assertEqual(orc.cancel_job(jid), "unchanged")
        with Loop(orc):
            flag.unlink()
            time.sleep(0.8)
        j = orc.store.get_job(jid)
        self.assertEqual((j["control_state"], j["state"]), (CONTROL_CANCELLED, P.SOURCE_READY))
        self.assertEqual(orc.resume(jid), "cancelled")
        self.assertEqual(orc.pause_job(jid), "cancelled")
        self.assertEqual(orc.store.nonterminal_count(), 0)

    def test_cancel_running_job_aborts_inflight_and_keeps_artifacts(self):
        orc = self.orc()
        jid = orc.submit(params(fake=sleeper("tts_chunk_4")), mode="THROUGH_TTS")
        with Loop(orc):
            wait_until(lambda: self.calls(jid, "tts") >= 3, 20, "3 chunks done")
            orc.cancel_job(jid)
            self.wait_released(orc, jid, "tts")
        self.assertEqual(orc.store.get_job(jid)["control_state"], CONTROL_CANCELLED)
        self.assertTrue(any(a["kind"] == "story_text" for a in orc.store.artifacts(jid)))      # artifact/lịch sử giữ nguyên
        self.assertTrue((self.job_dir(jid) / "tts" / "chunks" / "000001.wav").is_file())

    def test_cancel_completed_job_changes_nothing(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="STORY_ONLY")
        orc.run()
        self.assertEqual(orc.cancel_job(jid), "complete")
        self.assertEqual(orc.store.get_job(jid)["control_state"], CONTROL_RUNNING)


class RevisionTest(ControlCase):
    def paused_at_tiktok(self, orc, pipeline=("render_tiktok",), **extra) -> str:
        """Job chạy qua source..audio rồi tạm dừng đúng lúc render TikTok (render_youtube đã đi qua máy trạng thái nếu không được yêu cầu)."""
        jid = orc.submit(params(fake=sleeper("render_tiktok", 20), **extra), pipeline=spec(*pipeline))
        with Loop(orc):
            wait_until(lambda: orc.store.get_job(jid)["state"] == P.TIKTOK_RENDERING, 30, "render_tiktok started")
            orc.pause_job(jid)
            self.wait_released(orc, jid, "render_tiktok")
        return jid

    def test_update_pipeline_removes_unneeded_branch_while_running(self):
        orc = self.orc()
        jid = orc.submit(params(fake=sleeper("tts_chunk_3")), pipeline=spec("render_youtube", "render_tiktok", "output"))
        with Loop(orc):
            wait_until(lambda: orc.store.get_job(jid)["state"] == P.TTS_RUNNING, 20, "tts running")
            r = orc.request_update(jid, pipeline={"requested_stages": ["render_tiktok", "output"]})
            self.assertEqual(r["status"], "pending")                          # đang chạy TTS: chưa áp dụng giữa đơn vị
            act = {s["id"]: s["action"] for s in r["impact"]["stages"]}
            self.assertEqual((act["source"], act["story"], act["tts"], act["audio"]), ("KEEP", "KEEP", "CURRENT_CONTINUE", "RUN"))
            self.assertEqual((act["render_youtube"], act["render_tiktok"], act["output"], act["publish"]), ("REMOVE_FROM_PLAN", "RUN", "RUN", "OFF"))
            orc.pause_job(jid)
            self.wait_released(orc, jid, "tts")                               # ranh giới checkpoint: stage nhả lại => revision được áp dụng
            wait_until(lambda: orc.store.get_job(jid)["pipeline_revision"] == r["revision"], 15, "revision applied")
            orc.resume(jid)
            wait_until(lambda: P.is_complete(orc.store.get_job(jid)["state"], orc.store.get_job(jid)["target_idx"]), 40, "job done")
        j = orc.store.get_job(jid)
        self.assertEqual(j["pipeline"]["run"], ["source", "story", "tts", "audio", "render_tiktok", "output"])
        self.assertEqual(j["pipeline_revision"], 2)
        kinds = {a["kind"] for a in orc.store.artifacts(jid)}
        self.assertTrue({"video_tiktok", "output_package"} <= kinds)
        self.assertFalse({"video_youtube", "thumbnail"} & kinds)
        self.assertEqual(self.runs(orc, jid)["render_youtube"], ["skipped"])
        self.assertEqual([r["status"] for r in orc.store.revisions(jid)], ["applied"])

    def test_adding_stage_rewinds_and_runs_only_required_descendants(self):
        orc = self.orc()
        jid = self.paused_at_tiktok(orc)
        before = {k: list(v) for k, v in self.runs(orc, jid).items()}
        r = orc.request_update(jid, pipeline={"requested_stages": ["render_youtube", "render_tiktok"]})
        self.assertEqual(r["status"], "applied")                              # tạm dừng = điểm an toàn
        act = {s["id"]: s["action"] for s in r["impact"]["stages"]}
        self.assertEqual((act["tts"], act["audio"], act["render_youtube"], act["render_tiktok"]), ("KEEP", "KEEP", "RUN", "RUN"))
        self.assertEqual(r["impact"]["rewind_to"], "render_youtube")
        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["control_state"]), (P.YOUTUBE_RENDER_READY, CONTROL_PAUSED))
        orc.resume(jid)
        orc.run()
        after = self.runs(orc, jid)
        for s in ("source", "story", "tts", "audio"):
            self.assertEqual(after[s], before[s], f"{s} bị chạy lại")
        self.assertEqual(after["render_youtube"][-1], "succeeded")
        self.assertTrue({"video_youtube", "thumbnail", "video_tiktok"} <= {a["kind"] for a in orc.store.artifacts(jid)})
        self.assertTrue(P.is_complete(orc.store.get_job(jid)["state"], orc.store.get_job(jid)["target_idx"]))

    def test_config_and_param_changes_invalidate_exactly_the_declared_stages(self):
        orc = self.orc()
        jid = self.paused_at_tiktok(orc)
        # tiktok.* là phụ thuộc của audio (cắt part) nhưng KHÔNG của tts: chỉ audio (và phần sau) chạy lại
        imp = orc.preview_update(jid, params_patch={"tiktok": {"speed": 3.0}})
        act = {s["id"]: s["action"] for s in imp["stages"]}
        self.assertEqual((act["source"], act["story"], act["tts"]), ("KEEP", "KEEP", "KEEP"))
        self.assertEqual((act["audio"], act["render_tiktok"]), ("RERUN", "RUN"))
        self.assertEqual(imp["rewind_to"], "audio")
        # đổi tiêu đề project chỉ ảnh hưởng render_youtube/output/publish: job TikTok-only không phải chạy lại gì
        imp = orc.preview_update(jid, params_patch={"project": {"title": "Tên mới"}})
        self.assertIsNone(imp["rewind_to"])
        self.assertTrue(all(s["action"] in ("KEEP", "RUN", "OFF") for s in imp["stages"]), imp["stages"])
        # cấu hình ngoài SEMANTIC_KEYS bị từ chối rõ ràng
        bad = orc.preview_update(jid, config_patch={"poll_s": 1})
        self.assertFalse(bad["ok"])
        # áp dụng thật: snapshot có revision mới và job lùi về audio
        r = orc.request_update(jid, params_patch={"tiktok": {"speed": 3.0}})
        self.assertEqual(r["status"], "applied")
        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["params"]["tiktok"]["speed"]), (P.AUDIO_READY, 3.0))
        orc.resume(jid)
        orc.run()
        self.assertEqual(self.runs(orc, jid)["tts"], ["succeeded"])           # TTS không chạy lại
        self.assertEqual(self.runs(orc, jid)["audio"][-1], "succeeded")
        self.assertEqual(len(self.runs(orc, jid)["audio"]), 2)

    def test_pending_revision_survives_restart_and_applies_once(self):
        orc = self.orc()
        jid = self.paused_at_tiktok(orc)
        change = {"pipeline": {"requested_stages": ["render_youtube", "render_tiktok"]}, "config_patch": None, "params_patch": None}
        imp = orc.preview_update(jid, pipeline=change["pipeline"])
        rev, created = orc.store.create_revision(jid, change, imp, "after_current_safe_point")     # như thể tiến trình chết ngay sau khi ghi
        self.assertTrue(created)
        orc2 = self.orc()                                                     # khởi động lại: đọc bản pending từ DB
        self.assertEqual(orc2.store.pending_revision(jid)["revision"], rev["revision"])
        self.assertEqual(orc2.apply_pending(jid), "applied")
        self.assertEqual(orc2.apply_pending(jid), "none")                     # áp hai lần chỉ có một lần hiệu lực
        self.assertEqual([r["status"] for r in orc2.store.revisions(jid)], ["applied"])
        self.assertEqual(orc2.store.get_job(jid)["state"], P.YOUTUBE_RENDER_READY)

    def test_duplicate_request_is_idempotent_and_newer_supersedes(self):
        orc = self.orc()
        jid = orc.submit(params(fake=sleeper("tts_chunk_3")), pipeline=spec("render_youtube", "render_tiktok"))
        with Loop(orc):
            wait_until(lambda: orc.store.get_job(jid)["state"] == P.TTS_RUNNING, 20, "tts running")
            a = orc.request_update(jid, pipeline={"requested_stages": ["render_tiktok"]})
            b = orc.request_update(jid, pipeline={"requested_stages": ["render_tiktok"]})
            self.assertEqual((a["created"], b["created"], a["revision"] == b["revision"]), (True, False, True))
            c = orc.request_update(jid, pipeline={"requested_stages": ["render_youtube"]})
            self.assertTrue(c["created"])
            self.assertEqual([r["status"] for r in orc.store.revisions(jid)], ["superseded", "pending"])
            orc.cancel_job(jid)
            self.wait_released(orc, jid, "tts")
        self.assertEqual([r["status"] for r in orc.store.revisions(jid)], ["superseded", "cancelled"])

    def test_pause_and_apply_policy_interrupts_at_boundary_then_continues(self):
        orc = self.orc()
        jid = orc.submit(params(fake=sleeper("tts_chunk_4")), pipeline=spec("render_youtube", "render_tiktok"))
        with Loop(orc):
            wait_until(lambda: self.calls(jid, "tts") >= 3, 20, "3 chunks")
            r = orc.request_update(jid, pipeline={"requested_stages": ["render_tiktok"]}, apply_policy="pause_and_apply")
            wait_until(lambda: orc.store.get_job(jid)["pipeline_revision"] == r["revision"], 15, "revision applied")
            self.assertEqual(orc.store.get_job(jid)["control_state"], CONTROL_RUNNING)      # tự chạy tiếp, không cần bấm Tiếp tục
            wait_until(lambda: P.is_complete(orc.store.get_job(jid)["state"], orc.store.get_job(jid)["target_idx"]), 40, "job done")
        self.assertNotIn("video_youtube", {a["kind"] for a in orc.store.artifacts(jid)})

    def test_completed_job_is_not_mutated_use_clone(self):
        orc = self.orc()
        jid = orc.submit(params(), pipeline=spec("render_youtube"))
        orc.run()
        old = {a["path"]: a["sha256"] for a in orc.store.artifacts(jid)}
        with self.assertRaises(StageError) as cm:
            orc.request_update(jid, pipeline={"requested_stages": ["render_youtube", "render_tiktok"]})
        self.assertTrue(cm.exception.detail["clone_suggested"])
        self.assertEqual(orc.store.revisions(jid), [])
        new = orc.clone_job(jid, pipeline={"requested_stages": ["render_youtube", "render_tiktok"]})
        orc.run()
        runs = self.runs(orc, new)
        self.assertNotIn("source", runs)                                       # dùng lại artifact hợp lệ của job cũ
        self.assertEqual(runs["render_tiktok"], ["succeeded"])
        self.assertEqual({a["path"]: a["sha256"] for a in orc.store.artifacts(jid)}, old)       # job cũ nguyên vẹn
        self.assertEqual(orc.store.get_job(new)["pipeline"]["requested_stages"], ["render_youtube", "render_tiktok"])

    def test_legacy_mode_job_can_be_updated_via_derived_spec(self):
        orc = self.orc()
        jid = orc.submit(params(), mode="THROUGH_TTS")
        orc.pause_job(jid)
        r = orc.request_update(jid, pipeline={"requested_stages": ["audio"]})
        self.assertEqual(r["status"], "applied")
        j = orc.store.get_job(jid)
        self.assertEqual((j["target_stage"], j["pipeline"]["run"]), ("audio", ["source", "story", "tts", "audio"]))
        orc.resume(jid)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.YOUTUBE_RENDER_READY)

    def test_v4_migration_adds_control_columns_and_revisions_table(self):
        st = JobStore(self.root / "old.db")
        j = st.create_job({"x": 1})
        row = st.get_job(j)
        self.assertEqual((row["control_state"], row["pipeline_revision"], row["pause_origin"]), (CONTROL_RUNNING, 1, None))
        self.assertEqual(st.revisions(j), [])
        self.assertEqual(JobStore(self.root / "old.db").schema_version(), st.schema_version())        # mở lại: idempotent
