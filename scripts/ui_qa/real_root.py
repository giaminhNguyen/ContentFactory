"""Dựng một root tạm dùng thành phần THẬT (ffmpeg, ContentFlow, nguồn thật; Story/TTS/publish giả) rồi mở `cf ui` thật trên đó, để kiểm tra giao diện với ứng dụng thật.

    python scripts/ui_qa/real_root.py [--port 8802]

Lấy cấu hình máy từ config/config.local.json của repo (do setup sinh). Không có pool/template: giao diện phải tự tạo dữ liệu mẫu (nút "Tạo dữ liệu mẫu").
In `READY <url> <root>` khi sẵn sàng; Ctrl-C để dừng và dọn.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from contentfactory.orchestrator.config import load_config   # noqa: E402
from contentfactory.orchestrator import ops                   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8802)
    a = ap.parse_args()
    local = REPO / "config" / "config.local.json"
    if not local.is_file():
        print("Chưa có config/config.local.json: chạy setup trước.", file=sys.stderr)
        return 2
    root = Path(tempfile.mkdtemp(prefix="cf-realui-"))
    (root / "config").mkdir()
    shutil.copyfile(REPO / "modules.lock", root / "modules.lock")
    (root / "config" / "config.json").write_text(json.dumps({"poll_s": 0.1, "cleanup": {"enabled": False}}), encoding="utf-8")
    cfg = json.loads(local.read_text(encoding="utf-8-sig"))
    cfg["adapters"].update({"story": "fake", "tts": "fake", "publish": "fake"})
    cfg.setdefault("tools", {}).setdefault("contentflow", {})["root"] = str(REPO / "modules" / "ContentFlow")
    cfg.setdefault("supervip", {})["backend_dir"] = str(REPO / "modules" / "Subtitle_supperVip" / "backend")
    cfg["job_defaults"] = {"tiktok": {"speed": 2.0, "target_part_sec": 20}}
    cfg.pop("render", None)                                             # chưa có pool/template: để UI tự tạo dữ liệu mẫu
    (root / "config" / "config.local.json").write_text(json.dumps(cfg), encoding="utf-8")
    ops.channel_init(load_config(root), "kenh_that", "Kênh Thật", kids=False)
    chf = root / "channels" / "kenh_that" / "channel.json"
    ch = json.loads(chf.read_text(encoding="utf-8"))
    ch["preset"]["audio"] = {"qa": {"silence": {"max_ratio": 0.8, "max_gap_s": 8.0}}}
    chf.write_text(json.dumps(ch), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "contentfactory", "--root", str(root), "ui", "--no-open", "--port", str(a.port)],
                            env={**__import__("os").environ, "PYTHONPATH": str(REPO / "src"), "PYTHONIOENCODING": "utf-8"})
    for _ in range(80):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{a.port}/", timeout=1).read()
            break
        except OSError:
            time.sleep(0.25)
    (root / "fixture.json").write_text(json.dumps({"root": str(root), "base": f"http://127.0.0.1:{a.port}"}), encoding="utf-8")
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
