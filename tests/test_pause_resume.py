"""Phase 2.9: PAUSED_* vs FAILED_PERMANENT, Auto Resume ON/OFF, Resource Monitor, checkpoint, snapshot, migration, policy."""
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from contentfactory.contracts import ErrorClass, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.jobs.db import JobStore, SCHEMA, SCHEMA_VERSION
from contentfactory.jobs.policy import RetryPolicy, outcome_for
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.monitor import CallableProbe, DiskProbe
from contentfactory.orchestrator.runner import Orchestrator
from tests.support import RootCase, make_root, params, start_runner, wait_until


class Loop:
    """Chạy orchestrator ở thread nền (until_idle=False) để quan sát hold/auto resume."""

    def __init__(self, orc: Orchestrator) -> None:
        self.orc, self.stop = orc, threading.Event()
        self.t = threading.Thread(target=orc.run, kwargs={"until_idle": False, "stop": self.stop}, daemon=True)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join(30)


class PauseResumeTest(RootCase):
    def setUp(self) -> None:
        super().setUp()
        self.flag = self.root / "down.flag"
        self.flag.write_text("x")                                      # tồn tại = tài nguyên đang hỏng

    def net_orc(self) -> Orchestrator:
        orc = self.orc()
        orc.monitor.probes["network"] = CallableProbe("network", lambda: (not self.flag.exists(), "flag"))
        return orc

    def down(self, **extra) -> dict:
        return {"story": {"error_class": "RESOURCE", "resource": "network", "code": "NET_DOWN",
                          "fail_while_file": str(self.flag), **extra}}

    # -- lỗi tài nguyên tạm thời => PAUSED, không phải FAILED -------------------------------------
    def test_temporary_resource_failure_pauses_not_fails(self):
        orc = self.net_orc()
        jid = orc.submit(params(fake=self.down()), auto_resume=False)
        orc.run()                                                       # job bị giữ không giữ tiến trình lại
        j = orc.store.get_job(jid)
        self.assertEqual(j["hold_reason"], P.PAUSED_NETWORK)
        self.assertNotEqual(j["state"], P.FAILED)
        self.assertIsNone(j["failed_stage"])
        self.assertEqual(j["state"], P.SOURCE_READY)                    # vẫn ở đúng vị trí pipeline (hàng đợi story)
        self.assertEqual(j["retry_used"], 0)                            # không đốt ngân sách retry
        self.assertEqual(self.runs(orc, jid)["story"], ["held"])
        m = json.loads((self.job_dir(jid) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["hold"]["reason"], P.PAUSED_NETWORK)

    def test_permanent_error_still_fails(self):
        orc = self.orc()
        jid = orc.submit(params(fake={"story": {"error_class": "POLICY", "fail_until_attempt": 99, "code": "BAD"}}))
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["failed_stage"]), (P.FAILED, "story"))
        self.assertIsNone(j["hold_reason"])

    def test_transient_with_resource_holds_after_retries_exhausted(self):
        orc = self.net_orc()
        jid = orc.submit(params(fake={"story": {"error_class": "TRANSIENT", "resource": "network",
                                                "fail_while_file": str(self.flag)}}), auto_resume=False)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual(j["hold_reason"], P.PAUSED_NETWORK)
        self.assertEqual(self.runs(orc, jid)["story"], ["failed", "failed", "held"])    # 3 lần thử rồi mới giữ

    def test_auth_pauses_credential(self):
        orc = self.orc()
        jid = orc.submit(params(fake={"story": {"error_class": "AUTH", "fail_until_attempt": 99}}), auto_resume=False)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["hold_reason"], P.PAUSED_CREDENTIAL)

    def test_publish_hold_can_be_resumed_the_internal_sequence_is_not_an_adapter(self):
        orc = self.orc()
        self.assertEqual(orc._adapters_health(P.BY_NAME["publish"]), (True, "adapter sẵn sàng"))

    # -- Auto Resume ON ---------------------------------------------------------------------------
    def test_auto_resume_on_resumes_from_checkpoint_when_resource_returns(self):
        orc = self.net_orc()
        self.assertTrue(orc.cfg.data["auto_resume_default"])           # mặc định global BẬT
        jid = orc.submit(params(fake=self.down()))                      # không chỉ định => theo mặc định
        self.assertTrue(orc.store.get_job(jid)["auto_resume"])
        with Loop(orc):
            wait_until(lambda: orc.store.get_job(jid)["hold_reason"] == P.PAUSED_NETWORK, what="hold")
            time.sleep(0.4)                                             # tài nguyên vẫn hỏng: vẫn giữ, không lặp vô ích
            self.assertEqual(orc.store.get_job(jid)["hold_reason"], P.PAUSED_NETWORK)
            self.flag.unlink()                                          # tài nguyên trở lại
            wait_until(lambda: orc.store.get_job(jid)["state"] == P.PUBLISHED, what="auto resume to finish")
        runs = self.runs(orc, jid)
        self.assertEqual(runs["source"], ["succeeded"])                 # source không chạy lại
        self.assertEqual(runs["story"], ["held", "succeeded"])
        self.assertIn("auto", " ".join(t["note"] for t in orc.store.transitions(jid)))

    # -- Auto Resume OFF ----------------------------------------------------------------------------
    def test_auto_resume_off_monitors_but_waits_for_user(self):
        orc = self.net_orc()
        jid = orc.submit(params(fake=self.down()), auto_resume=False)
        with Loop(orc):
            wait_until(lambda: orc.store.get_job(jid)["hold_reason"] == P.PAUSED_NETWORK, what="hold")
            self.flag.unlink()
            wait_until(lambda: (orc.store.get_resource_status("network") or {}).get("ok") is True, what="monitor sees recovery")
            time.sleep(0.4)
            j = orc.store.get_job(jid)
            self.assertEqual(j["hold_reason"], P.PAUSED_NETWORK)        # monitor thấy rồi nhưng KHÔNG tự resume
            self.assertEqual(j["state"], P.SOURCE_READY)
            self.assertEqual(orc.resume(jid), "resumed")                # người dùng Resume
            wait_until(lambda: orc.store.get_job(jid)["state"] == P.PUBLISHED, what="finish after manual resume")

    def test_manual_resume_refused_while_resource_still_down(self):
        orc = self.net_orc()
        jid = orc.submit(params(fake=self.down()), auto_resume=False)
        orc.run()
        self.assertEqual(orc.resume(jid, now=True), "still_down")
        self.assertEqual(orc.store.get_job(jid)["hold_reason"], P.PAUSED_NETWORK)
        self.flag.unlink()
        self.assertEqual(orc.resume(jid, now=True), "resumed")
        self.assertEqual(orc.resume(jid), "not_held")

    # -- không auto-resume vô hạn khi không tiến triển ------------------------------------------------
    def test_no_infinite_auto_resume_without_progress(self):
        root = make_root({"retry": {"max_attempts": 3, "backoff_s": [0.05], "max_interruptions": 5, "jitter": 0,
                                    "floor_s": 0, "max_auto_resumes_without_progress": 2}})
        self.addCleanup(__import__("shutil").rmtree, root, True)
        orc = Orchestrator(load_config(root))
        orc.monitor.probes["network"] = CallableProbe("network", lambda: (True, "up"))   # probe luôn "ổn" nhưng stage vẫn hỏng
        jid = orc.submit(params(fake=self.down()))
        with Loop(orc):
            wait_until(lambda: orc.store.get_job(jid)["needs_user"], what="needs_user")
            n = len(self.runs(orc, jid)["story"])
            time.sleep(0.5)
            self.assertEqual(len(self.runs(orc, jid)["story"]), n)      # đã dừng thật
        j = orc.store.get_job(jid)
        self.assertNotEqual(j["state"], P.FAILED)                       # không chuyển FAILED
        self.assertEqual(j["hold_reason"], P.PAUSED_NETWORK)
        self.flag.unlink()
        self.assertEqual(orc.resume(jid), "resumed")                    # người dùng vẫn resume được
        self.assertFalse(orc.store.get_job(jid)["needs_user"])
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)

    # -- disk / missing input / token -----------------------------------------------------------------
    def test_low_disk_pauses_then_auto_resumes(self):
        orc = self.orc()
        free = {"gb": 0.0}
        orc.monitor.disk = DiskProbe(self.root, 0, usage=lambda p: SimpleNamespace(free=int(free["gb"] * 2 ** 30)))
        orc.monitor.probes["disk"] = orc.monitor.disk
        jid = orc.submit(params())
        with Loop(orc):
            wait_until(lambda: orc.store.get_job(jid)["hold_reason"] == P.PAUSED_DISK, what="paused disk")
            self.assertEqual(self.runs(orc, jid).get("source"), ["held"])
            free["gb"] = 100.0
            wait_until(lambda: orc.store.get_job(jid)["state"] == P.PUBLISHED, what="resumed and done")

    def test_missing_input_artifact_pauses(self):
        orc = self.orc()
        sf = self.root / "s.txt"
        from contentfactory.adapters.fake import _paragraph
        sf.write_text("\n\n".join(_paragraph(k) for k in range(1, 5)), encoding="utf-8")
        jid = orc.submit(params(), mode="TTS_ONLY", inputs={"story_text": str(sf)}, auto_resume=False)
        imp = next(a for a in orc.store.artifacts(jid) if a["stage"] == "import")
        (self.job_dir(jid) / imp["path"]).unlink()                       # file input biến mất
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual(j["hold_reason"], P.PAUSED_MISSING_INPUT)
        self.assertNotEqual(j["state"], P.FAILED)

    def test_token_hold_is_time_based(self):
        orc = self.orc()
        jid = orc.submit(params(fake={"story": {"error_class": "RESOURCE", "resource": "token", "code": "LIMIT",
                                                "fail_until_attempt": 1, "resume_after_s": 0.5}}), auto_resume=False)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual(j["hold_reason"], P.PAUSED_TOKEN)
        self.assertEqual(orc.resume(jid), "still_down")                  # chưa tới thời điểm reset
        time.sleep(0.6)
        self.assertEqual(orc.resume(jid), "resumed")
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)

    # -- checkpoint, start/target, snapshot qua restart ---------------------------------------------
    def test_restart_keeps_checkpoint_start_target_and_snapshot(self):
        jid = self.orc().submit(params(fake={"tts_chunk_3": {"sleep_s": 60, "attempt": 1}}), mode="THROUGH_TTS")
        proc = start_runner(self.root)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        store = self.orc().store
        wait_until(lambda: store.get_job(jid)["state"] == P.TTS_RUNNING and
                   (store.get_job(jid)["checkpoint"].get("tts") or {}).get("done", 0) >= 1, what="tts checkpoint")
        proc.kill()
        proc.wait()
        orc = self.orc()                                                 # "tiến trình mới"
        j = orc.store.get_job(jid)
        self.assertEqual((j["start_stage"], j["target_stage"]), ("source", "tts"))
        self.assertGreaterEqual(j["checkpoint"]["tts"]["done"], 1)
        self.assertEqual(j["checkpoint"]["tts"]["total"], 6)
        self.assertTrue(j["config_snapshot"])
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual(j["state"], P.AUDIO_READY)                      # tới đúng target rồi dừng
        self.assertEqual(self.runs(orc, jid)["source"], ["succeeded"])   # stage xong không chạy lại
        self.assertEqual(self.runs(orc, jid)["story"], ["succeeded"])
        self.assertEqual(self.runs(orc, jid)["tts"], ["interrupted", "succeeded"])

    def test_global_config_change_does_not_alter_existing_job(self):
        orc = self.orc()
        jid = orc.submit(params())
        snap = orc.store.get_job(jid)["config_snapshot"]
        p = self.root / "config" / "config.json"
        cfg = json.loads(p.read_text(encoding="utf-8"))
        cfg.update({"auto_resume_default": False, "retry": {"max_attempts": 9}, "source": {"languages": ["xx"]}})
        p.write_text(json.dumps(cfg), encoding="utf-8")
        orc2 = self.orc()
        self.assertFalse(orc2.cfg.data["auto_resume_default"])
        j = orc2.store.get_job(jid)
        self.assertEqual(j["config_snapshot"], snap)                     # job giữ nguyên snapshot
        self.assertTrue(j["auto_resume"])
        new = orc2.submit(params())
        self.assertFalse(orc2.store.get_job(new)["auto_resume"])         # job MỚI theo cấu hình mới
        self.assertNotEqual(orc2.store.get_job(new)["config_hash"], j["config_hash"])
        self.assertEqual(orc2._policy(j["config_snapshot"]).max_attempts, orc.cfg["retry"]["max_attempts"])

    def test_snapshot_excludes_machine_config_and_secrets(self):
        orc = self.orc()
        orc.cfg.data["youtube"] = {**orc.cfg.data.get("youtube", {}), "api_key": "SECRET123", "cookies_env": "YT_COOKIES"}
        jid = orc.submit(params())
        raw = json.dumps(orc.store.get_job(jid)["config_snapshot"])
        self.assertNotIn("SECRET123", raw)
        self.assertIn("YT_COOKIES", raw)                                 # tên biến môi trường (không phải secret) giữ lại
        for machine in ("lease_s", "heartbeat_s", "paths", "limits"):
            self.assertNotIn(f'"{machine}"', raw)

    def test_set_job_config_is_explicit_and_versioned(self):
        orc = self.orc()
        jid = orc.submit(params())
        rev = orc.set_job_config(jid, {"retry": {"max_attempts": 7}})
        j = orc.store.get_job(jid)
        self.assertEqual((rev, j["config_revision"]), (1, 1))
        self.assertEqual(orc._policy(j["config_snapshot"]).max_attempts, 7)
        with self.assertRaises(ValueError):
            orc.set_job_config(jid, {"paths": {"db": "x"}})              # config máy không đổi được qua job


