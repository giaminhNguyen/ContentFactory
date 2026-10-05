"""Máy chủ UI với dữ liệu mẫu cho kiểm tra bằng trình duyệt thật (scripts/ui_qa/qa.mjs).

    python scripts/ui_qa/fixture_server.py [--port 8799] [--keep]

Dựng một root tạm (adapter giả, cấu hình chạy nhanh) với: kênh đã khai/chưa khai made_for_kids, job hoàn tất, job lỗi, job bị giữ (mất mạng), job cần credential,
job đang chạy (chậm), profile TTS, file mẫu (phụ đề/truyện/audio). `opener` ghi vào opened.log thay vì mở Explorer. Ghi fixture.json (đường dẫn file mẫu, url).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from contentfactory.orchestrator.config import load_config           # noqa: E402
from contentfactory.orchestrator.runner import Orchestrator          # noqa: E402
from contentfactory.orchestrator.service import Service              # noqa: E402
from contentfactory.orchestrator.webui import App, UiServer          # noqa: E402
from tests.support import make_root, params                          # noqa: E402
from tests.test_automode import LENIENT_AUDIO, tts_profile, write_channel, write_config   # noqa: E402

URL = "https://www.youtube.com/watch?v=abcdefghijk"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--many", type=int, default=0, help="thêm N job nhỏ (subtitle-only) để kiểm tra danh sách lớn")
    ap.add_argument("--empty", action="store_true", help="không tạo dữ liệu mẫu (kiểm tra trạng thái trống)")
    a = ap.parse_args()
    root = make_root()
    write_config(root, job_defaults={"tiktok": {"speed": 2.0, "target_part_sec": 0.8}}, auto={"hold_wait_s": 2}, poll_s=0.05)
    fx = {"root": str(root)}
    if not a.empty:
        write_channel(root, "kenh_a", {"name": "Kênh Truyện A", "sequence": {"last_used": 26}, "publishing": {"made_for_kids": False, "privacy": "unlisted", "tags": ["truyen"]},
                                       "preset": {"audio": LENIENT_AUDIO}})
        write_channel(root, "chua_khai", {"name": "Kênh Chưa Khai", "preset": {"audio": LENIENT_AUDIO}})
        tts_profile("giong_vi", root, tuned=True)
        tts_profile("giong_api", root, needs=[("env:QA_TTS_KEY", "credential")])
    srt = root / "phu-de.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nXin chào các bạn.\n\n2\n00:00:02,000 --> 00:00:04,000\nHôm nay kể chuyện ma.\n", encoding="utf-8")
    story = root / "story.txt"
    story.write_text("\n\n".join(f"Đoạn {i}: " + "Tôi đi trên con đường làng vắng. " * 6 for i in range(1, 6)), encoding="utf-8")
    wav = root / "audio.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b"\x00\x00" * 8000 * 4)
    fx.update(srt=str(srt), story=str(story), wav=str(wav), youtube=URL)
    orc = Orchestrator(load_config(root))
    opened = root / "opened.log"
    app = App(orc, run_loop=True, opener=lambda p: opened.open("a", encoding="utf-8").write(p + "\n"))
    orc.monitor.probes["network"] = type("Down", (), {"resource": "network", "check": lambda s: (False, "mất mạng (giả lập)")})()
    if not a.empty:
        # lỗi/giữ trước, rồi mới job thành công (job thành công cùng nội dung sẽ lấp cache TTS và làm mất lỗi giả lập)
        orc.submit(params(channel="kenh_a", project={"title": "Truyện bị lỗi giọng đọc"}, fake={"tts_chunk_3": {"error_class": "POLICY", "fail_until_attempt": 99}}), auto_resume=False)
        orc.submit(params(channel="kenh_a", project={"title": "Truyện chờ mạng"}, fake={"story": {"error_class": "RESOURCE", "resource": "network", "fail_until_attempt": 99}}), auto_resume=True)
        orc.submit(params(channel="kenh_a", project={"title": "Truyện cần đăng nhập"}, fake={"story": {"error_class": "AUTH", "fail_until_attempt": 99}}), auto_resume=False)
        orc.run()
        svc = Service(orc)
        svc.create_run({"input": {"value": URL}, "channel": "kenh_a", "run": "full", "title": "Truyện đã hoàn tất"})
        orc.run()
        orc.submit(params(channel="kenh_a", project={"title": "Truyện đang chạy chậm"}, fake={"story": {"sleep_s": 600, "attempt": 1}}), auto_resume=True)
    for i in range(a.many):
        orc.submit(params(channel="kenh_a", project={"title": f"Job hàng loạt số {i + 1}"}), mode="SUBTITLE_ONLY", auto_resume=False)
    srv = UiServer(app, a.port)
    srv.start()
    fx["base"] = srv.url
    (root / "fixture.json").write_text(json.dumps(fx, ensure_ascii=False), encoding="utf-8")
    print("READY", srv.url, root / "fixture.json", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
        if not a.keep:
            shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
