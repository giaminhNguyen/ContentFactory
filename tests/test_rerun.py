"""Selective Manual Rerun (D-113): stage chọn bằng checkbox, stale theo stage_key, phiên/lịch sử, rerun_count, idempotency, publish upload mới."""
import json
import threading
import unittest

from contentfactory.adapters.fake import FakePublish, FakeStory
from contentfactory.contracts import ErrorClass, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.jobs.db import JobStore
from contentfactory.orchestrator.service import Service
from tests.support import RootCase, params

ALL = [s.name for s in P.STAGES]


def srun(orc, jid, stage, manual=None):
    rows = [r for r in orc.store.stage_runs(jid) if r["stage"] == stage]
    if manual is None:
        return rows
    return [r for r in rows if bool(r["rerun_session_id"]) == manual]


class RecPublish(FakePublish):
    def __init__(self):
        self.seen = []

    def publish(self, req, ctx):
        self.seen.append(req)
        return super().publish(req, ctx)


class FlakyStory(FakeStory):
    """Hỏng TRANSIENT `n` lần đầu sau khi được 'nạp đạn' — để thử retry TRONG cùng phiên chạy lại."""

    def __init__(self, inner):
        self.inner, self.fails, self.calls = inner, 0, 0

    def generate(self, bundle, profile, out_dir, ctx):
        self.calls += 1
        if self.fails > 0:
            self.fails -= 1
            raise StageError(ErrorClass.TRANSIENT, "FLAKY", "mô phỏng lỗi mạng")
        return self.inner.generate(bundle, profile, out_dir, ctx)

    def health(self):
        return self.inner.health()


class SlowStory(FakeStory):
    """Ngủ (hủy/tạm dừng được) ở các lần gọi có attempt nằm trong `slow` — để dừng phiên chạy lại giữa chừng mà không đổi params của job."""

    def __init__(self, inner, slow=()):
        self.inner, self.slow = inner, set(slow)

    def generate(self, bundle, profile, out_dir, ctx):
        if ctx.attempt in self.slow:
            ctx.cancel.wait(30)
        return self.inner.generate(bundle, profile, out_dir, ctx)

    def health(self):
        return self.inner.health()


class RerunBase(RootCase):
    def setUp(self):
        super().setUp()
        self.orc_ = self.orc()
        self.pub = RecPublish()
        self.orc_.adapters["publish"] = self.pub
        self.jid = self.orc_.submit(params())
        self.orc_.run()
        self.assertEqual(self.orc_.store.get_job(self.jid)["state"], P.PUBLISHED)
        self.svc = Service(self.orc_)

    def rr(self, stages, request_id=None, run=True):
        r = self.svc.rerun_start(self.jid, {"stages": stages, "request_id": request_id})
        if run:
            self.orc_.run()
        return r

    def session(self, sid):
        return self.orc_.store.reruns.get(sid)

    def options(self):
        return {s["id"]: s for s in self.svc.rerun_options(self.jid)["stages"]}

    def cur(self, kind):
        return [a for a in self.orc_.store.artifacts(self.jid) if a["kind"] == kind]


