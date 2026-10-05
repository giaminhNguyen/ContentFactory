"""Auto Tune: đo THỰC TẾ một adapter TTS bằng bộ văn bản chuẩn nội bộ để tìm cấu hình an toàn. Tất định, không AI.

Chỉ chạy khi người dùng yêu cầu (gọi engine thật tốn thời gian/tiền) — không bao giờ tự chạy trong job.
Đo: (1) cơ bản — chạy được không, định dạng/sample rate/số kênh thật; (2) tốc độ đọc (ký tự/giây) làm chuẩn cho kiểm tra độ dài
bất thường; (3) thang độ dài segment tăng dần: request lỗi, timeout, file hỏng, im lặng, độ dài bất thường so với trung vị.
Kết quả là TuneReport (bằng chứng source="runtime_test") + `apply_to_profile` ghi vào profile annotated, giữ giá trị cũ ở `alternatives`.
"""
from __future__ import annotations

import statistics
import tempfile
import time
from pathlib import Path
from typing import Callable

from ..contracts import CancelToken, ErrorClass, StageContext, StageError
from . import qa as chunk_qa
from . import schema as S

BENCH = {   # mỗi mục: một loại văn bản cần engine đọc đúng; ghép lại để dựng segment dài
    "vi": [("short", "Trời đã khuya."),
           ("dialogue", "— Anh đi đâu đấy? — Tôi ra ngoài một lát, rồi về ngay thôi."),
           ("commas", "Cô ấy mở cửa, nhìn quanh hành lang, rồi lặng lẽ bước vào, tay vẫn nắm chặt chiếc chìa khóa cũ."),
           ("question", "Bạn có nghe thấy tiếng động lạ ở tầng trên không?"),
           ("exclaim", "Chạy đi! Đừng ngoảnh lại!"),
           ("ellipsis", "Ông ngập ngừng… rồi chậm rãi nói tiếp… như sợ ai nghe thấy."),
           ("numbers_dates", "Năm 1987, vào ngày 15 tháng 3, căn nhà số 24 ngõ 7 có tất cả 3 tầng."),
           ("names", "Nguyễn Văn An gặp bà Trần Thị Hoa ở chợ Bến Thành."),
           ("long", "Đêm ấy gió thổi rất mạnh, mưa tạt vào khung cửa sổ gỗ cũ kỹ, và trong căn nhà vắng lặng chỉ còn "
                    "tiếng đồng hồ tích tắc vọng lại từ phòng khách, khiến ai nghe cũng thấy sống lưng lạnh dần.")],
    "en": [("short", "It was already late."),
           ("dialogue", "\"Where are you going?\" \"Just out for a moment. I'll be right back.\""),
           ("commas", "She opened the door, glanced down the hallway, and slipped inside, still gripping the old key."),
           ("question", "Did you hear that noise upstairs?"),
           ("exclaim", "Run! Don't look back!"),
           ("ellipsis", "He hesitated... then went on slowly... as if afraid someone might hear."),
           ("numbers_dates", "In 1987, on the 15th of March, house number 24 had exactly 3 floors."),
           ("names", "John Smith met Mrs. Alice Brown at the old market."),
           ("long", "That night the wind blew hard and rain lashed the aged wooden window, and in the empty house only the "
                    "ticking of the clock drifted in from the living room, sending a slow chill down every spine.")],
}
LENGTHS = (100, 150, 200, 300, 400, 500, 600, 800, 1000, 1500, 2000, 3000, 5000)


def make_ctx(workdir: Path, log: Callable | None = None) -> StageContext:
    return StageContext(job_id="autotune", stage="tts", attempt=1, stage_key="", workspace=workdir, stage_dir=workdir,
                        params={}, inputs={}, config={}, cancel=CancelToken(), log=log or (lambda *a, **k: None))


def compose(language: str, chars: int) -> str:
    """Văn bản benchmark dài ≤ `chars` ký tự, ghép các câu chuẩn thành đoạn tự nhiên (không cắt giữa câu)."""
    sents = [t for _, t in BENCH.get(language.split("-")[0], BENCH["en"])]
    out = ""
    i = 0
    while True:
        nxt = sents[i % len(sents)]
        cand = f"{out} {nxt}".strip()
        if len(cand) > chars:
            break
        out, i = cand, i + 1
        if i > 400:
            break
    room = chars - len(out) - 1                       # lấp cho gần đúng độ dài cần đo (cắt ở ranh giới từ) để chạm sát giới hạn
    if out and room > 20:
        words = sents[i % len(sents)].split()
        tail = ""
        for w in words:
            if len(tail) + len(w) + 1 > room:
                break
            tail = f"{tail} {w}".strip()
        out = f"{out} {tail}".strip()
    return out or sents[0][:chars]


