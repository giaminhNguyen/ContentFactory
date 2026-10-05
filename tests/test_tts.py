"""Phase 3: TTS framework — schema/profile (evidence+confidence), normalizer, planner + validator, manager (retry/cache/manifest),
adapter mới không sửa core, Analyzer (onboarding) và Auto Tune."""
import json
import os
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from contentfactory.adapters.command_tts import CommandTTS
from contentfactory.adapters.fake import FakeAudio, FakeTTS, _paragraph
from contentfactory.contracts import ErrorClass, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.runner import Orchestrator
from contentfactory.tts import analyzer as A
from contentfactory.tts import schema as S
from contentfactory.tts.autotune import AutoTuner, apply_to_profile, compose, make_ctx
from contentfactory.tts.manager import TTSManager
from contentfactory.tts.normalize import normalize_text
from contentfactory.tts.planner import AISegmentPlanner, RuleSegmentPlanner, validate_plan
from tests.support import REPO, RootCase, make_root, params

FIXTURE = REPO / "tests" / "fixtures" / "tts_repo"
CAPS = FakeTTS().capabilities()
NO_WAIT = {"retry": {"max_attempts": 3, "backoff_s": [0, 0]}}


def story(n: int = 6) -> str:
    return "\n\n".join(_paragraph(k) for k in range(1, n + 1))


