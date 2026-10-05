"""Phase 4: Audio Quality Pipeline — Pause Engine, boundary cleanup/join, Narration Master (loudness/compressor/limiter),
watermark + YouTube, TikTok time-stretch giữ cao độ + split thông minh, Audio QA, và pipeline thật với ffmpeg.

Phần tính toán thuần (pause, split, QA, parser, profile, wavio) chạy mọi nơi; phần dùng ffmpeg được bỏ qua nếu máy không có ffmpeg."""
import json
import math
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from contentfactory import fsutil
from contentfactory.audio import ffmpeg as F
from contentfactory.audio import profile as PR
from contentfactory.audio import wavio
from contentfactory.audio.pause import plan_pauses
from contentfactory.audio.processor import FfmpegAudio
from contentfactory.audio.qa import evaluate
from contentfactory.audio.split import plan_split
from contentfactory.contracts import ErrorClass, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator.stages import StageContract
from contentfactory.tts.autotune import make_ctx
from tests.support import RootCase, params

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = unittest.skipUnless(HAVE_FFMPEG, "cần ffmpeg + ffprobe")


# ============================================================================ tiện ích sinh/đo audio (stdlib)
def write_pcm(path: Path, samples: list[int], rate: int = 24000, ch: int = 1, width: int = 2) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        else:
            w.writeframes(b"".join(int(v).to_bytes(3, "little", signed=True) for v in samples))
    return path


def sine(f: float, secs: float, rate: int = 24000, amp: float = 0.5) -> list[int]:
    return [int(amp * 32767 * math.sin(2 * math.pi * f * i / rate)) for i in range(int(rate * secs))]


def burst(path: Path, lead: float, tone: float, trail: float, f: float = 300, rate: int = 24000, amp: float = 0.5) -> Path:
    return write_pcm(path, [0] * int(rate * lead) + sine(f, tone, rate, amp) + [0] * int(rate * trail), rate)


def decode(path: Path, t0: float = 0.0, t1: float | None = None) -> tuple[list[int], int]:
    i = wavio.info(path)
    a = int(t0 * i["rate"])
    n = (i["frames"] if t1 is None else int(t1 * i["rate"])) - a
    data = b"".join(wavio.read_frames(path, i, a, n))
    vals = wavio._decode(data, i["width"])
    return vals[0::i["channels"]], i["rate"]


def rms_db(path: Path, t0: float, t1: float) -> float:
    v, _ = decode(path, t0, t1)
    full = float(1 << (8 * wavio.info(path)["width"] - 1))
    ms = sum((x / full) ** 2 for x in v) / max(1, len(v))
    return 10 * math.log10(ms) if ms > 0 else -200.0


def dominant_freq(path: Path, t0: float, t1: float, fmin: int = 200, fmax: int = 1400, step: int = 10) -> int:
    v, rate = decode(path, t0, t1)
    v = v[::4]                                              # decimate (đủ cho < 5 kHz) để Goertzel đủ nhanh
    r = rate / 4
    best, bf = -1.0, 0
    for f in range(fmin, fmax + 1, step):
        w = 2 * math.cos(2 * math.pi * f / r)
        s1 = s2 = 0.0
        for x in v:
            s0 = x + w * s1 - s2
            s2, s1 = s1, s0
        mag = s1 * s1 + s2 * s2 - w * s1 * s2
        if mag > best:
            best, bf = mag, f
    return bf


def silent_runs(path: Path, thresh: float = 0.003, min_s: float = 0.05) -> list[tuple[float, float]]:
    """[(bắt đầu, độ dài)] các khoảng im lặng (|mẫu| < ngưỡng)."""
    v, rate = decode(path)
    full = float(1 << (8 * wavio.info(path)["width"] - 1))
    runs, start = [], None
    for i, x in enumerate(v):
        quiet = abs(x) / full < thresh
        if quiet and start is None:
            start = i
        elif not quiet and start is not None:
            if (i - start) / rate >= min_s:
                runs.append((start / rate, (i - start) / rate))
            start = None
    if start is not None and (len(v) - start) / rate >= min_s:
        runs.append((start / rate, (len(v) - start) / rate))
    return runs


def max_step(path: Path, t0: float, t1: float) -> float:
    """Bước nhảy lớn nhất giữa hai mẫu liên tiếp (so với full scale) trong [t0, t1]: tiếng click/pop làm số này vọt lên."""
    v, _ = decode(path, t0, t1)
    full = float(1 << (8 * wavio.info(path)["width"] - 1))
    return max((abs(a - b) for a, b in zip(v, v[1:])), default=0) / full


class AudioCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-audio-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ctx = make_ctx(self.tmp)
        self.audio = FfmpegAudio()

    def use(self, **p):
        self.ctx.params = p

    def chunks(self, n: int = 6, f: float = 300, tone: float = 1.0, lead: float = 0.3, trail: float = 0.4) -> list[Path]:
        return [burst(self.tmp / f"c{i}.wav", lead, tone + 0.1 * i, trail, f=f) for i in range(n)]


# ============================================================================ phần thuần
class PauseEngineTest(unittest.TestCase):
    R = {"scale": 1.0, "min_ms": 0, "max_ms": 3000, "compensate_edge": True, "tail_ms": 300}

    def test_pause_is_compensated_for_edge_silence_kept_by_cleanup(self):
        pl = plan_pauses([0, 600, 350, 0], self.R, keep_ms=40)
        self.assertEqual([p["inserted"] for p in pl], [0, 520, 270, 260])     # 600-2*40; 350-80; 0; tail 300-40
        self.assertEqual([p["requested"] for p in pl], [0, 600, 350, 300])

    def test_rules_scale_clamp_and_no_compensation(self):
        r = {**self.R, "scale": 1.5, "min_ms": 200, "max_ms": 800}
        self.assertEqual([p["inserted"] for p in plan_pauses([100, 600, 900, 0], r, 40)], [120, 720, 720, 260])
        r2 = {**self.R, "compensate_edge": False}
        self.assertEqual(plan_pauses([0, 600, 0], r2, 40)[1]["inserted"], 600)
        self.assertTrue(all(p["inserted"] >= 0 for p in plan_pauses([10, 20, 30, 5], self.R, 100)))   # không bao giờ âm