class AutoTuner:
    def __init__(self, adapter, language: str = "vi", profile: dict | None = None, *, timeout_s: float = 60.0,
                 repeats: int = 2, max_requests: int = 40, explore_max_chars: int = 1500, workdir: Path | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.adapter, self.language = adapter, language
        self.profile = S.resolve(profile, adapter.capabilities(), language, getattr(adapter, "engine_id", None))
        self.timeout_s, self.repeats, self.max_requests, self.explore_max = timeout_s, repeats, max_requests, explore_max_chars
        self.clock = clock
        self._own = workdir is None
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="cf-autotune-"))
        self.requests = 0
        self.baseline_cps: float | None = None      # trung vị ký tự/giây của các mẫu ngắn, dùng phát hiện độ dài bất thường

    # -- một lần gọi engine -----------------------------------------------------------------------
    def _probe(self, label: str, text: str) -> dict:
        self.requests += 1
        out = self.workdir / f"{self.requests:04d}_{label}.wav"
        ctx = make_ctx(self.workdir)
        t0 = self.clock()
        rec = {"label": label, "chars": len(text), "failures": []}
        try:
            self.adapter.synthesize({"index": self.requests, "text": text, "pause_after_ms": 0}, self.profile, out, ctx)
        except StageError as e:
            kind = "timeout" if "TIMEOUT" in e.code.upper() else "auth" if e.error_class == ErrorClass.AUTH else "request_failure"
            rec["failures"].append(kind)
            rec["error"] = f"{e.code}: {e.message}"[:200]
            return rec
        rec["seconds"] = round(self.clock() - t0, 3)
        if rec["seconds"] > self.timeout_s:
            rec["failures"].append("timeout")
        if not out.is_file():
            rec["failures"].append("no_output")
            return rec
        st = chunk_qa.wav_stats(out)
        if st is None:
            rec["note"] = "định dạng không phải WAV: không kiểm được hỏng/im lặng/độ dài"
            return rec
        rec |= {"duration_sec": round(st["duration_sec"], 3), "silence_ratio": round(st["silence_ratio"], 3),
                "sample_rate": st["sample_rate"], "channels": st["channels"]}
        if not st["decodable"] or st["duration_sec"] <= 0:
            rec["failures"].append("corrupt")
        elif st["silence_ratio"] > float(self.profile["qa"]["silence_ratio_max"]):
            rec["failures"].append("silence")
        if st["duration_sec"] > 0:
            rec["cps"] = round(len(text) / st["duration_sec"], 2)
            b = self.baseline_cps
            if b and not rec["failures"] and (rec["cps"] > b * 2.5 or rec["cps"] < b * 0.4):
                rec["failures"].append("duration_anomaly")        # quá nhanh (thiếu chữ) hoặc quá chậm (kéo dài/lặp/chèn im lặng)
        return rec

    # -- chạy ---------------------------------------------------------------------------------------
    def run(self) -> dict:
        caps = S.normalize_capabilities(self.adapter.capabilities())
        probes: list[dict] = []
        basic = self._probe("basic", compose(self.language, 120))
        probes.append(basic)
        report: dict = {"engine": getattr(self.adapter, "engine_id", None), "language": self.language, "probes": probes,
                        "works": not basic["failures"], "requests": 0}
        if basic["failures"]:
            report.update(requests=self.requests, max_ok_chars=None, recommendation=None,
                          summary=f"engine không chạy được ở bài kiểm tra cơ bản: {basic['failures']} {basic.get('error', '')}")
            return report
        report["measured"] = {"sample_rate": basic.get("sample_rate"), "channels": basic.get("channels"), "output_format": "wav"}
        for name in ("short", "numbers_dates", "dialogue"):                       # lấy mẫu tốc độ đọc trên nhiều loại câu
            if self.requests < self.max_requests:
                probes.append(self._probe(name, dict(BENCH.get(self.language.split("-")[0], BENCH["en"]))[name]))
        limit = caps["max_chars"] or self.explore_max
        ladder = [n for n in LENGTHS if n <= limit]
        if limit not in ladder and caps["max_chars"]:
            ladder.append(limit)                                                     # thử đúng giới hạn đã khai báo
        ladder = ladder or [limit]
        cps0 = [r["cps"] for r in probes if "cps" in r and not r["failures"]]
        self.baseline_cps = statistics.median(cps0) if cps0 else None
        ladder_results, ok_prefix, first_fail, fails_in_row = [], None, None, 0
        for n in ladder:
            if self.requests + self.repeats > self.max_requests:
                report["budget_exhausted"] = True
                break
            text = compose(self.language, n)
            reps = [self._probe(f"len{n}_{k}", text) for k in range(self.repeats)]
            probes += reps
            bad = sorted({f for r in reps for f in r["failures"]})
            ladder_results.append({"chars": len(text), "target": n, "failures": bad})
            if bad:
                first_fail = first_fail or {"chars": len(text), "failures": bad}
                fails_in_row += 1
                if fails_in_row >= 2:
                    break
            else:
                fails_in_row = 0
                if first_fail is None:
                    ok_prefix = len(text)
        med = self.baseline_cps
        report["ladder"] = ladder_results
        report["first_failure"] = first_fail
        report["max_ok_chars"] = ok_prefix
        report["cps_median"] = round(med, 2) if med else None
        reached_cap = bool(caps["max_chars"]) and ok_prefix is not None and first_fail is None and not report.get("budget_exhausted")
        rec = None
        if ok_prefix:
            mx = ok_prefix
            rec = {"max_chars": mx, "preferred_chars": max(20, int(mx * 0.7 / 10) * 10),
                   "duration_chars_per_sec": [round(med * 0.4, 1), round(med * 2.5, 1)] if med else None,
                   "limit_is_hard": first_fail is not None, "capped_by_declared_limit": reached_cap}
        report["recommendation"] = rec
        report["requests"] = self.requests
        report["summary"] = (f"an toàn tới ~{ok_prefix} ký tự" + (f"; lỗi đầu tiên ở {first_fail['chars']} ký tự: {first_fail['failures']}"
                                                                if first_fail else "; không thấy lỗi trong dải đã thử (chưa chắc là giới hạn thật)")
                             if ok_prefix else "không có độ dài nào đạt")
        return report


