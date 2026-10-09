"""Phase 9 — backend của giao diện: facade (service/service_admin) và máy chủ HTTP cục bộ (webui)."""
import http.client
import json
import os
import shutil
import threading
import time
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path

from contentfactory.contracts import StageError, clean_title
from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.runner import Orchestrator
from contentfactory.orchestrator.service import Service
from contentfactory.orchestrator.service_admin import AdminService
from contentfactory.orchestrator.webui import App, UiServer
from tests.support import RootCase, params, wait_until
from tests.test_batches import FakeYouTube, discovery as make_discovery, entry as yt_entry
from tests.test_automode import LENIENT_AUDIO, tts_profile, write_channel, write_config
from tests.test_render import FakeCFCase

URL = "https://www.youtube.com/watch?v=abcdefghijk"
KIDS_NO = {"made_for_kids": False}


def write_wav(path: Path, seconds: float = 3.0) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


class UiCase(RootCase):
    def setUp(self):
        super().setUp()
        write_config(self.root, job_defaults={"tiktok": {"speed": 2.0, "target_part_sec": 0.8}}, auto={"hold_wait_s": 2})
        write_channel(self.root, "kenh", {"name": "Kênh Thử", "sequence": {"last_used": 4}, "publishing": {"made_for_kids": False, "privacy": "unlisted"},
                                          "preset": {"audio": LENIENT_AUDIO}})
        write_channel(self.root, "chua_khai", {"name": "Chưa khai", "preset": {"audio": LENIENT_AUDIO}})
        self.o = self.orc()
        self.svc = Service(self.o)
        self.adm = AdminService(self.o)

    def run_job(self, **kw) -> str:
        r = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "run": "full", **kw})
        self.o.run()
        return r["job_id"]

    def story_file(self) -> Path:
        p = self.root / "story.txt"
        p.write_text("\n\n".join(f"Đoạn {i}: " + "Tôi đi trên con đường làng vắng. " * 6 for i in range(1, 6)), encoding="utf-8")
        return p


# ================================================================================== điều khiển job (Phase 2)
class JobControlViewTest(UiCase):
    def test_pause_resume_cancel_states_actions_and_filters(self):
        jid = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "run": "story"})["job_id"]
        d = self.svc.job_detail(jid)
        self.assertEqual((d["status"], d["actions"]["pause"], d["actions"]["cancel"], d["actions"]["unpause"]), ("queued", True, True, False))
        r = self.svc.pause(jid)
        self.assertEqual(r["result"], "changed")
        d = self.svc.job_detail(jid)
        self.assertEqual((d["status"], d["control"]["state"], d["actions"]["unpause"], d["actions"]["pause"]), ("paused", "PAUSED", True, False))
        self.assertEqual(d["diagnosis"]["resume"]["actions"], ["resume"])
        lst = self.svc.list_jobs("waiting")                                       # tạm dừng nằm cùng nhóm "đang chờ"
        self.assertEqual((lst["counts"]["waiting"], [j["id"] for j in lst["jobs"]]), (1, [jid]))
        self.assertEqual(self.svc.resume(jid)["result"], "unpaused")
        self.o.run()
        self.assertEqual(self.svc.job_detail(jid)["status"], "completed")
        self.assertEqual(self.svc.pause(jid)["result"], "complete")
        self.assertEqual(self.svc.cancel(jid)["result"], "complete")
        j2 = self.svc.create_run({"input": {"value": URL + "x"}, "channel": "kenh", "run": "story"})["job_id"]
        self.assertEqual(self.svc.cancel(j2)["result"], "changed")
        d = self.svc.job_detail(j2)
        self.assertEqual((d["status"], d["actions"]["cancel"], d["actions"]["clone"], d["actions"]["edit"]), ("cancelled", False, True, False))
        self.assertEqual(self.svc.resume(j2)["result"], "cancelled")
        with self.assertRaises(StageError):
            self.svc.pause("999999")

    def test_job_pipeline_goes_through_edit_target_not_revisions(self):
        jid = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "pipeline": {"mode": "custom", "requested_stages": ["render_youtube", "render_tiktok"]}, "kids": False})["job_id"]
        self.svc.pause(jid)
        with self.assertRaises(StageError) as cm:                                      # revision của job không còn nhận pipeline
            self.svc.preview_update(jid, {"pipeline": {"requested_stages": ["render_tiktok"]}})
        self.assertEqual(cm.exception.code, "PIPELINE_USE_TARGET")
        with self.assertRaises(StageError):
            self.svc.request_update(jid, {"pipeline": {"requested_stages": ["render_tiktok"]}})
        d = self.svc.job_detail(jid)
        edit = d["edit"]
        by = {s["id"]: s for s in edit["stages"]}
        self.assertEqual((edit["floor"], edit["target"], d["actions"]["edit"], d["actions"]["delete"]), ("source", "render_tiktok", True, True))
        self.assertTrue(by["output"]["selectable"] and "Chạy tiếp" not in by["output"]["effect"])
        self.assertIn("rồi dừng", by["render_youtube"]["effect"])
        r = self.svc.update_target(jid, {"target_stage": "render_youtube"})
        self.assertEqual((r["result"], r["held"]), ("changed", False))
        d = self.svc.job_detail(jid)
        self.assertEqual((d["edit"]["target"], d["mode"]["target"]), ("render_youtube", "render_youtube"))
        with self.assertRaises(StageError):
            self.svc.update_target(jid, {"target_stage": "nope"})
        with self.assertRaises(StageError):
            self.svc.update_target(jid, {})


# ================================================================================== Prosody (nhịp đọc): thông tin, nghe thử A/B, speech plan của job
class ProsodyViewTest(UiCase):
    def setUp(self):
        super().setUp()
        write_config(self.root, prosody={"default_profile": "natural"})              # cấu hình mặc định của máy: job mới dùng nhịp đọc Tự nhiên
        self.o = self.orc()
        self.svc, self.adm = Service(self.o), AdminService(self.o)

    def preview(self, **payload) -> dict:
        tid = self.adm.prosody_preview(payload)["task"]
        wait_until(lambda: self.adm.tasks.get(tid)["state"] != "running", 30, "preview done")
        t = self.adm.tasks.get(tid)
        self.assertEqual(t["state"], "done", t.get("error"))
        return t["result"]

    def test_info_lists_profiles_and_never_claims_llm_available_without_a_labeler(self):
        info = self.adm.prosody_info()
        self.assertEqual([p["id"] for p in info["profiles"]], ["natural", "fast", "dramatic", "custom"])
        self.assertFalse(info["semantic_available"])
        self.assertIn("scene", {k["id"] for k in info["kinds"]})

    def test_preview_ab_gives_two_cached_variants_and_serves_audio_by_id_only(self):
        r = self.preview(variants=[{"profile": "natural"}, {"profile": "dramatic"}])
        a, b = r["variants"]
        self.assertNotEqual(a["id"], b["id"])
        self.assertGreater(b["duration_sec"], a["duration_sec"])                   # dramatic nghỉ lâu hơn => dài hơn
        self.assertGreater(max(b["pauses_ms"]), max(a["pauses_ms"]))
        raw = self.adm.prosody_preview_file(a["id"])
        self.assertEqual((raw.ctype, raw.body[:4]), ("audio/wav", b"RIFF"))
        again = self.preview(variants=[{"profile": "natural"}])
        self.assertEqual(again["variants"][0]["id"], a["id"])                       # cache theo nội dung
        for bad in ("../x", "zz", "0" * 15, a["id"].upper()):
            with self.assertRaises(StageError):
                self.adm.prosody_preview_file(bad)
        with self.assertRaises(StageError):
            self.adm.prosody_preview({"variants": [{"profile": "nope"}]})
        with self.assertRaises(StageError):
            self.adm.prosody_preview({"variants": [{}, {}, {}]})

    def test_job_speech_plan_view_and_manual_override_through_a_revision(self):
        jid = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "run": "through_tts"})["job_id"]
        self.assertFalse(self.svc.speech_plan(jid)["available"])                    # chưa tới TTS
        self.o.run()
        sp = self.svc.speech_plan(jid)
        self.assertTrue(sp["available"] and sp["editable"], sp)
        self.assertEqual(sp["profile"], "natural")
        self.assertTrue(sp["boundaries"] and all(b["external"] for b in sp["boundaries"]))
        allb = self.svc.speech_plan(jid, "all")["boundaries"]
        self.assertGreaterEqual(len(allb), len(sp["boundaries"]))
        b = sp["boundaries"][0]
        clone = self.o.clone_job(jid, rerun_from="tts", params_patch={"prosody": {"overrides": {b["key"]: {"pause_ms": 1777}}}})
        self.o.run()
        sp2 = self.svc.speech_plan(clone)
        row = next(x for x in sp2["boundaries"] if x["key"] == b["key"])
        self.assertEqual((row["pause_ms"], row["manual"]), (1777, True))
        self.assertEqual(sp2["overrides"], {b["key"]: {"pause_ms": 1777}})
        # Reset Auto: {"pause_ms": null} bỏ override
        clone2 = self.o.clone_job(clone, rerun_from="tts", params_patch={"prosody": {"overrides": {b["key"]: {"pause_ms": None}}}})
        self.o.run()
        row2 = next(x for x in self.svc.speech_plan(clone2)["boundaries"] if x["key"] == b["key"])
        self.assertEqual((row2["pause_ms"], row2["manual"]), (b["pause_ms"], False))