class OptionsTest(RerunBase):
    def test_completed_job_offers_every_stage_from_backend_descriptors(self):
        o = self.svc.rerun_options(self.jid)
        self.assertIsNone(o["blocked"])
        self.assertEqual([s["id"] for s in o["stages"]], ALL)                                 # thứ tự + nhãn lấy từ registry của backend
        for s in o["stages"]:
            self.assertEqual((s["eligible"], s["ready_alone"], s["stale"], s["rerun_count"], s["needs"]), (True, True, False, 0, []), s["id"])
            self.assertTrue(s["label"])
        self.assertEqual(self.svc.job_detail(self.jid)["actions"]["rerun"], True)
        self.assertEqual(self.svc.job_detail(self.jid)["rerun"]["counts"], {})

    def test_validation_errors(self):
        for bad, code in (([], "EMPTY_SELECTION"), (None, "EMPTY_SELECTION"), (["nope"], "INVALID_STAGE"), ("story", "EMPTY_SELECTION")):
            with self.assertRaises(StageError) as e:
                self.svc.rerun_start(self.jid, {"stages": bad})
            self.assertEqual(e.exception.code, code)
        with self.assertRaises(StageError) as e:
            self.svc.rerun_start("999999", {"stages": ["story"]})
        self.assertEqual(e.exception.code, "JOB_NOT_FOUND")
        self.assertEqual(self.orc_.store.reruns.sessions(self.jid), [])

    def test_cancelled_and_paused_jobs_are_blocked(self):
        with self.orc_.store._tx() as c:
            c.execute("UPDATE jobs SET control_state='PAUSED' WHERE id=?", (self.jid,))
        o = self.svc.rerun_options(self.jid)
        self.assertEqual(o["blocked"]["code"], "JOB_PAUSED")
        self.assertFalse(any(s["eligible"] for s in o["stages"]))
        with self.assertRaises(StageError) as e:
            self.rr(["publish"], run=False)
        self.assertEqual(e.exception.code, "JOB_PAUSED")
        with self.orc_.store._tx() as c:
            c.execute("UPDATE jobs SET control_state='CANCELLED' WHERE id=?", (self.jid,))
        self.assertEqual(self.svc.rerun_options(self.jid)["blocked"]["code"], "JOB_CANCELLED")


