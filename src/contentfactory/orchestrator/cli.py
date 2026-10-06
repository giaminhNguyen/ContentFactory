"""CLI: python -m contentfactory [--root DIR] <lệnh>.  Cơ bản: go, status, open, resume, retry, doctor, channels, channel-init, setup, update, start, demo.
Nâng cao (--advanced -h): submit, plan, run, config, pools, retry-part, sequences, sequence-release, cleanup, resources."""
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
    if getattr(args, "title", None):
        p["project"] = {**(p.get("project") or {}), "title": args.title}
    if getattr(args, "channel", None):
        p["channel"] = args.channel
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
    if getattr(args, "stages", None):                                  # pipeline tùy chọn: dependency tự suy ra (không dùng chung với --mode/--start/--target)
        out["pipeline"] = {"version": 2, "requested_stages": [x.strip() for x in args.stages.split(",") if x.strip()]}
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


def _print_diagnosis(orc: Orchestrator, job_id: str) -> None:
    """Job lỗi/giữ: in nguyên nhân, provider, số lần thử, checkpoint và đường đi tiếp (không cần đọc log thô)."""
    from .diagnose import explain, format_lines
    d = explain(orc, job_id)
    if d["status"] in ("failed", "waiting", "attention"):
        print("\n".join(format_lines(d)))