# ================================================================================== nhận dạng đầu vào
class DetectTest(UiCase):
    def test_youtube_urls(self):
        for u, vid in ((URL, "abcdefghijk"), ("https://youtu.be/abcdefghijk?t=3", "abcdefghijk"), ("https://www.youtube.com/shorts/abcdefghijk", "abcdefghijk"),
                       ("  https://m.youtube.com/watch?v=abcdefghijk&list=x ", "abcdefghijk")):
            d = self.svc.detect_input(u)
            self.assertEqual((d["ok"], d["kind"], d["details"]["video_id"]), (True, "youtube_url", vid), u)
            self.assertEqual([m["id"] for m in d["modes"]], ["full", "through_tts", "story", "subtitle"])
            self.assertFalse(d["needs_title"])

    def test_bad_urls_and_paths_explain_what_to_do(self):
        self.assertIn("YouTube", self.svc.detect_input("https://example.com/watch?v=abcdefghijk")["problem"])
        self.assertIn("mã video", self.svc.detect_input("https://www.youtube.com/")["problem"])
        self.assertIn("Không tìm thấy", self.svc.detect_input(str(self.root / "khong-co.srt"))["problem"])
        self.assertEqual(self.svc.detect_input("")["kind"], "unknown")
        (self.root / "x.exe").write_bytes(b"x")
        self.assertIn(".exe", self.svc.detect_input(str(self.root / "x.exe"))["problem"])

    def test_files_by_kind_and_valid_modes_only(self):
        srt = self.root / "phu-de.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nXin chào\n", encoding="utf-8")
        d = self.svc.detect_input(str(srt))
        self.assertEqual((d["kind"], [m["id"] for m in d["modes"]]), ("transcript_file", ["full", "through_tts", "story"]))   # đã có phụ đề: không có "chỉ lấy phụ đề"
        d = self.svc.detect_input(str(self.story_file()))
        self.assertEqual((d["kind"], d["needs_title"], d["ambiguous"]), ("story_text", False, True))
        self.assertEqual([m["id"] for m in d["modes"]], ["story_full", "tts_only", "video_after_tts"])
        self.assertEqual(d["alternatives"], ["transcript_file"])
        plain = self.root / "ghi-chu.txt"
        plain.write_text("nội dung", encoding="utf-8")
        d = self.svc.detect_input(str(plain))
        self.assertEqual((d["kind"], d["ambiguous"], d["alternatives"]), ("transcript_file", True, ["story_text"]))
        self.assertEqual(self.svc.detect_input(str(plain), "story_text")["kind"], "story_text")                                # người dùng đổi cách hiểu
        d = self.svc.detect_input(str(write_wav(self.root / "a.wav")))
        self.assertEqual((d["kind"], d["needs_title"], d["auto_title"]), ("audio", False, True))
        self.assertEqual([m["id"] for m in d["modes"]], ["audio_full", "audio_package", "audio_video", "audio_youtube"])

    def test_project_folder(self):
        pkg = self.root / "out" / "p"
        pkg.mkdir(parents=True)
        (pkg / "project.json").write_text(json.dumps({"job_id": "000007", "version": 2, "project": {"title": "T"}}), encoding="utf-8")
        d = self.svc.detect_input(str(pkg))
        self.assertEqual((d["kind"], d["ok"], d["modes"], d["details"]["job_id"]), ("project", False, [], "000007"))
        (self.root / "empty").mkdir()
        self.assertIn("project.json", self.svc.detect_input(str(self.root / "empty"))["problem"])