def apply_to_profile(profile: dict, report: dict, run_id: str = "autotune") -> dict:
    """Ghi kết quả đo vào profile annotated (source=runtime_test); giá trị cũ khác được giữ ở `alternatives`."""
    import copy
    out = copy.deepcopy(profile)

    def put(container: dict, key: str, value, conf: str, quote: str) -> None:
        new = S.fact(value, "runtime_test", conf, [{"ref": f"autotune:{run_id}", "quote": quote[:160]}])
        old = container.get(key)
        if S.is_fact(old) and old["value"] != value:
            new["alternatives"] = [{k: old[k] for k in ("value", "source", "confidence") if k in old}] + old.get("alternatives", [])
        container[key] = new

    rec = report.get("recommendation")
    if not report.get("works") or not rec:
        out.setdefault("meta", {})["autotune"] = {"works": bool(report.get("works")), "summary": report.get("summary")}
        out["status"] = "candidate"
        return out
    seg = out.setdefault("segment", {})
    put(seg, "max_chars", rec["max_chars"], "high" if rec["limit_is_hard"] else "medium", report["summary"])
    put(seg, "preferred_chars", rec["preferred_chars"], "medium", "70% mức an toàn đã đo")
    if rec["duration_chars_per_sec"]:
        put(out.setdefault("qa", {}), "duration_chars_per_sec", rec["duration_chars_per_sec"], "medium",
            f"trung vị {report['cps_median']} ký tự/giây × [0.4, 2.5]")
    m = report.get("measured") or {}
    if m.get("sample_rate"):
        put(out.setdefault("meta", {}).setdefault("measured", {}), "sample_rate", m["sample_rate"], "high", "đo từ file WAV thật")
    out.setdefault("meta", {})["needs_tune"] = False
    out["meta"]["autotune"] = {"works": True, "summary": report["summary"], "requests": report["requests"]}
    out["status"] = "ready" if not out.get("needs_user") else "candidate"
    return out