class StoryChainTest(RerunBase):
    def set_guidance(self, text):
        self.orc_.cfg.data["story"]["guidance"] = text

    def test_story_rerun_really_regenerates_updates_current_and_marks_descendants_stale(self):
        first = self.cur("story_text")[0]
        self.set_guidance("Kết thúc mở")                                                      # job đang inherit => rerun lấy Cài đặt MỚI NHẤT
        r = self.rr(["story"], "req-1")
        s = self.session(r["id"])
        self.assertEqual((s["state"], s["number"], s["requested"]), ("succeeded", 1, ["story"]))
        self.assertEqual(self.orc_.store.get_job(self.jid)["state"], P.PUBLISHED)             # máy trạng thái của job không bị lùi
        runs = srun(self.orc_, self.jid, "story")
        self.assertEqual([bool(x["rerun_session_id"]) for x in runs], [False, True])
        g1, g2 = (json.loads(x["meta"])["guidance"] for x in runs)
        self.assertEqual((g1["source"], g1["text"]), ("none", ""))                            # lịch sử run đầu KHÔNG bị viết lại
        self.assertEqual((g2["source"], g2["text"]), ("settings", "Kết thúc mở"))
        new = self.cur("story_text")[0]
        self.assertNotEqual((new["run_id"], new["path"], new["sha256"]), (first["run_id"], first["path"], first["sha256"]))
        self.assertIn("/r0001/", new["path"])                                                 # thư mục riêng: không phát lại sidecar/cache cũ
        self.assertTrue((self.job_dir(self.jid) / first["path"]).is_file())                   # bản cũ còn trên đĩa (lịch sử)
        self.assertEqual((self.job_dir(self.jid) / "story" / "r0001" / "guidance_seen.txt").read_text(encoding="utf-8"), "Kết thúc mở")
        # không tự chạy hạ nguồn; hạ nguồn đánh dấu stale; thượng nguồn/ nhánh khác không
        for st in ALL[2:]:
            self.assertEqual(len(srun(self.orc_, self.jid, st)), 1, st)
        o = self.options()
        self.assertEqual([n for n in ALL if o[n]["stale"]], ALL[2:])
        self.assertEqual((o["source"]["stale"], o["story"]["stale"], o["story"]["rerun_count"], o["tts"]["rerun_count"]), (False, False, 1, 0))
        self.assertEqual(self.svc.job_detail(self.jid)["rerun"]["stale"]["tts"], True)

    def test_same_content_does_not_invalidate_unrelated_downstream(self):
        self.rr(["story"])                                                                    # không đề xuất => truyện y hệt (sha giống) => không có gì stale
        self.assertEqual([n for n, s in self.options().items() if s["stale"]], [])
        self.assertEqual(self.options()["story"]["rerun_count"], 1)
        self.assertEqual(len(srun(self.orc_, self.jid, "story", manual=True)), 1)

    def test_downstream_alone_is_refused_with_hint_when_prerequisite_is_stale(self):
        self.set_guidance("Hướng khác")
        self.rr(["story"])
        o = self.options()["render_youtube"]
        self.assertEqual((o["eligible"], o["ready_alone"], o["reason"], o["needs"]), (True, False, "INPUT_STALE", ["tts", "audio"]))
        self.assertIn("Hãy chọn thêm", o["hint"])
        p = self.svc.rerun_plan(self.jid, {"stages": ["render_youtube"]})
        self.assertFalse(p["ok"])
        self.assertEqual(p["suggested"], ["tts", "audio"])
        with self.assertRaises(StageError) as e:
            self.rr(["render_youtube"], run=False)
        self.assertEqual(e.exception.code, "RERUN_NOT_ELIGIBLE")
        self.assertIn("không còn đồng bộ", e.exception.message)
        self.assertEqual(e.exception.detail["suggested"], ["tts", "audio"])
        self.assertEqual(self.orc_.store.reruns.active(self.jid), None)                        # từ chối: không tạo phiên

    def test_skipping_a_middle_stage_is_refused_not_silently_added(self):
        self.set_guidance("Hướng khác")
        p = self.svc.rerun_plan(self.jid, {"stages": ["story", "audio"]})                      # TTS nằm giữa: Audio sẽ dùng giọng đọc của truyện cũ
        self.assertFalse(p["ok"])
        self.assertEqual(p["suggested"], ["tts"])
        with self.assertRaises(StageError):
            self.rr(["story", "audio"], run=False)

    def test_full_chain_uses_new_artifacts_in_the_same_session_and_only_selected_run(self):
        self.set_guidance("Nhịp nhanh dần")
        old_audio = self.cur("audio_master")[0]
        r = self.rr(["render_youtube", "story", "audio", "tts", "story"], "chain")           # lộn xộn + trùng => chuẩn hóa theo thứ tự pipeline
        self.assertEqual(r["requested"], ["story", "tts", "audio", "render_youtube"])
        s = self.session(r["id"])
        self.assertEqual(s["state"], "succeeded")
        self.assertEqual({k: v["state"] for k, v in s["stages"].items()}, {k: "succeeded" for k in ["story", "tts", "audio", "render_youtube"]})
        story, tts_run = self.cur("story_text")[0], srun(self.orc_, self.jid, "tts", manual=True)[0]
        # TTS đọc đúng truyện MỚI (khóa ngữ nghĩa dựa trên sha của story mới), không phải input chốt từ đầu phiên
        from contentfactory.orchestrator.stages import StageContract
        job = self.orc_.store.get_job(self.jid)
        inputs = self.orc_.store.inputs(self.jid, P.BY_NAME["tts"].requires)
        self.assertEqual(inputs["story_text"][0]["sha256"], story["sha256"])
        self.assertEqual(StageContract(P.BY_NAME["tts"]).stage_key(job["params"], job["config_snapshot"], inputs), tts_run["stage_key"])
        self.assertNotEqual(self.cur("audio_master")[0]["sha256"], old_audio["sha256"])
        self.assertIn("/r0001/", self.cur("video_youtube")[0]["path"])
        o = self.options()
        self.assertEqual([n for n in ALL if o[n]["stale"]], ["render_tiktok", "output", "publish"])    # không được chọn => vẫn stale, KHÔNG tự chạy
        self.assertEqual((o["render_youtube"]["rerun_count"], o["render_tiktok"]["rerun_count"], o["publish"]["rerun_count"]), (1, 0, 0))
        for st in ("render_tiktok", "output", "publish"):
            self.assertEqual(len(srun(self.orc_, self.jid, st)), 1, st)
        self.assertEqual(len(self.pub.seen), 1)                                                # không tự đăng khi Publish không được chọn
        # Output gói cả nhánh TikTok (stale) nên chưa chạy riêng được; Publish chỉ cần video mới + tiêu đề/mô tả (suy ra từ metadata, không đổi) nên chạy riêng được
        o = self.options()
        self.assertEqual((o["output"]["ready_alone"], o["output"]["needs"]), (False, ["render_tiktok"]))
        self.assertTrue(o["publish"]["ready_alone"])

    def test_job_custom_guidance_wins_over_changed_settings_on_rerun(self):
        self.orc_.store.update_params(self.jid, lambda p: {**p, "story_guidance": {"mode": "custom", "text": "Riêng của job"}}, "t")
        self.set_guidance("Cài đặt B")
        r = self.rr(["story"])
        g = json.loads(srun(self.orc_, self.jid, "story", manual=True)[0]["meta"])["guidance"]
        self.assertEqual((g["source"], g["text"]), ("job", "Riêng của job"))
        self.assertEqual(self.session(r["id"])["state"], "succeeded")
        # lần rerun kế tiếp sau khi đổi lại về inherit lấy Cài đặt hiện tại; lịch sử cũ vẫn giữ nguyên snapshot cũ
        self.orc_.store.update_params(self.jid, lambda p: {k: v for k, v in p.items() if k != "story_guidance"}, "t")
        self.rr(["story"])
        gs = [json.loads(x["meta"])["guidance"] for x in srun(self.orc_, self.jid, "story")]
        self.assertEqual([(x["source"], x["text"]) for x in gs], [("none", ""), ("job", "Riêng của job"), ("settings", "Cài đặt B")])