# ================================================================================== kế hoạch + tạo job
class RunTest(UiCase):
    def test_preview_asks_for_the_kids_declaration_only_when_needed(self):
        pv = self.svc.preview_run({"input": {"value": URL}, "channel": "chua_khai", "run": "full"})
        self.assertTrue(pv["needs_kids"] and not pv["can_run"])
        self.assertEqual(pv["problems"][0]["code"], "MISSING_MADE_FOR_KIDS")
        self.assertTrue(self.svc.preview_run({"input": {"value": URL}, "channel": "chua_khai", "run": "full", "kids": False})["can_run"])
        self.assertTrue(self.svc.preview_run({"input": {"value": URL}, "channel": "chua_khai", "run": "story"})["can_run"])           # không tới publish: không cần khai
        pv = self.svc.preview_run({"input": {"value": URL}, "channel": "kenh", "run": "full"})
        self.assertEqual((pv["can_run"], pv["sequence_next"], pv["privacy"], pv["channel_name"]), (True, 5, "unlisted", "Kênh Thử"))
        self.assertEqual([s["state"] for s in pv["plan"]["stages"]], ["run"] * 8)
        self.assertTrue(pv["warnings"])                                                              # chưa đặt tên truyện: cảnh báo tiêu đề nguồn

    def test_preview_shows_what_is_skipped_and_what_is_auto_selected(self):
        tts_profile("giong_vi", self.root)
        pv = self.svc.preview_run({"input": {"value": str(self.story_file())}, "channel": "kenh", "run": "tts_only", "title": "Tên"})
        self.assertEqual({s["name"]: s["state"] for s in pv["plan"]["stages"]}, {"source": "off", "story": "off", "tts": "run", "audio": "off", "render_youtube": "off",
                                                                                 "render_tiktok": "off", "output": "off", "publish": "off"})
        self.assertIn("tts_profile", {a["what"] for a in pv["auto"]})
        pv = self.svc.preview_run({"input": {"value": str(self.story_file())}, "channel": "kenh", "run": "tts_only"})
        self.assertNotIn("title", [x.get("field") for x in pv["problems"]])                         # để trống tên truyện: tự đặt, không còn là vấn đề
        pv = self.svc.preview_run({"input": {"value": URL}, "channel": "khong_co_kenh_nay_va_loi", "run": "full", "kids": False})
        self.assertTrue(pv["can_run"])                                                              # kênh chưa có file => cấu hình mặc định
        write_channel(self.root, "hong", {"preset": {"nope": 1}})
        pv = self.svc.preview_run({"input": {"value": URL}, "channel": "hong", "run": "full"})
        self.assertEqual(pv["problems"][0]["code"], "INVALID_CHANNEL_CONFIG")

    def test_every_mode_maps_to_the_right_start_and_target(self):
        story, wav = str(self.story_file()), str(write_wav(self.root / "a.wav"))
        cases = [({"input": {"value": URL}, "run": "subtitle"}, (None, "source")), ({"input": {"value": URL}, "run": "story"}, (None, "story")),
                 ({"input": {"value": URL}, "run": "through_tts"}, (None, "tts")),
                 ({"input": {"value": story}, "run": "tts_only", "title": "T"}, ("tts", "tts")),
                 ({"input": {"value": story}, "run": "video_after_tts", "title": "T"}, ("tts", "render_tiktok")),
                 ({"input": {"value": story}, "run": "story_full", "title": "T", "kids": False}, ("tts", "publish")),
                 ({"input": {"value": wav}, "run": "audio_video", "title": "T"}, ("audio", "render_tiktok")),
                 ({"input": {"value": wav}, "run": "audio_youtube", "title": "T"}, ("audio", "render_youtube")),
                 ({"input": {"value": wav}, "run": "audio_package", "title": "T"}, ("audio", "output")),
                 ({"input": {"value": wav}, "run": "audio_full", "title": "T", "kids": False}, ("audio", "publish"))]
        for payload, (start, target) in cases:
            r = self.svc.create_run({"channel": "kenh", **payload})
            j = self.o.store.get_job(r["job_id"])
            self.assertEqual((j["start_stage"], j["target_stage"]), (start or "source", target), payload["run"])
            self.assertEqual(j["params"]["ui"]["sig"] is not None, True)

    def test_full_path_from_each_partial_input_reaches_the_package(self):
        r = self.svc.create_run({"input": {"value": str(write_wav(self.root / "a.wav"))}, "channel": "kenh", "run": "audio_package", "title": "Có sẵn audio"})
        self.o.run()
        d = self.svc.job_detail(r["job_id"])
        self.assertEqual(d["status"], "completed")
        self.assertTrue(Path(d["output"]["project_dir"]).is_dir())
        self.assertEqual([p["state"] for p in d["pipeline"]], ["provided", "not_planned", "provided", "done", "done", "done", "done", "not_planned"])    # metadata + audio do người dùng đưa vào
        r2 = self.svc.create_run({"input": {"value": str(self.story_file())}, "channel": "kenh", "run": "story_full", "title": "Từ truyện", "kids": False})
        self.o.run()
        d2 = self.svc.job_detail(r2["job_id"])
        self.assertEqual(d2["status"], "completed")
        self.assertEqual([p["state"] for p in d2["pipeline"]][:2], ["provided", "provided"])        # tên + truyện do người dùng đưa vào: không bị hiểu nhầm là hệ thống đã chạy
        self.assertEqual([p["state"] for p in d2["pipeline"]][2:], ["done"] * 6)

    def test_request_id_and_same_input_dedupe(self):
        a = self.svc.create_run({"request_id": "r1", "input": {"value": URL}, "channel": "kenh", "run": "full"})
        b = self.svc.create_run({"request_id": "r1", "input": {"value": URL}, "channel": "kenh", "run": "full"})
        self.assertEqual((a["deduped"], b["deduped"], b["reason"], b["job_id"]), (False, True, "request", a["job_id"]))
        c = self.svc.create_run({"request_id": "r2", "input": {"value": URL}, "channel": "kenh", "run": "full"})
        self.assertEqual((c["deduped"], c["reason"], c["job_id"]), (True, "same_input_running", a["job_id"]))
        d = self.svc.create_run({"request_id": "r3", "input": {"value": URL}, "channel": "kenh", "run": "story"})
        self.assertFalse(d["deduped"])                                                          # chế độ khác = việc khác
        self.assertEqual(len(self.o.store.list_jobs()), 2)
        self.svc2 = Service(self.o)
        self.assertTrue(self.svc2.create_run({"request_id": "r1", "input": {"value": URL}, "channel": "kenh", "run": "full"})["deduped"])   # nhớ qua lần khởi động lại
        self.o.run()
        e = self.svc.create_run({"request_id": "r4", "input": {"value": URL}, "channel": "kenh", "run": "full"})
        self.assertFalse(e["deduped"])                                                          # job cũ đã xong: chạy lại có chủ ý thì tạo job mới

    def test_blank_title_is_auto_generated_from_file_or_folder(self):
        named = self.root / "Đêm_mưa_ở_làng.txt"
        named.write_text(self.story_file().read_text(encoding="utf-8"), encoding="utf-8")
        for path, want in ((named, "Đêm mưa ở làng"), (self.story_file(), clean_title(self.root.name))):                     # story.txt quá chung chung ⇒ tên thư mục
            params, _ = self.svc._spec(self.svc.detect_input(str(path), "story_text"), "tts_only", "  ", "kenh", None, None)
            self.assertEqual((params["project"]["title"], params["project"]["title_source"]), (want, "auto"))
        params, _ = self.svc._spec(self.svc.detect_input(str(named), "story_text"), "tts_only", "Tên tôi đặt", "kenh", None, None)
        self.assertEqual(params["project"], {"title": "Tên tôi đặt"})                                           # người dùng đặt thì giữ nguyên
        self.svc.cfg.data["publishing"]["title_policy"] = "require"
        with self.assertRaises(StageError) as e:
            self.svc._spec(self.svc.detect_input(str(named), "story_text"), "tts_only", "", "kenh", None, None)
        self.assertEqual(e.exception.code, "MISSING_TITLE")                                                    # cấu hình "Bắt buộc" vẫn được tôn trọng

    def test_validation_errors_create_no_job(self):
        story = str(self.story_file())
        bad = [({"input": {"value": URL}, "run": "tts_only"}, "INVALID_RUN_MODE"),
               ({"input": {"value": "https://example.com/x"}, "run": "full"}, "INVALID_INPUT"), ({"input": {"value": URL}, "run": "full", "channel": "chua_khai"}, "MISSING_MADE_FOR_KIDS")]
        for payload, code in bad:
            with self.assertRaises(StageError) as e:
                self.svc.create_run({"channel": "kenh", **payload})
            self.assertEqual(e.exception.code, code, payload)
        self.assertEqual(self.o.store.list_jobs(), [])

    def test_remember_kids_declaration_writes_the_channel_once(self):
        self.svc.create_run({"input": {"value": URL}, "channel": "chua_khai", "run": "full", "kids": True, "remember_kids": True})
        ch = json.loads((self.root / "channels" / "chua_khai" / "channel.json").read_text(encoding="utf-8"))
        self.assertIs(ch["publishing"]["made_for_kids"], True)
        self.assertEqual(ch["preset"], {"audio": LENIENT_AUDIO})                                  # phần còn lại của kênh không bị đụng
        self.assertFalse(self.svc.preview_run({"input": {"value": URL}, "channel": "chua_khai", "run": "full"})["needs_kids"])