def _print_links(orc: Orchestrator, job_id: str) -> None:
    """Gói output và liên kết YouTube (nếu đã đăng) — lấy từ artifact trong workspace, không đọc output/."""
    from ..jobs.workspace import job_dir
    jd = job_dir(orc.cfg.path("workspace"), job_id)
    for a in orc.store.artifacts(job_id):
        if a["kind"] in ("output_package", "publish_result"):
            try:
                d = json.loads((jd / a["path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if a["kind"] == "output_package":
                print(f"  gói output: {d.get('project_dir')} (phiên bản {d.get('version')}{', dùng lại' if d.get('reused') else ''})")
            else:
                print(f"  YouTube: {d.get('remote_url')}  (Full Audio {d.get('sequence')})")


def _load_local_env(root: Path) -> None:
    """config/secrets.local.env (KEY=VALUE, không commit) -> biến môi trường (không đè biến đã có)."""
    f = root / "config" / "secrets.local.env"
    if f.is_file():
        for ln in f.read_text(encoding="utf-8-sig").splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


BASIC_HELP = """\
Hằng ngày chỉ cần:   cf ui   (mở giao diện: dán link, chọn kênh, RUN, mở output)
                    hoặc:   cf go "<URL YouTube>" --channel <kênh> --open

Lệnh cơ bản:
  ui        mở giao diện web (cũng chạy nền: tự tiếp tục, đồng bộ video nền, dọn dẹp)
  samples   tạo truyện/phụ đề/audio/video mẫu để thử khi chưa có gì
  go        URL + kênh -> chạy hết -> gói output (và đăng YouTube nếu đã cấu hình)
  status    xem các job (kèm đường dẫn gói output và link YouTube)
  open      mở thư mục output của job gần nhất
  resume    tiếp tục job đang bị giữ (mất mạng, quota...) ; retry: chạy lại job lỗi
  doctor    kiểm tra máy đã sẵn sàng chưa và cần làm gì
  channels  danh sách kênh; channel-init <id>: tạo kênh mới (preset đầy đủ)
  templates template thumbnail/video (ContentFlow): list, use <kênh> <khóa> <id>, publish, duplicate, test-render, migrate...
  setup / update / start / demo   cài đặt máy mới / cập nhật / chạy dịch vụ nền / chạy thử nhanh

Lệnh nâng cao: cf --advanced -h   (submit, plan, run, config, pools, retry-part, sequences, cleanup, resources...)
"""

BASIC = {"ui", "samples", "go", "status", "open", "resume", "retry", "doctor", "channels", "channel-init", "templates", "setup", "update", "start", "demo"}


def build_parser(advanced: bool) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="contentfactory", description=BASIC_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="thư mục gốc (mặc định: repo, hoặc $CONTENTFACTORY_ROOT)")
    ap.add_argument("-v", "--verbose", action="store_true", help="in log JSON ra stderr")
    ap.add_argument("--advanced", action="store_true", help="hiện cả lệnh nâng cao trong -h")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<lệnh>")

    def add(name: str, help: str):
        return sub.add_parser(name, help=help) if (name in BASIC or advanced) else sub.add_parser(name)     # không truyền help: lệnh ẩn không lọt vào -h

    ui = add("ui", "mở giao diện web (máy chủ cục bộ 127.0.0.1 + chạy nền)")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-open", action="store_true", help="không tự mở trình duyệt")
    ui.add_argument("--no-runner", action="store_true", help="chỉ giao diện, không chạy vòng lặp xử lý job")
    sm = add("samples", "tạo dữ liệu mẫu (truyện, phụ đề, audio, video nền) trong samples/ và đăng ký pool")
    sm.add_argument("--dir", help="thư mục đích (mặc định: samples/ trong thư mục gốc)")
    sm.add_argument("--no-register", action="store_true", help="không ghi pool/template vào config.local.json")
    sm.add_argument("--force", action="store_true", help="tạo lại file đã có")
    g = add("go", "URL (hoặc file phụ đề) + kênh -> chạy hết -> gói output")
    g.add_argument("input", help="URL YouTube hoặc đường dẫn file phụ đề/transcript")
    g.add_argument("--channel", help="id kênh (mặc định: kênh trong job_defaults)")
    g.add_argument("--title", help="project.title (không đặt: tự làm sạch từ tiêu đề video nguồn, kèm cảnh báo)")
    g.add_argument("--kids", dest="kids", action="store_true", default=None, help="made_for_kids = true (nếu kênh chưa khai)")
    g.add_argument("--not-kids", dest="kids", action="store_false", help="made_for_kids = false (nếu kênh chưa khai)")
    g.add_argument("--open", action="store_true", help="mở thư mục output khi xong")
    g.add_argument("--no-wait", action="store_true", help="chỉ tạo job, không chạy (chạy bằng `start`)")
    st = add("status", "xem job")
    st.add_argument("job_id", nargs="?")
    op = add("open", "mở thư mục output của job (mặc định: gần nhất)")
    op.add_argument("job_id", nargs="?")
    rs = add("resume", "Resume job đang bị giữ (PAUSED_*); --now: đo lại tài nguyên ngay")
    rs.add_argument("job_id")
    rs.add_argument("--now", action="store_true")
    rt = add("retry", "retry job FAILED tại đúng stage lỗi")
    rt.add_argument("job_id")
    dc = add("doctor", "kiểm tra máy sẵn sàng chưa")
    dc.add_argument("--json", action="store_true")
    add("channels", "danh sách kênh và preset")
    ci = add("channel-init", "tạo kênh mới với preset mặc định (không ghi đè nếu đã có)")
    ci.add_argument("id")
    ci.add_argument("--name")
    ci.add_argument("--kids", choices=["yes", "no"], default="no", help="made_for_kids của kênh (khai báo COPPA, mặc định no)")
    ci.add_argument("--last-used", type=int, default=0, help="số Full Audio đã đăng trước đó")
    ci.add_argument("--force", action="store_true")
    tp = add("templates", "template thumbnail/video: list | show | use | validate | publish | archive | duplicate | preview | test-render | assets | migrate")
    tp.add_argument("action", nargs="?", default="list", choices=["list", "show", "use", "validate", "publish", "archive", "duplicate", "preview", "test-render", "assets", "migrate"])
    tp.add_argument("args", nargs="*", help="show/validate/archive/preview/test-render <id> | use <kênh> <khóa> <id> | publish <id> <version> | duplicate <id> <id mới>")
    tp.add_argument("--version", help="số version hoặc latest_published (use: ghim version)")
    tp.add_argument("--type", choices=["thumbnail", "video"])
    tp.add_argument("--fallback", help="use: id template dự phòng (chỉ khi khai báo rõ ràng)")
    tp.add_argument("--apply", action="store_true", help="migrate: thực sự ghi (mặc định chỉ liệt kê)")
    tp.add_argument("--json", action="store_true")
    for name, hp in (("setup", "cài đặt máy mới (idempotent)"), ("update", "cập nhật code/module/dependency")):
        sp = add(name, hp)
        sp.add_argument("--yes", action="store_true", help="không hỏi, dùng mặc định")
        sp.add_argument("--dry-run", action="store_true", help="chỉ in kế hoạch")
        sp.add_argument("--force", action="store_true", help="làm lại cả bước đã xong; ghi đè config.local.json")
    add("start", "chạy dịch vụ nền (tự bật uploader, đồng bộ pool, auto resume, cleanup)")
    dm = add("demo", "chạy thử toàn pipeline bằng adapter giả (nhanh, không tốn tiền)")
    dm.add_argument("--keep", action="store_true", help="giữ thư mục demo")
    # ----- nâng cao
    s = add("submit", "tạo job mới, in job id")
    s.add_argument("--input", help="URL hoặc text nguồn")
    s.add_argument("--title", help="project.title: tiêu đề chính cho thumbnail, YouTube, thư mục output (không đặt thì dùng tiêu đề video nguồn kèm cảnh báo)")
    s.add_argument("--channel", help="id kênh (channels/<id>/channel.json)")
    s.add_argument("--params-file")
    s.add_argument("--set", action="append", help="key=value (JSON nếu hợp lệ), lặp được")
    pl = add("plan", "xem kế hoạch (stage chạy/bỏ qua), không tạo job")
    pl.add_argument("--input")
    pl.add_argument("--params-file")
    pl.add_argument("--set", action="append")
    for sp in (s, pl):
        sp.add_argument("--mode", choices=sorted(P.MODES), help="; ".join(f"{k}={v[0] or '*'}→{v[1] or '*'}" for k, v in P.MODES.items()))
        sp.add_argument("--start", help="start_stage: " + ",".join(P.INDEX))
        sp.add_argument("--target", help="target_stage: " + ",".join(P.INDEX))
        sp.add_argument("--stages", help="pipeline tùy chọn: các stage muốn chạy, cách nhau dấu phẩy (vd render_tiktok hoặc publish); "
                                         "stage cần thiết tự được thêm. Không dùng chung với --mode/--start/--target")
        sp.add_argument("--artifact", action="append", help="kind=đường_dẫn (artifact có sẵn), lặp được")
        sp.add_argument("--metadata-title", help="tạo metadata thủ công chỉ từ tiêu đề")
        sp.add_argument("--from-job", help="dùng lại artifact của job khác")
    s.add_argument("--auto-resume", choices=["on", "off"], help="override mặc định toàn cục cho job này")
    r = add("run", "chạy job tới khi hết việc (hoặc --forever)")
    r.add_argument("--forever", action="store_true")
    cf = add("config", "đổi cấu hình job: --auto-resume on|off | --target STAGE | --patch JSON (config ngữ nghĩa)")
    cf.add_argument("job_id")
    cf.add_argument("--auto-resume", choices=["on", "off"])
    cf.add_argument("--target")
    cf.add_argument("--patch", help="JSON gộp sâu vào config ngữ nghĩa của job")
    add("resources", "trạng thái Resource Monitor")
    sq = add("sequences", "danh sách số Full Audio đã reserve theo kênh")
    sq.add_argument("--channel")
    sr = add("sequence-release", "nhả số Full Audio của một job CHƯA đăng (số đã cấp không bị cấp lại)")
    sr.add_argument("job_id")
    po = add("pools", "trạng thái source pool (Source Sync dùng chung); --sync để đồng bộ ngay")
    po.add_argument("--sync", action="store_true")
    rp = add("retry-part", "render lại đúng một part TikTok của job (ở render_tiktok)")
    rp.add_argument("job_id")
    rp.add_argument("part", type=int)
    rr = add("rerender", "dựng lại video/thumbnail của job bằng template HIỆN TẠI của kênh (job mới; không chạy lại Story/TTS/Audio)")
    rr.add_argument("job_id")
    rtp = add("retemplate", "chọn lại template cho MỘT kind của job (thumbnail|youtube|tiktok) và chốt snapshot mới")
    rtp.add_argument("job_id")
    rtp.add_argument("kind", choices=["thumbnail", "youtube", "tiktok"])
    rtp.add_argument("template_id")
    rtp.add_argument("--version", help="ghim version (mặc định latest_published)")
    cl = add("cleanup", "Auto Cleanup ngay (không đụng output/); --dry-run để chỉ xem")
    cl.add_argument("--dry-run", action="store_true")
    return ap


def _templates_cmd(a: argparse.Namespace, root: Path) -> int:
    from ..contracts import StageError
    from .template_ops import TemplateOps
    try:
        ops = TemplateOps(load_config(root))
        act, args = a.action, a.args
        out = None
        if act == "list":
            out = ops.list(a.type, include_archived=True)
            if not a.json:
                for r in out:
                    pub = f"v{r['latest_published']}" if r["latest_published"] else "-"
                    print(f"{r['id']:22} {r['type']:9} {r['scope']:8} publish={pub:4} {'draft=v' + str(r['draft']) if r['draft'] else '':9} "
                          f"{(r['name'] or '')[:28]:28} {','.join(u['channel'] + ':' + u['key'] for u in r['used_by'])}")
                return 0
        elif act == "show":
            out = ops.api.get_template(id=args[0], version=int(a.version) if a.version and a.version.isdigit() else (a.version or "latest"))
        elif act == "use":
            ch, key, tid = args[:3]
            out = ops.set_channel_template(ch, key, tid, int(a.version) if a.version and a.version.isdigit() else "latest_published", a.fallback)
        elif act == "validate":
            out = ops.api.validate(id=args[0], version=int(a.version) if a.version else None)
            if not a.json:
                for x in out["errors"] + out["warnings"]:
                    print(f"{x['level']:8} {x['message']}")
                print("OK" if out["ok"] else "KHÔNG HỢP LỆ")
                return 0 if out["ok"] else 1
        elif act == "publish":
            out = ops.api.publish(id=args[0], version=int(args[1]))
        elif act == "archive":
            out = ops.api.archive(id=args[0], version=int(a.version) if a.version else None)
        elif act == "duplicate":
            out = ops.api.duplicate(id=args[0], new_id=args[1])
        elif act in ("preview", "test-render"):
            fn = ops.api.preview if act == "preview" else ops.api.test_render
            out = fn(id=args[0], version=int(a.version) if a.version else None)
        elif act == "assets":
            out = ops.api.list_assets(type=a.type)
        elif act == "migrate":
            out = ops.migrate(apply=a.apply)
            if not a.json:
                for x in out:
                    print(f"{'đã chuyển' if a.apply else 'sẽ chuyển'}: {x['where']} ({x['scope']}) -> template {x['id']}")
                print("không có bố cục kiểu cũ cần chuyển" if not out else ("" if a.apply else "Chạy lại với --apply để thực hiện (có sao lưu .bak)."))
                return 0
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except (StageError, IndexError, ValueError) as e:
        print(f"LỖI: {getattr(e, 'message', None) or ('thiếu tham số: ' + str(e) if isinstance(e, IndexError) else e)}")
        return 2


def _fmt_bytes(n: float) -> str:
    return f"{n / 2 ** 30:.2f} GB" if n >= 2 ** 30 else f"{n / 2 ** 20:.1f} MB"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = build_parser("--advanced" in argv)
    a = ap.parse_args(argv)
    root = _root(a.root)
    _load_local_env(root)
    # ----- lệnh không cần dựng Orchestrator (không được hỏng vì cấu hình adapter sai/thiếu)
    if a.cmd == "ui":
        from . import webui
        return webui.serve(root, a.port, not a.no_open, not a.no_runner)
    if a.cmd == "samples":
        from . import samples as SM
        r = SM.make_samples(load_config(root), Path(a.dir) if a.dir else None, register=not a.no_register, force=a.force)
        print(f"Dữ liệu mẫu trong {r['dir']}: tạo {len(r['created'])}, bỏ qua {len(r['skipped'])} file đã có")
        for k in ("story", "subtitle", "audio"):
            print(f"  {k:9}: {r[k]}")
        print(f"  video nền : {r['video_dirs']['landscape']} (ngang), {r['video_dirs']['portrait']} (dọc)")
        for x in r["registered"]:
            print(f"  đã đăng ký: {x}")
        for x in r["warnings"]:
            print(f"  ! {x}")
        print(r["next"])
        return 0
    if a.cmd == "templates":
        return _templates_cmd(a, root)
    if a.cmd in ("doctor", "channels", "channel-init", "setup", "update", "demo"):
        from . import doctor as DR
        from . import ops
        from .setup_env import Setup, Update
        if a.cmd == "demo":
            return 0 if ops.demo(keep=a.keep)["ok"] else 1
        if a.cmd in ("setup", "update"):
            r = (Setup if a.cmd == "setup" else Update)(root, yes=a.yes, dry_run=a.dry_run, force=a.force).run()
            return 1 if r["failed"] else 0
        cfg = load_config(root)
        if a.cmd == "doctor":
            rep = DR.run_doctor(cfg)
            print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else DR.format_report(rep))
            return 0 if rep["ready"] else 1
        if a.cmd == "channels":
            rows = ops.list_channels(cfg)
            for c in rows:
                print(f"{c['id']:20} " + (f"{c['name']!r:28} preset={','.join(c['preset']) or '-'} watermark={'có' if c['watermark'] else 'không'} privacy={c['privacy']}"
                                          if c["ok"] else f"LỖI: {c['error']}"))
            if not rows:
                print("chưa có kênh nào; tạo: contentfactory channel-init <id> --name \"Tên kênh\"")
            return 0
        print(f"đã tạo {ops.channel_init(cfg, a.id, a.name, a.kids == 'yes', a.force, a.last_used)}")
        return 0
    orc = Orchestrator(load_config(root), echo=a.verbose)
    if a.cmd == "go":
        from . import ops
        try:
            res = ops.go(orc, a.input, a.channel, a.title, a.kids, wait=not a.no_wait)
        except Exception as e:                                          # noqa: BLE001 - lỗi cấu hình/đầu vào: in gọn, không traceback
            if getattr(e, "message", None):
                print(f"LỖI: {e.message}")
                return 2
            raise
        if a.open and res.get("output_dir"):
            ops.open_path(res["output_dir"])
        return 0 if res.get("ok") else 1
    if a.cmd == "open":
        from . import ops
        d = ops.latest_output(orc, a.job_id)
        if not d:
            print("chưa có gói output nào")
            return 1
        print(d)
        ops.open_path(d)
        return 0
    if a.cmd == "start":
        from . import ops
        ok, note = ops.ensure_uploader(orc.cfg, orc.adapters.get("publish"))
        print(f"uploader: {note}")
        print("ContentFactory đang chạy nền (Ctrl-C để dừng): auto resume, đồng bộ pool, cleanup. Tạo job bằng `go --no-wait` hoặc `submit` ở cửa sổ khác.")
        orc.run(until_idle=False)
        return 0
    if a.cmd == "cleanup":
        rep = orc.cleanup(dry_run=a.dry_run)
        print(f"{'sẽ xóa' if a.dry_run else 'đã xóa'} {rep['candidates'] if a.dry_run else rep['removed']} file, {_fmt_bytes(rep['freed_bytes'])}; theo loại: {rep['by_kind']}")
        return 0
    if a.cmd == "submit":
        print(orc.submit(_params(a), **{k: v for k, v in _spec(a).items() if v is not None}))
    elif a.cmd == "plan":
        pl = orc.plan(_params(a), **{k: v for k, v in _spec(a).items() if v is not None and k != "auto_resume"})
        print(json.dumps({"start": pl.start_stage, "target": pl.target_stage, "run": pl.run, "skip": pl.skip,
                          "locked": [s for s, i in pl.states.items() if i["state"] == "locked"],
                          "provided": [s for s, i in pl.states.items() if i["state"] == "provided"], "errors": pl.errors}, ensure_ascii=False))
        return 1 if pl.errors else 0
    elif a.cmd == "resume":
        print(f"job {a.job_id}: {orc.resume(a.job_id, now=a.now)}")
    elif a.cmd == "rerender":
        from . import ops
        new = ops.rerender(orc, a.job_id)
        print(f"job {new}: dựng lại từ audio của job {a.job_id} bằng template hiện tại của kênh; chạy `cf run`")
    elif a.cmd == "retemplate":
        snap = orc.retemplate(a.job_id, a.kind, a.template_id, int(a.version) if a.version else "latest_published")
        print(f"job {a.job_id}: {a.kind} -> {snap['id']}@v{snap['version']} (chỉ stage render tương ứng chạy lại)")
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
    elif a.cmd == "sequences":
        for r in orc.sequence.list(a.channel):
            print(f"{r['channel_id']:20} #{r['sequence']:<5} {r['status']:10} project={r['project_id']}")
    elif a.cmd == "sequence-release":
        print(f"job {a.job_id}: " + ("đã nhả số" if orc.sequence.release(a.job_id) else "không có số đang giữ"))
    elif a.cmd == "retry-part":
        print(f"job {a.job_id}: part {a.part} -> {orc.rerender_part(a.job_id, a.part)}")
    elif a.cmd == "resources":
        for st in orc.store.list_resource_status():
            print(f"{st['resource']:12} {'OK ' if st['ok'] else 'DOWN'} failures={st['failures']} {st['detail']}")
    elif a.cmd == "run":
        orc.run(until_idle=not a.forever)
    elif a.cmd == "retry":
        print(f"job {a.job_id} -> xếp lại stage {orc.retry(a.job_id)}")
        if orc.store.get_job(a.job_id)["state"] != P.FAILED:
            orc.run(until_idle=True)
    elif a.cmd == "status":
        for j in ([orc.store.get_job(a.job_id)] if a.job_id else orc.store.list_jobs()):
            _print_status(orc, j["id"])
            _print_diagnosis(orc, j["id"])
            _print_links(orc, j["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