class SplitPlannerTest(unittest.TestCase):
    @staticmethod
    def sents(total, every, kind="sentence"):
        return [{"t": float(t), "kind": kind} for t in range(every, int(total), every)]

    def check_contiguous(self, plan, total):
        ps = plan["parts"]
        self.assertEqual(ps[0]["start"], 0.0)
        self.assertEqual(ps[-1]["end"], total)
        for a, b in zip(ps, ps[1:]):
            self.assertEqual(a["end"], b["start"])

    def test_cuts_land_on_boundaries_near_target_without_forcing_exact_length(self):
        total, target = 100.0, 30.0
        bs = self.sents(total, 5)
        plan = plan_split(total, bs, target, bonus_sec={})
        self.check_contiguous(plan, total)
        self.assertEqual(plan["n"], 3)
        times = {b["t"] for b in bs}
        for p in plan["parts"][:-1]:
            self.assertIn(p["end"], times)                                  # chỉ cắt đúng ranh giới
            self.assertFalse(p["forced"] or p["mid_sentence"])
        lens = [p["end"] - p["start"] for p in plan["parts"]]
        self.assertTrue(all(25.5 <= x <= 41 for x in lens), lens)             # trong cửa sổ quanh 100/3, không ép đúng 30

    def test_prefers_nearby_boundary_over_cutting_at_exact_target(self):
        plan = plan_split(60.0, [{"t": 28.0, "kind": "sentence"}], 30.0)
        self.assertEqual(plan["parts"][0]["end"], 28.0)                       # không cắt giữa câu ở đúng 30s
        self.assertEqual(plan["n"], 2)

    def test_higher_level_boundary_wins_when_close_enough(self):
        bs = [{"t": 29.0, "kind": "sentence"}, {"t": 32.0, "kind": "paragraph"}, {"t": 28.5, "kind": "silence"}]
        self.assertEqual(plan_split(60.0, bs, 30.0, bonus_sec={"paragraph": 45, "sentence": 15, "silence": 5})["parts"][0]["end"], 32.0)
        bs2 = [{"t": 29.0, "kind": "sentence"}, {"t": 32.0, "kind": "paragraph"}]
        self.assertEqual(plan_split(60.0, bs2, 30.0, bonus_sec={"paragraph": 0, "sentence": 0})["parts"][0]["end"], 29.0)

    def test_mid_sentence_cuts_are_last_resort(self):
        bs = [{"t": 30.0, "kind": "cut"}, {"t": 33.0, "kind": "sentence"}]
        p = plan_split(60.0, bs, 30.0)["parts"][0]
        self.assertEqual((p["end"], p["mid_sentence"]), (33.0, False))        # bỏ qua ranh giới "cut" ngay đúng đích
        only_cut = plan_split(60.0, [{"t": 30.0, "kind": "cut"}], 30.0)
        self.assertEqual((only_cut["parts"][0]["end"], only_cut["parts"][0]["mid_sentence"]), (30.0, True))
        self.assertTrue(only_cut["warnings"])
        none = plan_split(60.0, [], 30.0)
        self.assertTrue(none["parts"][0]["forced"])
        self.assertTrue(none["warnings"])
        self.check_contiguous(none, 60.0)

    def test_window_is_widened_before_giving_up_on_natural_boundary(self):
        plan = plan_split(90.0, [{"t": 35.0, "kind": "paragraph"}], 30.0, 0.9, 1.1)   # ngoài cửa sổ [27, 33] nhưng còn trong ×2
        self.assertEqual(plan["parts"][0]["end"], 35.0)
        self.assertTrue(any("nới" in w for w in plan["warnings"]))

    def test_part_count_and_tail(self):
        self.assertEqual(plan_split(20.0, [], 30.0)["parts"][0]["boundary"], "end")        # ngắn hơn target: một part
        self.assertEqual(len(plan_split(20.0, [], 30.0)["parts"]), 1)
        p = plan_split(62.0, self.sents(62, 3), 30.0)
        self.assertEqual(p["n"], 2)                                                         # không sinh part cuối vụn
        self.assertTrue(all(x["end"] - x["start"] > 25 for x in p["parts"]))
        big = plan_split(3650.0, self.sents(3650, 7), 600.0)                                # cỡ thật: 1 giờ -> ~6 part ~10 phút
        self.assertEqual(big["n"], 6)
        self.check_contiguous(big, 3650.0)
        self.assertTrue(all(0.85 * 600 <= x["end"] - x["start"] <= 1.15 * 600 for x in big["parts"]),
                        [x["end"] - x["start"] for x in big["parts"]])

    def test_last_part_may_be_shorter_like_handoff_describes(self):
        """852 s, target 600 s: một part ~600 s + một part cuối ngắn hơn (không chia đều 2×426 s)."""
        plan = plan_split(852.0, self.sents(852, 20), 600.0)
        self.assertEqual((plan["n"], plan["mode"]), (2, "tail"))
        first, last = plan["parts"]
        self.assertTrue(510 <= first["end"] <= 690, first)
        self.assertLess(last["end"] - last["start"], first["end"] - first["start"])
        self.check_contiguous(plan, 852.0)

    def test_small_remainder_is_absorbed_instead_of_a_tiny_last_part(self):
        for total in (650.0, 1250.0, 3650.0):
            plan = plan_split(total, self.sents(total, 9), 600.0)
            self.assertEqual(plan["mode"] in ("even", "single"), True, total)
            lens = [p["end"] - p["start"] for p in plan["parts"]]
            self.assertGreater(min(lens), 0.8 * 600, (total, lens))
            self.assertLessEqual(max(lens), 1.2 * 600, (total, lens))
        self.assertEqual(plan_split(800.0, self.sents(800, 9), 600.0)["n"], 2)        # dồn dư làm quá dài => chia đều thành 2 part

    def test_uneven_boundaries_never_break_window_feasibility(self):
        bs = [{"t": t, "kind": "sentence"} for t in (12.0, 13.0, 41.0, 77.0, 78.0, 99.0, 131.0, 160.0)]
        plan = plan_split(180.0, bs, 45.0)
        self.check_contiguous(plan, 180.0)
        self.assertTrue(all(0 < x["end"] - x["start"] <= 45 * 1.15 * 2 for x in plan["parts"]))


class QAEvaluateTest(unittest.TestCase):
    CFG = PR.DEFAULT["qa"]

    def m(self, **kw):
        base = {"probe": {"duration": 60.0, "sample_rate": 48000, "channels": 1, "codec": "pcm_s24le"},
                "stats": {"peak_db": -3.0, "peak_count": 1, "samples": 2880000}, "silences": [],
                "loudness": {"I": -16.0, "TP": -2.0, "LRA": 5.0}, "decode_errors": 0}
        for k, v in kw.items():
            base[k] = {**base[k], **v} if isinstance(v, dict) else v
        return base

    def codes(self, m, expect=None, kind="narration"):
        r = evaluate(m, {"kind": kind, **(expect or {})}, self.CFG)
        return {e["code"] for e in r["errors"]}, {w["code"] for w in r["warnings"]}, r

    def test_clean_audio_passes(self):
        errs, warns, r = self.codes(self.m(), {"duration_sec": 60.0, "sample_rate": 48000, "channels": 1, "codec_prefix": "pcm",
                                               "lufs": -16.0, "true_peak_db": -1.0, "chunks": {"expected": 5, "found": 5}})
        self.assertEqual((errs, warns, r["ok"]), (set(), set(), True))

    def test_each_defect_is_detected(self):
        cases = {
            "CORRUPT": (self.m(decode_errors=3), {}),
            "EMPTY": (self.m(stats={"peak_db": float("-inf")}), {}),
            "EMPTY ": (self.m(probe={"duration": 0.05}), {}),
            "FORMAT_MISMATCH": (self.m(probe={"sample_rate": 22050}), {"sample_rate": 48000}),
            "FORMAT_MISMATCH ": (self.m(probe={"codec": "mp3"}), {"codec_prefix": "pcm"}),
            "ABNORMAL_DURATION": (self.m(probe={"duration": 30.0}), {"duration_sec": 60.0}),
            "MISSING_CHUNKS": (self.m(), {"chunks": {"expected": 10, "found": 9}}),
            "CLIPPING": (self.m(stats={"peak_db": 0.0, "peak_count": 500}), {}),
            "TRUE_PEAK_OVER": (self.m(loudness={"TP": 0.5}), {"true_peak_db": -1.0}),
            "EXCESSIVE_SILENCE": (self.m(silences=[{"start": 0, "end": 30, "duration": 30.0}]), {}),
            "EXCESSIVE_SILENCE ": (self.m(silences=[{"start": 5, "end": 10, "duration": 5.0}]), {}),
            "LOUDNESS_OFF": (self.m(loudness={"I": -23.0}), {"lufs": -16.0}),
        }
        for label, (m, expect) in cases.items():
            errs, _, r = self.codes(m, expect)
            self.assertIn(label.strip(), errs, label)
            self.assertFalse(r["ok"], label)

    def test_duration_tolerance_and_isolated_clipping_sample(self):
        self.assertEqual(self.codes(self.m(probe={"duration": 60.8}), {"duration_sec": 60.0})[0], set())     # trong ±max(1s, 2%)
        self.assertEqual(self.codes(self.m(stats={"peak_db": -0.05, "peak_count": 2}))[0], set())           # vài mẫu chạm đỉnh: chấp nhận

    def test_input_kind_only_hard_fails_on_broken_or_empty(self):
        errs, warns, r = self.codes(self.m(stats={"peak_db": 0.0, "peak_count": 900}, loudness={"I": -3.0},
                                           silences=[{"start": 0, "end": 40, "duration": 40.0}]), kind="input")
        self.assertEqual(errs, set())                                     # mastering sẽ xử lý clipping/độ to/khoảng lặng
        self.assertIn("CLIPPING", warns)
        self.assertTrue(r["ok"])
        self.assertIn("CORRUPT", self.codes(self.m(decode_errors=1), kind="input")[0])