# ================================================================================== danh sách + chi tiết
class JobsViewTest(UiCase):
    def test_list_counts_filters_pagination_and_version(self):
        done = self.run_job()
        held = self.svc.create_run({"input": {"value": URL + "1"}, "channel": "kenh", "run": "full", "auto_resume": False})["job_id"]
        self.o.store.get_job(held)
        l = self.svc.list_jobs()
        self.assertEqual(l["counts"]["all"], 2)
        self.assertEqual(l["counts"]["completed"], 1)
        row = next(j for j in l["jobs"] if j["id"] == done)
        self.assertEqual((row["status"], row["fraction"], row["title"], row["channel"]), ("completed", 1.0, "Truyện thử nghiệm", "kenh"))
        self.assertTrue(Path(row["output_dir"]).is_dir())
        self.assertEqual(self.svc.list_jobs("completed")["total"], 1)
        self.assertEqual([j["id"] for j in self.svc.list_jobs("running")["jobs"]], [held])             # job mới tạo = đang xếp hàng
        self.assertEqual(self.svc.list_jobs(limit=1)["has_more"], True)
        self.assertEqual(len(self.svc.list_jobs(limit=1, offset=1)["jobs"]), 1)
        v = l["version"]
        self.assertEqual(self.svc.list_jobs(since=v), {"changed": False, "version": v})
        self.o.run()
        self.assertTrue(self.svc.list_jobs(since=v)["changed"])

    def test_progress_updates_bump_the_version_so_polling_sees_them(self):
        j = self.svc.create_run({"input": {"value": URL}, "channel": "kenh", "run": "full"})["job_id"]
        v = self.svc.list_jobs()["version"]
        time.sleep(0.01)
        self.o.store.set_checkpoint(j, "source", {"done": 1, "total": 3, "detail": "x"})
        self.assertNotEqual(self.svc.list_jobs(since=v)["version"], v)

    def test_held_failed_and_attention_rows_carry_the_next_action(self):
        self.o.monitor.probes["network"] = type("Down", (), {"resource": "network", "check": lambda s: (False, "down")})()
        net = {"story": {"error_class": "RESOURCE", "resource": "network", "fail_until_attempt": 99}}
        a = self.o.submit(params(fake=net), auto_resume=False)
        b = self.o.submit(params(fake={"tts_chunk_2": {"error_class": "POLICY", "fail_until_attempt": 99}}), auto_resume=False)
        c = self.o.submit(params(fake={"story": {"error_class": "AUTH", "fail_until_attempt": 99}}), auto_resume=False)
        self.o.run()
        rows = {j["id"]: j for j in self.svc.list_jobs()["jobs"]}
        self.assertEqual((rows[a]["status"], rows[a]["next_action"], rows[a]["hold"]["title"]), ("waiting", "resume", "Đang chờ mạng"))
        self.assertEqual((rows[b]["status"], rows[b]["next_action"]), ("failed", "retry"))
        self.assertEqual((rows[c]["status"], rows[c]["next_action"]), ("attention", "resume"))
        self.assertEqual({j["id"] for j in self.svc.list_jobs("attention")["jobs"]}, {b, c})          # lỗi nằm chung nhóm "cần xử lý"
        self.assertEqual([j["id"] for j in self.svc.list_jobs("waiting")["jobs"]], [a])

    def test_job_detail_pipeline_for_completed_held_and_failed(self):
        self.o.monitor.probes["network"] = type("Down", (), {"resource": "network", "check": lambda s: (False, "down")})()
        h = self.o.submit(params(fake={"story": {"error_class": "RESOURCE", "resource": "network", "fail_until_attempt": 99}}), auto_resume=False)
        f = self.o.submit(params(fake={"tts_chunk_3": {"error_class": "POLICY", "fail_until_attempt": 99}}), auto_resume=False)
        self.o.run()                                                                                  # lỗi trước: job thành công cùng nội dung sẽ lấp cache TTS làm mất lỗi giả lập
        done = self.run_job()
        d = self.svc.job_detail(done)
        self.assertEqual([p["state"] for p in d["pipeline"]], ["done"] * 8)
        self.assertEqual((d["diagnosis"]["status"], d["output"]["tiktok_parts"] > 0, d["output"]["youtube_title"].startswith("[Full Audio][Kênh Thử số 5]")), ("completed", True, True))
        dh = self.svc.job_detail(h)
        self.assertEqual([p["state"] for p in dh["pipeline"]][:3], ["done", "held", "waiting"])
        self.assertEqual(dh["diagnosis"]["resume"]["actions"], ["resume", "enable_auto_resume"])
        df = self.svc.job_detail(f)
        self.assertEqual([p["state"] for p in df["pipeline"]][:4], ["done", "done", "failed", "waiting"])
        self.assertEqual((df["pipeline"][2]["done"], df["pipeline"][2]["total"]), (1, 6))
        self.assertEqual(df["diagnosis"]["resume"]["actions"], ["retry"])
        with self.assertRaises(StageError) as e:
            self.svc.job_detail("999999")
        self.assertEqual(e.exception.code, "JOB_NOT_FOUND")

    def test_actions_resume_retry_and_auto_resume(self):
        flag = self.root / "net.flag"
        flag.write_text("x")
        self.o.monitor.probes["network"] = type("P", (), {"resource": "network", "check": lambda s: (not flag.exists(), "flag")})()
        f = self.o.submit(params(fake={"tts_chunk_3": {"error_class": "POLICY", "fail_until_attempt": 99}}), auto_resume=False)
        j = self.o.submit(params(fake={"story": {"error_class": "RESOURCE", "resource": "network", "code": "NET", "fail_while_file": str(flag)}}), auto_resume=False)
        self.o.run()
        self.assertEqual(self.svc.resume(j)["result"], "still_down")
        self.assertEqual(self.svc.set_auto_resume(j, True)["auto_resume"], True)
        self.assertTrue(self.o.store.get_job(j)["auto_resume"])
        flag.unlink()
        self.assertEqual(self.svc.resume(j, now=True)["result"], "resumed")
        self.o.run()
        self.assertEqual(self.svc.job_detail(j)["status"], "completed")
        self.assertEqual(self.svc.resume(j)["result"], "not_held")
        with self.assertRaises(StageError):
            self.svc.retry(j)                                                                    # chỉ job lỗi mới chạy lại được
        self.assertEqual(self.svc.job_detail(f)["status"], "failed")
        self.assertIn("Giọng đọc", self.svc.retry(f)["message"])
        self.assertNotEqual(self.o.store.get_job(f)["state"], P.FAILED)                           # đã xếp lại đúng stage lỗi

    def test_open_output_opens_only_the_pipelines_own_folder(self):
        j = self.run_job()
        opened = []
        r = self.svc.open_output(j, opened.append)
        self.assertEqual(opened, [r["opened"]])
        self.assertEqual(Path(opened[0]).parent, self.root / "output")
        shutil.rmtree(opened[0])
        with self.assertRaises(StageError) as e:
            self.svc.open_output(j, opened.append)
        self.assertEqual(e.exception.code, "OUTPUT_MOVED")
        self.assertEqual(len(opened), 1)
        k = self.svc.create_run({"input": {"value": URL + "2"}, "channel": "kenh", "run": "subtitle"})["job_id"]
        with self.assertRaises(StageError) as e:
            self.svc.open_output(k, opened.append)
        self.assertEqual(e.exception.code, "NO_OUTPUT")

    def test_log_tail_is_bounded_and_pages_backwards(self):
        j = self.run_job()
        full = [json.loads(x) for x in (self.root / "workspace" / f"job_{j}" / "job.log.jsonl").read_text(encoding="utf-8").splitlines()]
        a = self.svc.job_log(j, tail=10)
        self.assertEqual(len(a["lines"]), 10)
        self.assertEqual(a["lines"][-1]["event"], full[-1]["event"])
        self.assertTrue(a["has_more"])
        seen = a["lines"]
        cursor = a
        while cursor["has_more"]:
            cursor = self.svc.job_log(j, tail=10, before=cursor["start"])
            seen = cursor["lines"] + seen
        self.assertEqual([x["event"] for x in seen], [x["event"] for x in full])                   # phân trang ngược khớp đúng toàn bộ log
        self.assertEqual(self.svc.job_log("000099")["lines"], [])


# ================================================================================== kênh
class ChannelsTest(UiCase):
    def test_get_save_validate_preview_and_create(self):
        tts_profile("giong_vi", self.root)
        g = self.svc.get_channel("kenh")
        self.assertEqual(g["channel"]["name"], "Kênh Thử")
        self.assertEqual(g["options"]["tts_profiles"][0]["name"], "giong_vi")
        raw = g["raw"]
        raw["preset"]["tts_profile"] = "giong_vi"
        raw["title_template"] = "[Tập {sequence}] {project_title}"
        self.svc.save_channel("kenh", raw)
        self.assertEqual(self.svc.get_channel("kenh")["channel"]["title_template"], "[Tập {sequence}] {project_title}")
        before = (self.root / "channels" / "kenh" / "channel.json").read_text(encoding="utf-8")
        for bad in ({"name": ""}, {"title_template": "{khong_co}"}, {"preset": {"nope": 1}}, {"publishing": {"privacy": "ai-cung-xem"}}, "not-a-dict"):
            with self.assertRaises(StageError) as e:
                self.svc.save_channel("kenh", bad)
            self.assertIn("CHANNEL", e.exception.code)
        self.assertEqual((self.root / "channels" / "kenh" / "channel.json").read_text(encoding="utf-8"), before)      # sai thì không ghi
        pv = self.svc.channel_preview("kenh", "Truyện Ma")
        self.assertEqual((pv["youtube_title"], pv["sequence"], pv["thumbnail"]), ("[Tập 5] Truyện Ma", 5, {"channel_name": "[Kênh Thử số 5]", "title": "Truyện Ma"}))
        self.svc.create_channel("moi", "Kênh Mới", True, 9)
        self.assertEqual(self.svc.get_channel("moi")["channel"]["sequence"]["last_used"], 9)
        for bad_id in ("", "a b", "../x", "x" * 41):
            with self.assertRaises(StageError):
                self.svc.create_channel(bad_id, None, False)
        with self.assertRaises(StageError) as e:
            self.svc.create_channel("moi", None, False)
        self.assertEqual(e.exception.code, "CHANNEL_EXISTS")
        with self.assertRaises(StageError):
            self.svc.get_channel("khong-co")
        self.assertEqual({c["id"] for c in self.svc.list_channels()["channels"]}, {"kenh", "chua_khai", "moi"})

    def test_asset_upload_is_sanitised_and_typed(self):
        r = self.svc.save_channel_asset("kenh", "../../watermark của tôi!.wav", write_wav(self.root / "wm.wav", 1.0).read_bytes())
        self.assertRegex(r["name"], r"^[\w.\-]+\.wav$")
        item = self.svc.watermarks.get("kenh", r["watermark_id"])                                        # audio đi vào Watermark Library, không ghi đè file rời
        self.assertEqual((item["source"], item["current_revision"], item["active"]), ("upload", 1, False))
        self.assertFalse((self.root / "channels" / "kenh" / r["name"]).exists())
        self.assertFalse((self.root / "watermark.wav").exists())
        with self.assertRaises(StageError) as bad_audio:
            self.svc.save_channel_asset("kenh", "hong.wav", b"RIFFxxxx")                                  # audio hỏng bị từ chối ngay
        self.assertEqual(bad_audio.exception.code, "WATERMARK_AUDIO_INVALID")
        img = self.svc.save_channel_asset("kenh", "logo.png", b"PNGxx")                                    # ảnh: giữ cách cũ
        self.assertTrue((self.root / "channels" / "kenh" / img["name"]).is_file())
        for bad in ("virus.exe", ".htaccess", "x.txt"):
            with self.assertRaises(StageError):
                self.svc.save_channel_asset("kenh", bad, b"x")


