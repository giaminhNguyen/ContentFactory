"""source_sync GIẢ: cùng API tối thiểu với ContentFlow (`SyncOptions`, `sync_videos`) nhưng chỉ sao chép byte, để test logic pool không cần ffmpeg.

Điều khiển: biến môi trường FAKE_SYNC_DELAY (giây/mỗi file), file tên `bad_*` báo lỗi từng file. Mỗi lần gọi ghi một dòng vào
`<output_folder>/../fake_sync_calls.log` gồm danh sách file thực sự được xử lý (file đã có ở đích và size > 0 thì bỏ qua, giống ContentFlow).
"""
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv")


@dataclass
class SyncOptions:
    source_folder: str = ""
    output_folder: str = ""
    fps_mode: str = "original"
    fps_custom: int = 30
    size_mode: str = "original"
    size_preset: str = "1080p"
    size_width: int = 1920
    size_height: int = 1080
    quality: str = "balanced"
    encoder: str = "auto"
    remove_audio: bool = True
    existing: str = "skip"


@dataclass
class FileResult:
    name: str
    status: str
    message: str = ""


@dataclass
class SyncResult:
    results: list
    encoder_used: str = "cpu"


def sync_videos(opts, on_status=None, on_progress=None, on_warning=None, cancel_flag=None):
    src, dst = Path(opts.source_folder), Path(opts.output_folder)
    dst.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in src.iterdir() if p.is_file() and p.suffix.lower() in EXTS)
    if not files:
        raise RuntimeError("Không tìm thấy video nào")
    res, done = [], []
    for i, f in enumerate(files):
        if cancel_flag is not None and cancel_flag.is_set():
            break
        d = dst / (f.stem + ".mp4")
        if opts.existing == "skip" and d.is_file() and d.stat().st_size > 0:
            res.append(FileResult(f.name, "skipped"))
            continue
        time.sleep(float(os.environ.get("FAKE_SYNC_DELAY", "0")))
        if f.name.startswith("bad_"):
            res.append(FileResult(f.name, "error", "File không có stream video."))
            continue
        d.write_bytes(f.read_bytes() + f"|{opts.size_width}x{opts.size_height}@{opts.fps_custom}".encode())
        done.append(f.name)
        res.append(FileResult(f.name, "ok"))
        if on_progress:
            on_progress((i + 1) / len(files))
    with open(dst.parent / "fake_sync_calls.log", "a", encoding="utf-8") as lg:
        lg.write(json.dumps({"synced": done, "size": f"{opts.size_width}x{opts.size_height}"}) + "\n")
    return SyncResult(res)
