"""CLI: python -m contentfactory [--root DIR] {submit,plan,run,status,retry,retry-part,resume,config,resources,pools}"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..jobs import pipeline as P
from .config import load_config
from .runner import Orchestrator


def _root(arg: str | None) -> Path:
    return Path(arg or os.environ.get("CONTENTFACTORY_ROOT") or Path(__file__).resolve().parents[3])


def _params(args: argparse.Namespace) -> dict:
    p = json.loads(Path(args.params_file).read_text(encoding="utf-8")) if args.params_file else {}
    if args.input:
        p["input"] = {"kind": "youtube_url" if args.input.startswith("http") else "text", "value": args.input}
    for kv in args.set or []:
        k, v = kv.split("=", 1)
        p[k] = json.loads(v) if v[:1] in '{["tfn0123456789-' else v
    return p


def _spec(args: argparse.Namespace) -> dict:
    """Phần điều khiển job: mode/start/target, artifact đưa vào (--artifact kind=path, lặp được), from-job, auto-resume."""
    inputs: dict = {}
    for kv in getattr(args, "artifact", None) or []:
        k, v = kv.split("=", 1)
        inputs.setdefault(k, []).append(v)
    for kv in getattr(args, "metadata_title", None) and [args.metadata_title] or []:
        inputs["metadata"] = {"title": kv}
    out = {"mode": args.mode, "start_stage": args.start, "target_stage": args.target, "inputs": inputs or None,
           "from_job": args.from_job}
    if getattr(args, "auto_resume", None) is not None:
        out["auto_resume"] = args.auto_resume == "on"
    return out


def _print_status(orc: Orchestrator, job_id: str) -> None:
    j = orc.store.get_job(job_id)
    runs = orc.store.stage_runs(job_id)
    line = f"job {job_id}: {j['state']}"
    if j["failed_stage"]:
        line += f" (FAILED_PERMANENT ở stage {j['failed_stage']}: {j['last_error']['code']})"
    if j["hold_reason"]:
        line += f" [{j['hold_reason']}: {j['hold_detail']}" + (" — cần người dùng Resume" if j["needs_user"] else "") + "]"
    print(line)
    print(f"  start={j['start_stage'] or '-'} target={j['target_stage'] or '-'} auto_resume={j['auto_resume']}"
          + (f" progress={j['progress']}" if j["progress"] else ""))
    for s in P.STAGES:
        r = [x for x in runs if x["stage"] == s.name]
        print(f"  {s.name:15} attempts={len(r)} " + (r[-1]["status"] if r else "-"))
        cp = (j.get("checkpoint") or {}).get(s.name) or {}
        sub = cp.get("parts") or cp.get("outputs")
        if sub:                                           # trạng thái từng output của stage render (part TikTok, video, thumbnail)
            print("      " + " ".join(f"{k}={v['state']}" + (f"({v['error']})" if v.get("error") else "") for k, v in sub.items()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="contentfactory")
    ap.add_argument("--root", help="thư mục gốc (mặc định: repo, hoặc $CONTENTFACTORY_ROOT)")
    ap.add_argument("-v", "--verbose", action="store_true", help="in log JSON ra stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit", help="tạo job mới, in job id")
    s.add_argument("--input", help="URL hoặc text nguồn")
    s.add_argument("--params-file")
    s.add_argument("--set", action="append", help="key=value (JSON nếu hợp lệ), lặp được")
    for sp in (s, pl := sub.add_parser("plan", help="xem kế hoạch (stage chạy/bỏ qua), không tạo job")):
        if sp is pl:
            sp.add_argument("--input"); sp.add_argument("--params-file"); sp.add_argument("--set", action="append")
        sp.add_argument("--mode", choices=sorted(P.MODES), help="; ".join(f"{k}={v[0] or '*'}→{v[1] or '*'}" for k, v in P.MODES.items()))
        sp.add_argument("--start", help="start_stage: " + ",".join(P.INDEX))
        sp.add_argument("--target", help="target_stage: " + ",".join(P.INDEX))
        sp.add_argument("--artifact", action="append", help="kind=đường_dẫn (artifact có sẵn), lặp được")
        sp.add_argument("--metadata-title", help="tạo metadata thủ công chỉ từ tiêu đề")
        sp.add_argument("--from-job", help="dùng lại artifact của job khác")
    s.add_argument("--auto-resume", choices=["on", "off"], help="override mặc định toàn cục cho job này")
    r = sub.add_parser("run", help="chạy job tới khi hết việc (hoặc --forever)")
    r.add_argument("--forever", action="store_true")
    st = sub.add_parser("status")
    st.add_argument("job_id", nargs="?")
    rt = sub.add_parser("retry", help="retry job FAILED tại đúng stage lỗi")
    rt.add_argument("job_id")
    rs = sub.add_parser("resume", help="Resume job đang bị giữ (PAUSED_*); --now: đo lại tài nguyên ngay")
    rs.add_argument("job_id")
    rs.add_argument("--now", action="store_true")
    cf = sub.add_parser("config", help="đổi cấu hình job: --auto-resume on|off | --target STAGE | --patch JSON (config ngữ nghĩa)")
    cf.add_argument("job_id")
    cf.add_argument("--auto-resume", choices=["on", "off"])
    cf.add_argument("--target")
    cf.add_argument("--patch", help="JSON gộp sâu vào config ngữ nghĩa của job")
    sub.add_parser("resources", help="trạng thái Resource Monitor")
    pl = sub.add_parser("pools", help="trạng thái source pool (Source Sync dùng chung); --sync để đồng bộ ngay")
    pl.add_argument("--sync", action="store_true")
    rp = sub.add_parser("retry-part", help="render lại đúng một part TikTok của job (ở render_tiktok)")
    rp.add_argument("job_id")
    rp.add_argument("part", type=int)
    a = ap.parse_args(argv)

    orc = Orchestrator(load_config(_root(a.root)), echo=a.verbose)
    if a.cmd == "submit":
        print(orc.submit(_params(a), **{k: v for k, v in _spec(a).items() if v is not None}))
    elif a.cmd == "plan":
        pl = orc.plan(_params(a), **{k: v for k, v in _spec(a).items() if v is not None and k != "auto_resume"})
        print(json.dumps({"start": pl.start_stage, "target": pl.target_stage, "run": pl.run, "skip": pl.skip,
                          "errors": pl.errors}, ensure_ascii=False))
        return 1 if pl.errors else 0
    elif a.cmd == "resume":
        print(f"job {a.job_id}: {orc.resume(a.job_id, now=a.now)}")
    elif a.cmd == "config":
        if a.auto_resume:
            orc.set_auto_resume(a.job_id, a.auto_resume == "on")
        if a.target:
            orc.set_target(a.job_id, a.target)
        if a.patch:
            print(f"revision {orc.set_job_config(a.job_id, json.loads(a.patch))}")
    elif a.cmd == "pools":
        r = orc.adapters.get("render")
        if not getattr(r, "requires_pool", False):
            print("adapter render hiện tại không dùng source pool (adapters.render != contentflow)")
        else:
            if a.sync:
                orc.pool_sync.sync()
            for name, spec in orc.pool_sync.specs().items():
                s = r.pool_status(spec)
                print(f"{name:20} {'READY' if s['ready'] else 'NOT READY'}{' (đang đồng bộ)' if s['syncing'] else ''} raw={s['raw_files']} "
                      f"todo={s['todo']} [{s['reason']}] {s['dir']}")
    elif a.cmd == "retry-part":
        print(f"job {a.job_id}: part {a.part} -> {orc.rerender_part(a.job_id, a.part)}")
    elif a.cmd == "resources":
        for st in orc.store.list_resource_status():
            print(f"{st['resource']:12} {'OK ' if st['ok'] else 'DOWN'} failures={st['failures']} {st['detail']}")
    elif a.cmd == "run":
        orc.run(until_idle=not a.forever)
    elif a.cmd == "retry":
        print(f"job {a.job_id} -> xếp lại stage {orc.retry(a.job_id)}")
    elif a.cmd == "status":
        for j in ([orc.store.get_job(a.job_id)] if a.job_id else orc.store.list_jobs()):
            _print_status(orc, j["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
