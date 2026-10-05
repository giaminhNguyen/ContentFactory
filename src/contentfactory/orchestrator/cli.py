"""CLI: python -m contentfactory [--root DIR] {submit,run,status,retry,jobs}"""
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


def _print_status(orc: Orchestrator, job_id: str) -> None:
    j = orc.store.get_job(job_id)
    runs = orc.store.stage_runs(job_id)
    print(f"job {job_id}: {j['state']}" + (f" (lỗi ở stage {j['failed_stage']}: {j['last_error']['code']})" if j["failed_stage"] else ""))
    for s in P.STAGES:
        r = [x for x in runs if x["stage"] == s.name]
        print(f"  {s.name:15} attempts={len(r)} " + (r[-1]["status"] if r else "-"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="contentfactory")
    ap.add_argument("--root", help="thư mục gốc (mặc định: repo, hoặc $CONTENTFACTORY_ROOT)")
    ap.add_argument("-v", "--verbose", action="store_true", help="in log JSON ra stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit", help="tạo job mới, in job id")
    s.add_argument("--input", help="URL hoặc text nguồn")
    s.add_argument("--params-file")
    s.add_argument("--set", action="append", help="key=value (JSON nếu hợp lệ), lặp được")
    r = sub.add_parser("run", help="chạy job tới khi hết việc (hoặc --forever)")
    r.add_argument("--forever", action="store_true")
    st = sub.add_parser("status")
    st.add_argument("job_id", nargs="?")
    rt = sub.add_parser("retry", help="retry job FAILED tại đúng stage lỗi")
    rt.add_argument("job_id")
    a = ap.parse_args(argv)

    orc = Orchestrator(load_config(_root(a.root)), echo=a.verbose)
    if a.cmd == "submit":
        print(orc.submit(_params(a)))
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
