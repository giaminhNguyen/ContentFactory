"""UI thật + ContentFlow THẬT cho luồng Template (qa.mjs --only templates). Mọi dữ liệu nằm trong root tạm (kênh, template/asset của người dùng, cache);
không đụng channels/ hay contentflow_user/ của repo.

    python scripts/ui_qa/real_templates.py [--port 8803]

Cần Python có Pillow (CF_TEST_CONTENTFLOW_PYTHON, mặc định .venv của repo) và ffmpeg/ffprobe trên PATH. In `READY <url> <root>`; Ctrl-C để dừng và dọn.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from tests.test_automode import LENIENT_AUDIO, write_channel   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8803)
    a = ap.parse_args()
    py = os.environ.get("CF_TEST_CONTENTFLOW_PYTHON") or str(REPO / ".venv" / "Scripts" / "python.exe")
    if not Path(py).exists():
        print(f"Không có Python cho ContentFlow: {py}", file=sys.stderr)
        return 2
    root = Path(tempfile.mkdtemp(prefix="cf-tplui-"))
    (root / "config").mkdir()
    shutil.copyfile(REPO / "modules.lock", root / "modules.lock")
    (root / "config" / "config.json").write_text(json.dumps({
        "poll_s": 0.1, "cleanup": {"enabled": False}, "adapters": {"render": "contentflow"},
        "tools": {"contentflow": {"root": str(REPO / "modules" / "ContentFlow"), "python": py, "base_dir": str(root / "cfbase"), "user_root": str(root / "cf_user")}},
        "job_defaults": {"tiktok": {"speed": 2.0, "target_part_sec": 20}}}), encoding="utf-8")
    kids = {"publishing": {"made_for_kids": False}, "preset": {"audio": LENIENT_AUDIO}}
    write_channel(root, "kenh_a", {"name": "Kênh Truyện A", **kids})
    write_channel(root, "kenh_b", {"name": "Kênh B", **kids, "templates": {"youtube_video": "youtube_framed"}})
    # ảnh mẫu cho tải asset lên
    from PIL import Image
    Image.new("RGBA", (400, 300), (232, 51, 111, 255)).save(root / "frame_mau.png")
    proc = subprocess.Popen([sys.executable, "-m", "contentfactory", "--root", str(root), "ui", "--no-open", "--port", str(a.port)],
                            env={**os.environ, "PYTHONPATH": str(REPO / "src"), "PYTHONIOENCODING": "utf-8"})
    for _ in range(80):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{a.port}/", timeout=1).read()
            break
        except OSError:
            time.sleep(0.25)
    (root / "fixture.json").write_text(json.dumps({"root": str(root), "base": f"http://127.0.0.1:{a.port}", "youtube": "https://www.youtube.com/watch?v=abcdefghijk",
                                                   "asset_png": str(root / "frame_mau.png")}), encoding="utf-8")
    print("READY", f"http://127.0.0.1:{a.port}/", root, flush=True)
    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        proc.wait(15)
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