class FfmpegParsersTest(unittest.TestCase):
    ASTATS = ("[Parsed_astats_0 @ 000001c9644a8bc0] Overall\n[Parsed_astats_0 @ 000001c9644a8bc0] DC offset: 0.000000\n"
              "[Parsed_astats_0 @ 000001c9644a8bc0] Peak level dB: -6.020865\n[Parsed_astats_0 @ 000001c9644a8bc0] RMS level dB: -9.031267\n"
              "[Parsed_astats_0 @ 000001c9644a8bc0] Peak count: 800.000000\n[Parsed_astats_0 @ 000001c9644a8bc0] Number of samples: 240000\n"
              "[Parsed_ebur128_2 @ 000001c9644aadc0] Summary:\n")

    def test_astats(self):
        s = F.parse_astats(self.ASTATS)
        self.assertEqual((s["peak_db"], s["peak_count"], s["samples"]), (-6.020865, 800.0, 240000.0))
        self.assertEqual(F.parse_astats("không có gì"), {})
        self.assertEqual(F.parse_astats(self.ASTATS.replace("-6.020865", "-inf"))["peak_db"], float("-inf"))

    def test_silence_including_open_ended(self):
        t = ("[silencedetect @ 0x1] silence_start: 1.5\n[silencedetect @ 0x1] silence_end: 3.0 | silence_duration: 1.5\n"
             "[silencedetect @ 0x1] silence_start: 8.25\n")
        got = F.parse_silence(t, duration=10.0)
        self.assertEqual([(s["start"], s["end"]) for s in got], [(1.5, 3.0), (8.25, 10.0)])
        self.assertEqual(len(F.parse_silence(t)), 1)

    def test_ebur128_and_loudnorm(self):
        t = "x\n[Parsed_ebur128_2 @ 0x] Summary:\n\n  Integrated loudness:\n    I:         -16.2 LUFS\n  Loudness range:\n    LRA:         5.1 LU\n" \
            "  True peak:\n    Peak:       -1.7 dBFS\n"
        self.assertEqual(F.parse_ebur128(t), {"I": -16.2, "LRA": 5.1, "TP": -1.7})
        ln = 'noise\n{\n\t"input_i" : "-9.75",\n\t"input_tp" : "-6.02",\n\t"input_lra" : "0.00",\n\t"input_thresh" : "-19.75",\n' \
             '\t"target_offset" : "-0.05",\n\t"normalization_type" : "dynamic"\n}\n'
        m = F.parse_loudnorm_json(ln)
        self.assertEqual((m["input_i"], m["target_offset"], m["normalization_type"]), (-9.75, -0.05, "dynamic"))
        with self.assertRaises(StageError):
            F.parse_loudnorm_json("không có json")

    def test_decode_error_counter_ignores_filter_noise(self):
        t = "[Parsed_astats_0 @ 0x] Peak level dB: -3\n[wav @ 0x] Invalid data found when processing input\nsize=N/A time=00:00:01\n"
        self.assertEqual(F.count_decode_errors(t), 1)
        self.assertEqual(F.count_decode_errors("[Parsed_ebur128_2 @ 0x] Summary:\n"), 0)

    def test_error_classification(self):
        self.assertEqual(F.classify(1, "No space left on device", ["ffmpeg"]).resource, "disk")
        self.assertEqual(F.classify(1, "x.wav: Invalid data found when processing input", ["ffmpeg"]).code, "AUDIO_DECODE_FAILED")
        self.assertEqual(F.classify(1, "x.wav: No such file or directory", ["ffmpeg"]).resource, "input")
        self.assertEqual(F.classify(1, "No such filter: 'foo'", ["ffmpeg"]).error_class, ErrorClass.POLICY)
        self.assertEqual(F.classify(137, "killed", ["ffmpeg"]).error_class, ErrorClass.TRANSIENT)

    def test_missing_tool_is_a_resource_problem_not_a_failure(self):
        a = FfmpegAudio({"ffmpeg": "định-không-tồn-tại-ffmpeg", "ffprobe": "định-không-tồn-tại-ffprobe"})
        self.assertFalse(a.health()["ok"])
        with self.assertRaises(StageError) as e:
            a.tools.ffmpeg(["-version"])
        self.assertEqual((e.exception.error_class, e.exception.code, e.exception.resource), (ErrorClass.RESOURCE, "FFMPEG_MISSING", "runtime"))


class ProfileTest(unittest.TestCase):
    def test_defaults_and_overrides(self):
        d = PR.resolve({})
        self.assertEqual((d["format"]["sample_rate"], d["format"]["channels"], d["format"]["sample_fmt"]), (48000, 1, "s24le"))
        self.assertEqual((d["tiktok"]["speed"], d["tiktok"]["target_part_sec"]), (2.0, 600.0))
        self.assertTrue(d["master"]["loudness"]["enabled"])
        self.assertFalse(d["master"]["compressor"]["enabled"])                         # nén nhẹ chỉ bật khi profile yêu cầu
        p = PR.resolve({"audio": {"master": {"loudness": {"target_lufs": -20}, "compressor": {"enabled": True}}},
                        "tiktok": {"speed": 1.5, "target_part_sec": 300}, "watermark": "w.wav"})
        self.assertEqual((p["master"]["loudness"]["target_lufs"], p["master"]["loudness"]["lra"]), (-20, 11.0))      # trộn sâu với mặc định
        self.assertTrue(p["master"]["compressor"]["enabled"])
        self.assertEqual((p["tiktok"]["speed"], p["tiktok"]["target_part_sec"], p["watermark_path"]), (1.5, 300.0, "w.wav"))
        self.assertEqual(PR.DEFAULT["master"]["loudness"]["target_lufs"], -16.0)       # resolve không làm bẩn DEFAULT

    def test_invalid_profiles_are_rejected(self):
        for bad in ({"audio": {"format": {"sample_fmt": "mp3"}}}, {"audio": {"format": {"channels": 5}}}, {"tiktok": {"speed": 9}},
                    {"tiktok": {"target_part_sec": 0}}, {"audio": {"tiktok": {"split": {"min_ratio": 1.5}}}},
                    {"audio": {"youtube": {"watermark": {"position": "middle"}}}}, {"audio": {"master": {"limiter": {"ceiling_db": 3}}}},
                    {"audio": {"join": {"pause": {"min_ms": 900, "max_ms": 100}}}}, {"audio": {"tiktok": {"stretch": {"engine": "magic"}}}}):
            with self.assertRaises(StageError, msg=str(bad)) as e:
                PR.resolve(bad)
            self.assertEqual(e.exception.code, "INVALID_AUDIO_PROFILE")


class StageKeyIsolationTest(unittest.TestCase):
    """Đổi watermark/mastering không được làm TTS chạy lại; đổi cách ghép chunk thì TTS (assemble) phải chạy lại."""

    def key(self, stage: str, params: dict) -> str:
        return StageContract(P.BY_NAME[stage]).stage_key(params, None, {"audio_master": [{"sha256": "x", "path": "p", "kind": "k", "bytes": 1, "meta": {}}]})

    def test_watermark_and_mastering_do_not_touch_tts_but_do_touch_audio(self):
        base = {"watermark": "a.wav", "audio": {"master": {"loudness": {"target_lufs": -16}}}, "tts": {"voice": "v"}}
        for change in ({"watermark": "b.wav"}, {"audio": {"master": {"loudness": {"target_lufs": -20}}}},
                       {"audio": {"tiktok": {"split": {"fade_ms": 5}}}}, {"tiktok": {"speed": 1.5}}):
            changed = {**base, **change}
            self.assertEqual(self.key("tts", base), self.key("tts", changed), change)
            self.assertNotEqual(self.key("audio", base), self.key("audio", changed), change)

    def test_join_profile_affects_tts_stage_but_not_audio_stage(self):
        base = {"audio": {"join": {"pause": {"scale": 1.0}}}}
        changed = {"audio": {"join": {"pause": {"scale": 1.4}}}}
        self.assertNotEqual(self.key("tts", base), self.key("tts", changed))
        self.assertEqual(self.key("audio", base), self.key("audio", changed))


class WavioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-wavio-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_concat_is_lossless_and_rejects_format_mismatch(self):
        a = write_pcm(self.tmp / "a.wav", sine(440, 0.5), 24000)
        b = write_pcm(self.tmp / "b.wav", sine(220, 0.25), 24000)
        r = wavio.concat([a, b], self.tmp / "o.wav")
        self.assertEqual(r["offsets"], [0.0, 0.5])
        va, _ = decode(a)
        vb, _ = decode(b)
        self.assertEqual(decode(self.tmp / "o.wav")[0], va + vb)
        c = write_pcm(self.tmp / "c.wav", sine(440, 0.1, 16000), 16000)
        with self.assertRaises(ValueError):
            wavio.concat([a, c], self.tmp / "bad.wav")
        self.assertFalse((self.tmp / "bad.wav").exists())

    def test_24bit_and_silence_and_slice_with_fade(self):
        s24 = [int(0.5 * 8388607 * math.sin(i / 10)) for i in range(4800)]
        p = write_pcm(self.tmp / "w.wav", s24, 48000, width=3)
        self.assertEqual(decode(p)[0], s24)                                  # đọc/ghi 24-bit không mất bit nào
        z = wavio.write_silence(self.tmp / "z.wav", 250, 48000, 1, 3)
        self.assertEqual((wavio.info(z)["frames"], set(decode(z)[0])), (12000, {0}))
        wavio.slice_wav(p, self.tmp / "s.wav", 0.01, 0.08, fade_ms=10)
        v, _ = decode(self.tmp / "s.wav")
        self.assertEqual(len(v), int(0.07 * 48000))
        self.assertLess(abs(v[0]), abs(s24[480]) * 0.05 + 50)               # fade-in: bắt đầu gần 0 thay vì nhảy vọt
        self.assertLess(abs(v[-1]), 0.05 * 8388607)
        raw_slice = s24[480:480 + len(v)]
        self.assertEqual(v[len(v) // 2], raw_slice[len(v) // 2])             # phần giữa không bị đụng tới
        with self.assertRaises(ValueError):
            wavio.slice_wav(p, self.tmp / "e.wav", 0.5, 0.4)

    def test_header_parser_reads_extensible_and_truncated(self):
        p = write_pcm(self.tmp / "t.wav", sine(440, 1.0), 24000)
        data = p.read_bytes()
        self.assertEqual(fsutil.wav_header(p)["frames"], 24000)
        p.write_bytes(data[:len(data) // 2])                                 # file bị cắt cụt: frames phản ánh dữ liệu thực có
        self.assertLess(fsutil.wav_header(p)["frames"], 24000)
        ext = bytearray(data[:44])
        # dựng header WAVE_FORMAT_EXTENSIBLE (tag 0xFFFE, subformat PCM) giống ffmpeg ghi cho 24-bit
        fmt = struct.pack("<HHIIHH", 0xFFFE, 1, 48000, 144000, 3, 24) + struct.pack("<HHI", 22, 24, 4) + struct.pack("<H", 1) + b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
        body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", 300) + b"\x01" * 300
        (self.tmp / "x.wav").write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
        h = fsutil.wav_header(self.tmp / "x.wav")
        self.assertEqual((h["format_tag"], h["bits"], h["frames"]), (1, 24, 100))
        (self.tmp / "g.wav").write_bytes(b"not a wav at all")
        with self.assertRaises(ValueError):
            fsutil.wav_header(self.tmp / "g.wav")
        _ = ext


# ============================================================================ có ffmpeg: join / pause / boundary
@needs_ffmpeg
class JoinTest(AudioCase):
    def test_many_chunks_join_cleanly_in_canonical_format(self):
        chunks = self.chunks(12)
        pauses = [0, 600, 0, 600, 350, 0, 600, 0, 0, 600, 350, 0]
        out = self.tmp / "raw.wav"
        r = self.audio.assemble(chunks, pauses, out, self.ctx)
        i = wavio.info(out)
        self.assertEqual((i["rate"], i["channels"], i["width"]), (48000, 1, 3))                  # chuẩn hóa kỹ thuật: 48 kHz mono 24-bit
        self.assertEqual(len(r["timeline"]), 12)
        self.assertAlmostEqual(i["duration"], r["duration_sec"], places=2)
        tl = r["timeline"]
        self.assertTrue(all(a["end_sec"] <= b["start_sec"] + 1e-6 for a, b in zip(tl, tl[1:])))   # không chồng lấn
        self.assertAlmostEqual(tl[-1]["end_sec"] + r["pauses"][-1]["inserted"] / 1000, r["duration_sec"], delta=0.01)
        speech_total = sum(1.0 + 0.1 * k for k in range(12))
        self.assertGreater(i["duration"], speech_total)                                           # có pause
        self.assertLess(i["duration"], speech_total + 12 * 0.8 + 1.0)                             # nhưng không dư thừa im lặng (biên 0.3/0.4s đã cắt)
        # không có click/pop: ở mọi điểm nối, bước nhảy giữa hai mẫu liền nhau rất nhỏ
        for e in tl[:-1]:
            self.assertLess(max_step(out, e["end_sec"] - 0.05, e["end_sec"] + 0.2), 0.03, e)      # 0.02 là độ dốc tự nhiên của sin 300 Hz
        self.assertEqual(self.audio.qa_full(out, {"kind": "input"}, self.ctx)["ok"], True)

    def test_naive_concat_clicks_but_cleanup_does_not(self):
        """Chunk cắt giữa sóng, không có im lặng ở biên (xấu nhất): nối thô gây nhảy biên độ, quy trình của ta thì không."""
        raw = [write_pcm(self.tmp / f"r{i}.wav", sine(300, 0.5 + 0.013 * i), 24000) for i in range(5)]
        naive = wavio.concat([self._to_canon(p, f"n{i}.wav") for i, p in enumerate(raw)], self.tmp / "naive.wav")
        r = self.audio.assemble(raw, [0, 0, 0, 0, 0], self.tmp / "clean.wav", self.ctx)
        step_naive = max(max_step(self.tmp / "naive.wav", t - 0.002, t + 0.002) for t in naive["offsets"][1:])
        step_clean = max(max_step(self.tmp / "clean.wav", e["end_sec"] - 0.02, e["end_sec"] + 0.04) for e in r["timeline"][:-1])
        self.assertGreater(step_naive, 0.1)
        self.assertLess(step_clean, 0.03)

    def _to_canon(self, p: Path, name: str) -> Path:
        out = self.tmp / name
        self.audio.tools.ffmpeg(["-v", "error", "-i", str(p), "-ar", "48000", "-ac", "1", "-c:a", "pcm_s24le", "-f", "wav", "-y", str(out)])
        return out

    def test_pause_engine_gives_the_requested_audible_pause(self):
        self.use(audio={"join": {"pause": {"scale": 1.0, "tail_ms": 0}}})
        out = self.tmp / "raw.wav"
        self.audio.assemble(self.chunks(4), [0, 700, 400, 0], out, self.ctx)
        gaps = [d for s, d in silent_runs(out, thresh=0.002, min_s=0.2)]
        # im lặng nghe được giữa các chunk: 700 ms và 400 ms (đã trừ phần biên), chunk liền nhau (0 ms) chỉ còn ~2×keep
        self.assertTrue(any(abs(g - 0.700) < 0.03 for g in gaps), gaps)
        self.assertTrue(any(abs(g - 0.400) < 0.03 for g in gaps), gaps)
        self.assertFalse(any(g > 0.75 for g in gaps), gaps)
        self.use(audio={"join": {"pause": {"scale": 2.0, "max_ms": 3000, "tail_ms": 0}}})
        out2 = self.tmp / "raw2.wav"
        self.audio.assemble(self.chunks(4), [0, 700, 400, 0], out2, self.ctx)
        self.assertTrue(any(abs(g - 1.4) < 0.04 for g in [d for _, d in silent_runs(out2, 0.002, 0.2)]))     # rule trong profile, không hardcode

    def test_boundary_cleanup_trims_long_edges_and_fades(self):
        chunks = [burst(self.tmp / "e.wav", 1.5, 1.0, 1.5)]
        out = self.tmp / "o.wav"
        r = self.audio.assemble(chunks, [0], out, self.ctx)
        self.assertAlmostEqual(r["timeline"][0]["end_sec"], 1.0 + 0.08, delta=0.05)                # 1.0s tiếng + 2×40ms biên giữ lại
        v, rate = decode(out)
        full = float(1 << 23)
        onset = next(i for i, x in enumerate(v) if abs(x) / full > 0.05) / rate                   # sóng 300 Hz biên độ 0.5 bắt đầu sau ~40 ms biên
        self.assertAlmostEqual(onset, 0.04, delta=0.02)
        self.assertLess(max(abs(x) for x in v[:int(0.03 * rate)]) / full, 0.002)                  # biên chuẩn là im lặng
        self.assertLess(max_step(out, 0, 0.1), 0.025)                                            # fade-in: không có bước nhảy vọt khi vào tiếng

    def test_profile_changes_the_canonical_format(self):
        self.use(audio={"format": {"sample_rate": 44100, "channels": 2, "sample_fmt": "s16le"}})
        out = self.tmp / "o.wav"
        self.audio.assemble(self.chunks(2), [0, 0], out, self.ctx)
        i = wavio.info(out)
        self.assertEqual((i["rate"], i["channels"], i["width"]), (44100, 2, 2))

    def test_missing_and_empty_chunks_are_reported(self):
        chunks = self.chunks(4)
        chunks[2].unlink()
        with self.assertRaises(StageError) as e:
            self.audio.assemble(chunks, [0] * 4, self.tmp / "o.wav", self.ctx)
        self.assertEqual((e.exception.code, e.exception.detail["missing"]), ("MISSING_CHUNKS", [3]))
        with self.assertRaises(StageError) as e:
            self.audio.assemble(self.chunks(3), [0, 0], self.tmp / "o.wav", self.ctx)                # số pause không khớp số chunk
        self.assertEqual(e.exception.code, "MISSING_CHUNKS")
        silent = write_pcm(self.tmp / "s.wav", [0] * 24000)
        with self.assertRaises(StageError) as e:
            self.audio.assemble([silent], [0], self.tmp / "o2.wav", self.ctx)
        self.assertEqual(e.exception.code, "CHUNK_EMPTY")
        bad = self.tmp / "bad.wav"
        bad.write_bytes(b"garbage")
        with self.assertRaises(StageError) as e:
            self.audio.assemble([bad], [0], self.tmp / "o3.wav", self.ctx)
        self.assertEqual((e.exception.error_class, e.exception.code), (ErrorClass.POLICY, "AUDIO_DECODE_FAILED"))

    def test_resume_reuses_prepared_chunks(self):
        chunks = self.chunks(3)
        self.audio.assemble(chunks, [0, 600, 0], self.tmp / "a.wav", self.ctx)
        marks = {p: p.stat().st_mtime_ns for p in (self.tmp / "join").glob("0000*.wav")}
        self.audio.assemble(chunks, [0, 600, 0], self.tmp / "b.wav", self.ctx)
        self.assertEqual(marks, {p: p.stat().st_mtime_ns for p in (self.tmp / "join").glob("0000*.wav")})   # không xử lý lại
        self.assertEqual((self.tmp / "a.wav").read_bytes(), (self.tmp / "b.wav").read_bytes())


# ============================================================================ có ffmpeg: master
@needs_ffmpeg
class MasterTest(AudioCase):
    def speech_like(self, name: str, amp_db: float, secs: float = 6.0) -> Path:
        """Có biến thiên độ to như giọng nói (loudnorm chỉ chạy chế độ linear khi LRA đo được khác 0)."""
        amp = 10 ** (amp_db / 20)
        out: list[int] = []
        for k in range(int(secs / 0.5)):
            out += sine(300, 0.5, 24000, amp * (1.0 if k % 3 else 0.45))
        return write_pcm(self.tmp / name, out, 24000)

    def measure(self, p: Path) -> dict:
        return self.audio.tools.measure(p, -45, 0.5)

    def test_loudness_hits_target_and_true_peak_stays_under_ceiling(self):
        for src_db in (-30.0, -6.0):                                         # quá nhỏ và quá to đều về cùng mục tiêu
            raw = self.speech_like(f"s{int(-src_db)}.wav", src_db)
            out = self.tmp / f"m{int(-src_db)}.wav"
            info = self.audio.master(raw, out, self.ctx)
            m = self.measure(out)
            self.assertAlmostEqual(m["loudness"]["I"], -16.0, delta=0.8, msg=src_db)
            self.assertLessEqual(m["loudness"]["TP"], -1.0 + 0.2)
            self.assertEqual(info["normalization"], "linear")                # linear: không phá động lực của giọng đọc
        self.assertEqual(wavio.info(out)["width"], 3)                         # nội bộ vẫn lossless 24-bit

    def test_profile_drives_target_and_no_hardcoding(self):
        raw = self.speech_like("s.wav", -20.0)
        self.use(audio={"master": {"loudness": {"target_lufs": -20.0}}})
        self.audio.master(raw, self.tmp / "m20.wav", self.ctx)
        self.assertAlmostEqual(self.measure(self.tmp / "m20.wav")["loudness"]["I"], -20.0, delta=0.8)
        self.use(audio={"master": {"loudness": {"enabled": False}, "limiter": {"enabled": False}}})
        self.audio.master(raw, self.tmp / "off.wav", self.ctx)
        a, b = self.measure(raw)["stats"], self.measure(self.tmp / "off.wav")["stats"]
        self.assertAlmostEqual(a["rms_db"], b["rms_db"], delta=0.3)          # tắt loudness/limiter: chỉ đổi định dạng, không đổi độ to

    def test_limiter_prevents_clipping_from_hot_input(self):
        hot = write_pcm(self.tmp / "hot.wav", sine(300, 4.0, 24000, 1.0), 24000)
        self.use(audio={"master": {"loudness": {"enabled": False}, "limiter": {"enabled": True, "ceiling_db": -2.0}}})
        self.audio.master(hot, self.tmp / "lim.wav", self.ctx)
        self.assertLessEqual(self.measure(self.tmp / "lim.wav")["stats"]["peak_db"], -2.0 + 0.3)
        self.assertGreater(self.measure(hot)["stats"]["peak_db"], -0.5)

    def test_compressor_is_optional_and_reduces_level_difference(self):
        quiet, loud = sine(300, 2.0, 24000, 0.03), sine(300, 2.0, 24000, 0.5)
        raw = write_pcm(self.tmp / "dyn.wav", quiet + loud, 24000)

        def spread(name: str) -> float:
            return rms_db(self.tmp / name, 2.3, 3.7) - rms_db(self.tmp / name, 0.3, 1.7)
        self.use(audio={"master": {"loudness": {"enabled": False}, "limiter": {"enabled": False}}})
        self.audio.master(raw, self.tmp / "plain.wav", self.ctx)
        self.use(audio={"master": {"loudness": {"enabled": False}, "limiter": {"enabled": False},
                                   "compressor": {"enabled": True, "threshold_db": -30.0, "ratio": 4.0, "attack_ms": 5, "release_ms": 100}}})
        info = self.audio.master(raw, self.tmp / "comp.wav", self.ctx)
        self.assertTrue(info["compressor"])
        self.assertLess(spread("comp.wav"), spread("plain.wav") - 3.0)

    def test_technical_normalization_of_foreign_input_and_silent_input(self):
        stereo = self.tmp / "st.wav"
        with wave.open(str(stereo), "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
            w.writeframes(b"".join(struct.pack("<hh", v, v) for v in sine(300, 3.0, 44100, 0.3)))
        out = self.tmp / "m.wav"
        self.audio.master(stereo, out, self.ctx)
        i = wavio.info(out)
        self.assertEqual((i["rate"], i["channels"], i["width"]), (48000, 1, 3))
        self.assertAlmostEqual(i["duration"], 3.0, delta=0.05)
        with self.assertRaises(StageError) as e:
            self.audio.master(write_pcm(self.tmp / "z.wav", [0] * 48000), self.tmp / "z_out.wav", self.ctx)
        self.assertEqual(e.exception.code, "AUDIO_SILENT")

    def test_master_is_cached_by_content_and_profile(self):
        raw = self.speech_like("s.wav", -20.0)
        out = self.tmp / "m.wav"
        self.assertFalse(self.audio.master(raw, out, self.ctx)["reused"])
        self.assertTrue(self.audio.master(raw, out, self.ctx)["reused"])
        self.use(audio={"master": {"loudness": {"target_lufs": -18.0}}})
        self.assertFalse(self.audio.master(raw, out, self.ctx)["reused"])                    # profile đổi => làm lại
        self.assertAlmostEqual(self.measure(out)["loudness"]["I"], -18.0, delta=0.8)


# ============================================================================ có ffmpeg: YouTube + watermark
@needs_ffmpeg
class YouTubeTest(AudioCase):
    def setUp(self):
        super().setUp()
        raw = write_pcm(self.tmp / "raw.wav", sine(300, 5.0, 24000, 0.3), 24000)
        self.master = self.tmp / "nm.wav"
        self.audio.master(raw, self.master, self.ctx)
        self.wm = burst(self.tmp / "wm.wav", 0.2, 1.2, 0.3, f=800, amp=0.9)           # watermark to và có biên im lặng

    def test_watermark_gap_story_layout_and_lossless_story(self):
        out = self.tmp / "yt.wav"
        info = self.audio.build_youtube_audio(self.master, self.wm, out, self.ctx)
        self.assertTrue(info["watermark"])
        story_start = info["story_start_sec"]
        self.assertAlmostEqual(story_start, info["watermark_duration_sec"] + 0.8, delta=0.002)
        self.assertAlmostEqual(info["duration_sec"], story_start + wavio.info(self.master)["duration"], delta=0.002)
        # gap hợp lý giữa watermark và truyện
        gap = [d for s, d in silent_runs(out, 0.002, 0.3) if abs(s - (story_start - 0.8 - 0.05)) < 0.2]
        self.assertTrue(gap and gap[0] >= 0.8, gap)
        # phần truyện giữ nguyên từng mẫu (không master lại, không mã hóa lại)
        n = 2 * 48000
        a, _ = decode(out)
        b, _ = decode(self.master)
        self.assertEqual(a[-n:], b[-n:])
        self.assertAlmostEqual(len(a) - len(b), (info["duration_sec"] - wavio.info(self.master)["duration"]) * 48000, delta=60)
        self.assertEqual((wavio.info(out)["rate"], wavio.info(out)["width"]), (48000, 3))

    def test_watermark_is_loudness_matched_to_narration_with_profile_offset(self):
        out = self.tmp / "yt.wav"
        info = self.audio.build_youtube_audio(self.master, self.wm, out, self.ctx)
        m = self.audio.tools.measure(self.tmp / "yt.wav", -60, 5)
        wm_only = self.tmp / "wm_only.wav"
        wavio.slice_wav(out, wm_only, 0.0, info["watermark_duration_sec"], 0)
        lufs = self.audio.tools.measure(wm_only, -60, 5)["loudness"]["I"]
        self.assertAlmostEqual(lufs, -16.0 - 2.0, delta=1.0)                              # target - offset_db(2.0)
        self.assertFalse(m["decode_errors"])
        self.use(audio={"youtube": {"watermark": {"offset_db": 0.0}}})
        out2 = self.tmp / "yt2.wav"
        i2 = self.audio.build_youtube_audio(self.master, self.wm, out2, self.ctx)
        wm2 = self.tmp / "wm2.wav"
        wavio.slice_wav(out2, wm2, 0.0, i2["watermark_duration_sec"], 0)
        self.assertAlmostEqual(self.audio.tools.measure(wm2, -60, 5)["loudness"]["I"], -16.0, delta=1.0)

    def test_no_watermark_means_story_only_and_position_end(self):
        plain = self.audio.build_youtube_audio(self.master, None, self.tmp / "p.wav", self.ctx)
        self.assertEqual((plain["watermark"], plain["story_start_sec"]), (False, 0.0))
        self.assertEqual((self.tmp / "p.wav").read_bytes(), self.master.read_bytes())
        self.use(audio={"youtube": {"watermark": {"position": "end", "gap_ms": 500}}})
        e = self.audio.build_youtube_audio(self.master, self.wm, self.tmp / "e.wav", self.ctx)
        self.assertEqual(e["story_start_sec"], 0.0)
        self.assertAlmostEqual(e["duration_sec"], wavio.info(self.master)["duration"] + 0.5 + e["watermark_duration_sec"], delta=0.002)

    def test_bad_watermark_is_rejected_with_clear_errors(self):
        silent = write_pcm(self.tmp / "ws.wav", [0] * 24000)
        with self.assertRaises(StageError) as e:
            self.audio.build_youtube_audio(self.master, silent, self.tmp / "o.wav", self.ctx)
        self.assertEqual(e.exception.code, "WATERMARK_SILENT")
        junk = self.tmp / "junk.wav"
        junk.write_bytes(b"not audio")
        with self.assertRaises(StageError) as e:
            self.audio.build_youtube_audio(self.master, junk, self.tmp / "o2.wav", self.ctx)
        self.assertEqual(e.exception.error_class, ErrorClass.POLICY)

    def test_changing_watermark_rebuilds_only_the_youtube_branch(self):
        """Đổi watermark không làm lại Narration Master hay TikTok; chỉ bản YouTube mới."""
        raw = self.tmp / "raw.wav"
        self.assertTrue(self.audio.master(raw, self.master, self.ctx)["reused"])             # master dùng lại
        y1 = self.audio.build_youtube_audio(self.master, self.wm, self.tmp / "yt.wav", self.ctx)
        tk1 = self.audio.build_tiktok_parts(self.master, 2.0, 100, self.tmp / "tt", self.ctx)
        wm2 = burst(self.tmp / "wm2.wav", 0.1, 2.0, 0.1, f=500, amp=0.5)
        self.assertTrue(self.audio.master(raw, self.master, self.ctx)["reused"])
        y2 = self.audio.build_youtube_audio(self.master, wm2, self.tmp / "yt.wav", self.ctx)
        tk2 = self.audio.build_tiktok_parts(self.master, 2.0, 100, self.tmp / "tt", self.ctx)
        self.assertNotEqual(y1["watermark_sha256"], y2["watermark_sha256"])
        self.assertNotAlmostEqual(y1["duration_sec"], y2["duration_sec"], places=1)
        self.assertFalse(tk1["stretch"]["reused"])
        self.assertTrue(tk2["stretch"]["reused"])                                             # TikTok không bị làm lại


# ============================================================================ có ffmpeg: TikTok
@needs_ffmpeg
class TikTokTest(AudioCase):
    """Narration giả: 8 "câu" 440 Hz dài 3 s, nghỉ 0.6 s (đoạn văn: nghỉ dài hơn)."""

    def setUp(self):
        super().setUp()
        self.chunks_ = [burst(self.tmp / f"s{i}.wav", 0.05, 3.0, 0.05, f=440) for i in range(8)]
        self.pauses = [0, 600, 0, 600, 0, 600, 0, 0]
        raw = self.tmp / "raw.wav"
        self.joined = self.audio.assemble(self.chunks_, self.pauses, raw, self.ctx)
        self.master = self.tmp / "nm.wav"
        self.audio.master(raw, self.master, self.ctx)
        self.timeline = [{**e, "kind": ("paragraph" if p >= 600 else "sentence") if i < 7 else "end"}
                         for i, (e, p) in enumerate(zip(self.joined["timeline"], self.pauses))]

    def split(self, target: float, timeline="default", speed: float = 2.0, **audio):
        if audio:
            self.use(audio=audio)
        return self.audio.build_tiktok_parts(self.master, speed, target, self.tmp / "tt", self.ctx,
                                             self.timeline if timeline == "default" else timeline)

    def test_time_stretch_x2_halves_duration_and_keeps_pitch(self):
        res = self.split(1000.0)
        stretched = self.tmp / "work" / "tiktok_stretched.wav"
        d_in = wavio.info(self.master)["duration"]
        self.assertAlmostEqual(res["stretch"]["duration_out_sec"], d_in / 2, delta=d_in * 0.005)
        self.assertEqual(res["stretch"]["engine"], "rubberband")
        f_in = dominant_freq(self.master, 0.5, 1.5)
        f_out = dominant_freq(stretched, 0.2, 0.7)
        self.assertEqual(f_in, 440)
        self.assertLessEqual(abs(f_out - 440), 10, f_out)                    # giữ cao độ (tăng tốc thô sẽ ra ~880 Hz)
        # đối chứng: tăng tốc bằng resample thô làm cao độ tăng gấp đôi, nên phép đo này có khả năng phân biệt
        naive = self.tmp / "naive.wav"
        self.audio.tools.ffmpeg(["-v", "error", "-i", str(self.master), "-af", "asetrate=96000,aresample=48000", "-y", str(naive)])
        self.assertGreaterEqual(dominant_freq(naive, 0.2, 0.7, 300, 1400), 860)

    def test_atempo_fallback_also_keeps_pitch(self):
        res = self.split(1000.0, tiktok={"stretch": {"engine": "atempo"}})
        self.assertEqual(res["stretch"]["engine"], "atempo")
        stretched = self.tmp / "work" / "tiktok_stretched.wav"
        self.assertAlmostEqual(res["stretch"]["duration_out_sec"], wavio.info(self.master)["duration"] / 2, delta=0.15)
        self.assertLessEqual(abs(dominant_freq(stretched, 0.2, 0.7) - 440), 10)

    def test_split_prefers_pauses_and_never_cuts_inside_speech(self):
        res = self.split(8.0)
        parts = res["parts"]
        total = res["stretch"]["duration_out_sec"]
        self.assertEqual(len(parts), round(total / 8.0))
        self.assertAlmostEqual(sum(p["duration_sec"] for p in parts), total, delta=0.01)       # không mất/thừa mẫu nào
        stretched = self.tmp / "work" / "tiktok_stretched.wav"
        for p in parts[:-1]:
            self.assertEqual(p["boundary"] in ("paragraph", "sentence"), True)
            self.assertFalse(p["forced"] or p["mid_sentence"])
            if p["boundary"] == "paragraph":
                self.assertLess(rms_db(stretched, p["end_sec"] - 0.03, p["end_sec"] + 0.03), -50, p)   # cắt giữa khoảng nghỉ đoạn: im lặng thật
        # chỉ còn ranh giới cấp đoạn: điểm cắt luôn rơi vào khoảng lặng
        para = [e for e in self.timeline if e["kind"] == "paragraph"]
        res2 = self.split(8.0, timeline=para + [self.timeline[-1]])
        for p in res2["parts"][:-1]:
            self.assertEqual(p["boundary"], "paragraph")
            self.assertLess(rms_db(stretched, p["end_sec"] - 0.03, p["end_sec"] + 0.03), -50, p)
        self.assertTrue(all(4.0 <= p["duration_sec"] <= 1.15 * 8 for p in parts), [p["duration_sec"] for p in parts])
        for p in parts:
            self.assertEqual(wavio.info(Path(p["path"]))["rate"], 48000)
        self.assertLess(max_step(Path(parts[1]["path"]), 0, 0.05), 0.02)                      # không click ở đầu part

    def test_split_avoids_mid_sentence_boundary_when_a_real_one_is_near(self):
        n = len(self.timeline)
        tl = [{**e, "kind": "cut"} for e in self.timeline]                                    # mọi ranh giới giữa câu...
        tl[3] = {**self.timeline[3], "kind": "sentence"}                                     # ...trừ một ranh giới thật
        res = self.split(8.0, timeline=tl)
        first = res["parts"][0]
        self.assertEqual(first["boundary"], "sentence")
        self.assertFalse(first["mid_sentence"])
        self.assertEqual(n, 8)

    def test_split_without_timeline_detects_silences(self):
        res = self.split(8.0, timeline=None)
        self.assertEqual(res["split"]["boundaries"], "silence")
        stretched = self.tmp / "work" / "tiktok_stretched.wav"
        self.assertGreater(len(res["parts"]), 1)
        for p in res["parts"][:-1]:
            self.assertFalse(p["forced"])
            self.assertLess(rms_db(stretched, p["end_sec"] - 0.03, p["end_sec"] + 0.03), -50)

    def test_speed_and_target_come_from_profile_not_code(self):
        a = self.split(1000.0, speed=1.0)
        self.assertEqual(a["stretch"]["engine"], "none")
        self.assertAlmostEqual(a["stretch"]["duration_out_sec"], wavio.info(self.master)["duration"], delta=0.02)
        self.assertEqual(len(a["parts"]), 1)
        b = self.split(5.0, speed=1.5)
        self.assertAlmostEqual(b["stretch"]["ratio"], 1 / 1.5, delta=0.01)
        self.assertGreater(len(b["parts"]), 2)

    def test_changing_split_target_reuses_the_stretched_audio(self):
        self.assertFalse(self.split(8.0)["stretch"]["reused"])
        self.assertTrue(self.split(6.0)["stretch"]["reused"])
        self.assertEqual(len(list((self.tmp / "tt").glob("part_*.wav"))), len(self.split(6.0)["parts"]))   # part cũ không còn sót


# ============================================================================ có ffmpeg: QA phát hiện audio lỗi giả lập
@needs_ffmpeg
class QADefectTest(AudioCase):
    def codes(self, p: Path, **expect) -> tuple[set, bool]:
        r = self.audio.qa_full(p, {"kind": "narration", **expect}, self.ctx)
        return {e["code"] for e in r["errors"]}, r["ok"]

    def good(self) -> Path:
        out = self.tmp / "good.wav"
        self.audio.master(write_pcm(self.tmp / "g_raw.wav", sine(300, 6.0, 24000, 0.3)), out, self.ctx)
        return out

    def test_good_audio_passes(self):
        g = self.good()
        errs, ok = self.codes(g, duration_sec=6.0, lufs=-16.0, true_peak_db=-1.0)
        self.assertEqual((errs, ok), (set(), True))
        self.assertTrue(self.audio.qa(g)["ok"])

    def test_corrupt_and_unreadable(self):
        junk = self.tmp / "junk.wav"
        junk.write_bytes(b"RIFF" + bytes(range(256)) * 20)
        errs, ok = self.codes(junk)
        self.assertEqual((errs, ok), ({"CORRUPT"}, False))
        self.assertEqual(self.audio.qa(junk), {"ok": False, "duration_sec": 0.0, "issues": ["UNREADABLE"]})

    def test_truncated_file_is_flagged_by_expected_duration(self):
        g = self.good()
        data = g.read_bytes()
        cut = self.tmp / "cut.wav"
        cut.write_bytes(data[:len(data) // 3])
        errs, ok = self.codes(cut, duration_sec=6.0)
        self.assertFalse(ok)
        self.assertTrue(errs & {"ABNORMAL_DURATION", "CORRUPT"}, errs)

    def test_empty_and_digital_silence(self):
        self.assertIn("EMPTY", self.codes(write_pcm(self.tmp / "e0.wav", []))[0])
        self.assertIn("EMPTY", self.codes(write_pcm(self.tmp / "e1.wav", [0] * 48000))[0])

    def test_clipping(self):
        sq = [32767 if (i // 40) % 2 else -32768 for i in range(48000)]
        errs, ok = self.codes(write_pcm(self.tmp / "clip.wav", sq, 48000))
        self.assertIn("CLIPPING", errs)
        self.assertFalse(ok)

    def test_excessive_silence(self):
        p = write_pcm(self.tmp / "sil.wav", sine(300, 1.0, 48000, 0.3) + [0] * 48000 * 15, 48000)
        errs, _ = self.codes(p)
        self.assertIn("EXCESSIVE_SILENCE", errs)

    def test_technical_format_mismatch_and_abnormal_duration(self):
        stereo = self.tmp / "st.wav"
        with wave.open(str(stereo), "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(22050)
            w.writeframes(b"".join(struct.pack("<hh", v, v) for v in sine(300, 2.0, 22050, 0.3)))
        errs, _ = self.codes(stereo, duration_sec=10.0)
        self.assertTrue({"FORMAT_MISMATCH", "ABNORMAL_DURATION"} <= errs, errs)

    def test_loudness_and_missing_chunks_expectations(self):
        g = self.good()
        self.assertIn("LOUDNESS_OFF", self.codes(g, lufs=-26.0)[0])
        self.assertIn("MISSING_CHUNKS", self.codes(g, chunks={"expected": 12, "found": 11})[0])

    def test_input_audio_is_judged_leniently_except_for_corruption(self):
        sq = [32767 if (i // 40) % 2 else -32768 for i in range(48000)]
        r = self.audio.qa_full(write_pcm(self.tmp / "hot.wav", sq, 48000), {"kind": "input"}, self.ctx)
        self.assertTrue(r["ok"])
        self.assertIn("CLIPPING", {w["code"] for w in r["warnings"]})


# ============================================================================ pipeline thật với ffmpeg
@needs_ffmpeg
class AudioPipelineTest(RootCase):
    def setUp(self):
        super().setUp()
        cfg = self.root / "config" / "config.json"
        d = json.loads(cfg.read_text(encoding="utf-8"))
        d["adapters"] = {"audio": "ffmpeg"}
        cfg.write_text(json.dumps(d), encoding="utf-8")
        self.wm = burst(self.root / "wm.wav", 0.1, 1.0, 0.2, f=800, amp=0.7)

    # Fake TTS có chunk ngắn và pause dài so với giọng thật => nới ngưỡng im lặng của QA bằng profile (cũng chứng minh QA cấu hình được)
    LENIENT = {"qa": {"silence": {"max_ratio": 0.7, "max_gap_s": 6.0}}}

    def go(self, orc, **kw):
        extra = kw.pop("p", {})
        audio = {**self.LENIENT, **(extra.pop("audio", {}))}
        for k, v in self.LENIENT.items():
            if isinstance(audio.get(k), dict):
                audio[k] = {**v, **audio[k]}
        jid = orc.submit(params(watermark=str(self.wm), tiktok={"speed": 2.0, "target_part_sec": 1.8}, audio=audio, **extra), **kw)
        orc.run()
        return jid

    def test_full_job_with_real_audio_stage(self):
        orc = self.orc()
        jid = self.go(orc)
        j = orc.store.get_job(jid)
        self.assertEqual(j["state"], P.PUBLISHED, j["last_error"])
        kinds = {a["kind"] for a in orc.store.artifacts(jid)}
        self.assertTrue({"audio_master", "audio_timeline", "narration_master", "audio_youtube", "audio_tiktok", "audio_report"} <= kinds)
        rep = json.loads((self.job_dir(jid) / "audio" / "audio_report.json").read_text(encoding="utf-8"))
        self.assertTrue(rep["input"]["ok"] and rep["narration_master"]["qa"]["ok"] and rep["youtube"]["qa"]["ok"])
        self.assertAlmostEqual(rep["narration_master"]["qa"]["measures"]["lufs"], -16.0, delta=1.0)
        tk = rep["tiktok"]
        self.assertEqual((tk["stretch"]["engine"], tk["split"]["boundaries"]), ("rubberband", "timeline"))
        self.assertGreaterEqual(len(tk["parts"]), 2)
        self.assertTrue(all(q["qa"]["ok"] for q in tk["qa"]))
        yt = rep["youtube"]["info"]
        self.assertTrue(yt["watermark"])
        parts = [a for a in orc.store.artifacts(jid) if a["kind"] == "audio_tiktok"]
        self.assertEqual(len(parts), len(tk["parts"]))
        tl = json.loads((self.job_dir(jid) / "audio" / "timeline.json").read_text(encoding="utf-8"))
        self.assertEqual(len(tl["segments"]), 6)
        self.assertEqual(tl["segments"][-1]["kind"], "end")
        self.assertIn("paragraph", {s["kind"] for s in tl["segments"]})

    def test_changing_watermark_reruns_audio_only_not_tts(self):
        orc = self.orc()
        first = self.go(orc)
        wm2 = burst(self.root / "wm2.wav", 0.1, 2.0, 0.1, f=500, amp=0.5)
        second = orc.submit(params(watermark=str(wm2), tiktok={"speed": 2.0, "target_part_sec": 1.8}, audio=self.LENIENT), start_stage="audio",
                            target_stage="audio", from_job={"job_id": first, "kinds": ["audio_master", "audio_timeline", "metadata"]})
        orc.run()
        self.assertEqual(set(self.runs(orc, second)), {"audio"})                                  # TTS không chạy lại
        a1 = {a["kind"]: a for a in orc.store.artifacts(first) if a["kind"] in ("audio_youtube", "narration_master")}
        a2 = {a["kind"]: a for a in orc.store.artifacts(second) if a["kind"] in ("audio_youtube", "narration_master")}
        self.assertNotEqual(a1["audio_youtube"]["sha256"], a2["audio_youtube"]["sha256"])        # bản YouTube đổi
        self.assertEqual(a1["narration_master"]["sha256"], a2["narration_master"]["sha256"])     # Narration Master giống hệt

    def test_imported_audio_goes_through_mastering_and_silence_based_split(self):
        wav = self.root / "voice.wav"
        write_pcm(wav, sum(([0] * 12000 + sine(250, 2.0, 24000, 0.2) + [0] * 12000 for _ in range(4)), []), 24000)
        orc = self.orc()
        jid = orc.submit(params(watermark=str(self.wm), tiktok={"speed": 2.0, "target_part_sec": 3.0}, audio=self.LENIENT),
                         mode="VIDEO_ONLY", inputs={"audio_master": str(wav), "metadata": {"title": "Giọng thu sẵn"}})
        orc.run()
        j = orc.store.get_job(jid)
        self.assertIsNone(j["failed_stage"], j["last_error"])
        rep = json.loads((self.job_dir(jid) / "audio" / "audio_report.json").read_text(encoding="utf-8"))
        self.assertEqual(rep["tiktok"]["split"]["boundaries"], "silence")                          # không có timeline => dò khoảng lặng
        self.assertAlmostEqual(rep["narration_master"]["qa"]["measures"]["lufs"], -16.0, delta=1.0)

    def test_profile_in_job_params_drives_mastering(self):
        orc = self.orc()
        jid = self.go(orc, p={"audio": {"master": {"loudness": {"target_lufs": -20.0}}}})
        rep = json.loads((self.job_dir(jid) / "audio" / "audio_report.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(rep["narration_master"]["qa"]["measures"]["lufs"], -20.0, delta=1.0)
        self.assertEqual(orc.store.get_job(jid)["state"], P.PUBLISHED)

    def test_defective_input_audio_fails_the_job_with_a_clear_qa_error(self):
        bad = self.root / "bad.wav"
        write_pcm(bad, [0] * 24000)
        orc = self.orc()
        jid = orc.submit(params(), mode="VIDEO_ONLY", inputs={"audio_master": str(bad), "metadata": {"title": "x"}}, auto_resume=False)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual((j["state"], j["last_error"]["code"]), (P.FAILED, "AUDIO_QA_FAILED"))
        self.assertIn("EMPTY", j["last_error"]["message"])

    def test_missing_ffmpeg_pauses_the_job_instead_of_failing_it(self):
        cfg = self.root / "config" / "config.json"
        d = json.loads(cfg.read_text(encoding="utf-8"))
        d["tools"] = {"ffmpeg": "định-không-tồn-tại", "ffprobe": "định-không-tồn-tại"}
        cfg.write_text(json.dumps(d), encoding="utf-8")
        orc = self.orc()
        jid = orc.submit(params(), mode="TTS_ONLY", inputs={"story_text": str(self._story())}, auto_resume=False)
        orc.run()
        j = orc.store.get_job(jid)
        self.assertEqual(j["hold_reason"], P.PAUSED_RESOURCE)
        self.assertNotEqual(j["state"], P.FAILED)

    def _story(self) -> Path:
        from contentfactory.adapters.fake import _paragraph
        p = self.root / "story.txt"
        p.write_text("\n\n".join(_paragraph(k) for k in range(1, 4)), encoding="utf-8")
        return p


if __name__ == "__main__":
    unittest.main()
