"""media_worker GIẢ (giao thức JSON-lines v1 giống ContentFlow thật) để test RenderAdapter không cần Pillow/ffmpeg.

Điều khiển bằng file JSON `<base-dir>/fake_cf_control.json` (đọc mỗi lần chạy):
  {"fail": {"<output_name>": {"code": "FFMPEG_FAILED", "class": "TRANSIENT", "times": 2}},   # lỗi `times` lần đầu (đếm trong fake_cf_counts.json)
   "crash": ["<output_name>"],                                                                 # thoát đột ngột không có sự kiện kết thúc
   "gate": "<đường dẫn file>",                                                                 # chờ cho tới khi file này biến mất (mô phỏng render lâu)
   "gate_for": ["<output_name>"],                                                              # chỉ áp dụng gate cho các output này (rỗng = tất cả)
   "delay_s": 0.0}
Mỗi lần thực thi render ghi một dòng vào `<base-dir>/fake_cf_calls.log`: {"output", "key", "type", "attempt"} (replay thì KHÔNG ghi).
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

VERSION = "0.1.0-fake"


def ext_id(key):
    return "mw-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def out(ev):
    sys.__stdout__.write(json.dumps(ev, ensure_ascii=True) + "\n")
    sys.__stdout__.flush()


def jload(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def run(a):
    req = json.loads(Path(a.request).read_text(encoding="utf-8-sig"))
    base = Path(a.base_dir or ".")
    key, od = req["idempotency_key"], Path(req["output_dir"])
    od.mkdir(parents=True, exist_ok=True)
    state_f = od / ".worker_state.json"
    state = jload(state_f, {"jobs": {}})
    ent = state["jobs"].get(key)
    if ent and ent.get("state") == "completed" and all(Path(x["path"]).is_file() and Path(x["path"]).stat().st_size == x["size"] for x in ent["outputs"]["artifacts"]):
        for ev in ent["events"]:
            out(ev)
        return 0
    ctl = jload(base / "fake_cf_control.json", {})
    name = req["params"].get("output_name", "video.mp4")
    ext = ext_id(key)
    with open(base / "fake_cf_calls.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"output": name, "key": key, "type": req["type"], "attempt": req.get("attempt")}) + "\n")
    out({"seq": 1, "external_job_id": ext, "event": "started", "message": f"{req['type']} attempt {req.get('attempt')}"})
    if name in ctl.get("crash", []):
        os._exit(7)
    gate = ctl.get("gate")
    if gate and (not ctl.get("gate_for") or name in ctl["gate_for"]):
        t0 = time.time()
        while Path(gate).exists():
            if (od / f"cancel.{ext}").exists():
                out({"seq": 2, "external_job_id": ext, "event": "failed", "error": {"class": "CANCELLED", "code": "CANCELLED", "message": "cancelled"}})
                return 4
            if time.time() - t0 > 60:
                break
            time.sleep(0.02)
    for pct in (0.25, 0.5, 0.75):
        out({"seq": 2, "external_job_id": ext, "event": "progress", "pct": pct, "message": "rendering"})
        time.sleep(float(ctl.get("delay_s", 0)) / 3)
    f = (ctl.get("fail") or {}).get(name)
    if f:
        counts_f = base / "fake_cf_counts.json"
        counts = jload(counts_f, {})
        counts[name] = counts.get(name, 0) + 1
        counts_f.write_text(json.dumps(counts), encoding="utf-8")
        if counts[name] <= int(f.get("times", 1)):
            err = {"class": f.get("class", "TRANSIENT"), "code": f.get("code", "FFMPEG_FAILED"), "message": f"injected failure #{counts[name]} for {name}"}
            state["jobs"][key] = {"state": "failed", "error": err}
            state_f.write_text(json.dumps(state), encoding="utf-8")
            out({"seq": 3, "external_job_id": ext, "event": "failed", "error": err})
            return 3
    audio = next((i["path"] for i in req["inputs"] if i["type"] == "audio"), "")
    asha = hashlib.sha256(Path(audio).read_bytes()).hexdigest()[:12] if audio and Path(audio).is_file() else "none"
    pool = next((i["path"] for i in req["inputs"] if i["type"] == "video_dir"), "")
    body = f"FAKE-{req['type'].upper()}|{name}|audio={asha}|pool={Path(pool).name if pool else ''}\n".encode()
    dst = od / name
    tmp = od / f".{name}.part"
    tmp.write_bytes(body)
    os.replace(tmp, dst)
    art = {"type": "video" if req["type"] == "render" else "thumbnail", "path": str(dst), "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
    events = [{"seq": 4, "external_job_id": ext, "event": "artifact", "artifact": {k: art[k] for k in ("type", "path", "sha256")}},
              {"seq": 5, "external_job_id": ext, "event": "completed", "pct": 1.0, "outputs": {"artifacts": [art]}}]
    state["jobs"][key] = {"state": "completed", "outputs": {"artifacts": [art]}, "events": events}
    state_f.write_text(json.dumps(state), encoding="utf-8")
    for ev in events:
        out(ev)
    return 0


def status(a):
    ent = jload(Path(a.output_dir) / ".worker_state.json", {"jobs": {}})["jobs"].get(a.key)
    out({"protocol": 1, "external_job_id": ext_id(a.key), "state": (ent or {}).get("state", "unknown"), "last_seq": 0})
    return 0


def main():
    sys.stdout = sys.stderr
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--request", required=True)
    r.add_argument("--base-dir")
    r.set_defaults(fn=run)
    s = sub.add_parser("status")
    s.add_argument("--output-dir", required=True)
    s.add_argument("--key", required=True)
    s.set_defaults(fn=status)
    h = sub.add_parser("health")
    h.set_defaults(fn=lambda a: (out({"ok": True, "version": VERSION, "protocol": 1, "capabilities": ["render", "thumbnail", "prepare_assets"]}), 0)[1])
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
