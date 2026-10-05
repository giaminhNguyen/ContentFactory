"""Audio QA: đánh giá phép đo (ffmpeg) theo ngưỡng của profile. Hàm thuần — test được bằng số liệu giả lập.

Mã lỗi: CORRUPT, EMPTY, FORMAT_MISMATCH, ABNORMAL_DURATION, CLIPPING, TRUE_PEAK_OVER, EXCESSIVE_SILENCE, LOUDNESS_OFF, MISSING_CHUNKS.
`expect` (tất cả tùy chọn): kind ("input"|"narration"|"youtube"|"part"), duration_sec, sample_rate, channels, codec_prefix ("pcm"),
lufs (mục tiêu), true_peak_db (trần), chunks {"expected": n, "found": k}.
Kind "input" (audio trước khi master) chỉ báo lỗi về hỏng/rỗng/định dạng; clipping/ồn/độ to chỉ là cảnh báo vì mastering sẽ xử lý.
"""
from __future__ import annotations

import math


def evaluate(m: dict, expect: dict, cfg: dict) -> dict:
    errors: list[dict] = []
    warns: list[dict] = []
    kind = expect.get("kind", "narration")
    strict = kind != "input"
    pr, st, lo = m.get("probe") or {}, m.get("stats") or {}, m.get("loudness") or {}
    dur = float(pr.get("duration") or 0.0)

    def add(code: str, msg: str, hard: bool = True) -> None:
        (errors if hard else warns).append({"code": code, "message": msg})

    if m.get("decode_errors", 0) > cfg.get("max_decode_errors", 0):
        add("CORRUPT", f"{m['decode_errors']} lỗi khi decode (file hỏng hoặc bị cắt cụt)")
    silent = st.get("peak_db") is not None and (st["peak_db"] == float("-inf") or st["peak_db"] < -90)
    if dur < cfg["min_duration_sec"] or st.get("samples") == 0 or silent:
        add("EMPTY", f"audio rỗng/không có tín hiệu (dài {dur:.2f}s, peak {st.get('peak_db')} dB)")
    for key, got in (("sample_rate", pr.get("sample_rate")), ("channels", pr.get("channels"))):
        if expect.get(key) and got != expect[key]:
            add("FORMAT_MISMATCH", f"{key}={got}, cần {expect[key]}")
    if expect.get("codec_prefix") and not str(pr.get("codec", "")).startswith(expect["codec_prefix"]):
        add("FORMAT_MISMATCH", f"codec={pr.get('codec')}, cần {expect['codec_prefix']}*")
    if "duration_sec" in expect:
        tol = max(cfg["duration_tolerance"]["abs_s"], expect["duration_sec"] * cfg["duration_tolerance"]["rel"])
        if abs(dur - expect["duration_sec"]) > tol:
            add("ABNORMAL_DURATION", f"dài {dur:.2f}s, kỳ vọng {expect['duration_sec']:.2f}s (±{tol:.2f}s)")
    ck = expect.get("chunks")
    if ck and ck["found"] != ck["expected"]:
        add("MISSING_CHUNKS", f"thiếu chunk: có {ck['found']}/{ck['expected']}")
    c = cfg["clipping"]
    if st.get("peak_db") is not None and st["peak_db"] >= c["peak_db_max"] and st.get("peak_count", 0) > c["peak_count_max"]:
        add("CLIPPING", f"peak {st['peak_db']:.2f} dB với {int(st.get('peak_count', 0))} mẫu chạm đỉnh (clipping)", hard=strict)
    if strict and expect.get("true_peak_db") is not None and lo.get("TP") is not None and lo["TP"] > expect["true_peak_db"] + 0.2:
        add("TRUE_PEAK_OVER", f"true peak {lo['TP']:.2f} dBFS vượt trần {expect['true_peak_db']:.2f}")
    s = cfg["silence"]
    sil = m.get("silences") or []
    if dur > 0 and not silent:
        ratio = sum(x["duration"] for x in sil) / dur
        longest = max((x["duration"] for x in sil), default=0.0)
        if ratio > s["max_ratio"]:
            add("EXCESSIVE_SILENCE", f"im lặng chiếm {ratio:.0%} (> {s['max_ratio']:.0%})", hard=strict)
        if longest > s["max_gap_s"]:
            add("EXCESSIVE_SILENCE", f"có khoảng im lặng {longest:.1f}s (> {s['max_gap_s']}s)", hard=strict)
    if strict and expect.get("lufs") is not None and lo.get("I") is not None and math.isfinite(lo["I"]):
        if abs(lo["I"] - expect["lufs"]) > cfg["loudness_tolerance_lu"]:
            add("LOUDNESS_OFF", f"{lo['I']:.1f} LUFS, mục tiêu {expect['lufs']:.1f} (±{cfg['loudness_tolerance_lu']})")
    return {"ok": not errors, "errors": errors, "warnings": warns, "kind": kind,
            "measures": {"duration_sec": round(dur, 3), "peak_db": st.get("peak_db"), "rms_db": st.get("rms_db"),
                         "peak_count": st.get("peak_count"), "lufs": lo.get("I"), "lra": lo.get("LRA"), "true_peak_db": lo.get("TP"),
                         "silence_ratio": round(sum(x["duration"] for x in sil) / dur, 4) if dur else None,
                         "longest_silence_s": round(max((x["duration"] for x in sil), default=0.0), 3),
                         "sample_rate": pr.get("sample_rate"), "channels": pr.get("channels"), "codec": pr.get("codec")}}
