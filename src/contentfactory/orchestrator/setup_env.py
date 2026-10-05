"""Setup / Update máy mới (D-85): `clone project -> setup -> nhập credential cần thiết -> RUN`.

`setup` idempotent, chạy lại được, không phá cấu hình đã chỉnh tay (`config/config.local.json` chỉ được BỔ SUNG khóa còn thiếu, trừ khi --force):
  python + git, clone module theo modules.lock, venv `.venv` (ContentFlow: Pillow; Subtitle_supperVip; yt-dlp), ffmpeg, build `yt-uploader` (Go) vào `tools/`,
  Claude Code CLI (nếu có npm), hỏi credential tùy chọn (YOUTUBE_API_KEY, OAuth client Google cho yt_uploader), hỏi thư mục video nguồn, tạo kênh đầu tiên, rồi chạy doctor.
`update`: git pull --ff-only, đưa module về đúng SHA trong modules.lock (bỏ qua module có thay đổi chưa commit), cài lại dependency khi requirements đổi, build lại uploader khi
module đổi, migration DB (tự chạy khi mở DB), doctor.
Mọi lệnh hệ thống đi qua `Sys` (tiêm được) nên test kiểm kế hoạch mà không cài gì thật. `--dry-run` chỉ in kế hoạch. Không tự cài thứ cần quyền admin ngoài winget/npm/pip chuẩn.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Config, _merge, load_config


class Sys:
    """Cổng ra hệ thống (thay được trong test)."""

    def run(self, argv: list[str], cwd: Path | None = None, timeout: float = 1800) -> tuple[int, str]:
        try:
            r = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
            return r.returncode, (r.stdout + r.stderr).strip()
        except (OSError, subprocess.SubprocessError) as e:
            return 127, repr(e)

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    @property
    def windows(self) -> bool:
        return os.name == "nt"


def parse_lock(root: Path) -> list[dict]:
    """modules.lock: `name path branch sha remote` mỗi dòng; # là chú thích."""
    out = []
    f = root / "modules.lock"
    for ln in f.read_text(encoding="utf-8").splitlines() if f.exists() else []:
        parts = ln.split()
        if len(parts) >= 5 and not ln.lstrip().startswith("#"):
            out.append({"name": parts[0], "path": parts[1], "branch": parts[2], "sha": parts[3], "remote": parts[4]})
    return out


def venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def uploader_exe(root: Path) -> Path:
    return root / "tools" / ("yt-uploader.exe" if os.name == "nt" else "yt-uploader")


def _hash_files(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.read_bytes() if p.is_file() else b"-")
    return h.hexdigest()[:16]