# ================================================================================== cài đặt / TTS / pool / doctor
class AdminTest(UiCase):
    def test_settings_roundtrip_validation_and_masking(self):
        s = self.adm.get_settings()
        keys = {i["key"] for i in s["items"]}
        self.assertTrue({"auto_resume_default", "limits.gpu", "cleanup.enabled", "job_defaults.tiktok.speed"} <= keys)
        self.assertEqual({i["group"] for i in s["items"]}, {g["id"] for g in s["groups"]})
        r = self.adm.update_settings({"limits.gpu": 2, "auto_resume_default": False, "job_defaults.tiktok.speed": 1.5, "monitor.disk_min_free_gb.default": 2})
        self.assertEqual(r["restart_needed"], ["monitor.disk_min_free_gb.default"])
        self.assertEqual(self.o.cfg.limit("gpu"), 2)                                              # đọc "sống": có hiệu lực ngay
        local = json.loads((self.root / "config" / "config.local.json").read_text(encoding="utf-8"))
        self.assertEqual((local["limits"]["gpu"], local["auto_resume_default"], local["job_defaults"]["tiktok"]["speed"]), (2, False, 1.5))
        item = next(i for i in self.adm.get_settings()["items"] if i["key"] == "limits.gpu")
        self.assertEqual((item["value"], item["modified"]), (2, True))
        self.assertEqual(load_config(self.root).limit("gpu"), 2)                                  # lần khởi động sau vẫn còn
        for bad in ({"limits.gpu": 0}, {"limits.gpu": 2.5}, {"limits.gpu": "2"}, {"auto_resume_default": "yes"}, {"nope.key": 1}, {"publishing.defaults.privacy": "all"},
                    {"job_defaults.tiktok.speed": 9}, {"job_defaults.language": ""}):
            with self.assertRaises(StageError):
                self.adm.update_settings(bad)
        self.assertEqual(self.o.cfg.limit("gpu"), 2)                                              # lỗi thì không đổi gì
        self.o.cfg.data["tools"]["yt_uploader"]["token"] = "SECRET-TOKEN-1"
        eff = json.dumps(self.adm.effective_config())
        self.assertNotIn("SECRET-TOKEN-1", eff)
        self.assertIn("***", eff)

    def test_broken_local_config_is_never_overwritten(self):
        f = self.root / "config" / "config.local.json"
        f.write_text("{hỏng", encoding="utf-8")
        with self.assertRaises(StageError) as e:
            self.adm.update_settings({"limits.gpu": 2})
        self.assertEqual(e.exception.code, "CONFIG_LOCAL_INVALID")
        self.assertEqual(f.read_text(encoding="utf-8"), "{hỏng")

    def test_tts_overview_lists_profiles_without_leaking_credentials(self):
        tts_profile("giong_vi", self.root, tuned=True)
        tts_profile("api_voice", self.root, needs=[("env:CF_UI_TEST_KEY", "credential")])
        os.environ["CF_UI_TEST_KEY"] = "gia-tri-bi-mat-123"
        self.addCleanup(os.environ.pop, "CF_UI_TEST_KEY", None)
        t = self.adm.tts_overview()
        self.assertTrue(t["engine"]["is_fake"] and t["engine"]["ok"])
        by = {p["name"]: p for p in t["profiles"]}
        self.assertEqual(t["auto"]["selected"], "giong_vi")
        self.assertTrue(by["giong_vi"]["selected_by_auto"] and by["giong_vi"]["autotune"]["works"])
        self.assertEqual(by["api_voice"]["credentials"], [{"name": "CF_UI_TEST_KEY", "ready": True}])
        self.assertNotIn("gia-tri-bi-mat-123", json.dumps(t))
        d = self.adm.tts_profile_detail("giong_vi")
        self.assertTrue(d["facts"] and {"path", "value", "source", "confidence"} <= set(d["facts"][0]))
        with self.assertRaises(StageError):
            self.adm.tts_profile_detail("khong-co")
        with self.assertRaises(StageError) as e:
            self.adm.tts_onboard("  ")
        self.assertEqual(e.exception.code, "MISSING_REFERENCE")

    def test_tts_onboard_runs_as_a_background_task_and_reports_errors(self):
        tid = self.adm.tts_onboard(str(self.root / "khong-ton-tai"))["task_id"]
        wait_until(lambda: self.adm.tasks.get(tid)["state"] != "running", timeout=20, what="task xong")
        t = self.adm.tasks.get(tid)
        self.assertEqual(t["state"], "error")
        self.assertTrue(t["error"]["message"])
        ref = Path(__file__).resolve().parent / "fixtures" / "tts_repo"
        tid = self.adm.tts_onboard(str(ref))["task_id"]
        wait_until(lambda: self.adm.tasks.get(tid)["state"] != "running", timeout=30, what="onboard xong")
        t = self.adm.tasks.get(tid)
        self.assertEqual(t["state"], "done", t)
        self.assertTrue(t["result"]["engine"])
        self.assertTrue((Path(t["result"]["out_dir"]) / "profile.candidate.json").is_file())

    def test_doctor_runs_in_background_and_groups_results(self):
        self.assertIsNone(self.adm.doctor_status()["report"])
        self.assertTrue(self.adm.doctor_run()["started"])
        wait_until(lambda: not self.adm.doctor_status()["running"] and self.adm.doctor_status()["report"], timeout=60, what="doctor xong")
        st = self.adm.doctor_status()
        labels = {g["label"]: g["status"] for g in st["groups"]}
        self.assertEqual(labels["TTS"], "warning")                                                # TTS giả luôn bị cảnh báo
        self.assertEqual(labels["Cơ sở dữ liệu"], "healthy")
        self.assertTrue(st["summary"]["ready"])
        self.assertEqual(sum(len(g["checks"]) for g in st["groups"]), len(st["report"]["checks"]))   # không check nào bị rơi khỏi nhóm