class CountAndRetryTest(RerunBase):
    def test_retry_inside_a_session_does_not_increase_rerun_count(self):
        flaky = FlakyStory(self.orc_.adapters["story"])
        self.orc_.adapters["story"] = flaky
        flaky.fails = 1
        r = self.rr(["story"])
        self.assertEqual(self.session(r["id"])["state"], "succeeded")
        self.assertEqual(flaky.calls, 2)                                                       # 1 lần hỏng + 1 retry
        manual = srun(self.orc_, self.jid, "story", manual=True)
        self.assertEqual([x["status"] for x in manual], ["failed", "succeeded"])
        self.assertEqual(self.options()["story"]["rerun_count"], 1)                            # retry cùng execution: vẫn 1
        self.rr(["story"])
        self.assertEqual(self.options()["story"]["rerun_count"], 2)                            # manual rerun mới: 2
        self.assertEqual(len(srun(self.orc_, self.jid, "story", manual=False)), 1)             # lần chạy ban đầu không tính

    def test_failed_stage_keeps_current_artifact_and_marks_rest_skipped(self):
        flaky = FlakyStory(self.orc_.adapters["story"])
        self.orc_.adapters["story"] = flaky
        flaky.fails = 99
        before = self.cur("story_text")[0]
        r = self.rr(["story", "tts"])
        s = self.session(r["id"])
        self.assertEqual(s["state"], "failed")
        self.assertEqual((s["stages"]["story"]["state"], s["stages"]["tts"]["state"]), ("failed", "skipped"))
        self.assertEqual(self.cur("story_text")[0]["run_id"], before["run_id"])                # artifact hiện hành còn nguyên
        self.assertEqual(self.svc.reruns(self.jid)["sessions"][0]["result"], "failed")
        self.assertEqual(self.orc_.store.get_job(self.jid)["state"], P.PUBLISHED)
        flaky.fails = 0
        r2 = self.rr(["story", "tts"])                                                         # chạy lại được sau khi phiên kết thúc
        self.assertEqual(self.session(r2["id"])["state"], "succeeded")
        self.assertEqual(self.options()["story"]["rerun_count"], 2)                            # phiên lỗi vẫn là một lần chạy lại đã bắt đầu