def _state(root: Path) -> dict:
    try:
        return json.loads((root / "runtime" / "setup_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(root: Path, st: dict) -> None:
    f = root / "runtime" / "setup_state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(st, indent=2), encoding="utf-8")


class Setup:
    def __init__(self, root: Path, system: Sys | None = None, yes: bool = False, dry_run: bool = False, force: bool = False, ask=None, out=print) -> None:
        self.root, self.sys, self.yes, self.dry, self.force, self.out = Path(root).resolve(), system or Sys(), yes, dry_run, force, out
        self.ask = ask or self._ask
        self.steps: list[dict] = []
        self.state = _state(self.root)

    # ---------------------------------------------------------------------------------------------- tiện ích
    def _ask(self, q: str, default: str | None = None, secret: bool = False) -> str | None:
        if self.yes or not sys.stdin.isatty():
            return default
        s = input(f"{q}{f' [{default}]' if default else ''}: ").strip()
        return s or default

    def step(self, name: str, status: str, detail: str = "", hint: str = "") -> None:
        self.steps.append({"step": name, "status": status, "detail": detail, "hint": hint})
        mark = {"done": "[ OK ]", "skip": "[ -- ]", "plan": "[PLAN]", "fail": "[FAIL]", "warn": "[WARN]"}[status]
        self.out(f"{mark} {name}" + (f": {detail}" if detail else "") + (f"\n        -> {hint}" if hint and status in ("fail", "warn") else ""))

    def do(self, name: str, argv: list[str], cwd: Path | None = None, timeout: float = 1800) -> bool:
        if self.dry:
            self.step(name, "plan", " ".join(argv))
            return True
        rc, out = self.sys.run(argv, cwd, timeout)
        if rc == 0:
            self.step(name, "done")
            return True
        self.step(name, "fail", (out or "")[-300:], "xem lỗi ở trên rồi chạy lại setup")
        return False

    # ---------------------------------------------------------------------------------------------- các bước
    def check_python_git(self) -> None:
        v = sys.version_info
        self.step("python", "done" if v >= (3, 10) else "fail", f"{sys.executable} {v.major}.{v.minor}", "" if v >= (3, 10) else "cần Python >= 3.10")
        if self.sys.which("git"):
            self.step("git", "done")
        elif self.sys.windows and self.sys.which("winget"):
            self.do("cài git (winget)", ["winget", "install", "--id", "Git.Git", "-e", "--accept-source-agreements", "--accept-package-agreements"])
        else:
            self.step("git", "fail", "không tìm thấy git", "cài git rồi chạy lại")

    def modules(self) -> None:
        for m in parse_lock(self.root):
            d = self.root / m["path"]
            if d.is_dir() and any(d.iterdir()):
                self.step(f"module {m['name']}", "skip", "đã có")
                continue
            if not self.sys.which("git"):
                self.step(f"module {m['name']}", "fail", "không có git để clone", "cài git")
                continue
            if self.do(f"clone {m['name']}", ["git", "clone", m["remote"], str(d)]):
                if not self.dry:
                    self.sys.run(["git", "checkout", m["sha"]], cwd=d)
                    self.state.setdefault("module_sha", {})[m["name"]] = m["sha"]
            elif not self.dry:
                self.steps[-1]["hint"] = f"remote {m['remote']} có thể cần SSH key/quyền truy cập; hoặc chép sẵn thư mục vào {m['path']}"

    def venv(self) -> None:
        py = venv_python(self.root)
        reqs = [self.root / "modules/ContentFlow/requirements.txt", self.root / "modules/Subtitle_supperVip/backend/requirements.txt"]
        sig = _hash_files(reqs)
        if not py.exists():
            if not self.do("tạo venv .venv", [sys.executable, "-m", "venv", str(self.root / ".venv")]):
                return
        if self.state.get("venv_reqs") == sig and py.exists() and not self.force:
            self.step("dependency Python (.venv)", "skip", "không đổi")
            return
        cmd = [str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"]
        self.do("cập nhật pip", cmd)
        ok = True
        for r in reqs:
            if r.is_file() or self.dry:                              # dry-run: module chưa clone vẫn in kế hoạch cài
                ok &= self.do(f"pip install -r {r.relative_to(self.root).as_posix()}", [str(py), "-m", "pip", "install", "--quiet", "-r", str(r)])
        ok &= self.do("pip install yt-dlp", [str(py), "-m", "pip", "install", "--quiet", "--upgrade", "yt-dlp"])
        if ok and not self.dry:
            self.state["venv_reqs"] = sig

    def ffmpeg(self) -> None:
        if self.sys.which("ffmpeg") and self.sys.which("ffprobe"):
            self.step("ffmpeg", "done", self.sys.which("ffmpeg"))
        elif self.sys.windows and self.sys.which("winget"):
            self.do("cài ffmpeg (winget Gyan.FFmpeg)", ["winget", "install", "--id", "Gyan.FFmpeg", "-e", "--accept-source-agreements", "--accept-package-agreements"])
            self.step("ffmpeg", "warn", "vừa cài: MỞ LẠI terminal để PATH có ffmpeg rồi chạy lại setup", "") if not self.dry else None
        else:
            self.step("ffmpeg", "fail", "không tìm thấy ffmpeg/ffprobe", "cài ffmpeg bản 'full' (có rubberband, loudnorm) rồi chạy lại")

    def uploader(self) -> None:
        exe, mod = uploader_exe(self.root), self.root / "modules" / "yt_uploader"
        sha = next((m["sha"] for m in parse_lock(self.root) if m["name"] == "yt_uploader"), "")
        if exe.exists() and self.state.get("uploader_sha") == sha and not self.force:
            self.step("yt-uploader", "skip", f"{exe} (đúng phiên bản)")
            return
        go = self.sys.which("go") or (r"C:\Program Files\Go\bin\go.exe" if self.sys.windows and Path(r"C:\Program Files\Go\bin\go.exe").exists() else None)
        if not mod.is_dir():
            if self.dry:
                self.step("yt-uploader", "plan", "sẽ build bằng Go sau khi clone modules/yt_uploader")
            else:
                self.step("yt-uploader", "fail", "thiếu modules/yt_uploader", "clone module trước")
            return
        if not go and self.sys.windows and self.sys.which("winget"):
            if self.yes or self.ask("Cài Go (winget GoLang.Go) để build yt-uploader? (y/n)", "y") == "y":
                self.do("cài Go (winget)", ["winget", "install", "--id", "GoLang.Go", "-e", "--accept-source-agreements", "--accept-package-agreements"])
                go = r"C:\Program Files\Go\bin\go.exe" if Path(r"C:\Program Files\Go\bin\go.exe").exists() else None
        if not go:
            self.step("yt-uploader", "warn", "chưa có Go để build", "cài Go (https://go.dev/dl) rồi chạy lại setup; hoặc đặt tools.yt_uploader.exe nếu đã có binary")
            return
        if not self.dry:
            exe.parent.mkdir(parents=True, exist_ok=True)
        if self.do("build yt-uploader", [go, "build", "-o", str(exe), "./cmd/yt-uploader"], cwd=mod, timeout=1200) and not self.dry:
            self.state["uploader_sha"] = sha

    def claude_cli(self) -> None:
        if self.sys.which("claude"):
            self.step("Claude Code CLI", "done", "đã có; nếu chưa đăng nhập hãy chạy `claude` một lần")
        elif self.sys.which("npm"):
            self.do("cài Claude Code CLI (npm)", ["npm", "install", "-g", "@anthropic-ai/claude-code"])
            self.step("Claude Code CLI", "warn", "vừa cài: chạy `claude` một lần để đăng nhập", "") if not self.dry else None
        else:
            self.step("Claude Code CLI", "warn", "chưa có claude và npm", "cài Node.js LTS (winget install OpenJS.NodeJS.LTS) rồi `npm i -g @anthropic-ai/claude-code`, đăng nhập bằng `claude`")

    def credentials(self) -> None:
        secrets = self.root / "config" / "secrets.local.env"
        have = secrets.read_text(encoding="utf-8") if secrets.exists() else ""
        if "YOUTUBE_API_KEY=" not in have and not os.environ.get("YOUTUBE_API_KEY"):
            k = self.ask("YOUTUBE_API_KEY (tùy chọn, Enter để bỏ qua)", None)
            if k and not self.dry:
                secrets.parent.mkdir(parents=True, exist_ok=True)
                with open(secrets, "a", encoding="utf-8") as f:
                    f.write(f"YOUTUBE_API_KEY={k}\n")
                self.step("YOUTUBE_API_KEY", "done", f"lưu ở {secrets.relative_to(self.root)} (không commit)")
            elif not k:
                self.step("YOUTUBE_API_KEY", "skip", "bỏ qua (tùy chọn)")
        else:
            self.step("YOUTUBE_API_KEY", "skip", "đã có")
        data = self.root / "tools" / "data"
        oauth = data / "oauth_client.json"
        if oauth.exists() or os.environ.get("YT_UPLOADER_CLIENT_ID"):
            self.step("OAuth client Google (yt_uploader)", "skip", "đã có")
        else:
            cid = self.ask("Google OAuth client_id cho YouTube upload (Enter để bỏ qua; xem modules/yt_uploader/docs/SECURITY.md)", None)
            sec = self.ask("Google OAuth client_secret", None, secret=True) if cid else None
            if cid and sec and not self.dry:
                data.mkdir(parents=True, exist_ok=True)
                oauth.write_text(json.dumps({"client_id": cid, "client_secret": sec}), encoding="utf-8")
                self.step("OAuth client Google (yt_uploader)", "done", f"lưu ở {oauth.relative_to(self.root)}; bước tiếp: `{uploader_exe(self.root).relative_to(self.root)} login --portable`")
            else:
                self.step("OAuth client Google (yt_uploader)", "skip", "bỏ qua: chưa upload thật được cho tới khi có", "")

    def pools_and_channel(self, local: dict) -> None:
        pools = (local.get("render") or {}).get("pools") or {}
        if not pools:
            land = self.ask("Thư mục video nguồn NGANG (16:9) cho YouTube (Enter để bỏ qua)", None)
            port = self.ask("Thư mục video nguồn DỌC (9:16) cho TikTok (Enter để dùng chung thư mục ngang)", None)
            if land:
                pools["gameplay"] = {"raw_dir": land, "orientation": "landscape"}
                pools["gameplay_vertical"] = {"raw_dir": port or land, "orientation": "portrait" if port else "landscape"}
                local.setdefault("render", {})["pools"] = pools
                self.step("source pools", "done", ", ".join(f"{k}={v['raw_dir']}" for k, v in pools.items()))
            else:
                self.step("source pools", "skip", "chưa cấu hình (render thật cần thư mục video nguồn)")
        else:
            self.step("source pools", "skip", "đã có")

    def config_local(self) -> dict:
        root, py = self.root, venv_python(self.root)
        f = root / "config" / "config.local.json"
        cur = json.loads(f.read_text(encoding="utf-8-sig")) if f.exists() else {}
        det: dict = {"adapters": {"source": "provider_chain", "story": "story_branch"}, "tools": {}}
        if self.sys.which("ffmpeg"):
            det["adapters"]["audio"] = "ffmpeg"
        if py.exists() and (root / "modules" / "ContentFlow").is_dir():
            det["adapters"]["render"] = "contentflow"
            det["tools"]["contentflow"] = {"python": str(py)}
            det["supervip"] = {"python": str(py)}
            det["youtube"] = {"yt_dlp_cmd": [str(py), "-m", "yt_dlp"]}
        if uploader_exe(root).exists():
            det["adapters"]["publish"] = "yt_uploader"
            det["tools"]["yt_uploader"] = {"exe": str(uploader_exe(root)), "portable": True, "data_dir": str(root / "tools" / "data")}
        if (root / "modules" / "oh-story-claudecode").is_dir():
            det["story_branch"] = {"oh_story_root": str(root / "modules" / "oh-story-claudecode")}
        # chỉ BỔ SUNG khóa còn thiếu: không đè thứ người dùng đã chỉnh (trừ --force)
        merged = _merge(cur, det) if self.force else _merge(det, cur)
        return merged

    def write_config(self, cfg: dict) -> None:
        f = self.root / "config" / "config.local.json"
        if self.dry:
            self.step("ghi config/config.local.json", "plan", json.dumps({k: cfg[k] for k in ("adapters",) if k in cfg}, ensure_ascii=False))
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.step("ghi config/config.local.json", "done", ", ".join(f"{k}={v}" for k, v in cfg.get("adapters", {}).items()))

    def first_channel(self) -> None:
        from . import ops
        cfg = load_config(self.root)
        if ops.list_channels(cfg):
            self.step("kênh", "skip", "đã có")
            return
        cid = self.ask("Tạo kênh đầu tiên - id (vd kenh_a; Enter để bỏ qua)", None)
        if not cid:
            self.step("kênh", "skip", "chưa tạo: chạy `channel-init <id>` sau")
            return
        name = self.ask("Tên hiển thị của kênh", cid)
        kids = (self.ask("Video của kênh này có phải 'made for kids' (khai báo COPPA)? (y/N)", "n") or "n").lower().startswith("y")
        if self.dry:
            self.step("tạo kênh", "plan", f"{cid} ({name}), made_for_kids={kids}")
        else:
            self.step("tạo kênh", "done", str(ops.channel_init(cfg, cid, name, kids)))

    def run(self) -> dict:
        self.out(f"Setup ContentFactory tại {self.root}" + (" (DRY-RUN: chỉ in kế hoạch)" if self.dry else ""))
        self.check_python_git()
        self.modules()
        self.venv()
        self.ffmpeg()
        self.uploader()
        self.claude_cli()
        self.credentials()
        cfg = self.config_local()
        self.pools_and_channel(cfg)
        self.write_config(cfg)
        if not self.dry:
            _save_state(self.root, self.state)
            self.first_channel()
            from .doctor import format_report, run_doctor
            self.out("\nDoctor:")
            rep = run_doctor(load_config(self.root))
            self.out(format_report(rep))
            self.report = rep
        fails = [s for s in self.steps if s["status"] == "fail"]
        self.out("\n" + ("Setup xong." if not fails else f"Setup còn {len(fails)} bước lỗi (xem [FAIL] ở trên)") +
                 " Chạy hằng ngày:  .\\cf.cmd go \"<URL YouTube>\" --channel <kênh> --open")
        return {"steps": self.steps, "failed": len(fails)}


class Update(Setup):
    """Cập nhật: code, module theo modules.lock, dependency, uploader, migration, doctor."""

    def run(self) -> dict:
        self.out(f"Update ContentFactory tại {self.root}" + (" (DRY-RUN)" if self.dry else ""))
        if (self.root / ".git").exists() and self.sys.which("git"):
            rc, st = (0, "") if self.dry else self.sys.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=self.root)
            if st.strip():
                self.step("git pull ContentFactory", "warn", "có thay đổi chưa commit: bỏ qua pull", "commit/stash rồi chạy lại update")
            else:
                self.do("git pull ContentFactory", ["git", "pull", "--ff-only"], cwd=self.root)
        else:
            self.step("git pull ContentFactory", "skip", "không phải git repo")
        for m in parse_lock(self.root):
            d = self.root / m["path"]
            if not d.is_dir():
                self.step(f"module {m['name']}", "warn", "chưa clone", "chạy setup")
                continue
            cur = "" if self.dry else self.sys.run(["git", "rev-parse", "HEAD"], cwd=d)[1].strip()
            dirty = False if self.dry else bool(self.sys.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=d)[1].strip())
            if cur == m["sha"]:
                self.step(f"module {m['name']}", "skip", "đúng SHA trong modules.lock")
            elif dirty:
                self.step(f"module {m['name']}", "warn", "có thay đổi chưa commit: không đổi SHA", f"xử lý thay đổi trong {m['path']}")
            else:
                self.sys.run(["git", "fetch", "--all", "--quiet"], cwd=d) if not self.dry else None
                self.do(f"module {m['name']} -> {m['sha'][:8]}", ["git", "checkout", "--quiet", m["sha"]], cwd=d)
        self.venv()
        self.uploader()
        cfg = self.config_local()
        self.write_config(cfg)
        if not self.dry:
            _save_state(self.root, self.state)
            from ..jobs.db import JobStore
            c = load_config(self.root)
            st = JobStore(c.path("db"))                                   # mở DB = chạy migration (có sao lưu .bak-vN)
            self.step("migration DB", "done", f"schema v{st.schema_version()}")
            from .doctor import format_report, run_doctor
            self.out("\nDoctor:")
            self.out(format_report(run_doctor(c)))
        fails = [s for s in self.steps if s["status"] == "fail"]
        self.out("\n" + ("Update xong." if not fails else f"Update còn {len(fails)} bước lỗi."))
        return {"steps": self.steps, "failed": len(fails)}