class PoolsTest(FakeCFCase):
    def test_pools_listing_sync_upsert_and_delete(self):
        o = self.orc()
        adm = AdminService(o)
        p = adm.pools()
        self.assertTrue(p["uses_pool"])
        rows = {r["name"]: r for r in p["pools"]}
        self.assertEqual(set(rows), {"gameplay", "gameplay_vertical"})
        self.assertEqual((rows["gameplay"]["files"], rows["gameplay"]["orientation"], rows["gameplay_vertical"]["orientation"]), (2, None, "portrait"))
        self.assertEqual(rows["gameplay"]["used_by"], ["youtube"])
        tid = adm.pool_sync(None)["task_id"]
        wait_until(lambda: adm.tasks.get(tid)["state"] != "running", timeout=60, what="sync xong")
        self.assertEqual(adm.tasks.get(tid)["state"], "done")
        self.assertEqual({r["name"]: r["state"] for r in adm.pools()["pools"]}, {"gameplay": "ready", "gameplay_vertical": "ready"})
        new = self.root / "raw_new"
        new.mkdir()
        adm.pool_upsert("khac", str(new), "landscape")
        row = next(r for r in adm.pools()["pools"] if r["name"] == "khac")
        self.assertEqual((row["files"], row["problems"], row["state"]), (0, ["Thư mục không có video nào (mp4/mov/mkv…)."], "problem"))
        local = json.loads((self.root / "config" / "config.local.json").read_text(encoding="utf-8"))
        self.assertEqual(local["render"]["pools"]["khac"]["orientation"], "landscape")
        for name, d in (("tên xấu!", str(new)), ("ok", str(self.root / "khong-co"))):
            with self.assertRaises(StageError):
                adm.pool_upsert(name, d, None)
        adm.pool_delete("khac")
        self.assertNotIn("khac", {r["name"] for r in adm.pools()["pools"]})
        with self.assertRaises(StageError) as e:
            adm.pool_delete("gameplay")
        self.assertEqual(e.exception.code, "POOL_IN_BASE_CONFIG")

    def test_fake_render_has_no_pools(self):
        write_config(self.root, adapters={"render": "fake"})
        p = AdminService(self.orc()).pools()
        self.assertFalse(p["uses_pool"])
        with self.assertRaises(StageError) as e:
            AdminService(self.orc()).pool_sync(None)
        self.assertEqual(e.exception.code, "NO_POOLS")


