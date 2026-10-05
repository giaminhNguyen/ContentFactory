"""QA một file audio bằng đúng bộ kiểm của stage audio (cần ffmpeg + ffprobe).

    python scripts/audio_qa.py voice.wav [--kind narration|input|youtube|part] [--lufs -16] [--duration 600] [--root .]

In báo cáo JSON; mã thoát 0 nếu đạt, 1 nếu có lỗi. Ngưỡng lấy từ profile mặc định (audio/profile.py), ghi đè bằng --profile file.json
(nội dung là `params` của job, vd {"audio": {"qa": {"silence": {"max_ratio": 0.5}}}}).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contentfactory.audio.processor import FfmpegAudio         # noqa: E402
from contentfactory.orchestrator.config import load_config     # noqa: E402
from contentfactory.tts.autotune import make_ctx               # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--kind", default="narration")
    ap.add_argument("--lufs", type=float)
    ap.add_argument("--duration", type=float)
    ap.add_argument("--root", default=".")
    ap.add_argument("--profile")
    a = ap.parse_args()
    audio = FfmpegAudio(load_config(Path(a.root)).data.get("tools", {}))
    ctx = make_ctx(Path(a.file).resolve().parent)
    if a.profile:
        ctx.params = json.loads(Path(a.profile).read_text(encoding="utf-8"))
    expect = {"kind": a.kind, **({"lufs": a.lufs} if a.lufs is not None else {}), **({"duration_sec": a.duration} if a.duration else {})}
    rep = audio.qa_full(Path(a.file), expect, ctx)
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
