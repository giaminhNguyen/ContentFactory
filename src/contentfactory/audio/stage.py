"""Stage AUDIO: master -> audio YouTube (watermark) + các part TikTok (tăng tốc, cắt theo thời lượng)."""
from __future__ import annotations

from pathlib import Path

from ..contracts import AudioProcessor, ErrorClass, StageContext, StageError, StageResult


def run(ctx: StageContext, audio: AudioProcessor) -> StageResult:
    master = ctx.one("audio_master")
    wm = ctx.params.get("watermark")                 # channel asset, tùy chọn (HANDOFF §10)
    watermark = Path(wm) if wm else None
    if watermark and not watermark.is_file():
        raise StageError(ErrorClass.POLICY, "WATERMARK_MISSING", str(watermark))
    tk = ctx.params["tiktok"]                        # speed / target_part_sec là config, không hardcode (HANDOFF §11)
    yt = ctx.stage_dir / "youtube.wav"
    yt_info = audio.build_youtube_audio(master, watermark, yt, ctx)
    parts = audio.build_tiktok_parts(master, float(tk["speed"]), float(tk["target_part_sec"]),
                                     ctx.stage_dir / "tiktok", ctx)
    if not parts:
        raise StageError(ErrorClass.POLICY, "NO_TIKTOK_PARTS", "không có part nào")
    ctx.progress(1, 1 + len(parts), "youtube audio")
    arts = [ctx.draft(yt, "audio_youtube", **yt_info)]
    for i, p in enumerate(parts, 1):
        arts.append(ctx.draft(p, "audio_tiktok", index=i, duration_sec=audio.qa(p)["duration_sec"]))
        ctx.progress(1 + i, 1 + len(parts), "tiktok parts")
    return StageResult(arts, {"tiktok_parts": len(parts), **yt_info})