# ================================================================================== HTTP
class HttpTest(UiCase):
    def setUp(self):
        super().setUp()
        self.opened: list[str] = []
        self.app = App(self.o, run_loop=True, opener=self.opened.append)
        self.srv = UiServer(self.app, 0)
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.base = self.srv.url.rstrip("/")

    def call(self, method, path, body=None, headers=None, token=True, raw=None):
        h = {"Content-Type": "application/json", **({"X-CF-Token": self.app.token} if token else {}), **(headers or {})}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}") if "json" in r.headers.get("Content-Type", "") else r.read(), r.headers
        except urllib.error.HTTPError as e:
            raw_body = e.read()
            return e.code, (json.loads(raw_body) if raw_body else {}), e.headers

    def test_story_mode_endpoints_and_create_run(self):
        from unittest import mock
        from contentfactory.story import mode as SM
        with mock.patch.object(SM, "BACKEND_READY", False):
            self._story_mode_unavailable()
        code, info, _ = self.call("GET", "/api/story-mode")
        self.assertEqual((code, info["available"]), (200, True))
        code, ok, _ = self.call("POST", "/api/runs", {"input": {"value": URL}, "channel": "kenh", "run": "story", "title": "Truyện remix", "story_mode": {"mode": "story_remix", "story": {"tone": "u ám"}}})
        self.assertEqual(code, 200)
        self.assertEqual(self.o.store.get_job(ok["job_id"])["params"]["story_mode"]["story"]["tone"], "u ám")

    def _story_mode_unavailable(self):
        self.app.stop()
        code, info, _ = self.call("GET", "/api/story-mode")
        self.assertEqual((code, info["available"], info["default_mode"]), (200, False, "story_branch"))
        code, eff, _ = self.call("POST", "/api/story-mode/effective", {"story_mode": {"mode": "story_remix", "story": {"tone": "x"}}})
        self.assertEqual((code, eff["story"]["tone"]["source"]), (200, "job"))
        code, err, _ = self.call("POST", "/api/story-mode/effective", {"story_mode": {"mode": "story_remix", "story": {"ending": "?"}}})
        self.assertEqual((code, err["error"]["code"]), (400, "INVALID_STORY_MODE"))
        code, err, _ = self.call("POST", "/api/runs", {"input": {"value": URL}, "channel": "kenh", "run": "story", "story_mode": {"mode": "story_remix"}})
        self.assertEqual((code, err["error"]["code"]), (400, "REMIX_UNAVAILABLE"))                       # không bao giờ chạy nhầm sang Story cũ
        self.assertEqual(self.o.store.list_jobs(), [])
        code, ok, _ = self.call("POST", "/api/runs", {"input": {"value": URL}, "channel": "kenh", "run": "story", "story_mode": {"mode": "story_branch"}})
        self.assertEqual(code, 200)
        self.assertNotIn("story_mode", self.o.store.get_job(ok["job_id"])["params"])

    def test_universe_endpoints_end_to_end(self):
        self.app.stop()
        code, sm, _ = self.call("GET", "/api/universe/summary")
        self.assertEqual((code, sm["active"], sm["revision"]), (200, 0, 0))
        self.assertTrue({f["key"] for f in sm["fields"]} >= {"display_name", "core_personality"})
        code, c, _ = self.call("POST", "/api/universe/characters", {"display_name": "Lan Phương", "core_personality": "Điềm tĩnh, hay nghi ngờ.", "genre_affinities": ["kinh dị"]})
        self.assertEqual(code, 200)
        cid, rev = c["character"]["character_id"], c["character"]["revision"]
        self.assertEqual(self.call("POST", "/api/universe/characters", {"display_name": "lan phuong"})[1]["error"]["code"], "DUPLICATE_CHARACTER")
        code, u, _ = self.call("PUT", f"/api/universe/characters/{cid}", {"revision": rev, "fields": {"temperament": "Lạnh"}})
        self.assertEqual((code, u["character"]["revision"]), (200, rev + 1))
        code, err, _ = self.call("PUT", f"/api/universe/characters/{cid}", {"revision": rev, "fields": {"temperament": "Nóng"}})
        self.assertEqual((code, err["error"]["code"]), (400, "REVISION_CONFLICT"))
        self.assertEqual(self.call("PUT", f"/api/universe/characters/{cid}", {"revision": "x", "fields": {}})[0], 400)
        code, lst, _ = self.call("GET", "/api/universe/characters?q=lan%20phuong")
        self.assertEqual((code, lst["total"]), (200, 1))
        code, d, _ = self.call("GET", f"/api/universe/characters/{cid}")
        self.assertEqual([e["action"] for e in d["character"]["audit"]][:3], ["conflict", "update", "create"])
        self.assertEqual(self.call("GET", "/api/universe/characters/ch_000000000000")[0], 404)
        code, lk, _ = self.call("POST", f"/api/universe/characters/{cid}/lock", {"revision": u["character"]["revision"]})
        self.assertTrue(lk["character"]["locked"])
        code, body, hdr = self.call("GET", "/api/universe/export.xlsx")
        self.assertEqual(code, 200)
        self.assertIn("spreadsheetml", hdr["Content-Type"])
        self.assertEqual(body[:2], b"PK")
        code, prev, _ = self.call("PUT", "/api/universe/import/preview", raw=body)
        self.assertEqual((code, prev["counts"]["unchanged"], prev["can_apply"]), (200, 1, False))
        self.assertEqual(self.call("PUT", "/api/universe/import/preview", raw=b"junk")[1]["error"]["code"], "INVALID_XLSX")
        self.assertEqual(self.call("GET", "/api/universe/audit?limit=5")[1]["events"][0]["action"], "lock")
        self.assertEqual(self.call("GET", "/api/universe/summary", token=False)[0], 401)

    def test_universe_story_cast_endpoints(self):
        self.app.stop()
        from contentfactory.universe import casting as CA
        d = self.o.universe.create_character({"display_name": "Thám Tử Vân", "core_personality": "điềm tĩnh quan sát tinh tế", "strengths": ["quan sát"], "genre_affinities": ["trinh thám"]})
        CA.cast_story(self.o.universe, {"story_id": "demo-1", "genre": "trinh thám", "slots": [
            {"slot_id": "hero", "role_code": "protagonist", "traits": ["điềm tĩnh", "quan sát"]},
            {"slot_id": "villain", "role_code": "antagonist", "traits": ["tham vọng"], "relationships": [{"with": "hero", "type": "enemy_of"}]}]})
        code, lst, _ = self.call("GET", "/api/universe/stories")
        self.assertEqual((code, lst["stories"][0]["story_id"], lst["stories"][0]["reused"]), (200, "demo-1", 1))
        code, st, _ = self.call("GET", "/api/universe/stories/demo-1")
        self.assertEqual((code, st["orphans"], st["can_replace"], len(st["cast"]["relationships"])), (200, [], True, 1))
        self.assertEqual(self.call("GET", "/api/universe/stories/nope")[0], 404)
        code, alt, _ = self.call("GET", "/api/universe/stories/demo-1/alternatives?slot=villain")
        self.assertEqual((code, [a["character_id"] for a in alt["alternatives"]]), (200, [d["character_id"]]))
        code, rep, _ = self.call("POST", "/api/universe/stories/demo-1/replace", {"slot_id": "villain", "character_id": d["character_id"]})
        self.assertEqual(code, 200)                                                                           # nhân vật có thể giữ vai khác trong cùng truyện? không: đã là chính ⇒ dàn vẫn hợp lệ
        self.assertEqual(rep["orphans"], [])
        self.assertEqual(self.call("POST", "/api/universe/stories/demo-1/replace", {"slot_id": "villain", "character_id": d["character_id"]})[1]["error"]["code"], "RECAST_LIMIT")

    def test_channel_run_endpoints(self):
        self.app.stop()                                                                    # không để vòng lặp nền chạy mất job con trong test
        self.o.batch_service()._discovery = make_discovery(FakeYouTube(videos=[yt_entry(i) for i in range(12, 0, -1)]))
        code, r, _ = self.call("POST", "/api/sources/inspect", {"value": "@abc"})
        self.assertEqual((code, r["kind"], r["title"]), (200, "channel", "Truyện ABC"))
        self.assertEqual(self.call("POST", "/api/sources/inspect", {"value": "https://vimeo.com/1"})[0], 400)
        code, d, _ = self.call("POST", "/api/sources/youtube/discover", {"url": "@abc", "output_channel": "kenh"})
        self.assertEqual((code, d["selected"], d["total"]), (200, 10, 12))
        body = {"url": "@abc", "output_channel": "kenh", "run": "story", "request_id": "rq-1"}
        code, b, _ = self.call("POST", "/api/batches", body)
        self.assertEqual((code, b["counts"]["total"], b["status"], b["deduped"]), (200, 10, "QUEUED", False))
        self.assertEqual(self.call("POST", "/api/batches", body)[1]["deduped"], True)
        bid = b["id"]
        self.assertEqual([x["id"] for x in self.call("GET", "/api/batches")[1]["batches"]], [bid])
        code, det, _ = self.call("GET", f"/api/batches/{bid}?status=queued&limit=4")
        self.assertEqual((code, len(det["items"]), det["has_more"], det["total_items"]), (200, 4, True, 10))
        self.assertEqual(self.call("POST", f"/api/batches/{bid}/pause")[1]["paused"], 10)
        self.assertEqual(self.call("GET", f"/api/batches/{bid}")[1]["status"], "PAUSED")
        self.assertEqual(self.call("POST", f"/api/batches/{bid}/resume")[1]["resumed"], 10)
        code, u, _ = self.call("POST", f"/api/batches/{bid}/target", {"target_stage": "tts", "scope": "unfinished"})
        self.assertEqual((code, u["counts"]["applied"]), (200, 10))
        self.assertEqual(self.call("POST", f"/api/batches/{bid}/target", {"target_stage": "tts", "scope": "x"})[0], 400)
        self.assertEqual(self.call("POST", f"/api/batches/{bid}/cancel-queued")[1]["cancelled_jobs"], 10)
        kids = [x["job_id"] for x in self.call("GET", f"/api/batches/{bid}?limit=3")[1]["items"]]
        code, bk, _ = self.call("POST", "/api/jobs/bulk", {"action": "resume", "job_ids": kids + ["999999"]})
        self.assertEqual((code, bk["counts"]["error"], len(bk["results"])), (200, 1, 4))
        self.assertEqual(self.call("POST", "/api/jobs/bulk", {"action": "explode", "job_ids": kids})[0], 400)
        code, lst, _ = self.call("GET", "/api/jobs")
        self.assertEqual([(r["type"], r["id"]) for r in lst["jobs"]], [("batch", bid)])            # job con không phải hàng cấp cao
        self.assertEqual(self.call("GET", "/api/batches/B999999")[0], 404)
        self.assertEqual(self.call("POST", f"/api/batches/{bid}/rescan", token=False)[0], 401)
        code, r, _ = self.call("POST", "/api/runs", {"input": {"value": "@abc"}, "channel": "kenh"})
        self.assertEqual((code, r["error"]["code"]), (400, "USE_CHANNEL_RUN"))

    def test_prosody_endpoints(self):
        code, info, _ = self.call("GET", "/api/tts/prosody")
        self.assertEqual((code, info["profiles"][0]["id"]), (200, "natural"))
        code, r, _ = self.call("POST", "/api/tts/prosody/preview", {"variants": [{"profile": "fast"}]})
        self.assertEqual(code, 200)
        wait_until(lambda: self.call("GET", f"/api/tasks/{r['task']}")[1]["state"] != "running", 30, "preview")
        res = self.call("GET", f"/api/tasks/{r['task']}")[1]["result"]
        code, body, headers = self.call("GET", res["variants"][0]["url"])
        self.assertEqual((code, headers.get_content_type(), body[:4]), (200, "audio/wav", b"RIFF"))
        self.assertEqual(self.call("GET", "/api/tts/prosody/preview/" + "0" * 16)[0], 404)
        self.assertEqual(self.call("POST", "/api/tts/prosody/preview", {"variants": [{"profile": "x"}]})[0], 400)
        self.assertEqual(self.call("GET", res["variants"][0]["url"], token=False)[0], 401)

    def test_job_control_endpoints(self):
        self.app.stop()                                                                   # dừng vòng lặp nền TRƯỚC khi tạo job: nếu không job có thể xong trước khi test kịp tạm dừng
        code, r, _ = self.call("POST", "/api/runs", {"input": {"value": URL}, "channel": "kenh", "run": "story"})
        jid = r["job_id"]
        self.assertEqual(self.call("POST", f"/api/jobs/{jid}/pause")[1]["result"], "changed")
        self.assertEqual(self.call("GET", f"/api/jobs/{jid}")[1]["status"], "paused")
        code, e, _ = self.call("POST", f"/api/jobs/{jid}/pipeline-revisions", {"pipeline": {"requested_stages": ["tts"]}})
        self.assertEqual((code, e["error"]["code"]), (400, "PIPELINE_USE_TARGET"))                     # đường pipeline cũ đã bỏ: chỉ còn Sửa job
        code, t, _ = self.call("PUT", f"/api/jobs/{jid}/target", {"target_stage": "tts"})
        self.assertEqual((code, t["result"], t["new_target"]), (200, "changed", "tts"))
        self.assertEqual(self.call("PUT", f"/api/jobs/{jid}/target", {"target_stage": "tts"})[1]["result"], "unchanged")      # lặp lại: idempotent
        self.assertEqual(self.call("PUT", f"/api/jobs/{jid}/target", {"target_stage": "nope"})[0], 400)
        self.assertEqual(self.call("PUT", "/api/jobs/999999/target", {"target_stage": "tts"})[0], 404)
        self.assertEqual(self.call("PUT", f"/api/jobs/{jid}/target", {"target_stage": "tts"}, token=False)[0], 401)
        self.assertEqual(self.call("POST", f"/api/jobs/{jid}/resume")[1]["result"], "unpaused")
        self.assertEqual(self.call("POST", f"/api/jobs/{jid}/cancel")[1]["result"], "changed")
        self.assertEqual(self.call("POST", f"/api/jobs/{jid}/clone", {})[0], 200)
        self.assertEqual(self.call("POST", "/api/jobs/999999/pause")[0], 404)
        self.assertEqual(self.call("POST", f"/api/jobs/{jid}/pause", token=False)[0], 401)

    def test_pipeline_descriptor_and_plan_endpoints(self):
        code, d, _ = self.call("GET", "/api/pipeline")
        self.assertEqual((code, [s["id"] for s in d["stages"]]), (200, [s.name for s in P.STAGES]))
        code, r, _ = self.call("POST", "/api/pipeline/plan", {"pipeline_spec": {"version": 2, "requested_stages": ["render_tiktok"]}, "input_kind": "youtube_url"})
        self.assertEqual((code, r["ok"], "render_youtube" in r["run"]), (200, True, False))
        code, r, _ = self.call("POST", "/api/pipeline/plan", {"pipeline_spec": {"version": 2, "requested_stages": ["nope"]}})
        self.assertEqual((code, r["ok"]), (200, False))
        self.assertEqual(self.call("POST", "/api/pipeline/plan", {"pipeline_spec": {}}, token=False)[0], 401)

    def test_token_host_and_origin_protection(self):
        self.assertEqual(self.call("GET", "/api/bootstrap", token=False)[0], 401)
        self.assertEqual(self.call("GET", "/api/bootstrap", headers={"X-CF-Token": "sai"}, token=False)[0], 401)
        self.assertEqual(self.call("GET", "/api/bootstrap")[0], 200)
        c = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=10)
        c.request("GET", "/api/bootstrap", headers={"Host": "evil.example", "X-CF-Token": self.app.token})
        self.assertEqual(c.getresponse().status, 403)                                                # chống DNS rebinding
        c.close()
        s, body, _ = self.call("POST", "/api/detect", {"value": URL}, headers={"Origin": "http://evil.example"})
        self.assertEqual((s, body["error"]["code"]), (403, "BAD_ORIGIN"))                           # chống CSRF
        self.assertEqual(self.call("POST", "/api/detect", {"value": URL}, headers={"Origin": f"http://127.0.0.1:{self.app.port}"})[0], 200)
        self.assertEqual(self.srv.httpd.server_address[0], "127.0.0.1")

    def test_static_files_token_injection_and_safety(self):
        s, html, h = self.call("GET", "/", token=False)
        self.assertEqual(s, 200)
        self.assertIn(f'<meta name="cf-token" content="{self.app.token}">'.encode(), html)
        self.assertIn("default-src 'self'", h["Content-Security-Policy"])
        self.assertEqual(h["Cache-Control"], "no-store")
        s, js, h = self.call("GET", "/js/main.js", token=False)
        self.assertEqual((s, h["Content-Type"].split(";")[0]), (200, "text/javascript"))
        etag = h["ETag"]
        self.assertEqual(self.call("GET", "/js/main.js", headers={"If-None-Match": etag}, token=False)[0], 304)
        self.assertEqual(self.call("GET", "/vendor/gsap.min.js", token=False)[0], 200)
        self.assertEqual(self.call("GET", "/jobs/123", token=False)[0], 200)                         # đường dẫn client-side => index.html
        for evil in ("/../../config/config.json", "/..%2F..%2Fconfig%2Fconfig.json", "/js/../../config.py", "/%2e%2e/%2e%2e/HANDOFF.md"):
            s, body, _ = self.call("GET", evil, token=False)
            self.assertNotIn(b"adapters", body if isinstance(body, bytes) else json.dumps(body).encode(), evil)
        self.assertEqual(self.call("POST", "/", {}, token=False)[0], 405)

    def test_api_errors_are_human_readable_json(self):
        s, b, _ = self.call("GET", "/api/jobs/999999")
        self.assertEqual((s, b["error"]["code"]), (404, "JOB_NOT_FOUND"))
        self.assertTrue(b["error"]["message"] and "Traceback" not in json.dumps(b))
        self.assertEqual(self.call("GET", "/api/khong-co")[0], 404)
        self.assertEqual(self.call("DELETE", "/api/bootstrap")[0], 405)
        s, b, _ = self.call("POST", "/api/detect", raw=b"{khong phai json")
        self.assertEqual((s, b["error"]["code"]), (400, "BAD_JSON"))
        s, b, _ = self.call("POST", "/api/detect", raw=b"[1,2]")
        self.assertEqual(s, 400)
        s, b, _ = self.call("POST", "/api/runs", {"input": {"value": URL}, "channel": "kenh", "run": "tts_only"})
        self.assertEqual((s, b["error"]["code"]), (400, "INVALID_RUN_MODE"))
        s, b, _ = self.call("POST", "/api/detect", raw=b"x" * (1 << 21))
        self.assertEqual(s, 413)
        s, b, _ = self.call("PUT", "/api/settings", {"changes": {"limits.gpu": 99}})
        self.assertEqual((s, b["error"]["code"]), (400, "INVALID_SETTING"))

    def test_daily_flow_over_http_with_the_background_runner(self):
        s, boot, _ = self.call("GET", "/api/bootstrap")
        self.assertEqual((s, boot["runtime"]["runner"], {c["id"] for c in boot["channels"]} >= {"kenh"}), (200, True, True))
        s, pv, _ = self.call("POST", "/api/preview", {"input": {"value": URL}, "channel": "kenh", "run": "full", "title": "Truyện Qua HTTP"})
        self.assertTrue(pv["can_run"])
        s, r, _ = self.call("POST", "/api/runs", {"request_id": "http-1", "input": {"value": URL}, "channel": "kenh", "run": "full", "title": "Truyện Qua HTTP"})
        self.assertEqual((s, r["deduped"]), (200, False))
        s, again, _ = self.call("POST", "/api/runs", {"request_id": "http-1", "input": {"value": URL}, "channel": "kenh", "run": "full", "title": "Truyện Qua HTTP"})
        self.assertEqual((again["deduped"], again["job_id"]), (True, r["job_id"]))
        wait_until(lambda: self.call("GET", f"/api/jobs/{r['job_id']}")[1]["status"] == "completed", timeout=60, what="job xong qua runner nền")
        d = self.call("GET", f"/api/jobs/{r['job_id']}")[1]
        self.assertEqual(d["title"], "Truyện Qua HTTP")
        self.assertEqual(d["output"]["youtube_title"], "[Full Audio][Kênh Thử số 5] | Truyện Qua HTTP")
        s, o, _ = self.call("POST", f"/api/jobs/{r['job_id']}/open-output", {})
        self.assertEqual((s, self.opened), (200, [o["opened"]]))
        lst = self.call("GET", "/api/jobs?status=completed")[1]
        self.assertEqual([j["id"] for j in lst["jobs"]], [r["job_id"]])
        self.assertEqual(self.call("GET", f"/api/jobs/{r['job_id']}/log?tail=5")[1]["lines"][-1]["event"], "stage_succeeded")

    def test_admin_endpoints_respond(self):
        for p in ("/api/channels", "/api/channels/kenh", "/api/channels/kenh/preview?title=A", "/api/tts", "/api/pools", "/api/settings", "/api/config/effective", "/api/doctor", "/api/runtime"):
            self.assertEqual(self.call("GET", p)[0], 200, p)
        s, b, _ = self.call("PUT", "/api/channels/kenh/asset?name=wm.wav", raw=write_wav(self.root / "up.wav", 1.0).read_bytes(), headers={"Content-Type": "application/octet-stream"})
        self.assertEqual((s, b["name"], b["revision"]), (200, "wm.wav", 1))                                   # cách tải lên cũ: giờ vào Watermark Library
        self.assertEqual(self.call("POST", "/api/cleanup", {"dry_run": True})[0], 200)
        s, b, _ = self.call("POST", "/api/samples", {})
        self.assertEqual(s, 200)
        self.assertTrue(Path(b["story"]).is_file() and Path(b["audio"]).is_file())
        self.assertEqual(self.call("GET", "/api/tasks/khong-co")[0], 404)

    def test_concurrent_polls_do_not_break_the_runner(self):
        r = self.call("POST", "/api/runs", {"input": {"value": URL}, "channel": "kenh", "run": "full"})[1]
        errors = []

        def poll():
            try:
                for _ in range(15):
                    self.call("GET", "/api/jobs")
                    self.call("GET", f"/api/jobs/{r['job_id']}")
            except Exception as e:                                                                   # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=poll) for _ in range(6)]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        self.assertEqual(errors, [])
        wait_until(lambda: self.call("GET", f"/api/jobs/{r['job_id']}")[1]["status"] == "completed", timeout=60, what="job xong")


if __name__ == "__main__":
    unittest.main()
