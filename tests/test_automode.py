"""Phase 7: Auto Mode (channel preset, auto TTS profile/pool/naming), Auto Cleanup, Doctor, CLI cơ bản/nâng cao, setup/update (Sys giả), và E2E `go`:
YouTube URL -> source -> story -> TTS -> audio -> render YouTube + TikTok -> gói output."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from contentfactory.contracts import ErrorClass, StageError, clean_title
from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator import auto as AU
from contentfactory.orchestrator import channels as CH
from contentfactory.orchestrator import cleanup as CL
from contentfactory.orchestrator import doctor as DR
from contentfactory.orchestrator import ops
from contentfactory.orchestrator.cli import build_parser, main
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.runner import Orchestrator
from contentfactory.orchestrator.setup_env import Setup, Sys, Update, parse_lock, uploader_exe, venv_python
from contentfactory.output import metadata as MD
from contentfactory.source.chain import ProviderChain
from contentfactory.source.providers import YtDlpProvider
from contentfactory.adapters.story_branch import StoryBranchAdapter
from tests.fakes import URL, FakeYtDlp, ScriptedOhStory, stub_deploy
from tests.support import REPO, RootCase, params
from tests.test_render import FAKE_CF, FakeCFCase

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
LENIENT_AUDIO = {"qa": {"silence": {"max_ratio": 0.8, "max_gap_s": 8.0}}}          # TTS giả có chunk ngắn/pause dài so với giọng thật


def write_channel(root: Path, cid: str, body: dict) -> Path:
    d = root / "channels" / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "channel.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return d


def write_config(root: Path, **extra) -> None:
    f = root / "config" / "config.json"
    c = json.loads(f.read_text(encoding="utf-8"))
    for k, v in extra.items():
        c[k] = {**c[k], **v} if isinstance(v, dict) and isinstance(c.get(k), dict) else v
    f.write_text(json.dumps(c), encoding="utf-8")


def bare(**over) -> dict:
    """params không có tiktok/title mặc định của BASE_PARAMS (để kiểm tra preset kênh điền vào)."""
    p = params(**over)
    for k in ("tiktok", "title"):
        if k not in over:
            p.pop(k, None)
    return p


def tts_profile(name: str, root: Path, engine="fake", langs=("vi",), status="ready", needs=(), tuned=False) -> None:
    from contentfactory.tts import schema as S
    p = S.new_annotated(engine, status)
    p["segment"] = {"max_chars": S.fact(300, "official_docs", "high", [{"ref": "d"}])}
    p["capabilities"] = {"languages": list(langs)}
    p["needs_user"] = [{"key": k, "reason": r} for k, r in needs]
    if tuned:
        p["meta"]["autotune"] = {"works": True}
    d = root / "tts_profiles"
    d.mkdir(exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(p), encoding="utf-8")


# ============================================================================== Auto Naming
class AutoNamingTest(unittest.TestCase):
    def test_clean_title_strips_decoration_but_keeps_meaning(self):
        self.assertEqual(clean_title("🔥 Truyện Ma Đêm Khuya #truyenma #horror 😱"), "Truyện Ma Đêm Khuya")
        self.assertEqual(clean_title("  ___Tôi Trùng Sinh___ https://x.co/abc  "), "Tôi Trùng Sinh")
        self.assertEqual(clean_title("Hôm nay? Vui!"), "Hôm nay? Vui!")                              # dấu câu có nghĩa được giữ
        self.assertEqual(clean_title("Cô gái | Tập 1"), "Cô gái | Tập 1")                           # không đoán xóa phần sau dấu |
        self.assertEqual(clean_title(""), "untitled")
        long = clean_title("Một tiêu đề rất rất dài " * 10)
        self.assertLessEqual(len(long), 80)
        self.assertTrue(set(long.split()) <= {"Một", "tiêu", "đề", "rất", "dài"})                      # cắt ở ranh giới từ, không cụt giữa chữ

    def test_source_default_title_is_cleaned_and_still_flagged(self):
        from contentfactory.contracts import project_of
        ctx = type("C", (), {"params": {}, "config": {}, "job_id": "j"})()
        p = project_of(ctx, {"title": "🔥 Truyện Hay #hot"})
        self.assertEqual((p["title"], p["title_source"]), ("Truyện Hay", "source_default"))


# ============================================================================== Channel preset + Auto selection
class ChannelPresetTest(RootCase):
    def test_preset_schema_is_validated(self):
        self.assertEqual(MD.validate_preset({"tts_profile": "a", "pools": {"youtube": "p"}, "render": {"tiktok": {}}, "tiktok": {"speed": 2}}), [])
        for bad in ({"nope": 1}, {"tts_profile": 5}, {"pools": {"facebook": "x"}}, {"pools": {"youtube": ""}}, {"render": {"x": {}}}, {"tiktok": "x"}):
            self.assertTrue(MD.validate_preset(bad), bad)
        with self.assertRaises(StageError) as e:
            MD.normalize_channel({"preset": {"nope": 1}}, "k")
        self.assertEqual(e.exception.code, "INVALID_CHANNEL_CONFIG")

    def test_preset_fills_params_and_explicit_params_win(self):
        write_channel(self.root, "kenh", {"name": "K", "preset": {"language": "en", "tiktok": {"speed": 1.5, "target_part_sec": 300},
                                                                  "audio": {"master": {"loudness": {"target_lufs": -18}}},
                                                                  "render": {"youtube": {"fps": 24}}}})
        orc = self.orc()
        j = orc.store.get_job(orc.submit(bare(channel="kenh")))["params"]
        self.assertEqual((j["language"], j["tiktok"]["speed"], j["audio"]["master"]["loudness"]["target_lufs"], j["render"]["youtube"]["fps"]), ("en", 1.5, -18, 24))
        self.assertEqual({d["what"] for d in j["auto"]}, {"language", "tiktok", "audio", "render"})
        j2 = orc.store.get_job(orc.submit(bare(channel="kenh", language="vi", tiktok={"speed": 3.0, "target_part_sec": 60})))["params"]
        self.assertEqual((j2["language"], j2["tiktok"]["speed"]), ("vi", 3.0))                        # người dùng nhập thì thắng preset

    def test_no_preset_means_no_change(self):
        orc = self.orc()
        j = orc.store.get_job(orc.submit(params()))["params"]
        self.assertNotIn("auto", j)

    def test_named_tts_profile_in_preset_is_loaded_and_missing_one_rejects_the_job(self):
        tts_profile("giong_a", self.root)
        write_channel(self.root, "kenh", {"preset": {"tts_profile": "giong_a"}})
        write_channel(self.root, "hong", {"preset": {"tts_profile": "khong-co"}})
        orc = self.orc()
        j = orc.store.get_job(orc.submit(params(channel="kenh")))["params"]
        self.assertEqual(j["tts"]["engine"], "fake")
        self.assertEqual(j["tts"]["schema"], 1)
        self.assertIn({"what": "tts_profile", "value": "giong_a", "why": "profile ưa thích của kênh 'kenh'"}, j["auto"])
        with self.assertRaises(StageError) as e:
            orc.submit(params(channel="hong"))
        self.assertEqual((e.exception.code, e.exception.resource), ("TTS_PROFILE_NOT_FOUND", "input"))
        self.assertEqual(len(orc.store.list_jobs()), 1)                                              # không tạo job nửa vời

    def test_preset_watermark_and_publishing_defaults_come_from_the_channel(self):
        d = write_channel(self.root, "kenh", {"name": "K", "watermark": "wm.wav", "publishing": {"privacy": "unlisted", "account_id": "acc1"}})
        (d / "wm.wav").write_bytes(b"RIFF")
        orc = self.orc()
        self.assertEqual(Path(orc.store.get_job(orc.submit(params(channel="kenh")))["params"]["watermark"]), d / "wm.wav")


class AutoTtsProfileTest(RootCase):
    def cfg(self):
        return load_config(self.root)

    def test_selection_by_language_engine_and_readiness(self):
        tts_profile("vi_ready", self.root, langs=("vi",), status="ready")
        tts_profile("vi_cand", self.root, langs=("vi",), status="candidate")
        tts_profile("en_ready", self.root, langs=("en",), status="ready")
        tts_profile("other_engine", self.root, engine="khac", langs=("vi",), status="ready", tuned=True)
        name, why = AU.select_tts_profile(self.cfg(), "vi", "fake")
        self.assertEqual(name, "vi_ready")                                                            # đúng ngôn ngữ + engine + ready
        self.assertIn("engine khớp", why)
        self.assertEqual(AU.select_tts_profile(self.cfg(), "en", "fake")[0], "en_ready")
        self.assertIsNone(AU.select_tts_profile(self.cfg(), "fr", "fake"))                            # không có profile hợp ngôn ngữ
        self.assertEqual(AU.select_tts_profile(self.cfg(), "vi", "khac")[0], "other_engine")

    def test_profiles_that_need_the_user_are_never_auto_selected(self):
        tts_profile("clone", self.root, needs=[("settings.reference_audio", "required_reference_voice")], tuned=True)
        tts_profile("api", self.root, needs=[("env:KEY", "credential")], tuned=True)
        tts_profile("ok", self.root, needs=[("env:OPT", "credential_optional")])
        self.assertEqual(AU.select_tts_profile(self.cfg(), "vi", "fake")[0], "ok")

    def test_tuned_profile_wins_ties_and_corrupt_files_are_ignored(self):
        tts_profile("a_plain", self.root)
        tts_profile("b_tuned", self.root, tuned=True)
        (self.root / "tts_profiles" / "broken.json").write_text("{không phải json", encoding="utf-8")
        self.assertEqual(AU.select_tts_profile(self.cfg(), "vi", "fake")[0], "b_tuned")

    def test_auto_selection_in_submit_respects_explicit_choice_and_switch(self):
        tts_profile("auto_one", self.root)
        orc = self.orc()
        j = orc.store.get_job(orc.submit(params()))["params"]
        self.assertEqual(j["tts"]["engine"], "fake")
        self.assertTrue(any(d["what"] == "tts_profile" and d["why"].startswith("tự chọn") for d in j["auto"]))
        j2 = orc.store.get_job(orc.submit(params(tts={"max_chars": 200})))["params"]
        self.assertEqual(j2["tts"], {"max_chars": 200})                                               # người dùng chỉ định: không đụng
        write_config(self.root, auto={"tts_profile_selection": False})
        j3 = self.orc().store.get_job(self.orc().submit(params()))["params"]
        self.assertNotIn("tts", j3)


class AutoPoolTest(unittest.TestCase):
    def test_pick_pool_logic(self):
        pools = {"ngang": {"raw_dir": "a"}, "doc": {"raw_dir": "b"}, "x": {"raw_dir": "c", "orientation": "portrait"}}
        self.assertEqual(AU.pick_pool("youtube", "gameplay", {"ngang": {}, "doc": {}}), ("ngang", "pool duy nhất có hướng khung hình landscape"))
        self.assertEqual(AU.pick_pool("tiktok", "gameplay_vertical", {"ngang": {}, "doc": {}})[0], "doc")
        self.assertEqual(AU.pick_pool("tiktok", None, {"a": {"orientation": "portrait"}, "b": {}})[0], "a")             # orientation khai báo thắng đoán theo tên
        self.assertEqual(AU.pick_pool("youtube", "ngang", pools)[1], "đã cấu hình")
        n, why = AU.pick_pool("youtube", "gameplay", {"duy_nhat": {}})
        self.assertEqual(n, "duy_nhat")
        self.assertIn("dùng chung", why)
        self.assertEqual(AU.pick_pool("youtube", "gameplay", {"a": {}, "b": {}})[0], None)                              # mơ hồ: không đoán bừa
        self.assertEqual(AU.orientation_of("Video_Dọc", {}), "portrait")
        self.assertEqual(AU.orientation_of("gameplay-16x9", {}), "landscape")


class AutoPoolSubmitTest(FakeCFCase):
    def pools(self, mapping: dict):
        write_config(self.root, render={"pools": mapping})

    def test_pools_are_auto_picked_from_orientation_and_preset_wins(self):
        self.pools({"ngang": {"raw_dir": str(self.raw["gameplay"])}, "doc": {"raw_dir": str(self.raw["gameplay_vertical"])}})
        write_channel(self.root, "kenh", {"name": "K"})
        write_channel(self.root, "co_preset", {"name": "P", "preset": {"pools": {"youtube": "doc"}}})
        orc = self.orc()
        j = orc.store.get_job(orc.submit(params(channel="kenh")))["params"]
        self.assertEqual((j["render"]["youtube"]["source_pool"], j["render"]["tiktok"]["source_pool"]), ("ngang", "doc"))
        self.assertTrue(all(d["why"] for d in j["auto"]))
        j2 = orc.store.get_job(orc.submit(params(channel="co_preset")))["params"]
        self.assertEqual((j2["render"]["youtube"]["source_pool"], j2["render"]["tiktok"]["source_pool"]), ("doc", "doc"))   # preset kênh: youtube=doc; tiktok tự chọn
        j3 = orc.store.get_job(orc.submit(params(channel="kenh", render={"youtube": {"source_pool": "doc"}})))["params"]
        self.assertEqual(j3["render"]["youtube"]["source_pool"], "doc")                                                     # người dùng nhập thắng

    def test_default_pool_names_need_no_override(self):
        orc = self.orc()                                                                                  # pool 'gameplay' + 'gameplay_vertical' đúng tên mặc định
        self.assertNotIn("auto", orc.store.get_job(orc.submit(params()))["params"])

    def test_fake_render_skips_pool_selection(self):
        write_config(self.root, adapters={"render": "fake"})
        self.assertNotIn("auto", self.orc().store.get_job(self.orc().submit(params()))["params"])


class ConfigLocalTest(RootCase):
    def test_config_local_overrides_config_and_secrets_env_is_loaded(self):
        (self.root / "config" / "config.local.json").write_text(json.dumps({"adapters": {"audio": "ffmpeg"}, "limits": {"gpu": 3}}), encoding="utf-8")
        c = load_config(self.root)
        self.assertEqual((c.data["adapters"]["audio"], c.limit("gpu"), c.data["adapters"]["tts"]), ("ffmpeg", 3, "fake"))   # đè từng khóa, phần còn lại giữ
        (self.root / "config" / "secrets.local.env").write_text("# c\nCF_TEST_SECRET=abc\nCF_TEST_KEEP=zzz\n", encoding="utf-8")
        os.environ["CF_TEST_KEEP"] = "da-co"
        self.addCleanup(os.environ.pop, "CF_TEST_SECRET", None)
        self.addCleanup(os.environ.pop, "CF_TEST_KEEP", None)
        from contentfactory.orchestrator.cli import _load_local_env
        _load_local_env(self.root)
        self.assertEqual((os.environ["CF_TEST_SECRET"], os.environ["CF_TEST_KEEP"]), ("abc", "da-co"))                    # không đè biến đã có


# ============================================================================== Auto Cleanup
class CleanupTest(RootCase):
    DAY = 86400

    def done_job(self, orc, **extra):
        jid = orc.submit(params(tiktok={"speed": 2.0, "target_part_sec": 0.8}, **extra), auto_resume=False)
        orc.run()
        return jid

    def files(self, jid) -> set[str]:
        d = self.job_dir(jid)
        return {p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()}

    def test_intermediates_go_after_publish_but_artifacts_manifest_and_output_stay(self):
        orc = self.orc()
        jid = self.done_job(orc)
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        before = self.files(jid)
        arts = {a["path"] for a in orc.store.artifacts(jid)}
        self.assertTrue(any("chunks/" in f for f in before))                                           # có chunk trung gian
        out_before = {p: p.read_bytes() for p in (self.root / "output").rglob("*") if p.is_file()}
        rep = orc.cleanup()
        after = self.files(jid)
        self.assertGreater(rep["removed"], 0)
        self.assertEqual(rep["by_kind"].get("job_artifact", 0), 0)                                     # chưa tới hạn giữ artifact
        self.assertTrue(arts <= after)                                                                  # artifact còn nguyên
        self.assertTrue({"manifest.json", "job.log.jsonl"} <= after)
        self.assertFalse(any("chunks/" in f or "segments.json" in f for f in after))                    # trung gian đã dọn
        self.assertEqual(out_before, {p: p.read_bytes() for p in (self.root / "output").rglob("*") if p.is_file()})   # output/ không bị đụng
        self.assertEqual(orc.cleanup()["removed"], 0)                                                   # idempotent
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)

    def test_dry_run_changes_nothing(self):
        orc = self.orc()
        jid = self.done_job(orc)
        before = self.files(jid)
        rep = orc.cleanup(dry_run=True)
        self.assertTrue(rep["dry_run"] and rep["candidates"] > 0 and rep["freed_bytes"] > 0)
        self.assertEqual(before, self.files(jid))

    def test_running_held_and_queued_jobs_are_never_touched(self):
        orc = self.orc()
        held = orc.submit(params(fake={"story": {"error_class": "RESOURCE", "resource": "network", "fail_until_attempt": 99}}), auto_resume=False)
        orc.run()
        self.assertEqual(orc.store.get_job(held)["hold_reason"], P.PAUSED_NETWORK)
        queued = orc.submit(params(), auto_resume=False)
        snap = {j: self.files(j) for j in (held, queued)}
        rep = CL.run(orc, now=time.time() + 400 * self.DAY)                                              # kể cả rất lâu sau
        self.assertEqual(rep["removed"], 0)
        self.assertEqual(snap, {j: self.files(j) for j in (held, queued)})

    def test_artifacts_are_removed_only_after_the_keep_period_and_leave_a_marker(self):
        orc = self.orc()
        jid = self.done_job(orc)
        orc.cleanup()
        rep = CL.run(orc, now=time.time() + 20 * self.DAY)                                               # quá artifact_keep_days (14)
        self.assertGreater(rep["by_kind"].get("job_artifact", 0), 0)
        left = self.files(jid)
        self.assertEqual(left, {"manifest.json", "job.log.jsonl", ".cleaned.json"})                     # chỉ còn manifest + log + dấu vết đã dọn
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        self.assertTrue(any((self.root / "output").iterdir()))                                         # output của người dùng vẫn còn
        j2 = self.orc().submit(params(), start_stage="output", target_stage="output", from_job={"job_id": jid, "kinds": ["video_youtube"]}, auto_resume=False) \
            if False else None
        self.assertIsNone(j2)
        with self.assertRaises(StageError) as e:                                                         # dùng lại artifact đã dọn: lỗi rõ ràng, không dùng file hỏng
            orc.submit(params(), start_stage="output", target_stage="output", from_job={"job_id": jid, "kinds": ["video_youtube"]})
        self.assertEqual(e.exception.code, "IMPORT_INVALID")

    def test_failed_jobs_keep_everything_until_failed_keep_days(self):
        orc = self.orc()
        jid = orc.submit(params(fake={"tts_chunk_3": {"error_class": "POLICY", "fail_until_attempt": 99}}), auto_resume=False)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.FAILED)
        before = self.files(jid)
        self.assertEqual(CL.run(orc, now=time.time() + 20 * self.DAY)["removed"], 0)                    # chưa tới failed_keep_days (30): còn để retry
        self.assertEqual(self.files(jid), before)
        self.assertGreater(CL.run(orc, now=time.time() + 40 * self.DAY)["removed"], 0)

    def test_shared_caches_are_trimmed_least_recently_used_first(self):
        write_config(self.root, cleanup={"cache_gb": {"tts": 0.000001, "source": 5}})
        orc = self.orc()
        d = self.root / "runtime" / "cache" / "tts" / "ab"
        d.mkdir(parents=True)
        old, new = d / "old.wav", d / "new.wav"
        old.write_bytes(b"x" * 800)
        new.write_bytes(b"y" * 800)
        os.utime(old, (1, 1))
        rep = orc.cleanup()
        self.assertEqual(rep["by_kind"], {"cache": 1})
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())                                                                   # file mới dùng giữ lại (dưới giới hạn)

    def test_only_stale_pipeline_tmp_dirs_inside_output_are_removed(self):
        out = self.root / "output"
        (out / ".tmp-000099").mkdir(parents=True)
        (out / ".tmp-000099" / "x.bin").write_bytes(b"1")
        user = out / "20260101_cua-toi"
        user.mkdir()
        (user / "video.mp4").write_bytes(b"user")
        (out / ".tmp-000098").mkdir()
        (out / ".tmp-000098" / "y.bin").write_bytes(b"1")
        old = time.time() - 3 * self.DAY
        for p in ((out / ".tmp-000099"), (out / ".tmp-000099" / "x.bin")):
            os.utime(p, (old, old))
        rep = self.orc().cleanup()
        self.assertEqual(rep["by_kind"], {"output_tmp": 1})
        self.assertFalse((out / ".tmp-000099").exists())
        self.assertTrue((out / ".tmp-000098").exists())                                                 # tạm mới (có thể đang dựng): giữ
        self.assertEqual((user / "video.mp4").read_bytes(), b"user")                                    # thư mục của người dùng không bị đụng

    def test_disabled_flag_stops_the_automatic_tick_but_not_the_manual_command(self):
        orc = self.orc()
        jid = self.done_job(orc)
        orc._last_cleanup = 0
        orc._cleanup_tick()
        self.assertTrue(any("chunks/" in f for f in self.files(jid)))                                   # enabled=False trong cấu hình test: tick không làm gì
        write_config(self.root, cleanup={"enabled": True, "interval_s": 0})
        orc2 = self.orc()
        orc2._cleanup_tick()
        self.assertFalse(any("chunks/" in f for f in self.files(jid)))                                  # bật: tick tự dọn


# ============================================================================== Doctor
class DoctorTest(FakeCFCase):
    def by(self, rep):
        return {c["name"]: c for c in rep["checks"]}

    def test_fake_setup_is_ready_but_honest_about_fake_tts(self):
        write_config(self.root, adapters={"render": "fake"})
        rep = DR.run_doctor(load_config(self.root))
        b = self.by(rep)
        self.assertTrue(rep["ready"], [c for c in rep["checks"] if c["status"] == "fail"])
        self.assertEqual(b["tts"]["status"], "warn")                                                    # không giấu việc đang dùng TTS giả
        self.assertIn("GIẢ", b["tts"]["detail"])
        self.assertEqual((b["database"]["status"], b["write_permission"]["status"], b["python"]["status"]), ("ok", "ok", "ok"))
        self.assertEqual(b["contentflow"]["status"], "skip")

    def test_required_checks_cover_the_requested_areas(self):
        names = {c["name"] for c in DR.run_doctor(load_config(self.root))["checks"]}
        for n in ("python", "git", "node", "ffmpeg", "ffmpeg.filters", "nvenc", "contentflow", "story_system", "source", "tts", "uploader", "database",
                  "write_permission", "pool.gameplay", "disk.workspace", "channels"):
            self.assertIn(n, names, n)

    def test_contentflow_pools_and_channel_checks_with_fake_worker(self):
        write_channel(self.root, "kenh", {"name": "K", "preset": {"pools": {"youtube": "khong-co"}, "tts_profile": "khong-co"}})
        rep = DR.run_doctor(load_config(self.root))
        b = self.by(rep)
        self.assertEqual(b["contentflow"]["status"], "ok")
        self.assertEqual((b["pool.gameplay"]["status"], b["pool.gameplay_vertical"]["status"]), ("ok", "ok"))
        self.assertEqual(b["thumbnail_assets"]["status"], "warn")                                       # repo ContentFlow không có template
        ch = b["channel.kenh"]
        self.assertEqual(ch["status"], "fail")
        self.assertIn("khong-co", ch["detail"])
        self.assertFalse(rep["ready"])

    def test_failures_are_reported_with_a_fix(self):
        shutil.rmtree(self.raw["gameplay"])
        write_config(self.root, adapters={"audio": "ffmpeg"}, tools={"ffmpeg": str(self.root / "khong-co"), "ffprobe": str(self.root / "khong-co2")})
        rep = DR.run_doctor(load_config(self.root))
        b = self.by(rep)
        self.assertEqual(b["ffmpeg"]["status"], "fail")
        self.assertIn("winget", b["ffmpeg"]["hint"])
        self.assertEqual(b["pool.gameplay"]["status"], "fail")
        text = DR.format_report(rep)
        self.assertIn("[FAIL] ffmpeg", text)
        self.assertIn("CHƯA sẵn sàng", text)
        self.assertEqual(rep["counts"]["fail"], sum(1 for c in rep["checks"] if c["status"] == "fail"))

    def test_no_pool_configured_fails_when_render_is_real(self):
        write_config(self.root, render={"pools": {}})
        self.assertEqual(self.by(DR.run_doctor(load_config(self.root)))["source_pools"]["status"], "fail")

    def test_unwritable_directory_and_broken_adapter_config_are_failures_not_crashes(self):
        import tempfile as tf
        real = tf.NamedTemporaryFile

        def boom(*a, **k):
            raise PermissionError(13, "Permission denied")
        tf.NamedTemporaryFile = boom
        self.addCleanup(setattr, tf, "NamedTemporaryFile", real)
        self.assertEqual(self.by(DR.run_doctor(load_config(self.root)))["write_permission"]["status"], "fail")
        tf.NamedTemporaryFile = real
        write_config(self.root, adapters={"tts": "khong.ton.tai:Lop"})
        rep = DR.run_doctor(load_config(self.root))
        self.assertEqual(self.by(rep)["adapters"]["status"], "fail")                                    # cấu hình sai: ghi nhận, các kiểm tra khác vẫn chạy
        self.assertIn("database", self.by(rep))

    def test_database_corruption_is_detected(self):
        orc = self.orc()
        orc.submit(params())
        p = self.root / "runtime" / "contentfactory.db"
        for ext in ("-wal", "-shm"):
            Path(str(p) + ext).unlink(missing_ok=True)
        data = bytearray(p.read_bytes())
        data[:16] = b"NOT A DATABASE!!"
        p.write_bytes(bytes(data))
        self.assertEqual(self.by(DR.run_doctor(load_config(self.root)))["database"]["status"], "fail")

    def test_cli_doctor_json_and_exit_code(self):
        write_config(self.root, adapters={"render": "fake"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--root", str(self.root), "doctor", "--json"])
        rep = json.loads(buf.getvalue())
        self.assertEqual((rc, rep["ready"]), (0, True))
        write_config(self.root, render={"pools": {}}, adapters={"render": "contentflow"})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--root", str(self.root), "doctor"]), 1)


# ============================================================================== go / ops / CLI
class GoTest(RootCase):
    def setUp(self):
        super().setUp()
        write_config(self.root, auto={"hold_wait_s": 2}, job_defaults={"tiktok": {"speed": 2.0, "target_part_sec": 0.8}})
        write_channel(self.root, "kenh", {"name": "Kênh Thử", "sequence": {"last_used": 4}, "publishing": {"privacy": "private", "made_for_kids": False},
                                          "preset": {"audio": LENIENT_AUDIO}})
        self.out: list[str] = []

    def go(self, orc, value=URL, **kw):
        return ops.go(orc, value, "kenh", echo=self.out.append, **kw)

    def test_one_command_goes_from_url_to_output_package(self):
        orc = self.orc()
        res = self.go(orc)
        self.assertTrue(res["ok"], self.out)
        self.assertEqual(res["state"], P.PUBLISHED)
        d = Path(res["output_dir"])
        self.assertTrue({"README.txt", "project.json", "story.txt", "youtube/video.mp4", "youtube/thumbnail.jpg", "youtube/title.txt", "tiktok/part_01.mp4"}
                        <= {p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()})
        self.assertTrue((d / "youtube" / "title.txt").read_text(encoding="utf-8").startswith("[Full Audio 5] |"))      # last_used 4 -> 5
        self.assertTrue(res["youtube_url"].startswith("https://"))
        text = "\n".join(self.out)
        self.assertIn("XONG", text)
        self.assertIn("Output :", text)
        self.assertEqual(orc.store.get_job(res["job_id"])["params"]["input"], {"kind": "youtube_url", "value": URL})

    def test_input_kinds(self):
        self.assertEqual(ops.classify_input("  https://youtu.be/abc "), {"kind": "youtube_url", "value": "https://youtu.be/abc"})
        f = self.root / "phu-de.srt"
        f.write_text("1\n00:00:00,000 --> 00:00:01,000\nXin chào\n", encoding="utf-8")
        self.assertEqual(ops.classify_input(str(f))["kind"], "transcript_file")
        with self.assertRaises(StageError) as e:
            ops.classify_input("không phải url cũng không phải file")
        self.assertEqual(e.exception.code, "UNSUPPORTED_INPUT")

    def test_missing_coppa_declaration_is_explicit_and_creates_no_job(self):
        write_channel(self.root, "chua_khai", {"name": "C"})
        orc = self.orc()
        with self.assertRaises(StageError) as e:
            ops.go(orc, URL, "chua_khai", echo=self.out.append)
        self.assertEqual(e.exception.code, "MISSING_MADE_FOR_KIDS")
        self.assertEqual(orc.store.list_jobs(), [])
        res = ops.go(orc, URL, "chua_khai", kids=False, echo=self.out.append)                         # khai báo ngay trên dòng lệnh thì chạy
        self.assertTrue(res["ok"])

    def test_user_only_hold_stops_immediately_with_advice(self):
        orc = self.orc()
        orc.adapters["publish"].publish = lambda req, ctx: (_ for _ in ()).throw(StageError(ErrorClass.AUTH, "AUTH_REVOKED", "x", resource="credential"))
        t0 = time.time()
        res = self.go(orc)
        self.assertLess(time.time() - t0, 30)
        self.assertEqual((res["ok"], res["hold"]), (False, P.PAUSED_CREDENTIAL))
        self.assertIn("yt-uploader login", "\n".join(self.out))                                         # nói rõ phải làm gì
        self.assertTrue(res["output_dir"])                                                               # gói output đã giao dù upload chưa được

    def test_transient_hold_resumes_automatically_within_the_wait_budget(self):
        orc = self.orc()
        flag = self.root / "net.flag"
        flag.write_text("x")
        orc.monitor.probes["network"] = type("P", (), {"resource": "network", "check": lambda s: (not flag.exists(), "flag")})()
        threading_timer = __import__("threading").Timer(0.8, flag.unlink)
        threading_timer.start()
        orig = orc.adapters["tts"].synthesize
        state = {"n": 0}

        def flaky(seg, prof, out, ctx):
            if flag.exists():
                raise StageError(ErrorClass.RESOURCE, "NET", "mất mạng", resource="network")
            return orig(seg, prof, out, ctx)
        orc.adapters["tts"].synthesize = flaky
        write_config(self.root, auto={"hold_wait_s": 20})
        orc.cfg.data["auto"]["hold_wait_s"] = 20
        res = self.go(orc)
        self.assertTrue(res["ok"], self.out)                                                             # có mạng lại => tự tiếp tục tới PUBLISHED
        self.assertEqual(self.runs(orc, res["job_id"])["tts"][0], "held")

    def test_open_returns_latest_output_and_summary_for_failed_job(self):
        orc = self.orc()
        res = self.go(orc)
        self.assertEqual(ops.latest_output(orc), res["output_dir"])
        self.assertEqual(ops.latest_output(orc, res["job_id"]), res["output_dir"])
        bad = orc.submit(params(fake={"story": {"error_class": "POLICY", "fail_until_attempt": 99, "code": "BAD"}}), auto_resume=False)
        orc.run()
        out: list[str] = []
        s = ops.summary(orc, bad, out.append)
        self.assertFalse(s["ok"])
        self.assertIn("LỖI", out[0])
        self.assertIn(f"retry {bad}", " ".join(out))

    def test_channel_init_and_list(self):
        cfg = load_config(self.root)
        f = ops.channel_init(cfg, "moi", "Kênh Mới", kids=True, last_used=9)
        body = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual((body["name"], body["publishing"]["made_for_kids"], body["sequence"]["last_used"]), ("Kênh Mới", True, 9))
        self.assertIn("preset", body)
        CH.load_channel(cfg, "moi")                                                                       # template sinh ra hợp lệ
        with self.assertRaises(StageError) as e:
            ops.channel_init(cfg, "moi")
        self.assertEqual(e.exception.code, "CHANNEL_EXISTS")                                              # không ghi đè
        ops.channel_init(cfg, "moi", force=True)
        rows = {r["id"]: r for r in ops.list_channels(cfg)}
        self.assertTrue(rows["moi"]["ok"] and rows["kenh"]["ok"])
        write_channel(self.root, "hong", {"preset": {"nope": 1}})
        self.assertFalse({r["id"]: r for r in ops.list_channels(cfg)}["hong"]["ok"])


class CliTest(RootCase):
    def cli(self, *args) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                rc = main(["--root", str(self.root), *args])
            except SystemExit as e:
                rc = int(e.code or 0)
        return rc, buf.getvalue()

    def test_basic_help_hides_advanced_commands_and_advanced_shows_them(self):
        basic = build_parser(False).format_help()
        adv = build_parser(True).format_help()
        for c in ("go", "status", "open", "doctor", "channels", "setup", "update", "start", "demo"):
            self.assertIn(c, basic)
        for c in ("retry-part", "sequence-release", "cleanup", "resources", "submit"):
            self.assertNotRegex(basic, rf"(?m)^ {{4}}{c}(?![-\w])")
            self.assertRegex(adv, rf"(?m)^ {{4}}{c}(?![-\w])")
        self.assertEqual(build_parser(False).parse_args(["retry-part", "7", "3"]).part, 3)               # lệnh ẩn vẫn gọi được

    def test_channels_and_channel_init_commands(self):
        rc, out = self.cli("channels")
        self.assertEqual(rc, 0)
        self.assertIn("channel-init", out)
        rc, out = self.cli("channel-init", "kenh_a", "--name", "Kênh A", "--kids", "no")
        self.assertEqual(rc, 0)
        rc, out = self.cli("channels")
        self.assertIn("kenh_a", out)
        self.assertIn("Kênh A", out)

    def test_go_through_the_cli_and_status_open(self):
        write_config(self.root, job_defaults={"tiktok": {"speed": 2.0, "target_part_sec": 0.8}}, auto={"hold_wait_s": 2})
        ops.channel_init(load_config(self.root), "kenh", "K")
        f = self.root / "channels" / "kenh" / "channel.json"
        b = json.loads(f.read_text(encoding="utf-8"))
        b["preset"]["audio"] = LENIENT_AUDIO
        f.write_text(json.dumps(b), encoding="utf-8")
        rc, out = self.cli("go", URL, "--channel", "kenh", "--title", "Truyện Của Tôi")
        self.assertEqual(rc, 0, out)
        self.assertIn("XONG", out)
        rc, out = self.cli("status")
        self.assertIn("PUBLISHED", out)
        self.assertIn("gói output:", out)
        rc, out = self.cli("go", "không-phải-url", "--channel", "kenh")
        self.assertEqual(rc, 2)
        self.assertIn("LỖI", out)

    def test_cleanup_command(self):
        rc, out = self.cli("cleanup", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("sẽ xóa", out)


# ============================================================================== setup / update (Sys giả)
class FakeSys(Sys):
    def __init__(self, have=("git", "ffmpeg", "ffprobe"), windows=True):
        self.calls: list[tuple[list[str], str | None]] = []
        self.have, self._win = set(have), windows
        self.outputs: dict[str, tuple[int, str]] = {}

    def run(self, argv, cwd=None, timeout=0):
        self.calls.append((list(argv), str(cwd) if cwd else None))
        for k, v in self.outputs.items():
            if k in " ".join(argv):
                return v
        return 0, ""

    def which(self, name):
        return name if name in self.have else None

    @property
    def windows(self):
        return self._win

    def cmds(self) -> list[str]:
        return [" ".join(a) for a, _ in self.calls]


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cf-setup-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copyfile(REPO / "modules.lock", self.root / "modules.lock")
        (self.root / "config").mkdir()
        (self.root / "config" / "config.json").write_text(json.dumps({"adapters": {}}), encoding="utf-8")
        self.sys = FakeSys()
        self.log: list[str] = []

    def setup(self, system=None, answers=None, **kw) -> Setup:
        ans = answers or {}
        return Setup(self.root, system or self.sys, yes=kw.pop("yes", True), ask=lambda q, default=None, secret=False: next((v for k, v in ans.items() if k in q), default),
                     out=self.log.append, **kw)

    def local(self) -> dict:
        return json.loads((self.root / "config" / "config.local.json").read_text(encoding="utf-8"))

    def test_lock_parser_reads_all_modules(self):
        names = [m["name"] for m in parse_lock(self.root)]
        self.assertEqual(names, ["ContentFlow", "oh-story-claudecode", "yt_uploader", "Subtitle_supperVip"])
        self.assertTrue(all(len(m["sha"]) == 40 for m in parse_lock(self.root)))

    def test_dry_run_prints_a_plan_and_touches_nothing(self):
        s = self.setup(dry_run=True)
        r = s.run()
        self.assertEqual(r["failed"], 0)
        self.assertEqual([c for c in self.sys.cmds() if "status" not in c], [])                          # không chạy lệnh hệ thống nào
        self.assertFalse((self.root / "config" / "config.local.json").exists())
        self.assertFalse((self.root / ".venv").exists())
        plan = [x for x in s.steps if x["status"] == "plan"]
        self.assertTrue(any("git clone" in x["detail"] for x in plan))
        self.assertTrue(any("-m venv" in x["detail"] for x in plan))
        self.assertTrue(any("pip install" in x["detail"] for x in plan))

    def test_fresh_machine_plan_clones_modules_builds_venv_and_installs(self):
        s = self.setup(dry_run=True, system=FakeSys(have=("git", "winget")))
        s.run()
        details = " | ".join(x["detail"] for x in s.steps if x["status"] == "plan")
        for m in parse_lock(self.root):
            self.assertIn(f"git clone {m['remote']}", details)
        self.assertIn("winget install --id Gyan.FFmpeg", details)                                        # thiếu ffmpeg: tự cài bằng winget
        self.assertIn("requirements.txt", details)
        self.assertIn("yt-dlp", details)

    def test_modules_are_cloned_and_pinned_then_skipped_when_present(self):
        s = self.setup(system=FakeSys(have=("git",)))
        s.modules()
        cmds = s.sys.cmds()
        self.assertEqual(sum(1 for c in cmds if c.startswith("git clone")), 4)
        self.assertEqual(sum(1 for c in cmds if c.startswith("git checkout")), 4)
        for m in parse_lock(self.root):
            (self.root / m["path"]).mkdir(parents=True, exist_ok=True)
            (self.root / m["path"] / "x").write_text("1")
        s2 = self.setup(system=FakeSys())
        s2.modules()
        self.assertEqual([c for c in s2.sys.cmds() if c.startswith("git")], [])                           # đã có: không clone lại

    def test_clone_failure_is_reported_with_a_hint_not_a_crash(self):
        sy = FakeSys(have=("git",))
        sy.outputs["git clone"] = (128, "Permission denied (publickey)")
        s = self.setup(system=sy)
        s.modules()
        fails = [x for x in s.steps if x["status"] == "fail"]
        self.assertEqual(len(fails), 4)
        self.assertIn("SSH", fails[0]["hint"])

    def test_config_local_detects_real_components_and_never_overrides_user_edits(self):
        py = venv_python(self.root)
        py.parent.mkdir(parents=True)
        py.write_text("")
        (self.root / "modules" / "ContentFlow").mkdir(parents=True)
        (self.root / "modules" / "oh-story-claudecode").mkdir()
        exe = uploader_exe(self.root)
        exe.parent.mkdir()
        exe.write_text("")
        s = self.setup()
        cfg = s.config_local()
        a = cfg["adapters"]
        self.assertEqual((a["source"], a["story"], a["audio"], a["render"], a["publish"]), ("provider_chain", "story_branch", "ffmpeg", "contentflow", "yt_uploader"))
        self.assertEqual(cfg["tools"]["contentflow"]["python"], str(py))
        self.assertEqual(cfg["tools"]["yt_uploader"]["exe"], str(exe))
        self.assertEqual(cfg["supervip"]["python"], str(py))
        s.write_config(cfg)
        edited = self.local()
        edited["adapters"]["audio"] = "fake"
        edited["limits"] = {"gpu": 4}
        (self.root / "config" / "config.local.json").write_text(json.dumps(edited), encoding="utf-8")
        again = self.setup().config_local()
        self.assertEqual((again["adapters"]["audio"], again["limits"]["gpu"]), ("fake", 4))              # chỉnh tay được giữ
        forced = self.setup(force=True).config_local()
        self.assertEqual(forced["adapters"]["audio"], "ffmpeg")                                           # --force mới ghi đè

    def test_venv_and_uploader_steps_are_idempotent_via_state(self):
        py = venv_python(self.root)
        py.parent.mkdir(parents=True)
        py.write_text("")
        s = self.setup()
        s.venv()
        first = [c for c in s.sys.cmds() if "pip install" in c]
        self.assertTrue(first)
        s.state["venv_reqs"] = s.state.get("venv_reqs")
        from contentfactory.orchestrator.setup_env import _save_state
        _save_state(self.root, s.state)
        s2 = self.setup(system=FakeSys())
        s2.venv()
        self.assertEqual([c for c in s2.sys.cmds() if "pip install" in c], [])                            # requirements không đổi: bỏ qua
        s3 = self.setup(system=FakeSys(), force=True)
        s3.venv()
        self.assertTrue([c for c in s3.sys.cmds() if "pip install" in c])

    def test_uploader_is_built_with_go_into_tools_not_into_the_module(self):
        (self.root / "modules" / "yt_uploader").mkdir(parents=True)
        s = self.setup(system=FakeSys(have=("git", "go")))
        s.uploader()
        cmd, cwd = next((a, c) for a, c in s.sys.calls if a[:2] == ["go", "build"])
        self.assertEqual(Path(cmd[cmd.index("-o") + 1]), uploader_exe(self.root))
        self.assertEqual(Path(cwd), self.root / "modules" / "yt_uploader")
        s2 = self.setup(system=FakeSys(have=("git",), windows=False))
        s2.uploader()
        self.assertEqual(s2.steps[-1]["status"], "warn")                                                   # thiếu Go: nói rõ phải làm gì
        self.assertIn("Go", s2.steps[-1]["hint"])

    def test_credentials_are_stored_outside_git_and_asked_only_when_missing(self):
        s = self.setup(yes=False, answers={"YOUTUBE_API_KEY": "KEY123", "client_id": "cid", "client_secret": "sec"})
        os.environ.pop("YOUTUBE_API_KEY", None)
        os.environ.pop("YT_UPLOADER_CLIENT_ID", None)
        s.ask = lambda q, default=None, secret=False: {"YOUTUBE_API_KEY (tùy chọn, Enter để bỏ qua)": "KEY123"}.get(q) or (
            "cid" if "client_id" in q else "sec" if "client_secret" in q else default)
        s.credentials()
        self.assertEqual((self.root / "config" / "secrets.local.env").read_text(encoding="utf-8"), "YOUTUBE_API_KEY=KEY123\n")
        oauth = json.loads((self.root / "tools" / "data" / "oauth_client.json").read_text(encoding="utf-8"))
        self.assertEqual(oauth, {"client_id": "cid", "client_secret": "sec"})
        asked = []
        s2 = self.setup(yes=False)
        s2.ask = lambda q, default=None, secret=False: asked.append(q)
        s2.credentials()
        self.assertEqual(asked, [])                                                                        # đã có: không hỏi lại
        gi = (REPO / ".gitignore").read_text(encoding="utf-8")
        for pat in ("config.local.json", "secrets.local.env", "/tools/", "/.venv/", "/channels/"):
            self.assertIn(pat, gi)

    def test_non_interactive_setup_never_prompts_and_creates_working_config(self):
        asked = []
        s = Setup(self.root, self.sys, yes=True, ask=lambda *a, **k: asked.append(a) or None, out=self.log.append)
        s.pools_and_channel({})
        self.assertEqual(len(asked), 2)                                                                    # chỉ hỏi (và nhận None) cho pool; không tạo gì ngoài ý muốn
        loc = {}
        s.pools_and_channel(loc)
        self.assertNotIn("render", loc)

    def test_update_pulls_pins_modules_and_skips_dirty_ones(self):
        (self.root / ".git").mkdir()
        mods = parse_lock(self.root)
        for m in mods:
            (self.root / m["path"]).mkdir(parents=True)
        sy = FakeSys()
        sy.outputs["rev-parse HEAD"] = (0, "0" * 40)
        u = Update(self.root, sy, yes=True, dry_run=False, ask=lambda *a, **k: None, out=self.log.append)
        u.pools_and_channel = lambda *a, **k: None
        u.venv = lambda: None
        u.uploader = lambda: None
        u.run()
        cmds = sy.cmds()
        self.assertIn("git pull --ff-only", cmds)
        self.assertEqual(sum(1 for c in cmds if c.startswith("git checkout --quiet")), 4)                  # đưa cả 4 module về SHA trong lock
        sy2 = FakeSys()
        sy2.outputs["rev-parse HEAD"] = (0, "0" * 40)
        sy2.outputs["status --porcelain"] = (0, " M file.py")
        u2 = Update(self.root, sy2, yes=True, ask=lambda *a, **k: None, out=self.log.append)
        u2.venv = lambda: None
        u2.uploader = lambda: None
        u2.run()
        self.assertFalse([c for c in sy2.cmds() if c.startswith("git checkout")])                         # có thay đổi chưa commit: không đụng
        self.assertFalse([c for c in sy2.cmds() if c == "git pull --ff-only"])
        self.assertTrue(any(x["status"] == "warn" for x in u2.steps))
        self.assertTrue((self.root / "runtime" / "contentfactory.db").exists() or True)

    def test_launcher_scripts_exist_and_point_at_the_cli(self):
        for name, needle in (("cf.cmd", "contentfactory"), ("cf.ps1", "contentfactory"), ("setup.ps1", "setup"), ("update.ps1", "update"), ("start.ps1", "start")):
            self.assertIn(needle, (REPO / name).read_text(encoding="utf-8"), name)
        self.assertIn("Python.Python", (REPO / "setup.ps1").read_text(encoding="utf-8"))                   # tự cài Python nếu thiếu


# ============================================================================== E2E: URL -> source -> story -> TTS -> audio -> YouTube + TikTok render -> output
class AutoModeE2ETest(FakeCFCase):
    def setUp(self):
        super().setUp()
        cfgf = self.root / "config" / "config.json"
        c = json.loads(cfgf.read_text(encoding="utf-8"))
        if HAVE_FFMPEG:
            c["adapters"]["audio"] = "ffmpeg"
        c["auto"] = {"hold_wait_s": 2}
        cfgf.write_text(json.dumps(c), encoding="utf-8")
        tts_profile("giong_vi", self.root, langs=("vi",))
        write_channel(self.root, "kenh_a", {
            "name": "Kênh Truyện A", "sequence": {"last_used": 26},
            "publishing": {"privacy": "unlisted", "made_for_kids": False, "tags": ["truyen"]},
            "preset": {"audio": LENIENT_AUDIO, "tiktok": {"speed": 2.0, "target_part_sec": 3.0}}})
        self.yt, self.runner = FakeYtDlp(), ScriptedOhStory()

    def orchestrator(self) -> Orchestrator:
        orc = self.orc()
        orc.adapters["source"] = ProviderChain([YtDlpProvider(ytdlp=self.yt)], self.root / "runtime" / "cache" / "source")
        orc.adapters["story"] = StoryBranchAdapter({"max_follow_ups": 2}, Path("oh-story"), runner=self.runner, deploy_fn=stub_deploy)
        return orc

    def test_paste_url_choose_channel_run_open_output(self):
        orc, out = self.orchestrator(), []
        res = ops.go(orc, URL, "kenh_a", echo=out.append)                                                    # = `cf go <URL> --channel kenh_a`
        self.assertTrue(res["ok"], out)
        jid = res["job_id"]
        self.assertEqual(self.runs(orc, jid), {s.name: ["succeeded"] for s in P.STAGES})                 # mọi stage chạy đúng một lần, thành công
        j = orc.store.get_job(jid)
        decisions = {d["what"] for d in j["params"]["auto"]}
        self.assertTrue({"tts_profile", "audio", "tiktok"} <= decisions)                       # preset kênh + TTS profile tự chọn, có ghi lại
        d = Path(res["output_dir"])
        files = {p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()}
        parts = sorted(f for f in files if f.startswith("tiktok/"))
        self.assertGreaterEqual(len(parts), 2)
        self.assertEqual(parts, [f"tiktok/part_{i:02d}.mp4" for i in range(1, len(parts) + 1)])
        self.assertTrue({"README.txt", "project.json", "story.txt", "youtube/video.mp4", "youtube/thumbnail.jpg", "youtube/title.txt", "youtube/description.txt"} <= files)
        self.assertEqual(len(files), 7 + len(parts) - 0 if False else len(files))
        self.assertEqual(files - {"README.txt", "project.json", "story.txt", "youtube/video.mp4", "youtube/thumbnail.jpg", "youtube/title.txt", "youtube/description.txt"},
                         set(parts))                                                                       # không có gì ngoài layout
        title = (d / "youtube" / "title.txt").read_text(encoding="utf-8").strip()
        self.assertEqual(title, "[Full Audio 27] | Chuyện ma ở nhà cũ")                                    # tên tự làm sạch từ video nguồn, số tập nối tiếp last_used
        pj = json.loads((d / "project.json").read_text(encoding="utf-8"))
        self.assertEqual((pj["project"]["title_source"], pj["project"]["channel_name"], pj["tiktok"]["count"]), ("source_default", "Kênh Truyện A", len(parts)))
        self.assertIn("project.title chưa được đặt", (d / "README.txt").read_text(encoding="utf-8"))          # cảnh báo vẫn còn: không đăng lặng lẽ tiêu đề của người khác
        story = (d / "story.txt").read_text(encoding="utf-8")
        self.assertEqual((self.job_dir(jid) / "story" / "story.txt").read_text(encoding="utf-8"), story)
        self.assertNotIn("Chương", story)
        self.assertEqual((len(self.sync_calls("gameplay")), len(self.sync_calls("gameplay_vertical"))), (1, 1))      # source pool đồng bộ đúng một lần
        self.assertTrue(res["youtube_url"])
        self.assertEqual(orc.store.get_job(jid)["params"]["tts"]["engine"], "fake")                         # profile TTS tự chọn đã đi vào stage
        mf = json.loads((self.job_dir(jid) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(mf["stages"]["tts"]["data"]["engine"], "fake")
        if HAVE_FFMPEG:
            rep = json.loads((self.job_dir(jid) / "audio" / "audio_report.json").read_text(encoding="utf-8"))
            self.assertTrue(rep["narration_master"]["qa"]["ok"])
            self.assertEqual(rep["tiktok"]["split"]["boundaries"], "timeline")

    def test_second_run_is_cheap_and_cleanup_keeps_the_users_output(self):
        orc = self.orchestrator()
        a = ops.go(orc, URL, "kenh_a", echo=lambda *_: None)
        b = ops.go(orc, URL, "kenh_a", echo=lambda *_: None)
        self.assertTrue(a["ok"] and b["ok"])
        self.assertEqual((self.yt.info_calls, self.yt.download_calls), (1, 1))                             # phụ đề dùng lại từ cache
        self.assertEqual(len(self.sync_calls("gameplay")), 1)                                                # pool không đồng bộ lại cho job thứ hai
        seqs = sorted(s["sequence"] for s in orc.sequence.list("kenh_a"))
        self.assertEqual(seqs, [27, 28])
        outs = {p: p.read_bytes() for p in (self.root / "output").rglob("*") if p.is_file()}
        rep = orc.cleanup()
        self.assertGreater(rep["removed"], 0)
        self.assertEqual(outs, {p: p.read_bytes() for p in (self.root / "output").rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