def write_wav(path: Path, frames: bytes, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


class CountingTTS(FakeTTS):
    """FakeTTS đếm lời gọi theo segment, chèn lỗi theo kịch bản."""

    def __init__(self, fail_first=None, silent_first=None, error_class=ErrorClass.TRANSIENT, engine_id="fake", extra_caps=None):
        self.calls: list[tuple[int, str, str | None]] = []
        self.fail_first, self.silent_first = dict(fail_first or {}), dict(silent_first or {})
        self.error_class, self.engine_id, self.extra_caps = error_class, engine_id, extra_caps or {}

    def capabilities(self):
        return {**super().capabilities(), **self.extra_caps}

    def n(self, index: int) -> int:
        return sum(1 for c in self.calls if c[0] == index)

    def synthesize(self, segment, profile, out_path, ctx):
        self.calls.append((segment["index"], segment["text"], profile.get("voice")))
        k = self.n(segment["index"])
        if k <= self.fail_first.get(segment["index"], 0):
            raise StageError(self.error_class, "TTS_FAIL", f"injected #{k}", resource="credential" if self.error_class == ErrorClass.AUTH else None)
        if k <= self.silent_first.get(segment["index"], 0):
            write_wav(out_path, b"\x00\x00" * 4000)
            return {"index": segment["index"], "duration_sec": 0.5}
        return super().synthesize(segment, profile, out_path, ctx)


# =============================================================================== schema / profile
class SchemaTest(unittest.TestCase):
    def test_fact_validation_requires_evidence_and_caps_ai_confidence(self):
        ok = S.fact(480, "official_docs", "high", [{"ref": "README.md:12", "quote": "max 480"}])
        self.assertEqual(S.validate_fact("x", ok), [])
        self.assertTrue(S.validate_fact("x", S.fact(1, "official_docs", "high")))               # nguồn thật phải có evidence
        self.assertTrue(S.validate_fact("x", S.fact(1, "made_up", "low", [{"ref": "a"}])))      # source ngoài danh sách
        self.assertTrue(S.validate_fact("x", S.fact(1, "source_code", "certain", [{"ref": "a"}])))
        self.assertTrue(S.validate_fact("x", S.fact(1, "ai_inference", "high", [{"ref": "a"}])))  # AI không được tự tin tuyệt đối
        self.assertEqual(S.validate_fact("x", S.fact(1, "ai_inference", "medium", [{"ref": "a"}])), [])
        self.assertEqual(S.validate_fact("x", S.fact(600, "default", "low")), [])                # default không cần evidence
        for src in ("official_docs", "source_code", "official_example", "runtime_test", "ai_inference"):
            self.assertIn(src, S.EVIDENCE_SOURCES)

    def test_annotated_profile_validates_and_resolves_like_flat(self):
        prof = S.new_annotated("eng")
        prof["segment"] = {"max_chars": S.fact(300, "official_docs", "high", [{"ref": "d"}]),
                           "preferred_chars": S.fact(200, "default", "low")}
        prof["voice"] = S.fact("alice", "user", "high")
        prof["settings"] = {"speed": S.fact(1.1, "source_code", "medium", [{"ref": "infer.py:9"}])}
        self.assertEqual(S.validate_annotated(prof), [])
        bad = json.loads(json.dumps(prof))
        bad["segment"]["max_chars"]["evidence"] = []
        self.assertTrue(S.validate_annotated(bad))
        flat = S.resolve(prof, CAPS, "vi")
        self.assertEqual((flat["segment"]["max_chars"], flat["voice"], flat["settings"]["speed"]), (300, "alice", 1.1))
        self.assertEqual(flat, S.resolve(S.unwrap(prof), CAPS, "vi"))                            # Fact hay giá trị trần đều như nhau

    def test_resolve_legacy_flat_clamps_to_capability(self):
        f = S.resolve({"max_chars": 5000, "pause_ms": {"paragraph": 900}}, CAPS, "vi")
        self.assertEqual(f["segment"]["max_chars"], CAPS["max_chars"])                           # kẹp theo giới hạn engine
        self.assertLessEqual(f["segment"]["preferred_chars"], f["segment"]["max_chars"])
        self.assertEqual((f["pause_ms"]["paragraph"], f["pause_ms"]["dialogue"]), (900, 350))     # trộn với mặc định

    def test_resolve_rejects_unusable_profiles(self):
        with self.assertRaises(StageError) as e:
            S.resolve({}, CAPS, "fr")
        self.assertEqual(e.exception.code, "INVALID_TTS_PROFILE")
        needs_ref = {**CAPS, "requires_reference_audio": True}
        with self.assertRaises(StageError):
            S.resolve({}, needs_ref, "vi")
        self.assertEqual(S.resolve({"settings": {"reference_audio": "me.wav"}}, needs_ref, "vi")["language"], "vi")
        with self.assertRaises(StageError):
            S.resolve({"pause_ms": {"paragraph": -5}}, CAPS, "vi")
        with self.assertRaises(StageError):
            S.normalize_capabilities({"max_chars": "lots"})

    def test_cache_identity_covers_audio_inputs_only(self):
        base = S.resolve({"voice": "a", "model": "m", "settings": {"speed": 1.0}}, CAPS, "vi", "eng")
        ident = S.cache_identity(base, CAPS)
        k = S.segment_key("xin chào", ident)
        same = S.resolve({"voice": "a", "model": "m", "settings": {"speed": 1.0}, "pause_ms": {"paragraph": 1}, "max_chars": 200,
                          "retry": {"max_attempts": 9}, "qa": {"silence_ratio_max": 0.5}}, CAPS, "vi", "eng")
        self.assertEqual(k, S.segment_key("xin chào", S.cache_identity(same, CAPS)))
        for change in ({"voice": "b"}, {"model": "m2"}, {"settings": {"speed": 1.2}}, {"profile_version": "2"}):
            other = S.resolve({"voice": "a", "model": "m", "settings": {"speed": 1.0}, **change}, CAPS, "vi", "eng")
            self.assertNotEqual(k, S.segment_key("xin chào", S.cache_identity(other, CAPS)), change)
        self.assertNotEqual(k, S.segment_key("xin chào!", ident))                                # text đổi
        self.assertNotEqual(k, S.segment_key("xin chào", S.cache_identity(base | {"engine": "eng2"}, CAPS)))
        self.assertNotEqual(k, S.segment_key("xin chào", S.cache_identity(base, {**CAPS, "engine_version": "2"})))
        only_speed = {**CAPS, "cache_settings": ["speed"]}                                      # adapter khai báo setting nào ảnh hưởng
        a = S.resolve({"settings": {"speed": 1.0, "log_level": "x"}}, only_speed, "vi", "eng")
        b = S.resolve({"settings": {"speed": 1.0, "log_level": "y"}}, only_speed, "vi", "eng")
        self.assertEqual(S.cache_identity(a, only_speed), S.cache_identity(b, only_speed))


# =============================================================================== normalizer
class NormalizeTest(unittest.TestCase):
    def test_normalizer_cleans_and_reports(self):
        raw = "﻿# Chương 1\r\n\r\n\r\n\r\nĐây là **in đậm** và `mã`...  Thật sao???  Ừ!!!​\n---\nHết"
        t, rep = normalize_text(raw, {})
        self.assertEqual(t, "Chương 1\n\nĐây là in đậm và mã… Thật sao? Ừ!\n\nHết")
        for step in ("control_chars", "newlines", "markdown", "ellipsis", "repeated_punct", "blank_lines"):
            self.assertIn(step, rep["changes"])

    def test_replacements_and_terminal_punct_and_idempotent(self):
        cfg = {"replacements": [{"from": "TP.HCM", "to": "Thành phố Hồ Chí Minh"}, {"pattern": r"\bGS\b", "to": "Giáo sư"}],
               "ensure_terminal_punct": True}
        t, _ = normalize_text("Ở TP.HCM có GS Nam\n\nXong rồi.", cfg)
        self.assertEqual(t, "Ở Thành phố Hồ Chí Minh có Giáo sư Nam.\n\nXong rồi.")
        self.assertEqual(normalize_text(t, cfg)[0], t)

    def test_nfc(self):
        decomposed = "Việt"
        self.assertEqual(normalize_text(decomposed)[0], "Việt")


# =============================================================================== planner + validator
class PlannerTest(unittest.TestCase):
    def flat(self, **seg):
        return S.resolve({"segment": {"preferred_chars": 200, "max_chars": 300, "min_chars": 40, **seg}}, CAPS, "vi")

    def test_rule_planner_respects_limits_pauses_and_validates(self):
        long_sentence = ", ".join(f"mệnh đề số {i} kể về căn nhà cũ" for i in range(1, 60)) + "."
        text = f"Đoạn mở đầu ngắn. Còn tiếp nữa.\n\n— Anh đi đâu đấy?\n\n{long_sentence}\n\nKết thúc."
        f = self.flat()
        plan = RuleSegmentPlanner().plan(text, f)
        v = validate_plan(plan, text, f)
        self.assertEqual(v["errors"], [])
        self.assertTrue(all(len(s["text"]) <= 300 for s in plan))
        self.assertGreater(len([s for s in plan if len(s["text"]) > 40]), 3)
        self.assertEqual(plan[0]["pause_after_ms"], f["pause_ms"]["paragraph"])                   # đoạn 1 gói trọn một segment
        self.assertEqual(plan[1]["pause_after_ms"], f["pause_ms"]["dialogue"])                    # lời thoại
        self.assertEqual(plan[-1]["pause_after_ms"], f["pause_ms"]["paragraph"])
        self.assertEqual(plan, RuleSegmentPlanner().plan(text, f))                                # tất định

    def test_long_sentence_is_cut_at_clause_boundaries_not_mid_word(self):
        text = "Một câu rất dài " + ", ".join(["đi qua cánh đồng"] * 40) + "."
        f = self.flat(max_chars=120, preferred_chars=100)
        plan = RuleSegmentPlanner().plan(text, f)
        self.assertEqual(validate_plan(plan, text, f)["errors"], [])
        self.assertTrue(all(s["text"].rstrip().endswith((",", ".")) for s in plan))

    def test_validator_rejects_bad_plans(self):
        text, f = story(4), self.flat()
        good = RuleSegmentPlanner().plan(text, f)
        codes = lambda plan: {e["code"] for e in validate_plan(plan, text, f)["errors"]}      # noqa: E731
        self.assertEqual(codes(good), set())
        self.assertIn("TEXT_MISMATCH", codes(good[:-1]))                                      # mất đoạn
        self.assertIn("TEXT_MISMATCH", codes(good + [{**good[0], "index": len(good) + 1}]))   # lặp đoạn
        k = max(range(len(good)), key=lambda i: len(good[i]["text"]))                         # segment dài nhất
        swap = lambda **kw: good[:k] + [{**good[k], **kw}] + good[k + 1:]                    # noqa: E731
        self.assertIn("TEXT_MISMATCH", codes(swap(text=good[k]["text"].replace(" ", " X ", 1))))   # đổi chữ
        self.assertIn("TEXT_MISMATCH", codes(swap(text=good[k]["text"] + " Thêm chữ bịa.")))      # thêm chữ
        self.assertIn("TOO_LONG", codes(swap(text=good[k]["text"] * 4)))
        self.assertIn("BAD_INDEX", codes([{**good[0], "index": 5}] + good[1:]))
        self.assertIn("BAD_PAUSE", codes([{**good[0], "pause_after_ms": -5}] + good[1:]))
        self.assertIn("BAD_PAUSE", codes([{**good[0], "pause_after_ms": True}] + good[1:]))
        self.assertIn("EMPTY_SEGMENT", codes([{**good[0], "text": "  "}] + good[1:]))
        self.assertIn("NO_SPEECH", codes([{**good[0], "text": "... --- !!!"}] + good[1:]))
        self.assertIn("EMPTY_PLAN", codes([]))
        cut = good[k]["text"].index(" ", 30)                                                  # dời ranh giới vào GIỮA một từ
        a, b = good[k]["text"][:cut - 2], good[k]["text"][cut - 2:]
        mid = good[:k] + [{**good[k], "text": a, "pause_after_ms": 0}, {**good[k], "text": b}] + good[k + 1:]
        for n, s in enumerate(mid, 1):
            s["index"] = n
        self.assertIn("TEXT_MISMATCH", codes(mid))

    def test_validator_warnings_do_not_block(self):
        text = "Câu một. Câu hai.\n\nCâu ba."
        f = self.flat(min_chars=100)
        plan = [{"index": 1, "text": "Câu một.", "pause_after_ms": 0}, {"index": 2, "text": "Câu hai.", "pause_after_ms": 600},
                {"index": 3, "text": "Câu ba.", "pause_after_ms": 600}]
        v = validate_plan(plan, text, f)
        self.assertEqual(v["errors"], [])
        self.assertIn("SHORT", {w["code"] for w in v["warnings"]})


class AIPlannerTest(unittest.TestCase):
    """AI Planner chỉ GOM CÂU; mọi kế hoạch vẫn qua validator, hỏng thì thử lại có phản hồi rồi rơi về rule."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-tts-plan-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.flat = S.resolve({"segment": {"preferred_chars": 200, "max_chars": 300, "min_chars": 40}}, CAPS, "vi")
        self.text = story(4)

    @staticmethod
    def parse(prompt: str) -> list[list[int]]:
        paras, cur = [], []
        for ln in prompt.splitlines():
            if ln.startswith("--- đoạn ---"):
                cur = []
                paras.append(cur)
            elif ln[:1].isdigit() and ". " in ln:
                cur.append(int(ln.split(".")[0]))
        return paras

    def good_llm(self, prompt):
        return json.dumps({"segments": [{"sentences": ids, "pause": "paragraph"} for ids in self.parse(prompt) if ids]})

    def manager(self, llm):
        return TTSManager(CountingTTS(), FakeAudio(), AISegmentPlanner(llm))

    def plan(self, llm):
        ctx = make_ctx(self.tmp)
        return self.manager(llm).plan(ctx, self.text, self.flat)

    def test_valid_ai_plan_is_used(self):
        segs, rep = self.plan(self.good_llm)
        self.assertEqual((rep["requested"], rep["used"]), ("ai", "ai"))
        self.assertEqual(len(segs), 4)                                                        # đúng nhóm theo đoạn
        self.assertEqual(validate_plan(segs, self.text, self.flat)["errors"], [])

    def test_bad_plan_is_retried_with_feedback_then_accepted(self):
        prompts = []

        def llm(prompt):
            prompts.append(prompt)
            if len(prompts) == 1:                                                              # lần đầu bỏ sót câu cuối
                groups = [ids for ids in self.parse(prompt) if ids]
                groups[-1] = groups[-1][:-1]
                return json.dumps({"segments": [{"sentences": g, "pause": "paragraph"} for g in groups if g]})
            return self.good_llm(prompt)
        segs, rep = self.plan(llm)
        self.assertEqual(rep["used"], "ai")
        self.assertEqual(len(prompts), 2)
        self.assertIn("TEXT_MISMATCH", prompts[1])                                            # phản hồi của validator được đưa lại cho LLM
        self.assertEqual(rep["attempts"][0]["errors"][0]["code"], "TEXT_MISMATCH")

    def test_unusable_ai_falls_back_to_rule(self):
        for llm in (lambda p: "không phải json", lambda p: (_ for _ in ()).throw(RuntimeError("llm down")),
                    lambda p: json.dumps({"segments": [{"sentences": [1, 1, 2], "pause": "sentence"}]})):
            shutil.rmtree(self.tmp, True)
            self.tmp.mkdir()
            segs, rep = self.plan(llm)
            self.assertEqual((rep["requested"], rep["used"]), ("ai", "rule"))
            self.assertEqual(validate_plan(segs, self.text, self.flat)["errors"], [])

    def test_plan_is_reused_on_resume_even_if_llm_is_nondeterministic(self):
        calls = []
        mgr = self.manager(lambda p: calls.append(1) or self.good_llm(p))
        ctx = make_ctx(self.tmp)
        first, _ = mgr.plan(ctx, self.text, self.flat)
        again, rep = mgr.plan(ctx, self.text, self.flat)
        self.assertEqual((first, len(calls), rep["reused_plan"]), (again, 1, True))
        other, rep2 = mgr.plan(ctx, self.text + "\n\nĐoạn mới hoàn toàn.", self.flat)           # văn bản đổi => lập kế hoạch lại
        self.assertFalse(rep2["reused_plan"])
        self.assertEqual(len(calls), 2)


# =============================================================================== manager trong pipeline
class PipelineTTSTest(RootCase):
    def story_file(self, n: int = 6) -> Path:
        p = self.root / f"story_{n}.txt"
        p.write_text(story(n), encoding="utf-8")
        return p

    def run_tts(self, orc, n: int = 6, **p) -> str:
        jid = orc.submit(params(**p), mode="TTS_ONLY", inputs={"story_text": str(self.story_file(n))}, auto_resume=False)
        orc.run()
        return jid

    def manifest(self, jid: str) -> dict:
        return json.loads((self.job_dir(jid) / "tts" / "tts_manifest.json").read_text(encoding="utf-8"))

    def test_manifest_artifact_and_segments(self):
        orc = self.orc()
        jid = self.run_tts(orc)
        self.assertEqual(orc.store.get_job(jid)["state"], P.AUDIO_READY)
        kinds = {a["kind"] for a in orc.store.artifacts(jid) if a["stage"] == "tts"}
        self.assertEqual(kinds, {"audio_master", "tts_manifest"})
        m = self.manifest(jid)
        self.assertEqual((m["engine"], m["language"], m["totals"]["segments"]), ("fake", "vi", 6))
        self.assertEqual({r["source"] for r in m["segments"]}, {"synth"})
        self.assertTrue(all(len(r["key"]) == 16 and r["attempts"] == 1 for r in m["segments"]))
        self.assertEqual(m["planner"]["used"], "rule")
        self.assertTrue((self.job_dir(jid) / "tts" / "segments.json").is_file())
        self.assertGreater(m["totals"]["duration_sec"], 0)

    def test_retry_is_per_segment(self):
        orc = self.orc()
        orc.adapters["tts"] = tts = CountingTTS(fail_first={3: 2})
        jid = self.run_tts(orc, tts=NO_WAIT)
        self.assertEqual(orc.store.get_job(jid)["state"], P.AUDIO_READY)
        self.assertEqual({i: tts.n(i) for i in range(1, 7)}, {1: 1, 2: 1, 3: 3, 4: 1, 5: 1, 6: 1})   # chỉ segment 3 bị gọi lại
        self.assertEqual(self.runs(orc, jid)["tts"], ["succeeded"])                                   # không phải retry cả stage
        row = self.manifest(jid)["segments"][2]
        self.assertEqual((row["attempts"], row["retry_errors"]), (3, ["TTS_FAIL", "TTS_FAIL"]))
        self.assertEqual(self.manifest(jid)["totals"]["retries"], 2)

    def test_bad_audio_is_detected_and_only_that_segment_retried(self):
        orc = self.orc()
        orc.adapters["tts"] = tts = CountingTTS(silent_first={2: 1})
        jid = self.run_tts(orc, tts=NO_WAIT)
        self.assertEqual((tts.n(2), tts.n(1)), (2, 1))
        self.assertEqual(self.manifest(jid)["segments"][1]["retry_errors"], ["QA:SILENT"])

    def test_exhausted_segment_keeps_finished_chunks_and_retry_resumes_there(self):
        orc = self.orc()
        orc.adapters["tts"] = tts = CountingTTS(fail_first={3: 999})
        jid = self.run_tts(orc, tts={"retry": {"max_attempts": 2, "backoff_s": [0]}})
        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["failed_stage"], j["last_error"]["code"]), (P.FAILED, "tts", "TTS_FAIL"))
        self.assertEqual(j["last_error"]["detail"]["segment"], 3)
        self.assertEqual((tts.n(1), tts.n(2), tts.n(4)), (1, 1, 0))                                  # 1,2 xong 1 lần; 4 chưa tới
        tts.fail_first.clear()
        orc.retry(jid)
        orc.run()
        self.assertEqual(orc.store.get_job(jid)["state"], P.AUDIO_READY)
        self.assertEqual((tts.n(1), tts.n(2)), (1, 1))                                               # chunk đã xong không tổng hợp lại
        self.assertEqual(self.manifest(jid)["totals"]["reused"], 2)

    def test_non_transient_error_is_not_retried_inside_the_stage(self):
        orc = self.orc()
        orc.adapters["tts"] = tts = CountingTTS(fail_first={1: 99}, error_class=ErrorClass.AUTH)
        jid = self.run_tts(orc, tts=NO_WAIT)
        self.assertEqual(tts.n(1), 1)
        self.assertEqual(orc.store.get_job(jid)["hold_reason"], P.PAUSED_CREDENTIAL)

    def test_cache_hit_when_text_and_config_are_the_same(self):
        orc = self.orc()
        orc.adapters["tts"] = tts = CountingTTS()
        first = self.run_tts(orc)
        self.assertEqual(len(tts.calls), 6)
        second = self.run_tts(orc)                                                                   # job khác, cùng text + config
        self.assertEqual(len(tts.calls), 6)                                                          # không gọi engine thêm
        m = self.manifest(second)
        self.assertEqual((m["totals"]["cache_hits"], m["totals"]["synthesized"]), (6, 0))
        self.assertEqual({r["source"] for r in m["segments"]}, {"cache"})
        self.assertEqual(orc.store.stage_runs(second)[0]["status"], "succeeded")
        self.assertEqual((self.job_dir(first) / "audio" / "master.wav").read_bytes(),
                         (self.job_dir(second) / "audio" / "master.wav").read_bytes())               # cùng kết quả

    def test_cache_ignores_pause_changes_but_misses_on_voice_model_settings_engine(self):
        orc = self.orc()
        orc.adapters["tts"] = tts = CountingTTS()
        self.run_tts(orc)
        n = len(tts.calls)
        self.run_tts(orc, tts={"pause_ms": {"paragraph": 900}})
        self.assertEqual(len(tts.calls), n)                                                          # pause không đổi audio chunk
        for change in ({"voice": "B"}, {"model": "m2"}, {"settings": {"speed": 1.25}}, {"profile_version": "2"}):
            before = len(tts.calls)
            self.run_tts(orc, tts=change)
            self.assertEqual(len(tts.calls) - before, 6, f"phải synth lại khi đổi {change}")
        before = len(tts.calls)
        orc.adapters["tts"] = other = CountingTTS(engine_id="other-engine")
        self.run_tts(orc)
        self.assertEqual(len(other.calls), 6)
        orc.adapters["tts"] = v2 = CountingTTS(extra_caps={"engine_version": "fake-2"})
        self.run_tts(orc)
        self.assertEqual(len(v2.calls), 6)
        self.assertGreater(before, 0)

    def test_corrupted_cache_entry_is_not_trusted(self):
        orc = self.orc()
        orc.adapters["tts"] = tts = CountingTTS()
        self.run_tts(orc)
        cached = sorted((self.root / "runtime" / "cache" / "tts").rglob("*.wav"))
        self.assertEqual(len(cached), 6)
        cached[0].write_bytes(b"corrupted")
        before = len(tts.calls)
        jid = self.run_tts(orc)
        self.assertEqual(len(tts.calls) - before, 1)                                                 # đúng một segment bị synth lại
        self.assertEqual(self.manifest(jid)["totals"]["cache_hits"], 5)
        self.assertEqual(orc.store.get_job(jid)["state"], P.AUDIO_READY)

    def test_changed_voice_inside_one_job_dir_does_not_reuse_stale_chunks(self):
        """Cùng thư mục chunk nhưng voice đổi (retry job sau khi sửa config): chunk cũ có key khác nên không được dùng lại."""
        tts = CountingTTS()
        work = Path(tempfile.mkdtemp(prefix="cf-tts-dir-"))
        self.addCleanup(shutil.rmtree, work, True)
        mgr = TTSManager(tts, FakeAudio())
        ctx = make_ctx(work)
        ctx.config = {}                                                                              # không cache chung: chỉ kiểm chunk trong thư mục job
        mgr.run(ctx, story(6), {"voice": "A"})
        n = len(tts.calls)
        mgr.run(ctx, story(6), {"voice": "B"})
        self.assertEqual(len(tts.calls) - n, 6)
        mgr.run(ctx, story(6), {"voice": "B"})
        self.assertEqual(len(tts.calls) - n, 6)                                                      # lần này dùng lại chunk của voice B

    def test_annotated_profile_with_evidence_drives_segmentation(self):
        prof = S.new_annotated("fake", "ready")
        prof["segment"] = {"max_chars": S.fact(200, "official_docs", "high", [{"ref": "README.md:3", "quote": "max 200 chars"}]),
                           "preferred_chars": S.fact(150, "runtime_test", "medium", [{"ref": "autotune:1"}])}
        self.assertEqual(S.validate_annotated(prof), [])
        orc = self.orc()
        jid = self.run_tts(orc, tts=prof)
        m = self.manifest(jid)
        self.assertGreater(m["totals"]["segments"], 6)                                               # 200 ký tự < độ dài đoạn mặc định
        self.assertTrue(all(r["chars"] <= 200 for r in m["segments"]))
        other = S.new_annotated("fake", "ready")
        other["segment"] = {"max_chars": S.fact(300, "official_docs", "high", [{"ref": "d"}])}
        self.assertNotEqual(m["profile_hash"], self.manifest(self.run_tts(orc, tts=other))["profile_hash"])

    def test_unsupported_language_fails_fast_with_clear_error(self):
        orc = self.orc()
        jid = self.run_tts(orc, language="fr")
        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["last_error"]["code"]), (P.FAILED, "INVALID_TTS_PROFILE"))

    def test_story_is_fully_covered_by_segments(self):
        orc = self.orc()
        jid = self.run_tts(orc, tts={"max_chars": 250})
        segs = json.loads((self.job_dir(jid) / "tts" / "segments.json").read_text(encoding="utf-8"))["segments"]
        joined = " ".join(" ".join(s["text"].split()) for s in segs)
        self.assertEqual(joined, " ".join(story(6).split()))                                          # không mất/lặp chữ nào


# =============================================================================== thêm adapter mà không sửa core
EXT_MODULE = '''
import wave
from pathlib import Path


class ToneTTS:
    engine_id = "tonetts"

    def __init__(self, config=None):
        self.amp = int((config or {}).get("amp", 1500))

    def capabilities(self):
        return {"max_chars": 400, "languages": ["vi", "en"], "engine_version": "ext-1", "sample_rate": 8000}

    def health(self):
        return {"ok": True}

    def synthesize(self, segment, profile, out_path, ctx):
        frames = b"".join((self.amp if (i // 20) % 2 else -self.amp).to_bytes(2, "little", signed=True) for i in range(4000))
        tmp = Path(str(out_path) + ".part")
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(frames)
        tmp.replace(out_path)
        return {"index": segment["index"], "duration_sec": 0.5}
'''


class ExtensibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-ext-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_job(self, extra: dict, text: str | None = None):
        root = make_root(extra)
        self.addCleanup(shutil.rmtree, root, True)
        f = root / "story.txt"
        f.write_text(text or story(3), encoding="utf-8")
        orc = Orchestrator(load_config(root))
        jid = orc.submit(params(), mode="TTS_ONLY", inputs={"story_text": str(f)}, auto_resume=False)
        orc.run()
        return orc, jid, root

    def test_new_python_adapter_via_config_only(self):
        (self.tmp / "tonetts_ext_mod.py").write_text(EXT_MODULE, encoding="utf-8")
        sys.path.insert(0, str(self.tmp))
        self.addCleanup(lambda: sys.path.remove(str(self.tmp)))
        orc, jid, root = self.run_job({"adapters": {"tts": "tonetts_ext_mod:ToneTTS"}, "adapter_config": {"tts": {"amp": 900}}})
        self.assertEqual(orc.store.get_job(jid)["state"], P.AUDIO_READY)
        m = json.loads((root / "workspace" / f"job_{jid}" / "tts" / "tts_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((m["engine"], m["engine_version"]), ("tonetts", "ext-1"))
        snap = orc.store.get_job(jid)["config_snapshot"]["semantic"]
        self.assertEqual(snap["adapter_config"]["tts"]["amp"], 900)                                  # cấu hình adapter vào snapshot của job

    def test_command_adapter_version_changes_with_its_spec(self):
        """Đổi lệnh/tham số của engine CLI => engine_version đổi => cache chunk cũ không bị dùng nhầm."""
        a = CommandTTS({"command": ["tool", "--text", "{text}"]})
        b = CommandTTS({"command": ["tool", "--text", "{text}", "--fast"]})
        c = CommandTTS({"command": ["tool", "--text", "{text}"], "timeout_s": 5})
        self.assertNotEqual(a.capabilities()["engine_version"], b.capabilities()["engine_version"])
        self.assertEqual(a.capabilities()["engine_version"], c.capabilities()["engine_version"])    # timeout không đổi âm thanh
        pinned = CommandTTS({"command": ["tool"], "capabilities": {"engine_version": "v9"}})
        self.assertEqual(pinned.capabilities()["engine_version"], "v9")

    def test_cli_engine_onboarded_from_repo_runs_in_pipeline_with_no_code(self):
        res = A.analyze(FIXTURE)
        cfg = json.loads(json.dumps(res["adapter"]["config"]))
        cfg["adapter_config"]["tts"]["command"][0] = sys.executable
        orc, jid, root = self.run_job(cfg)
        j = orc.store.get_job(jid)
        self.assertEqual(j["state"], P.AUDIO_READY, j["last_error"])
        m = json.loads((root / "workspace" / f"job_{jid}" / "tts" / "tts_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["engine"], "tts_repo")
        self.assertEqual(m["totals"]["segments"], 3)
        self.assertTrue(all(r["chars"] <= 480 for r in m["segments"]))                               # tôn trọng giới hạn engine
        self.assertTrue((root / "workspace" / f"job_{jid}" / "audio" / "master.wav").is_file())


class CommandTTSTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-cmd-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ctx = make_ctx(self.tmp)

    def run_cmd(self, code: str, **spec):
        a = CommandTTS({"engine_id": "x", "command": [sys.executable, "-c", code, "{out}"], **spec})
        return a.synthesize({"index": 1, "text": "xin chào"}, S.resolve({}, {}, "vi"), self.tmp / "o.wav", self.ctx)

    def err(self, code: str, **spec) -> StageError:
        with self.assertRaises(StageError) as e:
            self.run_cmd(code, **spec)
        return e.exception

    def test_success_and_atomic_output_and_no_leftovers(self):
        self.run_cmd("import sys,wave;w=wave.open(sys.argv[1],'wb');w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);"
                     "w.writeframes(b'\\x10\\x10'*800);w.close()")
        self.assertTrue((self.tmp / "o.wav").is_file())
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["o.wav"])

    def test_error_classification(self):
        e = self.err("import sys;sys.stderr.write('401 Unauthorized');sys.exit(1)")
        self.assertEqual((e.error_class, e.resource), (ErrorClass.AUTH, "credential"))
        e = self.err("import sys;sys.stderr.write('429 too many requests');sys.exit(1)")
        self.assertEqual((e.error_class, e.resource), (ErrorClass.TRANSIENT, "provider"))
        e = self.err("import sys;sys.stderr.write('CUDA out of memory');sys.exit(1)")
        self.assertEqual((e.error_class, e.resource), (ErrorClass.RESOURCE, "runtime"))
        self.assertEqual(self.err("pass").code, "TTS_NO_OUTPUT")
        e = self.err("import time;time.sleep(5)", timeout_s=0.5)
        self.assertEqual(e.code, "TTS_TIMEOUT")
        e = self.err("pass", env_required=["CF_TEST_MISSING_ENV_XYZ"])
        self.assertEqual((e.error_class, e.code), (ErrorClass.AUTH, "MISSING_CREDENTIAL"))

    def test_placeholders_and_optional_args(self):
        a = CommandTTS({"command": ["tool", "--t", "{text}", "--o", "{out}"], "optional_args": {"voice": ["--voice", "{voice}"],
                                                                                              "settings.speed": ["--speed", "{settings.speed}"]}})
        argv = a._argv({"text": "hi"}, {"voice": "bob", "settings": {"speed": 1.5}}, Path("x.wav"), None)
        self.assertEqual(argv, ["tool", "--t", "hi", "--o", "x.wav", "--voice", "bob", "--speed", "1.5"])
        self.assertEqual(a._argv({"text": "hi"}, {"settings": {}}, Path("x.wav"), None), ["tool", "--t", "hi", "--o", "x.wav"])
        with self.assertRaises(StageError) as e:
            CommandTTS({"command": ["tool", "{voice}"]})._argv({"text": "x"}, {}, Path("o"), None)
        self.assertEqual(e.exception.code, "MISSING_PLACEHOLDER")
        with self.assertRaises(ValueError):
            CommandTTS({})


# =============================================================================== Analyzer (onboarding)
class AnalyzerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-an-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def repo(self, files: dict[str, str]) -> Path:
        for name, body in files.items():
            f = self.tmp / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body, encoding="utf-8")
        return self.tmp

    def test_fixture_repo_facts_with_evidence_and_confidence(self):
        r = A.analyze(FIXTURE)
        caps, prof = r["capabilities"], r["profile"]
        self.assertEqual((caps["sample_rate"], caps["speed"], caps["streaming"], caps["voice_cloning"]), (24000, True, False, False))
        self.assertEqual(caps["languages"], ["en", "vi"])
        self.assertEqual(caps["voices"], ["alice", "bob"])
        self.assertEqual(caps["output_formats"], ["wav"])
        facts = prof["meta"]["capability_facts"]
        sr = facts["sample_rate"]
        self.assertEqual((sr["source"], sr["confidence"]), ("official_docs", "high"))               # docs và code khớp nhau => high
        self.assertTrue(any(e["ref"].startswith("infer.py") for e in sr["evidence"]))
        mx = facts["max_chars"]
        self.assertEqual((mx["value"], mx["source"], mx["confidence"]), (480, "source_code", "low"))   # docs nói 500, code nói 480
        self.assertIn("mâu thuẫn", mx["note"])
        self.assertEqual(prof["segment"]["max_chars"]["value"], 480)
        self.assertEqual(prof["segment"]["preferred_chars"]["source"], "default")                    # con số tự suy ra không giả làm bằng chứng
        self.assertEqual(prof["settings"]["speed"]["value"], 1.0)
        self.assertEqual(prof["voice"]["value"], "alice")
        self.assertEqual(S.validate_annotated(prof), [])
        for path, f in S.walk_facts(prof):
            self.assertIn(f["source"], S.EVIDENCE_SOURCES, path)
            self.assertIn(f["confidence"], S.CONFIDENCE, path)
            if f["source"] not in ("default", "user"):
                self.assertTrue(f["evidence"] and f["evidence"][0]["ref"], path)

    def test_does_not_ask_user_for_what_it_can_discover(self):
        r = A.analyze(FIXTURE)
        keys = {n["key"] for n in r["needs_user"]}
        self.assertEqual(keys, {"env:TONESPEAK_API_KEY"})                                           # chỉ credential (tùy chọn)
        self.assertEqual(r["needs_user"][0]["reason"], "credential_optional")
        self.assertEqual(r["adapter"]["spec"]["env_required"], [])                                  # tùy chọn => không chặn chạy
        for asked in ("voice", "speed", "max_chars", "sample_rate", "language", "model"):
            self.assertFalse(any(asked in k for k in keys))

    def test_generated_adapter_candidate_and_config(self):
        ad = A.analyze(FIXTURE)["adapter"]
        self.assertEqual((ad["kind"], ad["ready"]), ("command", True))
        spec = ad["spec"]
        self.assertEqual(spec["command"][1:], ["infer.py", "--text", "{text}", "--out", "{out}"])
        self.assertEqual(set(spec["optional_args"]), {"voice", "language", "settings.speed"})
        self.assertEqual(ad["config"]["adapters"]["tts"], "contentfactory.adapters.command_tts:CommandTTS")
        CommandTTS(spec)                                                                            # spec dựng được adapter

    def test_required_reference_voice_and_cli_params_are_asked(self):
        repo = self.repo({"clone.py": "import argparse\nap=argparse.ArgumentParser()\nap.add_argument('--text',required=True)\n"
                                      "ap.add_argument('--output',required=True)\nap.add_argument('--speaker_wav',required=True)\n"
                                      "ap.add_argument('--checkpoint_dir',required=True)\nap.parse_args()\n",
                          "README.md": "Zero-shot voice cloning from a reference audio clip.\n"})
        r = A.analyze(repo, engine="cloner")
        self.assertTrue(r["capabilities"]["requires_reference_audio"])
        self.assertTrue(r["capabilities"]["voice_cloning"])
        kinds = {n["reason"] for n in r["needs_user"]}
        self.assertEqual(kinds, {"required_reference_voice", "required_parameter"})
        with self.assertRaises(StageError):
            S.resolve(S.unwrap(r["profile"]), r["capabilities"], "vi")                              # thiếu reference => chặn sớm, rõ lý do
        self.assertEqual(S.resolve({"settings": {"reference_audio": "me.wav"}}, r["capabilities"], "vi")["language"], "vi")

    def test_http_and_python_api_get_candidate_skeletons(self):
        http = self.repo({"server.py": "import os\nfrom fastapi import FastAPI\napp=FastAPI()\nKEY=os.getenv('ACME_API_KEY')\n"
                                       "@app.post('/v1/tts')\ndef tts(text: str):\n    pass\n"})
        r = A.analyze(http, engine="acme-cloud")
        self.assertEqual((r["adapter"]["kind"], r["adapter"]["ready"]), ("http", False))
        compile(r["adapter"]["code"], "adapter_candidate.py", "exec")
        self.assertIn("env:ACME_API_KEY", {n["key"] for n in r["needs_user"]})
        shutil.rmtree(self.tmp)
        self.tmp.mkdir()
        py = self.repo({"engine/api.py": "class Engine:\n    def synthesize(self, text, speaker=None, speed=1.0):\n        pass\n"})
        r = A.analyze(py, engine="pyeng")
        self.assertEqual(r["adapter"]["kind"], "python")
        self.assertEqual(r["adapter"]["spec"]["params"], ["text", "speaker", "speed"])
        compile(r["adapter"]["code"], "adapter_candidate.py", "exec")

    def test_nothing_found_yields_defaults_and_needs_tune(self):
        r = A.analyze(self.repo({"README.md": "Just a project."}), engine="blank")
        self.assertEqual(r["adapter"]["kind"], "unknown")
        self.assertTrue(r["profile"]["meta"]["needs_tune"])
        self.assertEqual(r["profile"]["segment"]["max_chars"]["source"], "default")
        self.assertEqual(S.validate_annotated(r["profile"]), [])

    def test_ai_inference_only_fills_gaps_and_is_capped(self):
        seen = {}

        def ai(digest):
            seen.update(digest)
            return [{"key": "max_chars", "value": 9999, "confidence": "high"},                      # đã có bằng chứng: bị bỏ qua
                    {"key": "flag:ssml", "value": True, "confidence": "high", "ref": "ai", "quote": "có vẻ hỗ trợ SSML"}]
        r = A.analyze(FIXTURE, ai_infer=ai)
        self.assertIn("cli_args", seen)
        self.assertEqual(r["capabilities"]["max_chars"], 480)
        ssml = r["profile"]["meta"]["capability_facts"]["ssml"]
        self.assertEqual((ssml["value"], ssml["source"], ssml["confidence"]), (True, "ai_inference", "medium"))   # bị hạ từ high
        self.assertEqual(S.validate_annotated(r["profile"]), [])

    def test_analyzer_never_executes_repo_code(self):
        marker = self.tmp / "executed.flag"
        repo = self.repo({"pkg/evil.py": f"open(r'{marker}', 'w').write('x')\n", "setup.py": f"open(r'{marker}', 'w').write('x')\n"})
        A.analyze(repo, engine="evil")
        self.assertFalse(marker.exists())

    def test_write_onboarding_outputs(self):
        out = self.tmp / "onboard"
        paths = A.write_onboarding(A.analyze(FIXTURE), out)
        self.assertEqual(set(paths), {"profile", "analysis", "needs_user", "config_snippet"})
        prof = json.loads(paths["profile"].read_text(encoding="utf-8"))
        self.assertEqual(S.validate_annotated(prof), [])
        flat = S.resolve(prof, json.loads(paths["analysis"].read_text(encoding="utf-8"))["capabilities"], "vi")
        self.assertEqual((flat["segment"]["max_chars"], flat["voice"]), (480, "alice"))              # profile lưu rồi nạp lại dùng được
        self.assertIn("adapter_config", json.loads(paths["config_snippet"].read_text(encoding="utf-8")))

    def test_fetch_reference(self):
        self.assertEqual(A.fetch_reference(str(FIXTURE), self.tmp / "x"), FIXTURE)
        calls = []
        d = A.fetch_reference("https://github.com/acme/tts.git", self.tmp / "g", run=lambda *a, **k: calls.append(a[0]))
        self.assertEqual(calls[0][:4], ["git", "clone", "--depth", "1"])
        self.assertEqual(d.name, "repo")

        class Resp:
            def read(self):
                return b"<html><script>x()</script><body><h1>API</h1><p>Maximum of 300 characters per request.</p></body></html>"
        d = A.fetch_reference("https://docs.acme.example/tts", self.tmp / "h", urlopen=lambda *a, **k: Resp())
        r = A.analyze(d, engine="docs-only")
        self.assertEqual(r["capabilities"]["max_chars"], 300)                                        # chỉ có docs URL cũng đủ để bắt đầu
        self.assertEqual(r["profile"]["meta"]["capability_facts"]["max_chars"]["source"], "official_docs")
        with self.assertRaises(FileNotFoundError):
            A.fetch_reference("không-phải-đường-dẫn", self.tmp / "z")


# =============================================================================== Auto Tune
class LimitTTS(FakeTTS):
    """Engine giả có giới hạn thực tế khác nhau tuỳ `mode` khi segment dài hơn `limit`."""

    def __init__(self, mode: str, limit: int = 450, declared: int | None = None):
        self.mode, self.limit, self.declared, self.calls = mode, limit, declared, 0
        self.engine_id = "limit"

    def capabilities(self):
        return {"max_chars": self.declared, "languages": ["vi", "en"], "engine_version": "l1"}

    def synthesize(self, segment, profile, out_path, ctx):
        self.calls += 1
        n, over = len(segment["text"]), len(segment["text"]) > self.limit
        secs = max(0.2, n / 400)
        if over and self.mode == "fail":
            raise StageError(ErrorClass.TRANSIENT, "TTS_TOO_LONG", "input too long")
        if over and self.mode == "timeout":
            raise StageError(ErrorClass.TRANSIENT, "TTS_TIMEOUT", "no response")
        if over and self.mode == "corrupt":
            out_path.write_bytes(b"RIFFgarbage")
            return {}
        if over and self.mode == "silent":
            write_wav(out_path, b"\x00\x00" * int(8000 * secs))
            return {}
        if over and self.mode == "anomaly":
            secs *= 10
        if self.mode == "broken":
            raise StageError(ErrorClass.TRANSIENT, "TTS_DOWN", "engine unavailable")
        return super().synthesize({**segment, "text": "x" * int(secs * 400)}, profile, out_path, ctx)


class AutoTuneTest(unittest.TestCase):
    def tune(self, adapter, **kw):
        t = AutoTuner(adapter, "vi", **kw)
        self.addCleanup(shutil.rmtree, t.workdir, True)
        return t.run()

    def test_detects_each_kind_of_failure_and_finds_a_safe_length(self):
        expected = {"fail": "request_failure", "timeout": "timeout", "corrupt": "corrupt", "silent": "silence",
                    "anomaly": "duration_anomaly"}
        for mode, label in expected.items():
            r = self.tune(LimitTTS(mode, limit=450))
            self.assertTrue(r["works"], mode)
            self.assertIn(label, r["first_failure"]["failures"], mode)
            self.assertTrue(0 < r["max_ok_chars"] <= 450, (mode, r["max_ok_chars"]))
            rec = r["recommendation"]
            self.assertEqual(rec["max_chars"], r["max_ok_chars"])
            self.assertLess(rec["preferred_chars"], rec["max_chars"])
            self.assertTrue(rec["limit_is_hard"])
            self.assertIn("ký tự", r["summary"])

    def test_engine_without_problems_is_reported_as_not_failing(self):
        r = self.tune(LimitTTS("fail", limit=10 ** 6, declared=600))
        self.assertIsNone(r["first_failure"])
        self.assertTrue(r["recommendation"]["capped_by_declared_limit"])
        self.assertFalse(r["recommendation"]["limit_is_hard"])
        self.assertGreater(r["cps_median"], 0)
        self.assertEqual(r["measured"]["sample_rate"], 8000)

    def test_broken_engine_and_request_budget(self):
        r = self.tune(LimitTTS("broken"))
        self.assertFalse(r["works"])
        self.assertIsNone(r["recommendation"])
        self.assertEqual(r["requests"], 1)                                                           # hỏng ngay bước cơ bản thì dừng, không đốt request
        a = LimitTTS("fail", limit=10 ** 6)
        r = self.tune(a, max_requests=8)
        self.assertLessEqual(a.calls, 8)
        self.assertTrue(r["budget_exhausted"])

    def test_apply_to_profile_records_runtime_evidence_and_keeps_alternatives(self):
        analyzed = A.analyze(FIXTURE)
        prof = analyzed["profile"]
        adapter = LimitTTS("fail", limit=300, declared=480)
        r = self.tune(adapter)
        out = apply_to_profile(prof, r, "run-1")
        self.assertEqual(S.validate_annotated(out), [])
        mx = out["segment"]["max_chars"]
        self.assertEqual((mx["source"], mx["confidence"]), ("runtime_test", "high"))
        self.assertLessEqual(mx["value"], 300)
        self.assertEqual(mx["alternatives"][0]["value"], 480)                                        # giá trị từ tài liệu không mất
        self.assertEqual(mx["evidence"][0]["ref"], "autotune:run-1")
        self.assertEqual(out["qa"]["duration_chars_per_sec"]["source"], "runtime_test")
        self.assertFalse(out["meta"]["needs_tune"])
        self.assertEqual(prof["segment"]["max_chars"]["value"], 480)                                 # profile gốc không bị sửa tại chỗ
        flat = S.resolve(out, analyzed["capabilities"], "vi")
        self.assertEqual(flat["segment"]["max_chars"], mx["value"])
        self.assertEqual(flat["qa"]["duration_chars_per_sec"], out["qa"]["duration_chars_per_sec"]["value"])

    def test_failed_tune_leaves_profile_as_candidate(self):
        prof = A.analyze(FIXTURE)["profile"]
        out = apply_to_profile(prof, self.tune(LimitTTS("broken")))
        self.assertEqual(out["status"], "candidate")
        self.assertFalse(out["meta"]["autotune"]["works"])
        self.assertEqual(out["segment"]["max_chars"]["value"], 480)

    def test_tune_real_cli_engine_onboarded_from_fixture(self):
        res = A.analyze(FIXTURE)
        spec = json.loads(json.dumps(res["adapter"]["spec"]))
        spec["command"][0] = sys.executable
        adapter = CommandTTS(spec)
        r = self.tune(adapter, timeout_s=30)
        self.assertTrue(r["works"], r.get("summary"))
        self.assertEqual(r["measured"]["sample_rate"], 24000)                                        # đo thật, khớp tài liệu
        self.assertLessEqual(r["recommendation"]["max_chars"], 480)
        self.assertAlmostEqual(r["cps_median"], 15, delta=1.5)

    def test_benchmark_corpus_covers_required_text_kinds(self):
        for lang in ("vi", "en"):
            t = compose(lang, 600)
            self.assertLessEqual(len(t), 600)
            self.assertGreater(len(t), 300)
        from contentfactory.tts.autotune import BENCH
        ids = {i for i, _ in BENCH["vi"]}
        self.assertTrue({"short", "long", "dialogue", "commas", "question", "exclaim", "ellipsis", "numbers_dates", "names"} <= ids)


if __name__ == "__main__":
    unittest.main()
