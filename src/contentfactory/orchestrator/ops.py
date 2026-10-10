"""Vận hành cho người dùng (D-82…D-85): `go` (một lệnh: URL + kênh -> gói output), `open`, `start` (dịch vụ chạy nền), `demo`, `channel-init`, tự bật daemon yt_uploader.

`go` không hỏi gì giữa chừng: mọi thứ suy ra được thì suy (preset kênh, TTS profile, pool, tên), mọi lựa chọn tự động được ghi vào `params.auto` và log.
Job bị giữ (mạng, quota…) thì chờ tự tiếp tục trong thời hạn `auto.hold_wait_s`; lỗi cần người (đăng nhập, thiếu asset) thì dừng ngay và nói rõ phải làm gì.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ..contracts import ErrorClass, StageError
from ..jobs import pipeline as P
from ..jobs.workspace import job_dir
from . import channels as CH
from .diagnose import USER_ONLY_HOLDS, explain, format_lines
from .config import Config, load_config



# ====================================================================================== daemon uploader
def ensure_uploader(cfg: Config, publish_adapter, wait_s: float = 25.0) -> tuple[bool, str]:
    """Bảo đảm daemon yt_uploader chạy khi adapter publish là yt_uploader. Trả (ok, ghi chú). Chỉ bật nếu `tools.yt_uploader.exe` được cấu hình."""
    if cfg.data["adapters"].get("publish") != "yt_uploader" or publish_adapter is None:
        return True, "không cần (adapters.publish != yt_uploader)"
    if publish_adapter.health().get("ok"):
        return True, "daemon đang chạy"
    spec = cfg.data.get("tools", {}).get("yt_uploader", {})
    exe = spec.get("exe")
    if not exe or not Path(exe).exists():
        return False, "daemon chưa chạy và chưa cấu hình tools.yt_uploader.exe (chạy setup hoặc `yt-uploader serve --headless`)"
    port = publish_adapter.url.rsplit(":", 1)[-1]
    argv = [str(exe), "serve", "--headless", "--no-open", "--port", port] + (["--portable"] if spec.get("portable", True) else [])
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
                     **({} if os.name == "nt" else {"start_new_session": True}))
    if spec.get("portable", True) and not publish_adapter.data_dir:
        publish_adapter.data_dir = str(Path(exe).parent / "data")
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if publish_adapter.health().get("ok"):
            return True, "đã tự bật daemon yt_uploader"
        time.sleep(0.5)
    return False, "đã thử bật daemon yt_uploader nhưng chưa sẵn sàng (xem `doctor`)"


# ====================================================================================== go
def classify_input(value: str) -> dict:
    v = value.strip()
    if v.lower().startswith(("http://", "https://")):
        return {"kind": "youtube_url", "value": v}
    if Path(v).is_file():
        return {"kind": "transcript_file", "value": str(Path(v).resolve())}
    raise StageError(ErrorClass.POLICY, "UNSUPPORTED_INPUT", f"'{value}' không phải URL YouTube hay file phụ đề/transcript tồn tại")


def go(orc, value: str, channel: str | None = None, title: str | None = None, kids: bool | None = None, wait: bool = True,
       echo=print) -> dict:
    """Một lệnh: tạo job với preset của kênh + chạy tới khi xong (hoặc cần người). Trả {job_id, state, output_dir, youtube_url, hold, ok}."""
    cfg = orc.cfg
    ok, note = ensure_uploader(cfg, orc.adapters.get("publish"))
    if not ok:
        echo(f"! uploader: {note}")
    params: dict = {"input": classify_input(value)}
    cid = channel or cfg.data["job_defaults"].get("channel") or "default"
    params["channel"] = cid
    if title:
        params["project"] = {"title": title}
    ch = CH.load_channel(cfg, cid)
    if kids is not None:
        if not isinstance(kids, bool):
            raise StageError(ErrorClass.POLICY, "MISSING_MADE_FOR_KIDS", f"made_for_kids phải là boolean thật, nhận {kids!r}", resource="input")
        params["made_for_kids"] = kids
    elif not isinstance((ch.get("publishing") or {}).get("made_for_kids"), bool):
        raise StageError(ErrorClass.POLICY, "MISSING_MADE_FOR_KIDS",
                         f"kênh '{cid}' chưa khai made_for_kids (khai báo COPPA, không đoán): chạy `channel-init {cid} --force --kids no|yes` hoặc thêm --kids / --not-kids", resource="input")
    jid = orc.submit(params)
    job = orc.store.get_job(jid)
    for d in job["params"].get("auto", []):
        echo(f"  tự chọn {d['what']}: {d['value']}  ({d['why']})")
    echo(f"job {jid}: đang chạy (kênh '{cid}')")
    if not wait:
        return {"job_id": jid, "state": job["state"], "ok": True}
    stop = threading.Event()
    th = threading.Thread(target=orc.run, kwargs={"until_idle": False, "stop": stop}, daemon=True)
    th.start()
    last, hold_wait = None, float(cfg.data.get("auto", {}).get("hold_wait_s", 300))
    try:
        while True:
            j = orc.store.get_job(jid)
            line = f"{j['state']}" + (f" ({j['progress']})" if j.get("progress") else "") + (f" [giữ: {j['hold_reason']}]" if j.get("hold_reason") else "")
            if line != last:
                echo(f"  {line}")
                last = line
            if j["state"] in (P.PUBLISHED, P.FAILED) or P.is_complete(j["state"], j.get("target_idx")):
                break
            if j.get("hold_reason") and (j["hold_reason"] in USER_ONLY_HOLDS or j.get("needs_user") or time.time() - (j.get("hold_since") or time.time()) > hold_wait):
                break
            time.sleep(0.4)
    except KeyboardInterrupt:
        echo("đã dừng (Ctrl-C); chạy lại `start` hoặc `resume` để tiếp tục")
    stop.set()
    th.join(60)
    return summary(orc, jid, echo)


def rerender(orc, job_id: str) -> str:
    """Dựng lại video/thumbnail của một job bằng template HIỆN TẠI của kênh (job mới, dùng lại audio + metadata của job cũ: không chạy lại Source/Story/TTS/Audio).
    Đích là render_tiktok (không đăng lại). Job cũ giữ nguyên snapshot template của nó."""
    old = orc.store.get_job(job_id)
    if old is None:
        raise StageError(ErrorClass.POLICY, "JOB_NOT_FOUND", f"không có job {job_id}", resource="input")
    params = {k: v for k, v in old["params"].items() if k not in ("templates", "auto", "ui", "input")}
    return orc.submit(params, start_stage="render_youtube", target_stage="render_tiktok",
                      from_job={"job_id": job_id, "kinds": ["audio_youtube", "audio_tiktok", "metadata"]})


def summary(orc, jid: str, echo=print) -> dict:
    j = orc.store.get_job(jid)
    res = {"job_id": jid, "state": j["state"], "hold": j.get("hold_reason"), "output_dir": None, "youtube_url": None, "ok": P.is_complete(j["state"], j.get("target_idx"))}
    jd = job_dir(orc.cfg.path("workspace"), jid)
    for a in orc.store.artifacts(jid):
        if a["kind"] in ("output_package", "publish_result"):
            try:
                d = json.loads((jd / a["path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if a["kind"] == "output_package":
                res["output_dir"] = d.get("project_dir")
            else:
                res["youtube_url"] = d.get("remote_url")
    if res["ok"]:
        echo(f"XONG job {jid}")
    else:
        d = explain(orc, jid)
        head = {"failed": "LỖI", "waiting": "ĐANG CHỜ", "attention": "CẦN BẠN XỬ LÝ"}.get(d["status"])
        if head:
            echo(f"{head} job {jid}")
            for ln in format_lines(d):
                echo(ln)
        else:
            echo(f"job {jid}: {j['state']}")
    if res["output_dir"]:
        echo(f"  Output : {res['output_dir']}")
    if res["youtube_url"]:
        echo(f"  YouTube: {res['youtube_url']}")
    return res


def open_path(p: str | Path) -> None:
    p = str(p)
    if os.name == "nt":
        os.startfile(p)                                    # noqa: S606 - mở thư mục output cho người dùng
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def latest_output(orc, job_id: str | None = None) -> str | None:
    jobs = [orc.store.get_job(job_id)] if job_id else list(reversed(orc.store.list_jobs()))
    for j in jobs:
        if j is None:
            continue
        d = summary(orc, j["id"], echo=lambda *_: None)["output_dir"]
        if d:
            return d
    return None


# ====================================================================================== channel-init
CHANNEL_TEMPLATE = {
    "_doc": "Channel preset (Auto Mode). Chỉnh file này một lần; mỗi lần chạy chỉ cần URL + tên kênh. name: tên hiển thị (thumbnail/mô tả). "
            "sequence.last_used: số Full Audio đã đăng trước đó. watermark: file trong thư mục kênh (không bắt buộc). "
            "preset.tts_profile: tên profile trong tts_profiles/ (trống = tự chọn). preset.pools: pool video nguồn cho youtube/tiktok (trống = tự chọn). "
            "preset.render.youtube/tiktok: override profile render. preset.tiktok: speed/target_part_sec (trống = theo config). publishing: privacy, account_id, tags, playlists, made_for_kids.",
    "title_template": "[Full Audio][{channel_name} số {sequence}] | {project_title}",
    "description_template": "{project_title}\n\n{channel_name}",
    "sequence": {"last_used": 0},
    "publishing": {"privacy": "private", "made_for_kids": False},
    "preset": {"tts_profile": None, "pools": {}, "render": {}, "tiktok": {}},
}


def channel_init(cfg: Config, cid: str, name: str | None = None, kids: bool = False, force: bool = False, last_used: int = 0) -> Path:
    d = CH.channel_dir(cfg, cid)
    f = d / "channel.json"
    if f.exists() and not force:
        raise StageError(ErrorClass.POLICY, "CHANNEL_EXISTS", f"{f} đã có (dùng --force để ghi đè)")
    body = json.loads(json.dumps(CHANNEL_TEMPLATE))
    body["name"] = name or cid
    if not isinstance(kids, bool):                          # không ép kiểu: "false"/0 -> True sẽ khai báo COPPA sai
        raise StageError(ErrorClass.POLICY, "MISSING_MADE_FOR_KIDS", f"made_for_kids phải là boolean thật, nhận {kids!r}", resource="input")
    body["publishing"]["made_for_kids"] = kids
    body["sequence"]["last_used"] = int(last_used)
    CH.MD.normalize_channel(body, cid)                       # tự kiểm: template sinh ra phải hợp lệ
    d.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f


def list_channels(cfg: Config) -> list[dict]:
    d = CH.channel_dir(cfg, "x").parent
    out = []
    for p in sorted(x for x in d.iterdir() if x.is_dir() and not x.name.startswith(".")) if d.is_dir() else []:
        try:
            ch = CH.load_channel(cfg, p.name)
            out.append({"id": p.name, "name": ch["name"], "ok": True, "preset": sorted((ch.get("preset") or {}).keys()),
                        "watermark": bool(ch.get("watermark") and Path(ch["watermark"]).is_file()), "privacy": (ch.get("publishing") or {}).get("privacy")})
        except StageError as e:
            out.append({"id": p.name, "name": None, "ok": False, "error": e.message})
    return out


# ====================================================================================== demo
def demo(keep: bool = False, echo=print) -> dict:
    """Chạy toàn bộ pipeline trên adapter GIẢ (audio dùng ffmpeg thật nếu có) trong thư mục tạm: kiểm tra cài đặt nhanh, không tốn tiền, không cần mạng."""
    import shutil
    from .runner import Orchestrator
    root = Path(tempfile.mkdtemp(prefix="cf-demo-"))
    (root / "config").mkdir()
    lock = Path(__file__).resolve().parents[3] / "modules.lock"
    if lock.exists():
        shutil.copyfile(lock, root / "modules.lock")
    adapters = {"audio": "ffmpeg"} if shutil.which("ffmpeg") and shutil.which("ffprobe") else {}
    (root / "config" / "config.json").write_text(json.dumps({"adapters": adapters, "poll_s": 0.05, "cleanup": {"enabled": False},
                                                              }), encoding="utf-8")
    cfg = load_config(root)
    f = channel_init(cfg, "demo", "Kênh Demo")
    body = json.loads(f.read_text(encoding="utf-8"))            # dữ liệu giả có chunk ngắn/pause dài: nới QA im lặng và chia part nhỏ cho demo (preset của kênh)
    body["preset"].update({"tiktok": {"speed": 2.0, "target_part_sec": 1.0}, "audio": {"qa": {"silence": {"max_ratio": 0.8, "max_gap_s": 8.0}}}})
    f.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    orc = Orchestrator(cfg)
    echo(f"demo trong {root} (adapter giả{', audio bằng ffmpeg thật' if adapters else ''})")
    res = go(orc, "https://youtu.be/demo", "demo", title="Truyện Demo", kids=False, echo=echo)
    if res["output_dir"]:
        echo("  Nội dung gói output:")
        for p in sorted(Path(res["output_dir"]).rglob("*")):
            if p.is_file():
                echo(f"    {p.relative_to(res['output_dir']).as_posix()}")
    res["root"] = str(root)
    if not keep:
        shutil.rmtree(root, ignore_errors=True)
    return res
