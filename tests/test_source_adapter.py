"""SourceAdapter (ProviderChain) + providers + Transcript Processor + tích hợp Subtitle_supperVip."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from contentfactory.adapters.story_branch import StoryBranchAdapter
from contentfactory.contracts import ErrorClass, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.runner import Orchestrator
from contentfactory.source import transcript as transcript_mod
from contentfactory.source.chain import ProviderChain
from contentfactory.source.providers import (LocalSubtitleProvider, PlainTextProvider, SubtitleSupperVipProvider,
                                             YtDlpProvider)
from contentfactory.source.transcript import TranscriptProcessor
from contentfactory.story.validate import validate_story_text
from tests.fakes import FIXTURES, URL, FakeYtDlp, ScriptedOhStory, StubProvider, make_ctx, stub_deploy
from tests.support import RootCase, params

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "modules" / "Subtitle_supperVip" / "backend"
STUBS = Path(__file__).resolve().parent / "stubs"
META = {"title": "Chuyện ma ở nhà cũ", "published_at": "2026-01-01T00:00:00", "duration_seconds": 13, "video_type": "video"}


def fake_bridge(calls: list, meta=META):
    """Giả lập đầu ra của supervip_bridge.py: ghi snippet JSON y như `serialize(snippets, "json")` của module."""
    def run(args):
        calls.append(args)
        if args[0] == "health":
            return {"ok": True}
        out = Path(args[args.index("--out") + 1])
        out.write_bytes((FIXTURES / "supervip_snippets.json").read_bytes())
        return {"ok": True, "pass": 0, "snippets": 6, "metadata": meta, "metadata_error": None,
                "track": {"language": "Vietnamese", "language_code": "vi", "is_generated": False, "translated": False}}
    return run


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-sa-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def acquire(self, chain, job="000001", src=None, **params_):
        ctx = make_ctx(self.tmp, "source", params_, job_id=job)
        return chain.acquire(src or {"kind": "youtube_url", "value": URL}, ctx.stage_dir, ctx), ctx

    def chain(self, *providers, cache=True, **prefs):
        return ProviderChain(list(providers), self.tmp / "cache" if cache else None, prefs or None)


# ---------------------------------------------------------------------------------------------------
class SourceResultTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.calls: list = []
        self.provider = SubtitleSupperVipProvider({"python": sys.executable, "backend_dir": str(BACKEND)}, REPO,
                                                  runner=fake_bridge(self.calls))

    def test_youtube_url_gives_a_complete_source_result(self):
        res, _ = self.acquire(self.chain(self.provider))
        self.assertEqual((res["status"], res["error"]), ("ok", None))
        self.assertEqual((res["source_url"], res["source_type"], res["provider"], res["video_id"]),
                         (URL, "youtube", "supervip", "abcdefghijk"))
        self.assertEqual((res["title"], res["language"], res["subtitle_format"], res["subtitle_kind"], res["has_timestamps"]),
                         ("Chuyện ma ở nhà cũ", "vi", "json", "manual", True))
        self.assertEqual(res["metadata"]["duration_seconds"], 13)
        self.assertTrue(res["raw_subtitle_path"].is_file())
        self.assertEqual(res["attempts"], [{"provider": "supervip", "status": "ok", "seconds": res["attempts"][0]["seconds"]}])

    def test_bridge_is_asked_for_manual_first_then_auto_then_any_manual(self):
        self.acquire(self.chain(self.provider))
        args = next(c for c in self.calls if c[0] == "fetch")
        passes = json.loads(args[args.index("--passes") + 1])
        self.assertEqual([p["preference"] for p in passes], ["manual", "auto", "manual"])   # "ưu tiên sub có sẵn"
        self.assertFalse(any(p["allow_translation"] for p in passes))                       # không dịch máy của YouTube
        self.assertIn("--with-metadata", args)

    def test_raw_subtitle_is_kept_byte_for_byte(self):
        res, _ = self.acquire(self.chain(self.provider))
        self.assertEqual(res["raw_subtitle_path"].read_bytes(), (FIXTURES / "supervip_snippets.json").read_bytes())
        self.assertEqual(res["raw_subtitle_path"].name, "subtitle_raw.json")

    def test_timestamps_survive_in_structured_and_clean_has_none(self):
        res, ctx = self.acquire(self.chain(self.provider))
        out = TranscriptProcessor().process(res["raw_subtitle_path"], res["subtitle_format"], ctx.stage_dir, ctx)
        s = json.loads(out["structured"].read_text(encoding="utf-8"))
        self.assertTrue(all(c["start"] is not None and c["end"] > c["start"] and "gap_before" in c for c in s["cues"]))
        self.assertTrue(all(x["start"] is not None and x["end"] is not None for x in s["sentences"]))
        clean = out["clean"].read_text(encoding="utf-8")
        self.assertNotRegex(clean, r"\d{1,2}:\d{2}|-->")
        self.assertEqual(out["clean"].name, "transcript_clean.txt")
        self.assertEqual(out["structured"].name, "transcript_structured.json")

    def test_split_captions_are_rejoined_and_duplicates_and_sound_cues_removed(self):
        res, ctx = self.acquire(self.chain(self.provider))
        out = TranscriptProcessor().process(res["raw_subtitle_path"], "json", ctx.stage_dir, ctx)
        self.assertEqual(out["clean"].read_text(encoding="utf-8"),
                         "Hôm qua tôi đi chợ và gặp một người bạn cũ ở cổng. Ông ấy hỏi thăm tôi rất lâu, rồi mời tôi "
                         "đi uống cà phê. Tôi từ chối vì đang vội.\n")
        s = json.loads(out["structured"].read_text(encoding="utf-8"))
        self.assertEqual(s["stats"]["cues"], 5)        # 6 snippet -> bỏ "[Âm nhạc]" -> 5 cue, dòng "rồi mời tôi" lặp chỉ còn một
        self.assertEqual(s["stats"]["sentences"], 3)

    def test_formats_srt_vtt_json_txt_all_reconstruct(self):
        ctx = make_ctx(self.tmp, "source")
        for name, fmt in (("manual_split.srt", "srt"), ("gaps.vtt", "vtt"), ("supervip_snippets.json", "json")):
            raw = self.tmp / name
            shutil.copyfile(FIXTURES / name, raw)
            out = TranscriptProcessor().process(raw, fmt, self.tmp / fmt, ctx)
            self.assertGreater(out["stats"]["sentences"], 0, fmt)
            self.assertLess(out["stats"]["sentences"], out["stats"]["cues"] + 1)
        bad = self.tmp / "bad.json"
        bad.write_text("{không phải mảng}", encoding="utf-8")
        with self.assertRaises(StageError) as cm:
            TranscriptProcessor().process(bad, "json", self.tmp / "bad", ctx)
        self.assertEqual(cm.exception.code, "BAD_SUBTITLE_FILE")


# ---------------------------------------------------------------------------------------------------
@unittest.skipUnless((BACKEND / "app" / "services" / "subtitles.py").is_file(), "thiếu modules/Subtitle_supperVip")
class RealBridgeTest(TmpCase):
    """Chạy bridge THẬT trên code THẬT của Subtitle_supperVip (app.services.subtitles); chỉ thay thư viện mạng bằng stub."""

    def provider(self):
        env = {"PYTHONPATH": str(STUBS)}
        return SubtitleSupperVipProvider({"python": sys.executable, "backend_dir": str(BACKEND), "env": env}, REPO)

    def fetch(self, video_id: str):
        p = self.provider()
        ctx = make_ctx(self.tmp, "source")
        src = {"kind": "youtube_url", "value": f"https://youtu.be/{video_id}"}
        return p.acquire(src, self.tmp / "w", ctx, {"languages": ["vi", "en"], "allow_translation": False})

    def test_manual_vietnamese_track_is_chosen_by_the_modules_own_logic(self):
        res = self.fetch("manualvi_01")
        self.assertEqual((res["language"], res["subtitle_kind"], res["subtitle_format"]), ("vi", "manual", "json"))
        snippets = json.loads(res["raw_subtitle_path"].read_text(encoding="utf-8"))
        self.assertEqual(snippets, [{"text": "Chào các bạn thân mến", "start": 0.0, "duration": 2.0}])   # đúng định dạng serialize() của module
        self.assertIn("YOUTUBE_API_KEY", res["metadata"]["metadata_error"])    # không có key: metadata là best-effort
        self.assertIsNone(res["title"])

    def test_falls_through_manual_then_auto_then_any_manual(self):
        self.assertEqual(self.fetch("autoonly_01")["subtitle_kind"], "auto")
        german = self.fetch("onlygerman1")                                      # không có vi/en: pass 3 lấy sub thủ công bất kỳ
        self.assertEqual((german["language"], german["subtitle_kind"]), ("de", "manual"))

    def test_module_exceptions_map_to_stage_errors(self):
        with self.assertRaises(StageError) as cm:
            self.fetch("nosubs_0001")
        self.assertEqual((cm.exception.error_class, cm.exception.code), (ErrorClass.POLICY, "NO_SUBTITLES"))
        with self.assertRaises(StageError) as cm:
            self.fetch("blocked_001")
        self.assertEqual((cm.exception.error_class, cm.exception.code), (ErrorClass.RESOURCE, "YOUTUBE_BLOCKED"))
        with self.assertRaises(StageError) as cm:
            self.fetch("unknown_vid")
        self.assertEqual(cm.exception.error_class, ErrorClass.TRANSIENT)      # lỗi lạ: retry như worker của module

    def test_module_state_is_never_touched(self):
        def snapshot():
            return sorted(str(p.relative_to(BACKEND.parent)) for p in BACKEND.parent.rglob("*")
                          if p.is_file() and ".git" not in p.parts and "node_modules" not in p.parts)
        before = snapshot()
        self.fetch("manualvi_01")
        self.assertEqual(snapshot(), before)                                    # không DB, không data/, không __pycache__ mới
        self.assertFalse(list(BACKEND.parent.rglob("*.db")))

    def test_health_runs_the_modules_code(self):
        h = self.provider().health()
        self.assertTrue(h["ok"], h)
        self.assertTrue(h["module_file"].endswith("subtitles.py"))


# ---------------------------------------------------------------------------------------------------
class FallbackTest(TmpCase):
    def test_fallback_provider_is_used_when_primary_is_blocked(self):
        primary = StubProvider("supervip", fail=[StageError(ErrorClass.RESOURCE, "YOUTUBE_BLOCKED", "IP bị chặn")])
        fallback = StubProvider("ytdlp")
        res, _ = self.acquire(self.chain(primary, fallback))
        self.assertEqual(res["provider"], "ytdlp")
        self.assertEqual([(a["provider"], a["status"]) for a in res["attempts"]], [("supervip", "error"), ("ytdlp", "ok")])
        self.assertEqual(res["attempts"][0]["code"], "YOUTUBE_BLOCKED")

    def test_unavailable_provider_is_skipped_without_being_called(self):
        down, up = StubProvider("supervip", available=False), StubProvider("ytdlp")
        res, _ = self.acquire(self.chain(down, up))
        self.assertEqual((down.calls, res["provider"]), (0, "ytdlp"))
        self.assertEqual(res["attempts"][0]["status"], "skipped")

    def test_definitive_error_does_not_fall_through(self):
        primary = StubProvider("supervip", fail=[StageError(ErrorClass.POLICY, "VIDEO_UNAVAILABLE", "private")])
        fallback = StubProvider("ytdlp")
        with self.assertRaises(StageError) as cm:
            self.acquire(self.chain(primary, fallback))
        self.assertEqual(cm.exception.code, "VIDEO_UNAVAILABLE")
        self.assertEqual(fallback.calls, 0)

    def test_aggregated_error_prefers_a_retryable_one(self):
        a = StubProvider("supervip", fail=[StageError(ErrorClass.POLICY, "NO_SUBTITLES", "x")])
        b = StubProvider("ytdlp", fail=[StageError(ErrorClass.TRANSIENT, "YTDLP_FAILED", "mạng")])
        with self.assertRaises(StageError) as cm:
            self.acquire(self.chain(a, b))
        self.assertEqual((cm.exception.error_class, cm.exception.code), (ErrorClass.TRANSIENT, "YTDLP_FAILED"))
        self.assertEqual(len(cm.exception.detail["attempts"]), 2)
        a2 = StubProvider("supervip", fail=[StageError(ErrorClass.POLICY, "NO_SUBTITLES", "x")])
        with self.assertRaises(StageError) as cm:
            self.acquire(self.chain(a2, StubProvider("ytdlp", fail=[StageError(ErrorClass.POLICY, "LANGUAGE_UNAVAILABLE", "y")])), job="000002")
        self.assertEqual(cm.exception.code, "NO_SUBTITLES")                     # cùng loại: giữ lỗi của provider chính

    def test_no_provider_available_is_a_resource_error(self):
        with self.assertRaises(StageError) as cm:
            self.acquire(self.chain(StubProvider("supervip", available=False)))
        self.assertEqual((cm.exception.error_class, cm.exception.code), (ErrorClass.RESOURCE, "NO_SOURCE_PROVIDER"))

    def test_missing_title_is_filled_in_by_another_provider_else_falls_back_to_video_id(self):
        res, _ = self.acquire(self.chain(StubProvider("supervip", title=None), StubProvider("ytdlp", title="Tiêu đề từ yt-dlp")))
        self.assertEqual((res["title"], res["metadata"]["title_from"]), ("Tiêu đề từ yt-dlp", "ytdlp"))
        res2, _ = self.acquire(self.chain(StubProvider("supervip", title=None), cache=False), job="000002")
        self.assertEqual((res2["title"], res2["metadata"]["title_from"]), ("abcdefghijk", "fallback"))

    def test_ytdlp_provider_keeps_the_phase2_downloader_working(self):
        yt = FakeYtDlp()
        res, _ = self.acquire(self.chain(YtDlpProvider(ytdlp=yt)))
        self.assertEqual((res["provider"], res["subtitle_format"], res["subtitle_kind"], res["title"]),
                         ("ytdlp", "srt", "manual", "Chuyện ma ở nhà cũ"))
        self.assertEqual(res["raw_subtitle_path"].read_bytes(), (FIXTURES / "manual_split.srt").read_bytes())

    def test_ytdlp_provider_prefers_manual_and_uses_auto_only_without_manual_track(self):
        res, _ = self.acquire(self.chain(YtDlpProvider(ytdlp=FakeYtDlp("auto_rolling.vtt", manual=False))))
        self.assertEqual((res["subtitle_kind"], res["subtitle_format"], res["language"]), ("auto", "vtt", "vi"))
        out = TranscriptProcessor().process(res["raw_subtitle_path"], "vtt", self.tmp / "t", make_ctx(self.tmp, "source"))
        self.assertEqual(out["stats"]["cues"], 4)                     # auto-caption rolling được khử trùng lặp

    def test_ytdlp_provider_reports_missing_subtitles_and_non_youtube_urls(self):
        class NoSubs(FakeYtDlp):
            def info(self, url):
                return {"id": "abcdefghijk", "title": "x", "subtitles": {}, "automatic_captions": {}}
        with self.assertRaises(StageError) as cm:
            self.acquire(self.chain(YtDlpProvider(ytdlp=NoSubs())))
        self.assertEqual((cm.exception.error_class, cm.exception.code), (ErrorClass.POLICY, "NO_SUBTITLES"))
        with self.assertRaises(StageError) as cm:
            self.acquire(self.chain(YtDlpProvider(ytdlp=FakeYtDlp())), src={"kind": "youtube_url", "value": "https://vimeo.com/1"})
        self.assertEqual(cm.exception.code, "NOT_YOUTUBE_URL")

    def test_unsupported_source_kind(self):
        with self.assertRaises(StageError) as cm:
            self.acquire(self.chain(StubProvider()), src={"kind": "ftp", "value": "x"})
        self.assertEqual(cm.exception.code, "UNSUPPORTED_SOURCE")


class LocalAndTextProviderTest(TmpCase):
    def test_local_files_are_copied_byte_for_byte_in_every_format(self):
        chain = self.chain(LocalSubtitleProvider())
        for i, name in enumerate(("manual_split.srt", "gaps.vtt", "supervip_snippets.json")):
            res, _ = self.acquire(chain, job=f"00000{i}", src={"kind": "transcript_file", "value": str(FIXTURES / name)})
            self.assertEqual(res["raw_subtitle_path"].read_bytes(), (FIXTURES / name).read_bytes())
            self.assertEqual((res["provider"], res["source_type"], res["has_timestamps"]),
                             ("local", "local_subtitle", True))

    def test_bad_local_inputs(self):
        for value, code in ((str(self.tmp / "khong-co.srt"), "FILE_NOT_FOUND"),):
            with self.assertRaises(StageError) as cm:
                self.acquire(self.chain(LocalSubtitleProvider()), src={"kind": "transcript_file", "value": value})
            self.assertEqual(cm.exception.code, code)
        odd = self.tmp / "a.docx"
        odd.write_text("x", encoding="utf-8")
        with self.assertRaises(StageError) as cm:
            self.acquire(self.chain(LocalSubtitleProvider()), src={"kind": "transcript_file", "value": str(odd)})
        self.assertEqual(cm.exception.code, "UNSUPPORTED_FORMAT")

    def test_plain_text_has_no_timestamps_but_gets_paragraphs_and_punctuation(self):
        text = "Hôm qua tôi đi chợ. Gặp một người bạn cũ!\n\nSáng hôm sau mọi chuyện đã khác"
        res, ctx = self.acquire(self.chain(PlainTextProvider()), src={"kind": "text", "value": text})
        self.assertEqual((res["provider"], res["subtitle_format"], res["has_timestamps"], res["source_type"]),
                         ("text", "txt", False, "plain_text"))
        out = TranscriptProcessor().process(res["raw_subtitle_path"], "txt", ctx.stage_dir, ctx)
        s = json.loads(out["structured"].read_text(encoding="utf-8"))
        self.assertTrue(all(x["start"] is None for x in s["sentences"]))
        self.assertEqual(out["clean"].read_text(encoding="utf-8"),
                         "Hôm qua tôi đi chợ. Gặp một người bạn cũ!\n\nSáng hôm sau mọi chuyện đã khác.\n")


# ---------------------------------------------------------------------------------------------------
class CacheAndInvalidationTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = StubProvider("supervip")
        self.chain_ = self.chain(self.p)

    def stamp(self, d: Path):
        return {f.name: f.stat().st_mtime_ns for f in d.iterdir() if f.is_file()}

    def process(self, res, ctx, **cfg):
        return TranscriptProcessor(cfg).process(res["raw_subtitle_path"], res["subtitle_format"], ctx.stage_dir, ctx)

    def test_rerun_in_the_same_workspace_downloads_and_rebuilds_nothing(self):
        res, ctx = self.acquire(self.chain_)
        self.assertFalse(self.process(res, ctx)["reused"])
        before = self.stamp(ctx.stage_dir)
        res2, ctx2 = self.acquire(self.chain_)
        out2 = self.process(res2, ctx2)
        self.assertEqual((self.p.calls, res2["origin"], out2["reused"]), (1, "job", True))
        self.assertEqual(self.stamp(ctx.stage_dir), before)

    def test_new_job_for_same_video_hits_the_shared_cache(self):
        self.acquire(self.chain_, job="000001")
        res, _ = self.acquire(self.chain_, job="000002")
        self.assertEqual((self.p.calls, res["origin"]), (1, "cache"))
        self.assertEqual(res["raw_subtitle_path"].read_bytes(), (FIXTURES / "manual_split.srt").read_bytes())

    def test_changed_preferences_or_refresh_fetch_again(self):
        self.acquire(self.chain_)
        self.acquire(self.chain_, source_languages=["en"])               # tùy chọn đổi => khóa cache khác
        self.assertEqual(self.p.calls, 2)
        self.acquire(self.chain_, refresh_source=True)
        self.assertEqual(self.p.calls, 3)

    def test_concurrent_jobs_for_the_same_video_download_only_once(self):
        import threading
        import time as _time
        original = self.p.acquire

        def slow(*a, **k):                      # mở rộng cửa sổ race: nếu không có khóa, cả hai luồng sẽ cùng tải
            _time.sleep(0.3)
            return original(*a, **k)
        self.p.acquire = slow
        origins = []

        def worker(job):
            res, _ = self.acquire(self.chain_, job=job)
            origins.append(res["origin"])
        ts = [threading.Thread(target=worker, args=(f"00000{i}",)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.p.calls, 1)
        self.assertEqual(sorted(origins), ["cache", "cache", "cache", "network"])

    def test_corrupted_raw_is_restored_from_cache_without_the_network(self):
        res, ctx = self.acquire(self.chain_)
        res["raw_subtitle_path"].write_bytes(b"corrupted")
        res2, _ = self.acquire(self.chain_)
        self.assertEqual((self.p.calls, res2["origin"]), (1, "cache"))
        self.assertEqual(res2["raw_subtitle_path"].read_bytes(), (FIXTURES / "manual_split.srt").read_bytes())

    def test_processor_config_or_version_change_reparses_but_does_not_refetch(self):
        res, ctx = self.acquire(self.chain_)
        self.process(res, ctx)
        self.assertFalse(self.process(res, ctx, sentence_gap=0.1)["reused"])    # đổi cấu hình dựng câu
        self.assertTrue(self.process(res, ctx, sentence_gap=0.1)["reused"])
        old = transcript_mod.PARSER_VERSION
        transcript_mod.PARSER_VERSION = old + "-next"                           # đổi phiên bản processor
        self.addCleanup(setattr, transcript_mod, "PARSER_VERSION", old)
        self.assertFalse(self.process(res, ctx, sentence_gap=0.1)["reused"])
        self.assertEqual(self.p.calls, 1)

    def test_changed_raw_invalidates_structured_and_clean(self):
        res, ctx = self.acquire(self.chain_)
        first = self.process(res, ctx)["clean"].read_text(encoding="utf-8")
        res["raw_subtitle_path"].write_text((FIXTURES / "gaps.vtt").read_text(encoding="utf-8"), encoding="utf-8")
        out = TranscriptProcessor().process(res["raw_subtitle_path"], "vtt", ctx.stage_dir, ctx)
        self.assertFalse(out["reused"])
        self.assertNotEqual(out["clean"].read_text(encoding="utf-8"), first)


class StoryInvalidationTest(TmpCase):
    def test_story_is_not_redone_when_upstream_is_unchanged_but_restarts_when_it_changes(self):
        ctx = make_ctx(self.tmp, "story")
        t = self.tmp / "transcript.txt"
        t.write_text("Hôm qua tôi đi chợ.\n", encoding="utf-8")
        bundle = {"title": "Chuyện", "language": "vi", "source_language": "vi", "transcript": t}
        runner = ScriptedOhStory()
        ad = StoryBranchAdapter({"max_follow_ups": 2}, Path("x"), runner=runner, deploy_fn=stub_deploy)
        ad.generate(bundle, {"chapters": 3}, ctx.stage_dir, ctx)
        first_calls = runner.n
        runner2 = ScriptedOhStory()
        res = StoryBranchAdapter({"max_follow_ups": 2}, Path("x"), runner=runner2, deploy_fn=stub_deploy).generate(
            bundle, {"chapters": 3}, ctx.stage_dir, ctx)
        self.assertEqual(runner2.n, 0)                                           # upstream không đổi: không gọi agent nào
        self.assertEqual(len(res["sections"]), 3)

        t.write_text("Một câu chuyện hoàn toàn khác.\n", encoding="utf-8")       # upstream đổi
        runner3 = ScriptedOhStory()
        res3 = StoryBranchAdapter({"max_follow_ups": 2}, Path("x"), runner=runner3, deploy_fn=stub_deploy).generate(
            bundle, {"chapters": 3}, ctx.stage_dir, ctx)
        self.assertEqual(runner3.calls[0], "/story-branch analyze")              # làm lại từ đầu
        self.assertEqual(runner3.n, first_calls)
        self.assertTrue(res3["stats"]["invalidated"])
        self.assertEqual(len(list(ctx.stage_dir.glob("oh-story.stale-*"))), 1)   # bản cũ được giữ lại để debug


# ---------------------------------------------------------------------------------------------------
class SourceStageInPipelineTest(RootCase):
    def orchestrator(self, *providers, story=None):
        orc = Orchestrator(load_config(self.root))
        orc.adapters["source"] = ProviderChain(list(providers), self.root / "runtime" / "cache" / "source")
        if story is not None:
            orc.adapters["story"] = story
        return orc

    def source_dir(self, jid):
        return self.job_dir(jid) / "source"

    def test_artifact_layout_and_source_json(self):
        orc = self.orchestrator(StubProvider("supervip", description="MÔ TẢ CỦA NGUỒN"))
        jid = orc.submit(params(input={"kind": "youtube_url", "value": URL}, title=None))
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        d = self.source_dir(jid)
        for name in ("source.json", "subtitle_raw.srt", "transcript_structured.json", "transcript_clean.txt"):
            self.assertTrue((d / name).is_file(), name)
        doc = json.loads((d / "source.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["provider"], doc["video_id"], doc["status"], doc["raw_subtitle_path"]),
                         ("supervip", "abcdefghijk", "ok", "source/subtitle_raw.srt"))
        self.assertEqual(doc["transcript"]["clean"], "source/transcript_clean.txt")
        self.assertNotIn("origin", doc)
        kinds = {a["kind"] for a in orc.store.artifacts(jid) if a["stage"] == "source"}
        self.assertEqual(kinds, {"subtitle_raw", "transcript_structured", "transcript", "metadata"})
        # mô tả video NGUỒN không được chép sang gói output
        out = next((self.root / "output").iterdir())
        self.assertNotIn("MÔ TẢ CỦA NGUỒN", (out / "youtube" / "description.txt").read_text(encoding="utf-8"))

    def test_retrying_the_source_stage_creates_no_duplicate_job_or_artifact(self):
        flaky = StubProvider("supervip", fail=[StageError(ErrorClass.TRANSIENT, "SUPERVIP_ERROR", "mạng chập chờn")])
        orc = self.orchestrator(flaky)
        jid = orc.submit(params(input={"kind": "youtube_url", "value": URL}))
        orc.run()
        self.assertEqual(self.runs(orc, jid)["source"], ["failed", "succeeded"])        # retry tự động theo backoff
        self.assertEqual(flaky.calls, 2)
        self.assertEqual(len(orc.store.list_jobs()), 1)
        src = [a for a in orc.store.artifacts(jid) if a["stage"] == "source"]
        self.assertEqual(len(src), 4)
        self.assertEqual(len({a["path"] for a in src}), 4)

    def test_provider_failure_fails_only_that_job_and_a_retry_resumes_at_source(self):
        bad = StubProvider("supervip", fail=[StageError(ErrorClass.POLICY, "NO_SUBTITLES", "không có phụ đề")])
        orc = self.orchestrator(bad)
        a = orc.submit(params(input={"kind": "youtube_url", "value": URL}))
        orc.run()
        b = orc.submit(params(input={"kind": "youtube_url", "value": URL}))      # job khác được nhận khi a đang FAILED
        orc.run()
        ja, jb = orc.store.get_job(a), orc.store.get_job(b)
        self.assertEqual((ja["state"], ja["failed_stage"], ja["last_error"]["code"]), (P.FAILED, "source", "NO_SUBTITLES"))
        self.assertEqual(jb["state"], P.PUBLISHED)                                      # job khác không bị ảnh hưởng
        self.assertEqual([x for x in orc.store.artifacts(a)], [])                       # không để lại artifact nửa vời
        self.assertEqual(len(orc.store.list_jobs()), 2)
        orc.retry(a)                                                                    # provider đã ổn: retry đúng stage source
        orc.run()
        self.assertEqual(orc.store.get_job(a)["state"], P.PUBLISHED)
        self.assertEqual(self.runs(orc, a)["source"], ["failed", "succeeded"])

    def test_fallback_inside_the_pipeline(self):
        primary = StubProvider("supervip", fail=[StageError(ErrorClass.RESOURCE, "YOUTUBE_BLOCKED", "IP bị chặn")] * 5)
        orc = self.orchestrator(primary, StubProvider("ytdlp"))
        jid = orc.submit(params(input={"kind": "youtube_url", "value": URL}))
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)
        m = json.loads((self.job_dir(jid) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["stages"]["source"]["data"]["provider"], "ytdlp")
        self.assertEqual(self.runs(orc, jid)["source"], ["succeeded"])                  # fallback xảy ra TRONG một lần chạy stage

    def test_source_to_transcript_to_story_to_final_story_txt(self):
        runner = ScriptedOhStory()
        story = StoryBranchAdapter({"max_follow_ups": 2}, Path("x"), runner=runner, deploy_fn=stub_deploy)
        orc = self.orchestrator(StubProvider("supervip", fixture="supervip_snippets.json", kind="manual"), story=story)
        # supervip_snippets.json là định dạng json: provider giả khai đúng định dạng
        jid = orc.submit(params(input={"kind": "youtube_url", "value": URL}, story_profile={"chapters": 4}))
        orc.run()
        job = orc.store.get_job(jid)
        self.assertEqual((job["state"], job["last_error"]), (P.PUBLISHED, None))
        text = (self.job_dir(jid) / "story" / "story.txt").read_text(encoding="utf-8")
        self.assertEqual(validate_story_text(text), [])
        self.assertNotIn("Chương", text)
        seeded = (self.job_dir(jid) / "story" / "oh-story").rglob("原文.md")
        self.assertIn("Hôm qua tôi đi chợ và gặp một người bạn cũ ở cổng.", next(seeded).read_text(encoding="utf-8"))
        self.assertEqual(self.runs(orc, jid), {s.name: ["succeeded"] for s in P.STAGES})

    def test_plain_text_and_local_inputs_run_through_the_same_pipeline(self):
        orc = self.orchestrator(PlainTextProvider(), LocalSubtitleProvider())
        a = orc.submit(params(input={"kind": "text", "value": "Một câu chuyện ngắn. Có hai đoạn.\n\nĐoạn hai ở đây."}))
        b = orc.submit(params(input={"kind": "transcript_file", "value": str(FIXTURES / "manual_split.srt")}))
        orc.run()
        self.assertEqual((orc.store.get_job(a)["state"], orc.store.get_job(b)["state"]), (P.PUBLISHED, P.PUBLISHED))
        doc = json.loads((self.source_dir(a) / "source.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["provider"], doc["has_timestamps"]), ("text", False))


if __name__ == "__main__":
    unittest.main()
