"""Shim chạy bằng Python của ContentFlow để gọi `source_sync.sync_videos` (worker của ContentFlow KHÔNG có job type sync; không sửa ContentFlow).

    python sync_shim.py request.json

request: {cf_root, source_folder, output_folder, size: "WxH", fps, quality, encoder, remove_audio, cancel_file}
stdout (JSON-lines): {"event": "progress", "pct"} | {"event": "warning", "message"} | {"event": "result", "files": [...], "encoder"} |
{"event": "failed", "class", "code", "message"}.  Mã thoát: 0 xong, 3 lỗi, 4 hủy.
Hủy: file `cancel_file` xuất hiện (hoặc tiến trình bị kill: file đích cụt sẽ không được tin ở lần sau, xem pools.py).
"""
import json
import sys
import threading
import time
from pathlib import Path


def emit(**kw) -> None:
    sys.__stdout__.write(json.dumps(kw, ensure_ascii=True) + "\n")
    sys.__stdout__.flush()


def main() -> int:
    req = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    sys.path.insert(0, req["cf_root"])
    sys.stdout = sys.stderr                                   # chỉ dòng giao thức được ra stdout thật
    try:
        import source_sync as ss
        w, h = (int(x) for x in str(req["size"]).split("x"))
        opts = ss.SyncOptions(source_folder=req["source_folder"], output_folder=req["output_folder"],
                              fps_mode="custom" if req.get("fps") else "original", fps_custom=int(req.get("fps") or 30),
                              size_mode="custom", size_width=w, size_height=h, quality=req.get("quality", "balanced"),
                              encoder=req.get("encoder", "auto"), remove_audio=bool(req.get("remove_audio", True)), existing="skip")
        cancel = threading.Event()
        cf = req.get("cancel_file")
        done = threading.Event()

        def watch() -> None:
            while not done.wait(0.2):
                if cf and Path(cf).exists():
                    cancel.set()
                    return
        threading.Thread(target=watch, daemon=True).start()
        last = [-1.0, 0.0]

        def progress(p: float) -> None:
            now = time.monotonic()
            if p >= 1.0 or (p - last[0] >= 0.01 and now - last[1] > 0.5):
                last[0], last[1] = p, now
                emit(event="progress", pct=round(p, 4))
        res = ss.sync_videos(opts, on_progress=progress, on_warning=lambda m: emit(event="warning", message=m), cancel_flag=cancel)
        done.set()
        if cancel.is_set():
            emit(event="failed", code="CANCELLED", message="đã hủy", **{"class": "CANCELLED"})
            return 4
        emit(event="result", encoder=res.encoder_used,
             files=[{"name": r.name, "status": r.status, "message": r.message} for r in res.results])
        return 0
    except BaseException as exc:                              # noqa: BLE001 - mọi lỗi phải thành sự kiện `failed` có kiểu
        code = getattr(exc, "code", None) or "INTERNAL_ERROR"
        try:
            import video_utils as vu
            klass = vu.ERROR_CLASS.get(code, "TRANSIENT")
        except Exception:                                     # noqa: BLE001
            klass = "TRANSIENT"
        if hasattr(exc, "code") and exc.code is None:         # VideoError không mã = cấu hình/đầu vào sai (giống media_core.classify)
            code, klass = "INVALID_CONFIG", "POLICY"
        if "không tìm thấy video" in str(exc).lower() or "không tồn tại" in str(exc).lower():
            code, klass = "MISSING_INPUT", "POLICY"
        emit(event="failed", code=code, message=str(exc) or type(exc).__name__, **{"class": klass})
        return 3


if __name__ == "__main__":
    sys.exit(main())