class PolicyTableTest(RootCase):
    P = RetryPolicy(max_attempts=3, backoff_s=(2, 10), jitter=0.0, floor_s=0.0, cap_s=300, retry_after_hold_threshold_s=600)

    def out(self, err, used=0):
        return outcome_for(err, used, self.P, 1000.0, lambda: 0.5)

    def test_backoff_exponent_is_capped_for_very_long_outages(self):
        from contentfactory.orchestrator.monitor import CallableProbe
        o = self.orc()
        o.monitor.probes["network"] = CallableProbe("network", lambda: (False, "down"))
        o.monitor.base_s, o.monitor.max_s = 30.0, 300.0
        o.store.put_resource_status("network", False, "down", 0.0, 0.0, None, 5000)          # ~vài ngày mất mạng liên tục
        st = o.monitor.check("network", force=True)                                         # trước đây: OverflowError làm sập cả vòng lặp
        self.assertFalse(st["ok"])
        self.assertAlmostEqual(st["next_check_at"] - st["checked_at"], 300.0, places=3)

    def test_a_failing_background_task_does_not_kill_the_scheduler(self):
        o = self.orc()
        jid = o.submit(params(), auto_resume=False)

        def boom():
            raise RuntimeError("monitor hỏng")
        o._monitor_tick = boom
        o.run()
        self.assertEqual(o.store.get_job(jid)["state"], P.PUBLISHED)                        # job vẫn chạy xong
        self.assertTrue(any("background_task_error" in x for x in (self.root / "runtime" / "logs" / "orchestrator.jsonl").read_text(encoding="utf-8").splitlines()))

    def test_table(self):
        T = ErrorClass.TRANSIENT
        o = self.out(StageError(T, "X"))
        self.assertEqual((o.action, o.delay), ("retry", 2))
        self.assertEqual(self.out(StageError(T, "X"), used=1).delay, 10)
        self.assertEqual(self.out(StageError(T, "X", retry_after_s=30)).delay, 30)      # không bao giờ nhỏ hơn Retry-After
        o = self.out(StageError(T, "X", retry_after_s=7200, resource="quota"))
        self.assertEqual((o.action, o.reason, o.resume_after), ("hold", P.PAUSED_QUOTA, 8200.0))
        self.assertEqual(self.out(StageError(T, "X"), used=2).action, "failed")            # hết retry, không rõ tài nguyên
        self.assertEqual(self.out(StageError(T, "X", resource="network"), used=2).reason, P.PAUSED_NETWORK)
        self.assertEqual(self.out(StageError(ErrorClass.RESOURCE, "X", resource="disk")).reason, P.PAUSED_DISK)
        self.assertEqual(self.out(StageError(ErrorClass.RESOURCE, "X", resource="token")).reason, P.PAUSED_TOKEN)
        self.assertEqual(self.out(StageError(ErrorClass.RESOURCE, "X")).reason, P.PAUSED_RESOURCE)
        self.assertEqual(self.out(StageError(ErrorClass.AUTH, "X")).reason, P.PAUSED_CREDENTIAL)
        self.assertEqual(self.out(StageError(ErrorClass.POLICY, "X", resource="input")).reason, P.PAUSED_MISSING_INPUT)
        self.assertEqual(self.out(StageError(ErrorClass.POLICY, "X")).action, "failed")
        self.assertEqual(self.out(StageError(ErrorClass.AMBIGUOUS, "X")).action, "failed")

    def test_jitter_bounded(self):
        pol = RetryPolicy(backoff_s=(10,), jitter=0.2, floor_s=0.0)
        self.assertEqual(pol.delay(0, None, lambda: 0.0), 8.0)
        self.assertEqual(pol.delay(0, None, lambda: 1.0), 12.0)


