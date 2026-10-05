"""Stage AUDIO: audio_master (narration thô từ TTS hoặc nhập ngoài) -> Narration Master -> bản YouTube + các part TikTok + báo cáo QA.

Luồng (chi tiết xử lý ở audio/processor.py, độc lập engine TTS):
    QA đầu vào -> Narration Master (loudness/compressor/limiter theo profile) -> QA
               +-> YouTube: watermark (asset của channel) + gap + master -> QA
               +-> TikTok : tăng tốc giữ cao độ -> cắt thông minh theo timeline/khoảng lặng -> QA từng part
QA hỏng (corrupt, rỗng, clipping, im lặng quá mức, sai định dạng, độ dài bất thường...) => POLICY, không cho đi tiếp với audio xấu.
Đổi watermark/profile chỉ đổi stage_key của stage này (TTS không chạy lại); bên trong, các bước trung gian được cache theo nội dung.
"""
from __future__ import annotations

from pathlib import Path

from ..contracts import AudioProcessor, ErrorClass, StageContext, StageError, StageResult
from ..fsutil import atomic_write_json
from .profile import resolve


def _fail(what: str, rep: dict) -> StageError:
    codes = ",".join(e["code"] for e in rep["errors"])
    return StageError(ErrorClass.POLICY, "AUDIO_QA_FAILED", f"{what}: {codes}: " + "; ".join(e["message"] for e in rep["errors"])[:400],
                      {"what": what, "errors": rep["errors"], "measures": rep.get("measures")})


def run(ctx: StageContext, audio: AudioProcessor) -> StageResult:
    prof = resolve(ctx.params)
    raw = ctx.one("audio_master")
    timeline = ctx.read_json("audio_timeline") if ctx.inputs.get("audio_timeline") else None
    watermark = Path(prof["watermark_path"]) if prof["watermark_path"] else None      # channel asset, tùy chọn (HANDOFF §10)
    if watermark and not watermark.is_file():
        raise StageError(ErrorClass.POLICY, "WATERMARK_MISSING", str(watermark), resource="input")
    report: dict = {"schema": 1, "profile": {"format": prof["format"], "master": prof["master"], "youtube": prof["youtube"],
                                             "tiktok": {k: v for k, v in prof["tiktok"].items()}, "qa": prof["qa"]},
                    "health": audio.health()}
    ctx.progress(0, 4, "qa input")
    qin = audio.qa_full(raw, {"kind": "input"}, ctx)
    report["input"] = qin
    if not qin["ok"]:
        raise _fail("audio đầu vào", qin)
    in_dur = qin["measures"].get("duration_sec")

    # ---- Narration Master
    nm = ctx.stage_dir / "narration_master.wav"
    minfo = audio.master(raw, nm, ctx)
    lo = prof["master"]["loudness"]
    expect = {"kind": "narration", **({"duration_sec": in_dur} if in_dur else {}),
              **({"lufs": lo["target_lufs"], "true_peak_db": prof["master"]["limiter"]["ceiling_db"] if prof["master"]["limiter"]["enabled"]
                  else lo["true_peak_db"]} if lo["enabled"] else {})}
    qnm = audio.qa_full(nm, expect, ctx)
    report["narration_master"] = {"info": minfo, "qa": qnm}
    if not qnm["ok"]:
        raise _fail("Narration Master", qnm)
    ctx.progress(1, 4, "narration master")

    # ---- YouTube
    yt = ctx.stage_dir / "youtube.wav"
    yinfo = audio.build_youtube_audio(nm, watermark, yt, ctx)
    qyt = audio.qa_full(yt, {"kind": "youtube", "duration_sec": yinfo["duration_sec"]}, ctx)
    report["youtube"] = {"info": yinfo, "qa": qyt}
    if not qyt["ok"]:
        raise _fail("audio YouTube", qyt)
    ctx.progress(2, 4, "youtube")

    # ---- TikTok
    tk = prof["tiktok"]
    res = audio.build_tiktok_parts(nm, float(tk["speed"]), float(tk["target_part_sec"]), ctx.stage_dir / "tiktok", ctx, timeline)
    parts = res["parts"]
    if not parts:
        raise StageError(ErrorClass.POLICY, "NO_TIKTOK_PARTS", "không có part nào")
    part_reports = []
    for p in parts:
        q = audio.qa_full(Path(p["path"]), {"kind": "part", "duration_sec": p["duration_sec"]}, ctx)
        part_reports.append({"index": p["index"], "qa": q})
        if not q["ok"]:
            raise _fail(f"part TikTok {p['index']}", q)
    report["tiktok"] = {"stretch": res.get("stretch"), "split": res.get("split"), "warnings": res.get("warnings", []),
                        "parts": [{k: (v if k != "path" else Path(v).name) for k, v in p.items()} for p in parts], "qa": part_reports}
    ctx.progress(3, 4, "tiktok")

    rep_file = atomic_write_json(ctx.stage_dir / "audio_report.json", report)
    arts = [ctx.draft(nm, "narration_master", duration_sec=minfo.get("duration_sec"), lufs=qnm["measures"].get("lufs")),
            ctx.draft(yt, "audio_youtube", **{k: v for k, v in yinfo.items() if isinstance(v, (int, float, str, bool))}),
            *[ctx.draft(Path(p["path"]), "audio_tiktok", index=p["index"], duration_sec=p["duration_sec"], boundary=p["boundary"],
                        forced=p["forced"], mid_sentence=p["mid_sentence"]) for p in parts],
            ctx.draft(rep_file, "audio_report")]
    ctx.progress(4, 4, "done")
    warnings = (qin["warnings"] + qnm["warnings"] + qyt["warnings"] + [w for r in part_reports for w in r["qa"]["warnings"]])
    return StageResult(arts, {"tiktok_parts": len(parts), "narration_duration_sec": minfo.get("duration_sec"),
                              "youtube_duration_sec": yinfo["duration_sec"], "watermark": bool(yinfo.get("watermark")),
                              "stretch_engine": (res.get("stretch") or {}).get("engine"), "qa_warnings": len(warnings),
                              "split_warnings": len(res.get("warnings", []))})
