"""Cầu nối tới Subtitle_supperVip: gọi lại ĐÚNG code acquisition của nó, không chạy API/worker/DB của nó.

Chạy bằng interpreter có cài dependency của Subtitle_supperVip (youtube-transcript-api, pydantic-settings, ...),
KHÁC với tiến trình orchestrator (orchestrator chỉ dùng stdlib). Giao thức: một dòng JSON cuối cùng trên stdout;
log (nếu có) đi stderr. Lỗi đã phân loại trả `ok: false` với exit 0; chỉ crash thật mới exit != 0.

    supervip_bridge.py health --backend-dir DIR
    supervip_bridge.py fetch  --backend-dir DIR --video-id ID --passes JSON --out FILE [--with-metadata]

Dùng của module: services/subtitles.py (fetch_selected, choose_transcript qua fetch_selected, serialize, các
exception SubtitleUnavailable / LanguageUnavailable / BlockedByYouTube) và services/youtube.py
(YouTubeDataClient._video_details cho metadata, chỉ khi có YOUTUBE_API_KEY).
KHÔNG import: app.main, app.worker, app.models, app.database, app.services.jobs (hàng đợi/SQLite của module).
"""
import argparse
import json
import os
import sys
import tempfile


def emit(obj: dict) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def load(backend_dir: str):
    sys.path.insert(0, backend_dir)
    # `app.config` đọc ../.env tương đối theo cwd: chạy ở thư mục tạm để không nạp .env/DB/data của module.
    os.chdir(tempfile.mkdtemp(prefix="cf-supervip-"))
    from app.services import subtitles
    return subtitles


def cmd_health(a) -> None:
    subtitles = load(a.backend_dir)
    import importlib.metadata as md
    try:
        version = md.version("youtube-transcript-api")
    except md.PackageNotFoundError:                          # vd thư viện được cung cấp qua PYTHONPATH, không có metadata
        version = "unknown"
    emit({"ok": True, "python": sys.version.split()[0], "youtube_transcript_api": version,
          "module_file": subtitles.__file__})


def metadata_for(video_id: str) -> tuple[dict | None, str | None]:
    key = os.environ.get("YOUTUBE_API_KEY", "")
    if not key:
        return None, "YOUTUBE_API_KEY chưa đặt: không lấy được metadata từ YouTube Data API"
    try:
        from app.services.youtube import YouTubeDataClient
        details = YouTubeDataClient(key)._video_details([video_id]).get(video_id)
        if not details:
            return None, "Data API không trả video này"
        return {**details, "published_at": details["published_at"].isoformat() if details.get("published_at") else None}, None
    except Exception as e:                                   # metadata chỉ là bổ sung, không làm hỏng việc lấy phụ đề
        return None, f"{type(e).__name__}: {str(e)[:200]}"


def cmd_fetch(a) -> None:
    subtitles = load(a.backend_dir)
    last_language_error = None
    for i, p in enumerate(json.loads(a.passes)):
        try:
            transcript, translated, snippets = subtitles.fetch_selected(
                a.video_id, p["languages"], p["preference"], bool(p.get("allow_translation")))
        except subtitles.LanguageUnavailable as e:           # pass này không có track phù hợp: thử pass kế
            last_language_error = str(e)
            continue
        except subtitles.SubtitleUnavailable as e:
            return emit({"ok": False, "error": {"kind": "SubtitleUnavailable", "message": str(e)[:400]}})
        except subtitles.BlockedByYouTube as e:
            return emit({"ok": False, "error": {"kind": "BlockedByYouTube", "message": str(e)[:400]}})
        except Exception as e:
            return emit({"ok": False, "error": {"kind": "Other", "type": type(e).__name__, "message": str(e)[:400]}})
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(subtitles.serialize(snippets, "json"))
        meta, meta_error = metadata_for(a.video_id) if a.with_metadata else (None, None)
        return emit({"ok": True, "pass": i, "snippets": len(snippets), "metadata": meta, "metadata_error": meta_error,
                     "track": {"language": transcript.language, "language_code": transcript.language_code,
                               "is_generated": bool(transcript.is_generated), "translated": bool(translated)}})
    emit({"ok": False, "error": {"kind": "LanguageUnavailable", "message": last_language_error or "không có track phù hợp"}})


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("health")
    h.add_argument("--backend-dir", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--backend-dir", required=True)
    f.add_argument("--video-id", required=True)
    f.add_argument("--passes", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--with-metadata", action="store_true")
    a = ap.parse_args()
    try:
        {"health": cmd_health, "fetch": cmd_fetch}[a.cmd](a)
    except Exception as e:                                   # lỗi khi import module / thiếu dependency
        emit({"ok": False, "error": {"kind": "BridgeError", "type": type(e).__name__, "message": str(e)[:400]}})


if __name__ == "__main__":
    main()
