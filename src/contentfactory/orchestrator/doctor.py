"""Doctor (D-84): kiểm tra máy đã sẵn sàng chạy đúng cấu hình HIỆN TẠI chưa, và nói rõ phải làm gì khi chưa.

Mỗi kiểm tra trả {name, group, status, detail, hint}:
  ok    đạt
  warn  chạy được nhưng kém (vd TTS giả, không có NVENC) hoặc nên làm
  fail  sẽ làm pipeline đã cấu hình không chạy được
  skip  không dùng trong cấu hình hiện tại (vd adapter đang là fake)
`ready` = không có fail. Không thay đổi gì trên máy (ngoại trừ tạo/xóa file thử ghi và mở/tạo DB nếu chưa có).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ..jobs.db import JobStore
from . import auto as AU
from . import channels as CH
from .config import Config
from .registry import build_adapters


def _run(argv: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, repr(e)


def _c(name: str, group: str, status: str, detail: str, hint: str = "") -> dict:
    return {"name": name, "group": group, "status": status, "detail": detail, "hint": hint}


def _real(cfg: Config, kind: str) -> bool:
    return cfg.data["adapters"].get(kind) not in (None, "fake")


class Doctor:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.tools = cfg.data.get("tools", {})
        self.results: list[dict] = []
        self.adapters: dict = {}
        self.adapter_error: str | None = None

    def add(self, *a, **k) -> None:
        self.results.append(_c(*a, **k))

    # -------------------------------------------------------------------------------------------------- nhóm kiểm tra
    def system(self) -> None:
        g = "Hệ thống"
        v = sys.version_info
        self.add("python", g, "ok" if v >= (3, 10) else "fail", f"{sys.executable} ({v.major}.{v.minor}.{v.micro})",
                 "" if v >= (3, 10) else "cần Python >= 3.10")
        rc, out = _run(["git", "--version"])
        self.add("git", g, "ok" if rc == 0 else "warn", out.splitlines()[0] if rc == 0 else "không tìm thấy git",
                 "" if rc == 0 else "cần cho setup/update (winget install Git.Git)")
        node = shutil.which("node")
        need_node = _real(self.cfg, "story")
        if node:
            self.add("node", g, "ok", _run([node, "--version"])[1])
        else:
            self.add("node", g, "warn" if need_node else "skip", "không tìm thấy node", "cần nếu cài Claude Code CLI bằng npm (winget install OpenJS.NodeJS.LTS)" if need_node else "")
        for d in ("workspace", "output", "runtime"):
            p = self.cfg.path(d)
            free = self._free_gb(p)
            lim_fail, lim_warn = (2, 20) if d == "workspace" else (1, 5)
            st = "fail" if free < lim_fail else "warn" if free < lim_warn else "ok"
            self.add(f"disk.{d}", g, st, f"{free:.1f} GB trống tại {p}", "" if st == "ok" else "giải phóng dung lượng hoặc chạy `cleanup`; render/TTS cần nhiều chỗ")
        # quyền ghi
        bad = []
        for d in (self.cfg.path("workspace"), self.cfg.path("output"), self.cfg.path("runtime"), self.cfg.root / "config",
                  CH.channel_dir(self.cfg, "x").parent):
            try:
                d.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=d, prefix=".doctor-", delete=True):
                    pass
            except OSError as e:
                bad.append(f"{d}: {e.strerror or e}")
        self.add("write_permission", g, "fail" if bad else "ok", "; ".join(bad) if bad else "ghi được vào workspace/output/runtime/config/channels",
                 "đổi thư mục dự án sang nơi có quyền ghi" if bad else "")
        try:
            st = JobStore(self.cfg.path("db"))
            con = st._connect()
            ic = con.execute("PRAGMA integrity_check").fetchone()[0]
            con.close()
            n = len(st.list_jobs())
            self.add("database", g, "ok" if ic == "ok" else "fail", f"{self.cfg.path('db')}: schema v{st.schema_version()}, {n} job, integrity={ic}",
                     "" if ic == "ok" else "khôi phục từ file .bak-v* cạnh DB")
        except Exception as e:                                         # noqa: BLE001
            self.add("database", g, "fail", f"không mở được DB: {e!r}", "kiểm tra quyền ghi thư mục runtime/")
        loc = self.cfg.root / "config" / "config.local.json"
        self.add("config", g, "ok" if loc.exists() else "warn", "đã có config/config.local.json" if loc.exists() else "chưa có config/config.local.json (đang chạy cấu hình mặc định = adapter GIẢ)",
                 "" if loc.exists() else "chạy setup để cấu hình máy")
        tracked = self.cfg.root / "config" / "config.json"                      # config.json được commit: không được chứa bí mật
        try:
            raw = json.loads(tracked.read_text(encoding="utf-8-sig")) if tracked.is_file() else {}
        except (OSError, ValueError):
            raw = {}
        leaked = [k for k in ("token", "client_secret", "api_key", "password") if k in json.dumps(raw)]
        if leaked:
            self.add("config.secrets", g, "warn", f"config/config.json (được commit) có khóa nhạy cảm: {', '.join(leaked)}",
                     "chuyển sang config/config.local.json hoặc config/secrets.local.env (không commit)")

    def media(self) -> None:
        g = "Media"
        ff, fp = self.tools.get("ffmpeg") or "ffmpeg", self.tools.get("ffprobe") or "ffprobe"
        have = bool(shutil.which(ff) or Path(ff).exists()) and bool(shutil.which(fp) or Path(fp).exists())
        need = _real(self.cfg, "audio") or _real(self.cfg, "render")
        if not have:
            self.add("ffmpeg", g, "fail" if need else "warn", "không tìm thấy ffmpeg/ffprobe", "winget install Gyan.FFmpeg (rồi mở lại terminal) hoặc đặt tools.ffmpeg/tools.ffprobe")
            self.add("nvenc", g, "skip", "cần ffmpeg")
            return
        rc, out = _run([ff, "-hide_banner", "-version"])
        self.add("ffmpeg", g, "ok", out.splitlines()[0] if out else "ok")
        rc, filters = _run([ff, "-hide_banner", "-filters"])
        missing = [f for f in ("loudnorm", "alimiter", "silenceremove") if f not in filters]
        warn = [f for f in ("rubberband", "acompressor") if f not in filters]
        self.add("ffmpeg.filters", g, "fail" if missing and need else "warn" if (missing or warn) else "ok",
                 f"thiếu {missing}" if missing else (f"thiếu {warn} (TikTok sẽ dùng atempo, chất lượng thấp hơn)" if warn else "đủ loudnorm/alimiter/rubberband"),
                 "dùng bản ffmpeg 'full' (Gyan.FFmpeg)" if missing or warn else "")
        if _real(self.cfg, "render"):
            rc, enc = _run([ff, "-hide_banner", "-encoders"])
            nv = "h264_nvenc" in enc
            ok = nv and _run([ff, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"], 30)[0] == 0
            smi = shutil.which("nvidia-smi")
            self.add("nvenc", g, "ok" if ok else "warn", ("NVENC hoạt động" + (f" ({_run([smi, '--query-gpu=name', '--format=csv,noheader'])[1].splitlines()[0]})" if smi else ""))
                     if ok else "không dùng được NVENC: render sẽ dùng libx264 (CPU, chậm hơn nhiều với video dài)",
                     "" if ok else "cập nhật driver NVIDIA / dùng ffmpeg có h264_nvenc")
        else:
            self.add("nvenc", g, "skip", "render đang là fake")

    def components(self) -> None:
        g = "Thành phần"
        mods = self.cfg.root / "modules"
        # ContentFlow
        if _real(self.cfg, "render"):
            cf = self.adapters.get("render")
            h = cf.health() if cf else {"ok": False, "error": self.adapter_error}
            ok = bool(h.get("ok"))
            self.add("contentflow", g, "ok" if ok else "fail", f"worker {h.get('worker_version')} ({', '.join(h.get('capabilities') or [])})" if ok else h.get("error", "không chạy được media_worker"),
                     "" if ok else "chạy setup (tạo venv có Pillow) hoặc đặt tools.contentflow.python")
            base = Path(self.tools.get("contentflow", {}).get("base_dir") or "config/contentflow")
            base = base if base.is_absolute() else self.cfg.root / base
            tmpl = (((self.cfg.data.get("render") or {}).get("profiles") or {}).get("youtube") or {}).get("thumbnail", {}).get("config_overrides", {}).get("template", {}).get("file")
            has_t = bool(tmpl and Path(tmpl).is_file()) or (base / "assets" / "template.png").is_file()
            self.add("thumbnail_assets", g, "ok" if has_t else "warn", "có template thumbnail" if has_t else "chưa có template thumbnail 1648x928 + font: stage render YouTube sẽ giữ job (PAUSED_MISSING_INPUT)",
                     "" if has_t else "đặt assets/template.png vào config/contentflow/ hoặc render.profiles.youtube.thumbnail.config_overrides")
        else:
            self.add("contentflow", g, "skip", "adapters.render = fake")
        # story
        if _real(self.cfg, "story"):
            st = self.adapters.get("story")
            h = st.health() if st else {"ok": False, "error": self.adapter_error}
            self.add("story_system", g, "ok" if h.get("ok") else "fail", json.dumps({k: v for k, v in h.items() if k != "ok"}, ensure_ascii=False)[:300] if h.get("ok") else str(h)[:300],
                     "" if h.get("ok") else "cài Claude Code CLI (npm i -g @anthropic-ai/claude-code), đăng nhập, và có modules/oh-story-claudecode")
        else:
            self.add("story_system", g, "skip", "adapters.story = fake")
        if not (mods / "oh-story-claudecode").is_dir() and _real(self.cfg, "story"):
            self.add("module.oh-story", g, "fail", "thiếu modules/oh-story-claudecode", "chạy setup (clone theo modules.lock)")
        # source
        if _real(self.cfg, "source"):
            s = self.adapters.get("source")
            h = s.health() if s else {"ok": False, "error": self.adapter_error}
            self.add("source", g, "ok" if h.get("ok") else "fail", json.dumps(h, ensure_ascii=False, default=str)[:300],
                     "" if h.get("ok") else "cần Subtitle_supperVip (Python có youtube-transcript-api) hoặc yt-dlp: chạy setup")
            self.add("credential.youtube_api_key", g, "ok" if os.environ.get("YOUTUBE_API_KEY") else "warn",
                     "có YOUTUBE_API_KEY" if os.environ.get("YOUTUBE_API_KEY") else "không có YOUTUBE_API_KEY (tùy chọn: metadata đầy đủ; thiếu thì tiêu đề lấy qua yt-dlp)",
                     "" if os.environ.get("YOUTUBE_API_KEY") else "đặt biến môi trường YOUTUBE_API_KEY nếu muốn")
        else:
            self.add("source", g, "skip", "adapters.source = fake")
        # TTS
        tts = self.adapters.get("tts")
        if _real(self.cfg, "tts") and tts is not None:
            h = tts.health()
            self.add("tts", g, "ok" if h.get("ok") else "fail", f"{getattr(tts, 'engine_id', '?')}: {json.dumps(h, ensure_ascii=False, default=str)[:200]}",
                     "" if h.get("ok") else "kiểm tra dependency/credential của engine TTS (scripts/tts_onboard.py)")
        else:
            self.add("tts", g, "warn", "đang dùng TTS GIẢ: audio là âm tổng hợp, KHÔNG phải giọng đọc; chưa có engine TTS thật",
                     "onboard một engine: scripts/tts_onboard.py <repo/docs>, rồi đặt adapters.tts + adapter_config.tts")
        profs = AU.list_tts_profiles(self.cfg)
        self.add("tts_profiles", g, "ok" if profs else "skip", f"{len(profs)} profile: {', '.join(n for n, _ in profs)}" if profs else "chưa có profile trong tts_profiles/ (dùng mặc định của adapter)")
        # uploader
        if _real(self.cfg, "publish"):
            pub = self.adapters.get("publish")
            h = pub.health() if pub else {"ok": False, "error": self.adapter_error}
            exe = self.tools.get("yt_uploader", {}).get("exe")
            if h.get("ok"):
                self.add("uploader", g, "ok", f"daemon chạy, features={h.get('features')}")
                try:
                    st, d = pub._call("GET", "/accounts")
                    n = len(d) if isinstance(d, list) else 0
                    self.add("uploader.account", g, "ok" if n else "fail", f"{n} tài khoản Google đã đăng nhập" if n else "chưa đăng nhập tài khoản Google nào",
                             "" if n else "chạy: yt-uploader login")
                except Exception as e:                                 # noqa: BLE001
                    self.add("uploader.account", g, "warn", f"không đọc được danh sách tài khoản: {e!r}")
            else:
                can = bool(exe and Path(exe).exists())
                self.add("uploader", g, "warn" if can else "fail", "daemon chưa chạy" + ("; `go`/`start` sẽ tự bật" if can else f" và chưa cấu hình tools.yt_uploader.exe ({h.get('error', '')})"),
                         "" if can else "chạy setup (build yt-uploader) hoặc yt-uploader serve --headless")
            dd = self.tools.get("yt_uploader", {}).get("data_dir") or os.environ.get("YT_UPLOADER_DATA_DIR") or (Path(os.environ.get("APPDATA", "")) / "yt-uploader")
            oauth = bool(os.environ.get("YT_UPLOADER_CLIENT_ID")) or (Path(dd) / "oauth_client.json").is_file()
            self.add("credential.youtube_oauth", g, "ok" if oauth else "warn", "đã có OAuth client Google" if oauth else "chưa có OAuth client (client_id/secret) để đăng nhập YouTube",
                     "" if oauth else "tạo OAuth client trên Google Cloud Console rồi chạy setup (nhập client_id/secret)")
        else:
            self.add("uploader", g, "skip", "adapters.publish = fake (chưa upload thật)")

    def sources(self) -> None:
        g = "Nguồn video"
        render = self.adapters.get("render")
        pools = (self.cfg.data.get("render") or {}).get("pools") or {}
        if not getattr(render, "requires_pool", False):
            self.add("source_pools", g, "skip", "render không dùng pool (fake)")
            return
        if not pools:
            self.add("source_pools", g, "fail", "chưa cấu hình pool video nguồn (render.pools)", "thêm render.pools.<tên> = {raw_dir: ...} (setup sẽ hỏi)")
            return
        from .pools import PoolSyncService

        class _O:                                                       # PoolSyncService chỉ cần cfg/log/adapters để liệt kê spec
            cfg, adapters = self.cfg, {"render": render}
            log = type("L", (), {"emit": staticmethod(lambda *a, **k: None)})()
        specs = PoolSyncService(_O).specs()
        for name, spec in pools.items():
            raw = Path(spec["raw_dir"])
            n = len([p for p in raw.iterdir() if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")]) if raw.is_dir() else 0
            if not raw.is_dir() or not n:
                self.add(f"pool.{name}", g, "fail", f"{raw} " + ("không tồn tại" if not raw.is_dir() else "không có video"), "đặt video nguồn vào thư mục này")
                continue
            s = render.pool_status(specs[name]) if name in specs else None
            self.add(f"pool.{name}", g, "ok", f"{n} video" + (", đã đồng bộ" if s and s["ready"] else ", chưa đồng bộ (tự chạy nền khi run/go)" if s else ", không profile nào dùng"))

    def channels(self) -> None:
        g = "Kênh"
        d = CH.channel_dir(self.cfg, "x").parent
        ids = sorted(p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")) if d.is_dir() else []
        if not ids:
            self.add("channels", g, "warn", "chưa có kênh nào (sẽ dùng kênh 'default' không có preset)", "tạo kênh: python -m contentfactory channel-init <id>")
            return
        for cid in ids:
            try:
                ch = CH.load_channel(self.cfg, cid)
            except Exception as e:                                      # noqa: BLE001
                self.add(f"channel.{cid}", g, "fail", getattr(e, "message", str(e))[:300], "sửa channel.json")
                continue
            notes, st = [], "ok"
            pre = ch.get("preset") or {}
            if ch.get("watermark") and not Path(ch["watermark"]).is_file():
                notes.append(f"watermark không tồn tại: {ch['watermark']}")
                st = "warn"
            if pre.get("tts_profile"):
                try:
                    AU.load_tts_profile(self.cfg, pre["tts_profile"])
                except Exception as e:                                  # noqa: BLE001
                    notes.append(f"tts_profile: {getattr(e, 'message', e)}")
                    st = "fail"
            for pid, pn in (pre.get("pools") or {}).items():
                if pn not in ((self.cfg.data.get("render") or {}).get("pools") or {}):
                    notes.append(f"pool '{pn}' ({pid}) chưa cấu hình")
                    st = "fail" if getattr(self.adapters.get("render"), "requires_pool", False) else st
            self.add(f"channel.{cid}", g, st, f"'{ch['name']}'" + (": " + "; ".join(notes) if notes else ", preset đủ"))

    def templates(self) -> None:
        g = "Template"
        render = self.adapters.get("render")
        if not _real(self.cfg, "render") or not getattr(render, "supports_templates", False):
            self.add("templates", g, "skip", "adapter render hiện tại không dùng template ContentFlow")
            return
        from .template_ops import TemplateOps
        for n, item in enumerate(TemplateOps(self.cfg, api=render.templates).health()):
            first = item["message"].split(":")[0][:40]
            self.add("templates" if n == 0 else f"templates.{n}", g, item["level"], item["message"],
                     "cập nhật module ContentFlow (setup/update) hoặc chọn template khác cho kênh" if item["level"] == "fail" else
                     "chạy `cf templates migrate`" if "migrate" in item["message"] else "")

    def workers(self) -> None:
        g = "Worker Runtime"
        try:
            from .worker_admin import WorkerService
            svc = WorkerService(self.cfg)
        except Exception as e:                                       # noqa: BLE001 - DB hỏng không làm crash doctor
            self.add("workers", g, "warn", f"không mở được workers.db: {e!r}", "kiểm tra quyền ghi thư mục runtime/")
            return
        try:
            ws = svc.probe_all()
        except Exception as e:                                       # noqa: BLE001 - probe #1 không làm crash
            self.add("workers", g, "warn", f"không kiểm tra được worker: {e!r}")
            return
        if not ws:
            self.add("workers", g, "warn", "chưa cấu hình Worker Runtime nào",
                     "chạy `contentfactory workers scan` để phát hiện CLI trên PATH, rồi gán pool/routing")
            return
        for w in ws:
            st = w["status"] or "BROKEN"
            rows = f"{w['driver_id']} · {w['executable']}" + (f" · v{w['version']}" if w["version"] else "")
            if st == "READY":
                self.add(f"workers.{w['id']}", g, "ok", f"{w['name']}: {rows} · auth={w['auth']} · model={w['default_model'] or '-'}")
            elif st == "DISABLED":
                self.add(f"workers.{w['id']}", g, "skip", f"{w['name']}: {rows} (người dùng tắt)")
            elif st == "DETECTED":
                self.add(f"workers.{w['id']}", g, "warn", f"{w['name']}: {rows} · chưa probe", "chạy `workers probe` cho worker này")
            elif st == "NOT_FOUND":
                self.add(f"workers.{w['id']}", g, "warn", f"{w['name']}: {rows} · không tìm thấy executable",
                         "mất CLI (thường do chưa cài/probe cũ); chạy `workers probe` khi đã cài")
            else:
                detail = w.get("probe_error") or w.get("detail") or st
                self.add(f"workers.{w['id']}", g, "fail", f"{w['name']}: {rows} · {detail}",
                         "đăng nhập lại CLI (auth), hoặc xoá/cài lại rồi `workers scan`")

    # -------------------------------------------------------------------------------------------------- chạy tất cả
    @staticmethod
    def _free_gb(p: Path) -> float:
        q = p
        while not q.exists() and q.parent != q:
            q = q.parent
        return shutil.disk_usage(q).free / 2 ** 30

    def run(self) -> dict:
        try:
            self.adapters = build_adapters(self.cfg)
        except Exception as e:                                          # noqa: BLE001 - cấu hình sai: ghi nhận, vẫn chạy các kiểm tra còn lại
            self.adapter_error = f"cấu hình adapter lỗi: {e}"
            self.add("adapters", "Hệ thống", "fail", self.adapter_error, "sửa config/config.json hoặc config.local.json")
        for step in (self.system, self.media, self.components, self.sources, self.channels, self.templates, self.workers):
            try:
                step()
            except Exception as e:                                      # noqa: BLE001 - một kiểm tra hỏng không được làm mất cả báo cáo
                self.add(step.__name__, "Hệ thống", "fail", f"kiểm tra bị lỗi: {e!r}")
        counts = {s: sum(1 for r in self.results if r["status"] == s) for s in ("ok", "warn", "fail", "skip")}
        return {"ready": counts["fail"] == 0, "counts": counts, "checks": self.results}


def format_report(rep: dict) -> str:
    mark = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "skip": "[ -- ]"}
    out, group = [], None
    for r in rep["checks"]:
        if r["group"] != group:
            group = r["group"]
            out.append(f"\n== {group} ==")
        out.append(f"{mark[r['status']]} {r['name']}: {r['detail']}")
        if r["hint"] and r["status"] in ("warn", "fail"):
            out.append(f"        -> {r['hint']}")
    c = rep["counts"]
    out.append(f"\nTổng: {c['ok']} ok, {c['warn']} cảnh báo, {c['fail']} lỗi, {c['skip']} bỏ qua. "
               + ("SẴN SÀNG chạy cấu hình hiện tại." if rep["ready"] else "CHƯA sẵn sàng: sửa các mục [FAIL] ở trên."))
    return "\n".join(out)


def run_doctor(cfg: Config) -> dict:
    return Doctor(cfg).run()
