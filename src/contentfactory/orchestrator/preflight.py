"""Preflight THEO PIPELINE ĐÃ CHỌN (Agent Plan Phase 9, D-106): chỉ kiểm những gì các stage SẼ CHẠY cần — không đăng thì không đòi đăng nhập YouTube,
đã có audio thì không đòi engine TTS, không render YouTube thì không đòi pool/template/ảnh thumbnail của nhánh đó.

Mỗi dòng: {id, label, status, detail, hint, stages, blocking}
  ok    đạt                         warn  chạy được nhưng nên chú ý / tài nguyên tạm thời chưa sẵn sàng (job sẽ giữ lại và tự chạy tiếp)
  fail  chắc chắn làm stage đó không chạy được; chỉ CHẶN nút Chạy khi `blocking` (lỗi cấu hình người dùng phải sửa), còn lại là thông tin
Tầng này chỉ ĐỌC (health của adapter, cấu hình, ổ đĩa); không gọi mạng, không tạo gì. Health được nhớ ngắn (10 giây) vì giao diện hỏi lại sau mỗi lần gõ.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from ..contracts import StageError
from ..jobs import pipeline as P

ADAPTER_LABEL = {"source": "Nguồn video/phụ đề", "story": "Viết truyện", "tts": "Giọng đọc (TTS)", "audio": "Xử lý audio", "planner": "Lập kế hoạch giọng đọc",
                 "render": "Render video", "output": "Đóng gói output", "sequence": "Đánh số tập", "publish": "Đăng YouTube"}
HINT = {"tts": "Mở Giọng đọc để kiểm tra engine/profile.", "render": "Mở Cài đặt & Doctor để xem ContentFlow.", "publish": "Mở Cài đặt & Doctor: daemon yt-uploader và đăng nhập YouTube.",
        "source": "Mở Cài đặt & Doctor để xem provider nguồn.", "story": "Mở Cài đặt & Doctor để xem engine truyện."}
TTL = 10.0
MIN_FREE_GB = 2.0


def _row(id: str, label: str, status: str, detail: str = "", hint: str = "", stages=(), blocking: bool = False) -> dict:
    return {"id": id, "label": label, "status": status, "detail": detail, "hint": hint, "stages": list(stages), "blocking": blocking}


def _health(orc, name: str) -> tuple[bool | None, str]:
    """(ok|None nếu adapter không có health, chi tiết). Nhớ TTL giây."""
    cache = orc.__dict__.setdefault("_preflight_cache", {})
    hit = cache.get(name)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    ad = orc.adapters.get(name)
    if ad is None or not hasattr(ad, "health"):
        res = (None, "")
    else:
        try:
            h = ad.health()
            res = (bool(h.get("ok")), "" if h.get("ok") else str(h.get("error") or h.get("detail") or "chưa sẵn sàng"))
        except Exception as e:                                                       # noqa: BLE001 — health lỗi = chưa sẵn sàng, không được làm hỏng bản xem trước
            res = (False, str(e))
    cache[name] = (time.time(), res)
    return res


def _ffmpeg(cfg) -> str | None:
    f = (cfg.data.get("tools") or {}).get("ffmpeg") or "ffmpeg"
    return shutil.which(f) or (f if Path(f).is_file() else None)


def run(orc, run_stages: list[str], channel: dict, merged: dict, templates: dict | None, template_error: str | None = None) -> dict:
    cfg = orc.cfg
    stages = [s for s in P.STAGES if s.name in set(run_stages)]
    rows: list[dict] = []
    skipped: list[str] = []

    # 1) adapter của từng stage sẽ chạy (suy từ khai báo Stage.adapters, không viết riêng từng stage)
    seen: dict[str, list[str]] = {}
    for st in stages:
        for a in st.adapters:
            seen.setdefault(a, []).append(st.name)
    for a, used in seen.items():
        ok, detail = _health(orc, a)
        if ok is None:
            continue
        label = ADAPTER_LABEL.get(a, a)
        rows.append(_row(f"adapter.{a}", label, "ok" if ok else "warn", detail if not ok else "sẵn sàng",
                         "" if ok else (HINT.get(a, "Mở Cài đặt & Doctor.") + " Job sẽ giữ lại và tự chạy tiếp khi sẵn sàng."), used))
    for a in ("tts", "publish", "story", "source"):                                     # nói rõ cái gì KHÔNG bị kiểm vì không nằm trong kế hoạch
        if a not in seen and a in ADAPTER_LABEL and orc.adapters.get(a) is not None and hasattr(orc.adapters.get(a), "health"):
            skipped.append(f"{ADAPTER_LABEL[a]}: không nằm trong kế hoạch nên không kiểm tra.")

    # 2) công cụ dùng chung: ffmpeg cho audio/render
    need_ff = [s.name for s in stages if s.name in ("audio", "render_youtube", "render_tiktok")]
    if need_ff:
        ff = _ffmpeg(cfg)
        rows.append(_row("tool.ffmpeg", "FFmpeg", "ok" if ff else "fail", ff or "không tìm thấy ffmpeg", "" if ff else "winget install Gyan.FFmpeg hoặc đặt tools.ffmpeg (Doctor chỉ cách).", need_ff))

    # 3) nhánh render: video nền theo hướng + template + ảnh thumbnail
    adapter = orc.adapters.get("render")
    uses_pool = bool(adapter is not None and getattr(adapter, "requires_pool", False))
    for sname, pid in (("render_youtube", "youtube"), ("render_tiktok", "tiktok")):
        if sname not in run_stages:
            continue
        label = "YouTube" if pid == "youtube" else "TikTok"
        if uses_pool:
            name = ((merged.get("render") or {}).get(pid) or {}).get("source_pool")
            spec = ((cfg.data.get("render") or {}).get("pools") or {}).get(name or "")
            if not name or not spec:
                rows.append(_row(f"pool.{pid}", f"Video nền {label}", "fail", "chưa chọn được pool video phù hợp", "Thêm pool ở Nguồn Media → Video (ngang cho YouTube, dọc cho TikTok).", [sname], True))
            else:
                raw = Path(str(spec.get("raw_dir", "")))
                n = sum(1 for p in raw.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}) if raw.is_dir() else 0
                rows.append(_row(f"pool.{pid}", f"Video nền {label}", "ok" if n else "fail", f"pool “{name}”: {n} video" if n else f"pool “{name}” không có video / thư mục không truy cập được",
                                 "" if n else "Sửa thư mục pool ở Nguồn Media → Video.", [sname], not n))
        if templates is not None:
            kinds = ["youtube", "thumbnail"] if pid == "youtube" else ["tiktok"]
            got = [templates[k] for k in kinds if k in templates]
            if got:
                rows.append(_row(f"template.{pid}", f"Template {label}", "ok", " · ".join(f"{t['name'] or t['id']} (v{t['version']})" for t in got), "", [sname]))
    if template_error:
        rows.append(_row("template.error", "Template", "fail", template_error, "Mở Kênh → Template và chọn template đã publish.", [s for s in run_stages if s.startswith("render_")], True))
    if "render_youtube" in run_stages:
        pool = (channel.get("thumbnail") or {}).get("image_pool")
        if pool:
            try:
                n = len(orc.image_pools.usable(pool))
                rows.append(_row("image_pool", "Pool ảnh thumbnail", "ok", f"pool “{pool}”: {n} ảnh hợp lệ", "", ["render_youtube"]))
            except StageError as e:
                rows.append(_row("image_pool", "Pool ảnh thumbnail", "fail", e.message, (e.detail or {}).get("hint", ""), ["render_youtube"], True))

    # 4) output: ổ đĩa
    if any(s.name == "output" for s in stages):
        try:
            out = cfg.path("output")
            free = shutil.disk_usage(out if out.exists() else out.anchor or ".").free / 2 ** 30
            rows.append(_row("disk.output", "Chỗ trống ổ output", "ok" if free >= MIN_FREE_GB else "warn", f"{free:.1f} GB trống",
                             "" if free >= MIN_FREE_GB else "Gần đầy: giải phóng ổ trước khi chạy dài.", ["output"]))
        except OSError as e:
            rows.append(_row("disk.output", "Chỗ trống ổ output", "warn", str(e), "", ["output"]))
    return {"checks": rows, "skipped": skipped, "ok": not any(r["status"] == "fail" for r in rows), "blocking": [r for r in rows if r["status"] == "fail" and r["blocking"]]}