class SessionDedupeTest(RerunBase):
    def test_double_click_same_request_id_creates_one_session(self):
        a = self.rr(["publish"], "dbl", run=False)
        b = self.rr(["publish"], "dbl", run=False)
        self.assertEqual((a["id"], b["id"], a["deduped"], b["deduped"]), (a["id"], a["id"], False, True))
        self.assertEqual(len(self.orc_.store.reruns.sessions(self.jid)), 1)
        with self.assertRaises(StageError) as e:                                                # request khác khi phiên còn hoạt động => bận
            self.rr(["story"], "other", run=False)
        self.assertEqual(e.exception.code, "RERUN_BUSY")
        self.orc_.run()
        self.assertEqual(len(self.pub.seen), 2)                                                 # chỉ MỘT upload mới (1 ban đầu + 1)
        c = self.rr(["publish"], "dbl", run=False)                                              # gửi lại sau khi xong: vẫn trả phiên cũ, không đăng lần nữa
        self.assertEqual((c["id"], c["deduped"]), (a["id"], True))

    def test_concurrent_requests_one_wins(self):
        results, errors = [], []

        def go(i):
            try:
                results.append(self.svc.rerun_start(self.jid, {"stages": ["output"], "request_id": f"r{i}"})["id"])
            except StageError as e:
                errors.append(e.code)
        ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual((len(results), errors), (1, ["RERUN_BUSY"] * 3))

    def test_normal_claim_skips_job_with_active_session(self):
        jid2 = self.orc_.submit(params(), mode="STORY_ONLY")                                    # dở dang: đang xếp hàng ở source
        sess, _ = self.orc_.store.reruns.create(jid2, ["source"])
        self.assertEqual(self.orc_.store.claim(P.BY_NAME["source"], 5, 5, "o", 30), [])         # runner thường không nhận job đang có phiên chạy lại
        self.orc_.store.reruns.finish(sess["id"], "cancelled")
        self.assertEqual([c.job_id for c in self.orc_.store.claim(P.BY_NAME["source"], 5, 5, "o", 30)], [jid2])


