"""Audio profile: MỌI tham số mastering/phân phối nằm ở đây (và trong params.audio của job), không hardcode vào code xử lý.

    params.audio = {"format": {...}, "join": {...}, "master": {...}, "youtube": {...}, "tiktok": {...}, "qa": {...}}
    params.tiktok = {"speed": 2.0, "target_part_sec": 600}      (lựa chọn sản phẩm cấp job, giữ từ Phase 1)
    params.watermark = "<đường dẫn file>"                        (asset của channel)

`resolve()` trả profile đầy đủ (mặc định ⊕ giá trị job). Các giá trị mặc định là điểm xuất phát hợp lý cho giọng đọc truyện
(-16 LUFS, true peak -1.5 dB, mono 48 kHz 24-bit nội bộ) chứ không phải chân lý: chỉnh theo kênh/nền tảng bằng profile.
Phần `format`/`join` dùng khi ghép chunk (stage tts), `master`/`youtube`/`tiktok`/`qa` ở stage audio — `stage_key` của từng stage chỉ
phụ thuộc phần của nó (pipeline.params_deps dùng đường dẫn có dấu chấm, vd "audio.join").
"""
from __future__ import annotations

import copy

from ..contracts import ErrorClass, StageError

DEFAULT: dict = {
    # định dạng kỹ thuật chuẩn cho toàn bộ chuỗi xử lý nội bộ (lossless). Lossy chỉ xảy ra một lần ở bước render video (AAC).
    "format": {"sample_rate": 48000, "channels": 1, "sample_fmt": "s24le"},
    "join": {   # chunk TTS -> narration thô: dọn biên + chèn pause
        "edge": {"threshold_db": -50.0, "keep_ms": 40, "fade_ms": 5, "highpass_hz": 0},
        "pause": {"scale": 1.0, "min_ms": 0, "max_ms": 3000, "compensate_edge": True, "tail_ms": 300},
    },
    "master": {  # narration thô -> Narration Master
        "highpass_hz": 0,
        "compressor": {"enabled": False, "threshold_db": -20.0, "ratio": 2.0, "attack_ms": 20, "release_ms": 250, "makeup_db": 0.0},
        "loudness": {"enabled": True, "target_lufs": -16.0, "true_peak_db": -1.5, "lra": 11.0},
        "limiter": {"enabled": True, "ceiling_db": -1.0, "attack_ms": 5, "release_ms": 50},
    },
    "youtube": {"watermark": {"position": "start", "gap_ms": 800, "offset_db": -2.0, "fade_ms": 10, "trim": True}},
    "tiktok": {
        "stretch": {"engine": "rubberband", "options": {"transients": "smooth", "detector": "soft", "phase": "laminar",
                                                          "window": "standard", "pitchq": "quality"}},
        "split": {"min_ratio": 0.85, "max_ratio": 1.15, "min_last_ratio": 0.4, "fade_ms": 10,
                  "bonus_sec": {"scene": 90.0, "paragraph": 45.0, "sentence": 15.0, "silence": 5.0},
                  "silence": {"threshold_db": -40.0, "min_s": 0.30, "paragraph_s": 0.80}},
        "loudness": {"enabled": True, "target_lufs": -16.0},
        "limiter": {"enabled": True, "ceiling_db": -1.0, "attack_ms": 5, "release_ms": 50},
    },
    "qa": {
        "min_duration_sec": 0.3,
        "duration_tolerance": {"abs_s": 1.0, "rel": 0.02},
        "clipping": {"peak_db_max": -0.1, "peak_count_max": 3},
        "silence": {"threshold_db": -45.0, "min_s": 0.5, "max_ratio": 0.30, "max_gap_s": 4.0},
        "loudness_tolerance_lu": 1.5,
        "max_decode_errors": 0,
    },
}
SAMPLE_FMTS = {"s16le": 2, "s24le": 3, "s32le": 4}


def _merge(a: dict, b: dict) -> dict:
    for k, v in b.items():
        a[k] = _merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return a


def resolve(params: dict | None) -> dict:
    """Profile hiệu lực của job: mặc định ⊕ params.audio ⊕ (tiktok.speed/target_part_sec, watermark). Raise POLICY nếu sai."""
    p = params or {}
    prof = _merge(copy.deepcopy(DEFAULT), copy.deepcopy(p.get("audio") or {}))
    tk = p.get("tiktok") or {}
    prof["tiktok"]["speed"] = float(tk.get("speed", 2.0))
    prof["tiktok"]["target_part_sec"] = float(tk.get("target_part_sec", 600))
    prof["watermark_path"] = p.get("watermark") or None
    errs = []
    f = prof["format"]
    if f["sample_fmt"] not in SAMPLE_FMTS:
        errs.append(f"format.sample_fmt phải thuộc {sorted(SAMPLE_FMTS)}")
    if not 8000 <= int(f["sample_rate"]) <= 192000:
        errs.append("format.sample_rate ngoài [8000, 192000]")
    if int(f["channels"]) not in (1, 2):
        errs.append("format.channels phải là 1 hoặc 2")
    if not 0.25 <= prof["tiktok"]["speed"] <= 4.0:
        errs.append("tiktok.speed ngoài [0.25, 4.0]")
    if prof["tiktok"]["target_part_sec"] <= 0:
        errs.append("tiktok.target_part_sec phải > 0")
    sp = prof["tiktok"]["split"]
    if not 0 < sp["min_ratio"] <= 1 <= sp["max_ratio"]:
        errs.append("tiktok.split: cần 0 < min_ratio <= 1 <= max_ratio")
    if prof["tiktok"]["stretch"]["engine"] not in ("rubberband", "atempo"):
        errs.append("tiktok.stretch.engine phải là rubberband | atempo")
    if prof["youtube"]["watermark"]["position"] not in ("start", "end", "both"):
        errs.append("youtube.watermark.position phải là start | end | both")
    lo = prof["master"]["loudness"]
    if lo["enabled"] and not -50 <= lo["target_lufs"] <= -5:
        errs.append("master.loudness.target_lufs ngoài [-50, -5]")
    if lo["true_peak_db"] > 0 or prof["master"]["limiter"]["ceiling_db"] > 0:
        errs.append("true_peak_db/ceiling_db phải <= 0")
    if prof["join"]["pause"]["min_ms"] > prof["join"]["pause"]["max_ms"]:
        errs.append("join.pause.min_ms > max_ms")
    if errs:
        raise StageError(ErrorClass.POLICY, "INVALID_AUDIO_PROFILE", "; ".join(errs), {"errors": errs})
    return prof
