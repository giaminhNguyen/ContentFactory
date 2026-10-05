"""Validation THẬT (Phase 8): URL YouTube thật -> Source thật (Subtitle_supperVip/yt-dlp) -> Story (giả, mặc định) -> TTS giả -> Audio (ffmpeg thật)
-> thumbnail + render YouTube/TikTok bằng ContentFlow thật -> gói output; đo thời gian từng stage và kiểm tra video bằng ffprobe.

    python scripts/validate_real.py https://www.youtube.com/watch?v=... [--keep] [--story real]

Dùng cấu hình máy (config.local.json: .venv, ffmpeg, ContentFlow) nhưng dữ liệu chạy nằm trong thư mục tạm (workspace/output/runtime/channels riêng).
Không đăng YouTube (publish=fake). Story thật tốn token Claude: chỉ bật khi chủ động --story real. Thoát 0 nếu mọi kiểm tra đạt.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from contentfactory.jobs import pipeline as P                      # noqa: E402
from contentfactory.orchestrator import ops                        # noqa: E402
from contentfactory.orchestrator.config import load_config         # noqa: E402
from contentfactory.orchestrator.runner import Orchestrator        # noqa: E402

FONT = Path(r"C:\Windows\Fonts\arial.ttf")


def ff(*a: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *a], check=True)


def probe(p: Path) -> dict:
    d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(p)],
                                  capture_output=True, text=True, check=True).stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    return {"w": v and v["width"], "h": v and v["height"], "dur": float(d["format"]["duration"]), "vcodec": v and v["codec_name"],
            "acodec": a and a["codec_name"], "has_audio": a is not None}


def make_media(root: Path) -> dict:
    land, port = root / "pool_land", root / "pool_port"
    land.mkdir()
    port.mkdir()
    for i in (1, 2):
        ff("-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration=8", "-pix_fmt", "yuv420p", str(land / f"c{i}.mp4"))
        ff("-f", "lavfi", "-i", f"testsrc2=size=720x1280:rate=30:duration=8", "-pix_fmt", "yuv420p", str(port / f"c{i}.mp4"))
    tpl = root / "template.png"
    ff("-f", "lavfi", "-i", "color=c=0x203040:s=1648x928", "-frames:v", "1", str(tpl))
    return {"land": land, "port": port, "template": tpl}


def scenario_subtitle_only(orc: Orchestrator, url: str) -> list[str]:
    """Chế độ SUBTITLE_ONLY với nguồn thật: chỉ stage source chạy, artifact transcript được tạo."""
    jid = orc.submit({"input": {"kind": "youtube_url", "value": url}, "made_for_kids": False}, mode="SUBTITLE_ONLY")
    orc.run(until_idle=True)
    ran = sorted({r["stage"] for r in orc.store.stage_runs(jid)})
    kinds = {x["kind"] for x in orc.store.artifacts(jid)}
    print(f"\n[SUBTITLE_ONLY] stages={ran} artifacts={sorted(kinds)}")
    out = []
    if ran != ["source"]:
        out.append(f"SUBTITLE_ONLY chạy stage khác: {ran}")
    if not kinds & {"transcript_clean", "transcript"}:
        out.append(f"SUBTITLE_ONLY thiếu transcript: {sorted(kinds)}")
    return out


def scenario_video_only(orc: Orchestrator, root: Path, seconds: int, target: float) -> list[str]:
    """Từ audio có sẵn (start=audio, đến output/publish) dài `seconds` giây: audio master + render YouTube/TikTok thật, nhiều part; kiểm part <-> audio."""
    chunk = root / "chunk.wav"
    ff("-f", "lavfi", "-i", "sine=frequency=300:duration=3:sample_rate=48000", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.7",
       "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]", "-map", "[a]", "-c:a", "pcm_s16le", str(chunk))
    long_wav = root / "long.wav"
    ff("-stream_loop", str(max(1, int(seconds / 3.7))), "-i", str(chunk), "-c:a", "pcm_s16le", str(long_wav))
    t0 = time.time()
    jid = orc.submit({"made_for_kids": False, "channel": "validate", "tiktok": {"speed": 2.0, "target_part_sec": target}},
                     mode="VIDEO_ONLY", inputs={"audio_master": str(long_wav), "metadata": {"title": "Video Only Dài"}})
    orc.run(until_idle=True)
    first_ran = sorted({r['stage'] for r in orc.store.stage_runs(jid)})
    orc.set_target(jid, 'publish')                  # nới target: các stage đã xong không được chạy lại
    orc.run(until_idle=True)
    res = ops.summary(orc, jid, echo=lambda *_: None)
    wall = time.time() - t0
    out: list[str] = []
    ran = sorted({r["stage"] for r in orc.store.stage_runs(jid)})
    print(f"\n[VIDEO_ONLY {seconds}s -> nới target publish] state={res['state']} {wall:.1f}s stages={ran}")
    for st in P.STAGES:
        runs = [r for r in orc.store.stage_runs(jid) if r["stage"] == st.name]
        if runs:
            print(f"  {st.name:16}{sum((r['ended_at'] or r['started_at']) - r['started_at'] for r in runs):>8.2f}s")
    if not res["ok"]:
        return [f"audio->output không PUBLISHED: {res['state']} hold={res['hold']}"]
    if any(len([r for r in orc.store.stage_runs(jid) if r["stage"] == x]) != 1 for x in first_ran):
        out.append("nới target đã chạy lại stage đã xong")
    if set(ran) & {"source", "story", "tts"}:
        out.append(f"audio->output đã chạy stage thượng nguồn: {ran}")
    d = Path(res["output_dir"])
    yv = probe(d / "youtube" / "video.mp4")
    parts = sorted((d / "tiktok").glob("part_*.mp4"))
    durs = [probe(x)["dur"] for x in parts]
    print(f"  youtube {yv['w']}x{yv['h']} {yv['dur']:.1f}s | {len(parts)} part TikTok: {[round(x, 1) for x in durs]}")
    if len(parts) < 2:
        out.append(f"audio {seconds}s với target {target}s phải ra >= 2 part, được {len(parts)}")
    if abs(yv["dur"] - seconds) > 0.05 * seconds + 3:
        out.append(f"YouTube dài {yv['dur']:.1f}s, audio {seconds}s")
    if abs(sum(durs) * 2 - yv["dur"]) > 0.03 * yv["dur"] + 2 + 0.5 * len(parts):
        out.append(f"tổng part x2 ({sum(durs) * 2:.1f}s) lệch YouTube ({yv['dur']:.1f}s)")
    over = [round(x, 1) for x in durs[:-1] if x > target * 1.3]
    if over:
        out.append(f"part dài hơn 130% target {target}s: {over}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--story", choices=["fake", "real"], default="fake")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--target-part-sec", type=float, default=20.0)
    ap.add_argument("--long-audio-sec", type=int, default=240)
    a = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix="cf-validate-"))
    media = make_media(root)
    over = {
        "paths": {"workspace": str(root / "workspace"), "output": str(root / "output"), "runtime": str(root / "runtime"), "db": str(root / "runtime" / "cf.db")},
        "channels_dir": str(root / "channels"), "tts_profiles_dir": str(root / "tts_profiles"),
        "adapters": {"story": "story_branch" if a.story == "real" else "fake", "tts": "fake", "publish": "fake"},
        "render": {"pools_dir": str(root / "pools"), "pool_sync_background": False,
                   "pools": {"gameplay": {"raw_dir": str(media["land"]), "orientation": "landscape"},
                             "gameplay_vertical": {"raw_dir": str(media["port"]), "orientation": "portrait"}},
                   "profiles": {"youtube": {"thumbnail": {"config_overrides": {"template": {"file": str(media["template"])},
                                                                                "title": {"font": str(FONT)}, "channel": {"font": str(FONT)}}}}}},
        "tools": {"contentflow": {"base_dir": str(root / "cfbase")}},
        "cleanup": {"enabled": False}, "job_defaults": {"tiktok": {"speed": 2.0, "target_part_sec": a.target_part_sec}},
    }
    cfg = load_config(REPO, over)
    ops.channel_init(cfg, "validate", "Kênh Validate", kids=False)
    chf = root / "channels" / "validate" / "channel.json"
    ch = json.loads(chf.read_text(encoding="utf-8"))
    ch["preset"]["audio"] = {"qa": {"silence": {"max_ratio": 0.8, "max_gap_s": 8.0}}}
    chf.write_text(json.dumps(ch), encoding="utf-8")
    orc = Orchestrator(cfg)
    print(f"root: {root}\nURL : {a.url}")
    t0 = time.time()
    res = ops.go(orc, a.url, "validate", title="Validate Real", kids=False)
    total = time.time() - t0
    problems: list[str] = []
    print(f"\nTổng {total:.1f}s")
    print(f"{'stage':16}{'attempts':>9}{'giây':>9}")
    for s in P.STAGES:
        runs = [r for r in orc.store.stage_runs(res["job_id"]) if r["stage"] == s.name]
        sec = sum((r["ended_at"] or r["started_at"]) - r["started_at"] for r in runs)
        print(f"{s.name:16}{len(runs):>9}{sec:>9.2f}")
    if not res["ok"]:
        problems.append(f"job không PUBLISHED: {res['state']} hold={res['hold']}")
    else:
        d = Path(res["output_dir"])
        yv = probe(d / "youtube" / "video.mp4")
        print("youtube/video.mp4", yv)
        if (yv["w"], yv["h"]) != (1920, 1080) and yv["w"] * 9 != yv["h"] * 16:
            problems.append(f"YouTube không phải 16:9: {yv}")
        if not yv["has_audio"]:
            problems.append("YouTube không có audio")
        parts = sorted((d / "tiktok").glob("part_*.mp4"))
        total_tt = 0.0
        for p in parts:
            v = probe(p)
            total_tt += v["dur"]
            print(p.name, v)
            if v["w"] * 16 != v["h"] * 9:
                problems.append(f"{p.name} không phải 9:16: {v}")
            if not v["has_audio"]:
                problems.append(f"{p.name} không có audio")
        if abs(total_tt * 2 - yv["dur"]) > max(2.0, 0.03 * yv["dur"]) + 0.5 * len(parts):
            problems.append(f"tổng TikTok x2 ({total_tt * 2:.1f}s) lệch audio YouTube ({yv['dur']:.1f}s) quá nhiều")
        leftovers = [p.name for p in d.rglob("*") if p.is_file() and (p.suffix in (".tmp", ".wav", ".part", ".json") and p.name != "project.json")]
        if leftovers:
            problems.append(f"output chứa file không thuộc layout: {leftovers}")
    problems += scenario_subtitle_only(orc, a.url)
    problems += scenario_video_only(orc, root, a.long_audio_sec, a.target_part_sec)
    print("\nKẾT QUẢ:", "ĐẠT" if not problems else "CÓ VẤN ĐỀ")
    for p in problems:
        print(" -", p)
    if a.keep:
        print("giữ thư mục:", root)
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