class PublishRerunTest(RerunBase):
    def test_manual_publish_makes_a_new_upload_and_keeps_old_ids_in_history(self):
        old = srun(self.orc_, self.jid, "publish")[0]
        old_id = json.loads(old["data"])["remote_id"]
        r = self.rr(["publish"], "pub-1")
        self.assertEqual(self.session(r["id"])["state"], "succeeded")
        new = srun(self.orc_, self.jid, "publish", manual=True)[0]
        new_id = json.loads(new["data"])["remote_id"]
        self.assertNotEqual(new_id, old_id)                                                    # định danh thực thi mới => upload mới
        self.assertEqual(json.loads(srun(self.orc_, self.jid, "publish", manual=False)[0]["data"])["remote_id"], old_id)   # upload cũ còn trong lịch sử
        self.assertEqual(json.loads(self.cur("publish_result")[0]["meta"])["remote_id"], new_id)             # kết quả hiện hành trỏ upload mới
        self.assertEqual(self.orc_.store.get_job(self.jid)["state"], P.PUBLISHED)
        h = self.svc.reruns(self.jid)
        self.assertEqual([x["remote"]["id"] for x in h["initial"] if x["id"] == "publish"], [old_id])
        self.assertEqual(h["sessions"][0]["stages"][0]["remote"]["id"], new_id)
        keys = {q["idempotency_key"] for q in self.pub.seen}
        self.assertEqual(len(keys), 2)
        self.assertEqual(h["counts"]["publish"], 1)
        r2 = self.rr(["publish"], "pub-2")
        self.assertEqual(len({q["idempotency_key"] for q in self.pub.seen}), 3)                 # lần thủ công kế tiếp có khóa thực thi mới nữa
        self.assertEqual(self.session(r2["id"])["number"], 2)

    def test_retry_in_same_manual_publish_reuses_the_execution_key(self):
        calls = []
        orig = self.pub.publish

        def flaky(req, ctx):
            calls.append(req["idempotency_key"])
            if len(calls) == 1:
                raise StageError(ErrorClass.TRANSIENT, "NET", "rớt mạng")
            return orig(req, ctx)
        self.pub.publish = flaky
        r = self.rr(["publish"])
        self.assertEqual(self.session(r["id"])["state"], "succeeded")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])                                                    # retry cùng execution: cùng idempotency key => uploader không đăng đôi
        self.assertEqual(self.options()["publish"]["rerun_count"], 1)

    def test_render_and_publish_in_one_session_publish_the_new_video(self):
        r = self.rr(["render_youtube", "publish"])
        self.assertEqual(self.session(r["id"])["state"], "succeeded")
        vid = self.cur("video_youtube")[0]
        self.assertIn("/r0001/", vid["path"])
        self.assertEqual(self.pub.seen[-1]["video"], self.job_dir(self.jid) / vid["path"])        # upload dùng đúng video vừa dựng trong cùng phiên
        self.assertTrue((self.job_dir(self.jid) / "render" / "youtube" / "r0001" / "calls.log").read_text(encoding="utf-8").startswith("render_youtube"))
        self.assertEqual(len(srun(self.orc_, self.jid, "render_tiktok")), 1)                    # TikTok không bị đụng
        # output đóng gói: render mới cùng nội dung (fake) nên không stale; publish đã chạy lại theo video mới
        self.assertEqual([n for n, s in self.options().items() if s["stale"]], [])

    def test_sequence_is_not_reallocated_by_manual_rerun(self):
        seq = self.orc_.sequence.get(self.jid)
        self.rr(["output", "publish"])
        self.assertEqual(self.orc_.sequence.get(self.jid), seq)


class HistoryTest(RerunBase):
    def test_history_lists_initial_run_and_each_session_durably(self):
        self.orc_.cfg.data["story"]["guidance"] = "G1"
        self.rr(["publish"], "a")
        self.rr(["story"], "b")
        reopened = Service(type(self.orc_)(self.orc_.cfg, adapters=self.orc_.adapters))           # tiến trình khác mở lại DB: lịch sử còn nguyên
        h = reopened.reruns(self.jid)
        self.assertEqual([s["number"] for s in h["sessions"]], [2, 1])
        self.assertEqual([s["requested"] for s in h["sessions"]], [["story"], ["publish"]])
        self.assertEqual({s["result"] for s in h["sessions"]}, {"succeeded"})
        story = next(x for x in h["sessions"][0]["stages"] if x["id"] == "story")
        self.assertEqual((story["guidance"]["source"], story["guidance"]["text"]), ("settings", "G1"))
        self.assertEqual([x["id"] for x in h["initial"]], ALL)
        self.assertEqual(h["counts"], {"story": 1, "publish": 1})
        self.assertTrue(all(s["created_at"] and s["started_at"] and s["ended_at"] for s in h["sessions"]))

    def test_crash_recovery_requeues_running_session_with_same_identity(self):
        r = self.rr(["publish"], "crash", run=False)
        self.orc_.store.reruns.claim(1, "dead-owner", 0.0)                                      # nhận rồi 'chết' (lease hết hạn ngay)
        sid = r["id"]
        self.assertEqual(self.session(sid)["state"], "running")
        self.assertEqual(self.orc_.store.reruns.recover_expired(), [sid])
        self.assertEqual(self.session(sid)["state"], "queued")
        self.orc_.run()
        self.assertEqual(self.session(sid)["state"], "succeeded")
        self.assertEqual(len(self.pub.seen), 2)