class MigrationTest(RootCase):
    def test_v0_database_is_migrated_in_place_with_backup(self):
        db = self.root / "legacy.db"
        c = sqlite3.connect(db)
        c.executescript(SCHEMA)
        c.execute("INSERT INTO jobs(id,seq,created_at,updated_at,state,params) VALUES('000001',1,1,1,?,'{}')", (P.STORY_READY,))
        c.commit()
        c.close()
        self.assertEqual(sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0], 0)
        st = JobStore(db)
        self.assertEqual(st.schema_version(), SCHEMA_VERSION)
        j = st.get_job("000001")
        self.assertEqual(j["state"], P.STORY_READY)                       # dữ liệu cũ nguyên vẹn
        self.assertIsNone(j["hold_reason"])
        self.assertIsNone(j["target_stage"])                              # job cũ: chạy full như trước
        self.assertTrue(Path(f"{db}.bak-v0").exists())
        self.assertEqual(JobStore(db).schema_version(), SCHEMA_VERSION)                # mở lại: idempotent
        self.assertEqual(st.nonterminal_count(), 1)

    def test_legacy_job_without_snapshot_still_runs(self):
        orc = self.orc()
        c = sqlite3.connect(orc.store.path)                               # mô phỏng job tạo trước migration: không start/target/snapshot
        c.execute("INSERT INTO jobs(id,seq,created_at,updated_at,state,params) VALUES('000099',99,1,1,?,?)",
                  (P.NEW, json.dumps({**params(), "title": "cũ"})))
        c.commit()
        c.close()
        orc.run()
        self.assertEqual(orc.store.get_job("000099")["state"], P.PUBLISHED)