class OtherStagesTest(RerunBase):
    def test_tts_rerun_resynthesizes_instead_of_replaying_the_shared_cache(self):
        before = self.cur("audio_master")[0]
        r = self.rr(["tts"])
        self.assertEqual(self.session(r["id"])["state"], "succeeded")
        calls = (self.job_dir(self.jid) / "tts" / "r0001" / "calls.log").read_text(encoding="utf-8")
        self.assertIn("tts_chunk_", calls)                                                      # engine thật sự được gọi (không phải cache hit)
        now = self.cur("audio_master")[0]
        self.assertEqual((now["run_id"] != before["run_id"], "/r0001/" in now["path"]), (True, True))
        self.assertTrue((self.job_dir(self.jid) / before["path"]).is_file())                    # master cũ không bị ghi đè
        self.assertEqual(self.options()["tts"]["rerun_count"], 1)

    def test_output_rerun_packages_current_artifacts_and_keeps_sequence(self):
        old = json.loads(self.cur("output_package")[0]["meta"])
        r = self.rr(["output"])
        self.assertEqual(self.session(r["id"])["state"], "succeeded")
        self.assertEqual(len(srun(self.orc_, self.jid, "output", manual=True)), 1)
        self.assertIn("/r0001/", self.cur("output_package")[0]["path"])
        self.assertTrue(old)

    def test_source_rerun_marks_dependents_stale_only_if_consumed_content_changed(self):
        before = {k: self.cur(k)[0]["sha256"] for k in ("transcript", "metadata")}
        r = self.rr(["source"])
        self.assertEqual(self.session(r["id"])["state"], "succeeded")
        after = {k: self.cur(k)[0]["sha256"] for k in ("transcript", "metadata")}
        o = self.options()
        self.assertEqual(o["story"]["stale"], before != after)                                    # story tiêu thụ transcript + metadata: chỉ stale khi nội dung đổi thật
        if before == after:
            self.assertEqual([n for n, s in o.items() if s["stale"]], [])
        self.assertEqual((o["source"]["rerun_count"], o["source"]["stale"]), (1, False))

    def test_job_running_a_stage_blocks_rerun(self):
        jid2 = self.orc_.submit(params(), mode="STORY_ONLY")
        [claim] = self.orc_.store.claim(P.BY_NAME["source"], 1, 5, "w", 30)                      # worker thường đang chạy source
        self.assertEqual(claim.job_id, jid2)
        o = self.svc.rerun_options(jid2)
        self.assertEqual(o["blocked"]["code"], "JOB_BUSY")
        with self.assertRaises(StageError) as e:
            self.svc.rerun_start(jid2, {"stages": ["source"]})
        self.assertEqual(e.exception.code, "JOB_BUSY")


class ControlTest(RerunBase):
    def test_deleting_the_job_aborts_the_session_without_touching_current_artifacts(self):
        self.orc_.adapters["story"] = SlowStory(self.orc_.adapters["story"], slow={2})
        before = self.cur("story_text")[0]
        r = self.rr(["story"], run=False)
        th = threading.Thread(target=self.orc_.run, daemon=True)
        th.start()
        end = __import__("time").time() + 10
        while __import__("time").time() < end and self.session(r["id"])["stages"]["story"]["state"] != "running":
            __import__("time").sleep(0.02)
        self.orc_.delete_job(self.jid)
        th.join(15)
        self.assertFalse(th.is_alive())
        self.assertEqual(self.session(r["id"])["state"], "cancelled")
        self.assertEqual(self.session(r["id"])["stages"]["story"]["state"], "cancelled")
        self.assertEqual(self.cur("story_text")[0]["run_id"], before["run_id"])

    def test_pause_requeues_the_session_and_resume_finishes_it(self):
        self.orc_.adapters["story"] = SlowStory(self.orc_.adapters["story"], slow={2})
        r = self.rr(["story"], run=False)
        th = threading.Thread(target=self.orc_.run, daemon=True)
        th.start()
        import time
        end = time.time() + 10
        while time.time() < end and self.session(r["id"])["stages"]["story"]["state"] != "running":
            time.sleep(0.02)
        with self.orc_.store._tx() as c:
            c.execute("UPDATE jobs SET control_state='PAUSED', pause_origin='USER' WHERE id=?", (self.jid,))
        self.orc_._tokens[self.jid].request_pause()
        th.join(15)
        self.assertFalse(th.is_alive())
        self.assertEqual(self.session(r["id"])["state"], "queued")                                # giữ phiên, không mất
        self.assertEqual(self.orc_.store.reruns.active_count(), 0)                                # job tạm dừng: không giữ vòng lặp
        with self.orc_.store._tx() as c:
            c.execute("UPDATE jobs SET control_state='RUNNING', pause_origin=NULL WHERE id=?", (self.jid,))
        self.orc_.run()
        self.assertEqual(self.session(r["id"])["state"], "succeeded")                             # lần nhận lại: attempt >= 3 nên không còn ngủ
        self.assertEqual(self.options()["story"]["rerun_count"], 1)                               # tạm dừng/tiếp tục không tăng đếm


class CliTest(RerunBase):
    def cli(self, *argv):
        import contextlib
        import io
        from contentfactory.orchestrator.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--root", str(self.root), *argv])
        return code, buf.getvalue()

    def test_cli_lists_plans_and_runs(self):
        code, out = self.cli("rerun", self.jid)
        self.assertEqual(code, 0)
        self.assertEqual([x["id"] for x in json.loads(out)["stages"]], ALL)
        code, out = self.cli("rerun", self.jid, "publish", "--plan")
        self.assertEqual((code, json.loads(out)["ok"]), (0, True))
        code, out = self.cli("rerun", self.jid, "publish")
        self.assertEqual(code, 0, out)
        self.assertIn("kết quả: succeeded", out)
        self.assertEqual(self.svc.rerun_options(self.jid)["stages"][-1]["rerun_count"], 1)
        code, out = self.cli("rerun", self.jid, "nope")
        self.assertEqual(code, 2)
        self.assertIn("LỖI", out)


class SchemaTest(RootCase):
    def test_old_db_upgrades_and_old_jobs_have_no_rerun_data(self):
        import sqlite3
        db = self.root / "runtime" / "old.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(db)
        c.executescript("CREATE TABLE jobs(id TEXT PRIMARY KEY, seq INTEGER UNIQUE NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, state TEXT NOT NULL, "
                        "params TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, not_before REAL, lease_owner TEXT, lease_until REAL, retry_used INTEGER NOT NULL DEFAULT 0, "
                        "failed_stage TEXT, last_error TEXT);"
                        "CREATE TABLE stage_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, stage TEXT NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL, "
                        "owner TEXT, stage_key TEXT, started_at REAL NOT NULL, ended_at REAL, error TEXT, data TEXT);"
                        "CREATE TABLE artifacts(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, stage TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL, path TEXT NOT NULL, "
                        "sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, meta TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(job_id, path));"
                        "CREATE TABLE transitions(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, job_id TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, stage TEXT, attempt INTEGER, note TEXT);"
                        "INSERT INTO jobs(id,seq,created_at,updated_at,state,params) VALUES('000001',1,1,1,'PUBLISHED','{}');"
                        "INSERT INTO stage_runs(job_id,stage,attempt,status,started_at) VALUES('000001','story',1,'succeeded',1);"
                        "PRAGMA user_version=3;")
        c.close()
        st = JobStore(db)
        self.assertEqual(st.reruns.counts("000001"), {})
        self.assertEqual(st.stage_runs("000001")[0]["rerun_session_id"], None)
        self.assertIsNone(st.stage_runs("000001")[0]["meta"])
        self.assertEqual(JobStore(db).schema_version(), st.schema_version())                   # idempotent


if __name__ == "__main__":
    unittest.main()
