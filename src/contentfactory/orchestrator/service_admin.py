"""Phần quản trị của facade giao diện (D-89): TTS, source pool, cài đặt, doctor, dọn dẹp, tác vụ nền.

Cài đặt đi qua MỘT bảng khai báo (`SETTINGS`): thêm một tùy chọn vào UI = thêm một dòng ở đây (nhãn, nhóm, kiểu, khoảng giá trị, mô tả, có nguy hiểm/cần khởi động lại không).
Giá trị được ghi vào `config/config.local.json` (của máy này, không commit) bằng cách gộp từng khóa, đồng thời cập nhật cấu hình đang chạy khi khóa đó được đọc "sống".
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

from ..contracts import ErrorClass, StageError
from ..fsutil import atomic_write_json
from ..tts import prosody as PRO
from ..tts import schema as TS
from ..tts.autotune import make_ctx
from ..tts.manager import TTSManager
from . import auto as AU
from . import doctor as DR
from . import ops
from .config import DEFAULTS

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey", "key")


def _err(code: str, message: str, hint: str = "", **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, message, {**detail, **({"hint": hint} if hint else {})}, resource="input")


# ===================================================================================== cài đặt (bảng khai báo)
# (khóa chấm, nhóm, nhãn, kiểu, mô tả, tùy chọn)  — tùy chọn: min/max/step/options/danger/restart/unit
SETTINGS: list[tuple[str, str, str, str, str, dict]] = [
    ("auto_resume_default", "general", "Tự tiếp tục khi gặp sự cố tạm thời (Auto Resume)", "bool",
     "Mất mạng, hết quota, hết hạn mức AI… job được giữ rồi tự chạy tiếp khi hết nguyên nhân. Có thể đổi riêng cho từng job.", {}),
    ("job_defaults.language", "general", "Ngôn ngữ mặc định", "text", "Mã ngôn ngữ của truyện/giọng đọc (vd vi, en). Dùng để tự chọn giọng đọc.", {"max_len": 8}),
    ("job_defaults.channel", "general", "Kênh mặc định", "channel", "Kênh được chọn sẵn ở màn hình Chạy.", {}),
    ("job_defaults.tiktok.speed", "audio", "Tốc độ audio TikTok", "number", "Tăng tốc audio cho video TikTok, giữ nguyên cao độ giọng.", {"min": 1.0, "max": 3.0, "step": 0.1, "unit": "x"}),
    ("job_defaults.tiktok.target_part_sec", "audio", "Độ dài mỗi part TikTok", "number",
     "Độ dài mục tiêu của mỗi video TikTok (giây, tính theo audio sau khi tăng tốc). Hệ thống cắt ở ranh giới câu/đoạn gần nhất.", {"min": 15, "max": 1800, "step": 15, "unit": "giây"}),
    ("render.pool_sync_background", "render", "Đồng bộ video nền ở chế độ nền", "bool", "Chuẩn hoá video nguồn một lần, dùng chung cho mọi job.", {}),
    ("render.pool_sync_interval_s", "render", "Chu kỳ kiểm tra video nền", "number", "Bao lâu kiểm tra thư mục video nguồn có thay đổi không.", {"min": 30, "max": 86400, "step": 30, "unit": "giây"}),
    ("limits.gpu", "resources", "Số video render song song", "int", "Số job được render cùng lúc. Tăng chỉ khi máy mạnh (nhiều GPU/CPU).", {"min": 1, "max": 4}),
    ("limits.default", "resources", "Số job chạy song song mỗi bước", "int", "Giới hạn cho các bước còn lại (phụ đề, truyện, giọng đọc, audio, upload).", {"min": 1, "max": 8}),
    ("retry.max_attempts", "resources", "Số lần tự thử lại mỗi lượt", "int", "Lỗi tạm thời được thử lại bấy nhiêu lần (có chờ tăng dần) trước khi giữ job.", {"min": 1, "max": 10}),
    ("publishing.defaults.privacy", "publishing", "Chế độ đăng mặc định", "select", "Dùng khi kênh không đặt riêng. Nên để 'private' cho tới khi bạn tin tưởng pipeline.",
     {"options": [["private", "Riêng tư (private)"], ["unlisted", "Không công khai (unlisted)"], ["public", "Công khai (public)"]]}),
    ("publishing.title_policy", "publishing", "Khi chưa đặt tên truyện", "select", "'Cảnh báo' dùng tiêu đề video nguồn đã làm sạch; 'Bắt buộc' chặn job tới khi bạn đặt tên.",
     {"options": [["warn", "Cảnh báo, vẫn chạy"], ["require", "Bắt buộc phải đặt tên"]]}),
    ("cleanup.enabled", "storage", "Tự dọn dẹp", "bool", "Xoá file trung gian sau khi đăng, job cũ và cache quá cỡ. Không bao giờ đụng thư mục output.", {"danger": "Tắt thì ổ đĩa sẽ đầy dần."}),
    ("cleanup.artifact_keep_days", "storage", "Giữ dữ liệu job đã xong", "int", "Sau số ngày này, dữ liệu trong workspace của job đã đăng bị xoá (manifest và log được giữ).", {"min": 1, "max": 365, "unit": "ngày", "danger": "Xoá rồi không dựng lại được từ workspace."}),
    ("cleanup.failed_keep_days", "storage", "Giữ dữ liệu job lỗi", "int", "Job lỗi giữ lâu hơn để còn chạy lại.", {"min": 1, "max": 365, "unit": "ngày"}),
    ("cleanup.cache_gb.tts", "storage", "Trần cache giọng đọc", "number", "Vượt trần thì xoá cache ít dùng nhất trước.", {"min": 1, "max": 500, "step": 1, "unit": "GB"}),
    ("cleanup.cache_gb.source", "storage", "Trần cache phụ đề", "number", "", {"min": 1, "max": 100, "step": 1, "unit": "GB"}),
    ("auto.tts_profile_selection", "advanced", "Tự chọn giọng đọc", "bool", "Khi kênh không chỉ định, chọn profile TTS hợp ngôn ngữ nhất.", {}),
    ("auto.pool_selection", "advanced", "Tự chọn video nền", "bool", "Khi kênh không chỉ định, chọn pool theo hướng khung hình (ngang cho YouTube, dọc cho TikTok).", {}),
    ("auto.hold_wait_s", "advanced", "Thời gian `go` chờ khi job bị giữ", "int", "Chỉ ảnh hưởng lệnh `cf go` (giao diện luôn theo dõi nền).", {"min": 0, "max": 3600, "unit": "giây"}),
    ("monitor.disk_min_free_gb.default", "resources", "Dung lượng trống tối thiểu", "number", "Dưới mức này job bị giữ (hết chỗ đĩa).", {"min": 0.1, "max": 500, "step": 0.5, "unit": "GB", "restart": True}),
]
SETTING_GROUPS = [("general", "Chung"), ("audio", "Audio"), ("render", "Render"), ("publishing", "Đăng"), ("resources", "Tài nguyên"), ("storage", "Lưu trữ"), ("advanced", "Nâng cao")]


def _get(d: dict, dotted: str, default=None):
    cur = d
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _set(d: dict, dotted: str, value) -> None:
    cur = d
    keys = dotted.split(".")
    for k in keys[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value


def _mask(obj, key=""):
    if isinstance(obj, dict):
        return {k: _mask(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(x, key) for x in obj]
    if isinstance(obj, str) and obj and any(h in key.lower() for h in SECRET_HINTS) and key.lower() not in ("youtube_api_key_env",):
        return "***"
    return obj


# ===================================================================================== tác vụ nền
class Tasks:
    def __init__(self) -> None:
        self._t: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, fn, *a) -> str:
        tid = uuid.uuid4().hex[:10]
        rec = {"id": tid, "kind": kind, "state": "running", "started": time.time(), "finished": None, "result": None, "error": None}
        with self._lock:
            self._t[tid] = rec
            for old in sorted(self._t.values(), key=lambda r: r["started"])[:-40]:
                self._t.pop(old["id"], None)

        def run() -> None:
            try:
                rec["result"] = fn(*a)
                rec["state"] = "done"
            except StageError as e:
                rec["error"] = {"code": e.code, "message": e.message, "hint": (e.detail or {}).get("hint")}
                rec["state"] = "error"
            except Exception as e:                                       # noqa: BLE001
                rec["error"] = {"code": "UNEXPECTED", "message": f"{type(e).__name__}: {e}", "hint": "Xem log của ContentFactory."}
                rec["state"] = "error"
            rec["finished"] = time.time()
        threading.Thread(target=run, name=f"task-{kind}", daemon=True).start()
        return tid

    def get(self, tid: str) -> dict | None:
        return self._t.get(tid)

    def running(self, kind: str) -> str | None:
        return next((r["id"] for r in self._t.values() if r["kind"] == kind and r["state"] == "running"), None)


class AdminService:
    def __init__(self, orc) -> None:
        self.orc, self.cfg = orc, orc.cfg
        self.tasks = Tasks()
        self._doctor = {"running": False, "started": None, "finished": None, "report": None}
        self._doctor_lock = threading.Lock()
        self._uploader_cache: tuple[float, dict] | None = None

    # ================================================================================= cài đặt
    def _local_file(self) -> Path:
        return self.cfg.root / "config" / "config.local.json"

    def _read_local(self) -> dict:
        f = self._local_file()
        try:
            return json.loads(f.read_text(encoding="utf-8-sig")) if f.is_file() else {}
        except (OSError, ValueError) as e:
            raise _err("CONFIG_LOCAL_INVALID", f"config.local.json đang hỏng, không ghi đè: {e}", "Sửa hoặc xoá file rồi thử lại.") from None

    def _write_local(self, data: dict) -> None:
        f = self._local_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(f, data)

    def get_settings(self) -> dict:
        channels = [c["id"] for c in ops.list_channels(self.cfg)]
        items = []
        for key, group, label, typ, help_, opt in SETTINGS:
            cur = _get(self.cfg.data, key)
            o = dict(opt)
            if typ == "channel":
                o["options"] = [[c, c] for c in channels]
            items.append({"key": key, "group": group, "label": label, "type": typ, "help": help_, "value": cur, "default": _get(DEFAULTS, key),
                          "modified": cur != _get(DEFAULTS, key), **o})
        return {"groups": [{"id": g, "label": n} for g, n in SETTING_GROUPS], "items": items,
                "paths": {"root": str(self.cfg.root), "workspace": str(self.cfg.path("workspace")), "output": str(self.cfg.path("output")),
                          "runtime": str(self.cfg.path("runtime")), "config_local": str(self._local_file())},
                "storage": self.storage_usage()}

    def update_settings(self, changes: dict) -> dict:
        known = {k: (t, o) for k, _, _, t, _, o in SETTINGS}
        applied, restart = {}, []
        local = self._read_local()
        for key, raw in changes.items():
            if key not in known:
                raise _err("UNKNOWN_SETTING", f"Cài đặt không tồn tại: {key}")
            typ, opt = known[key]
            val = self._coerce(key, typ, opt, raw)
            _set(local, key, val)
            _set(self.cfg.data, key, val)
            applied[key] = val
            if opt.get("restart"):
                restart.append(key)
        self._write_local(local)
        return {"applied": applied, "restart_needed": restart, "message": "Đã lưu." + (" Một số thay đổi có hiệu lực sau khi khởi động lại ứng dụng." if restart else "")}

    @staticmethod
    def _coerce(key: str, typ: str, opt: dict, raw):
        def bad(msg: str) -> StageError:
            return _err("INVALID_SETTING", f"{key}: {msg}", "Nhập giá trị trong khoảng cho phép.")
        if typ == "bool":
            if not isinstance(raw, bool):
                raise bad("phải là bật/tắt")
            return raw
        if typ in ("int", "number"):
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise bad("phải là số")
            v = int(raw) if typ == "int" else float(raw)
            if typ == "int" and v != raw:
                raise bad("phải là số nguyên")
            if "min" in opt and v < opt["min"] or "max" in opt and v > opt["max"]:
                raise bad(f"nằm ngoài khoảng {opt.get('min')}–{opt.get('max')}")
            return v
        if typ in ("text", "channel"):
            if not isinstance(raw, str) or not raw.strip() or len(raw) > opt.get("max_len", 80):
                raise bad("phải là chuỗi không rỗng")
            return raw.strip()
        if typ == "select":
            if raw not in [o[0] for o in opt["options"]]:
                raise bad("lựa chọn không hợp lệ")
            return raw
        raise bad("kiểu không hỗ trợ")

    def effective_config(self) -> dict:
        return _mask(copy.deepcopy(self.cfg.data))

    # ================================================================================= lưu trữ / dọn dẹp
    def storage_usage(self) -> list[dict]:
        out = []
        for name in ("workspace", "output", "runtime"):
            p = self.cfg.path(name)
            out.append({"name": name, "path": str(p), "bytes": self._du(p, 4000), "free_bytes": shutil.disk_usage(p if p.exists() else p.anchor or ".").free})
        return out

    @staticmethod
    def _du(path: Path, limit: int) -> int | None:
        total, n = 0, 0
        for r, _, files in os.walk(path):
            for f in files:
                n += 1
                if n > limit * 50:
                    return None                                      # quá lớn: không quét (tránh treo UI); null = "nhiều"
                try:
                    total += os.path.getsize(os.path.join(r, f))
                except OSError:
                    pass
        return total

    def cleanup(self, dry_run: bool) -> dict:
        rep = self.orc.cleanup(dry_run=dry_run)
        return {"dry_run": dry_run, "files": rep["candidates"] if dry_run else rep["removed"], "bytes": rep["freed_bytes"], "by_kind": rep["by_kind"]}

    # ================================================================================= doctor
    GROUPS = [("system", "Hệ thống", ("python", "git", "config", "config.secrets", "adapters")), ("story", "Story", ("story_system", "node")),
              ("subtitle", "Phụ đề", ("source", "credential.youtube_api_key")), ("tts", "TTS", ("tts", "tts_profiles")),
              ("ffmpeg", "FFmpeg", ("ffmpeg", "ffmpeg.filters")), ("contentflow", "ContentFlow", ("contentflow", "thumbnail_assets")),
              ("gpu", "GPU / NVENC", ("nvenc",)), ("uploader", "YouTube uploader", ("uploader", "credential.youtube_oauth")),
              ("pools", "Nguồn video", ("source_pools", "pool.")), ("disk", "Ổ đĩa", ("disk.", "write_permission")), ("database", "Cơ sở dữ liệu", ("database",)),
              ("channels", "Kênh", ("channels", "channel."))]

    def doctor_status(self) -> dict:
        with self._doctor_lock:
            d = dict(self._doctor)
        rep = d["report"]
        d["groups"] = self._group_report(rep) if rep else []
        d["summary"] = ({"healthy": sum(1 for g in d["groups"] if g["status"] == "healthy"), "warning": sum(1 for g in d["groups"] if g["status"] == "warning"),
                         "needs_action": sum(1 for g in d["groups"] if g["status"] == "needs_action"), "ready": rep["ready"]} if rep else None)
        return d

    def doctor_run(self) -> dict:
        with self._doctor_lock:
            if self._doctor["running"]:
                return {"started": False, "message": "Đang kiểm tra."}
            self._doctor.update(running=True, started=time.time())

        def go() -> None:
            try:
                rep = DR.run_doctor(self.cfg)
            except Exception as e:                                       # noqa: BLE001
                rep = {"ready": False, "checks": [{"name": "doctor", "group": "Hệ thống", "status": "fail", "detail": f"{type(e).__name__}: {e}", "hint": "Xem log."}], "counts": {}}
            with self._doctor_lock:
                self._doctor.update(running=False, finished=time.time(), report=rep)
        threading.Thread(target=go, name="doctor", daemon=True).start()
        return {"started": True}

    def _group_report(self, rep: dict) -> list[dict]:
        out = []
        used = set()
        for gid, label, prefixes in self.GROUPS:
            checks = []
            for c in rep["checks"]:
                n = c["name"]
                if any(n == p or (p.endswith(".") and n.startswith(p)) for p in prefixes):
                    checks.append(c)
                    used.add(n)
            out.append(self._group(gid, label, checks))
        rest = [c for c in rep["checks"] if c["name"] not in used]
        if rest:
            out.append(self._group("other", "Khác", rest))
        return [g for g in out if g["checks"]]

    @staticmethod
    def _group(gid: str, label: str, checks: list[dict]) -> dict:
        sts = {c["status"] for c in checks}
        status = "needs_action" if "fail" in sts else "warning" if "warn" in sts else "healthy" if "ok" in sts else "skipped"
        return {"id": gid, "label": label, "status": status, "checks": [{"name": c["name"], "status": c["status"], "detail": c.get("detail", ""), "hint": c.get("hint", "")} for c in checks]}

    # ================================================================================= TTS
    def tts_overview(self) -> dict:
        adapter = self.orc.adapters.get("tts")
        name = self.cfg.data["adapters"].get("tts")
        try:
            health = adapter.health() if adapter is not None else {"ok": False, "error": "chưa có adapter TTS"}
        except Exception as e:                                           # noqa: BLE001
            health = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        engine_id = getattr(adapter, "engine_id", None)
        language = self.cfg.data["job_defaults"].get("language", "vi")
        sel = AU.select_tts_profile(self.cfg, language, engine_id)
        profiles = []
        for pname, prof in AU.list_tts_profiles(self.cfg):
            facts = list(TS.walk_facts(prof)) if prof.get("schema") == TS.SCHEMA_VERSION else []
            conf = {"high": 0, "medium": 0, "low": 0}
            for _, f in facts:
                conf[f["confidence"]] = conf.get(f["confidence"], 0) + 1
            needs = prof.get("needs_user", [])
            cred = []
            for n in needs:
                if str(n.get("key", "")).startswith("env:"):
                    var = n["key"][4:]
                    cred.append({"name": var, "ready": bool(os.environ.get(var))})            # chỉ tên biến và có/không, KHÔNG giá trị
            caps = prof.get("capabilities") or {}
            profiles.append({"name": pname, "engine": prof.get("engine"), "status": prof.get("status"), "languages": caps.get("languages") or [],
                             "voice": TS.unwrap(prof.get("voice")), "model": TS.unwrap(prof.get("model")), "confidence": conf,
                             "autotune": ((prof.get("meta") or {}).get("autotune") or {}), "needs_user": needs, "credentials": cred,
                             "selected_by_auto": bool(sel and sel[0] == pname), "voice_cloning": bool(caps.get("voice_cloning"))})
        return {"engine": {"adapter": name, "engine_id": engine_id, "ok": bool(health.get("ok")), "health": _mask(health),
                           "kind": "giả (thử nghiệm)" if name == "fake" else ("lệnh cục bộ" if name == "command_tts" else "tuỳ chỉnh"),
                           "is_fake": name == "fake"},
                "profiles": profiles, "auto": {"language": language, "selected": sel[0] if sel else None, "why": sel[1] if sel else None},
                "profiles_dir": str(AU.tts_profiles_dir(self.cfg))}

    # ---- Prosody (nhịp đọc): thông tin cho giao diện + nghe thử nhanh, không cần chạy job
    def prosody_info(self) -> dict:
        planner = self.orc.adapters.get("planner")
        return {**PRO.describe(), "default_profile": (self.cfg.data.get("prosody") or {}).get("default_profile"),
                "semantic_available": getattr(planner, "semantic_labeler", None) is not None,
                "preview_text": PRO.PREVIEW_TEXT}

    def prosody_preview(self, payload: dict) -> dict:
        """Tổng hợp đoạn mẫu ~25s với 1–2 biến thể nhịp đọc (A/B) bằng engine + profile hiện hành; chạy nền (engine có thể chậm). Kết quả cache theo
        (văn bản mẫu, prosody, engine/profile) và dùng chung cache chunk TTS với job thật."""
        variants = payload.get("variants") or [payload]
        if not isinstance(variants, list) or not 1 <= len(variants) <= 2:
            raise _err("INVALID_PREVIEW", "Nghe thử nhận 1 biến thể, hoặc 2 biến thể để so sánh A/B.")
        norm = []
        for v in variants:
            v = {k: v[k] for k in ("profile", "custom", "scale") if isinstance(v, dict) and k in v}
            PRO.resolve_prosody(v)                                           # sai thì báo ngay, không mở tác vụ nền
            norm.append(v)
        name = payload.get("tts_profile") or None
        profile = AU.load_tts_profile(self.cfg, name) if name else None
        if profile is None and self.cfg.data.get("auto", {}).get("tts_profile_selection", True):
            sel = AU.select_tts_profile(self.cfg, self.cfg.data["job_defaults"].get("language", "vi"), getattr(self.orc.adapters.get("tts"), "engine_id", None))
            profile = AU.load_tts_profile(self.cfg, sel[0]) if sel else None
        if self.tasks.running("prosody_preview"):
            raise _err("PREVIEW_BUSY", "Đang có một bản nghe thử chạy.", "Đợi bản đó xong rồi thử lại.")
        return {"task": self.tasks.start("prosody_preview", self._prosody_preview, norm, profile)}

    def _prosody_preview(self, variants: list[dict], profile: dict | None) -> dict:
        import tempfile
        tts, audio = self.orc.adapters.get("tts"), self.orc.adapters.get("audio")
        if tts is None or audio is None:
            raise _err("NO_TTS", "Chưa có adapter TTS/audio để nghe thử.", "Mở Giọng đọc để kiểm tra engine.")
        base = self.cfg.path("runtime") / "previews" / "prosody"
        base.mkdir(parents=True, exist_ok=True)
        ident = TS.stable_hash({"engine": getattr(tts, "engine_id", None), "profile": profile})
        out = []
        for v in variants:
            pr = PRO.resolve_prosody(v)
            pid = TS.stable_hash({"text": PRO.PREVIEW_TEXT, "pro": pr, "tts": ident})[:16]
            dest, side = base / f"{pid}.wav", base / f"{pid}.json"
            info = None
            if dest.is_file() and side.is_file():
                try:
                    info = json.loads(side.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    info = None
            if info is None:
                wd = Path(tempfile.mkdtemp(prefix="cf-prosody-"))
                try:
                    ctx = make_ctx(wd)
                    ctx.params = {"prosody": v, "language": self.cfg.data["job_defaults"].get("language", "vi")}
                    ctx.config = {"tts_cache_dir": str(self.cfg.path("runtime") / "cache" / "tts")}
                    res = TTSManager(tts, audio, self.orc.adapters.get("planner")).run(ctx, PRO.PREVIEW_TEXT, profile)
                    plan = json.loads((wd / "speech_plan.json").read_text(encoding="utf-8"))
                    shutil.copyfile(wd / "audio" / "master.wav", dest)
                    info = {"duration_sec": res.data["duration_sec"], "groups": len(plan["groups"]), "pauses_ms": [g["pause_after_ms"] for g in plan["groups"][:-1]],
                            "boundaries": plan["qc"]["boundaries"], "warnings": plan["qc"]["warnings"] + plan["qc"].get("audio", {}).get("warnings", [])}
                    atomic_write_json(side, info)
                finally:
                    shutil.rmtree(wd, ignore_errors=True)
            out.append({"id": pid, "url": f"/api/tts/prosody/preview/{pid}", "profile": pr["profile"], "label": PRO.resolve_prosody(v)["profile"], **info})
        return {"variants": out, "text": PRO.PREVIEW_TEXT}

    def prosody_preview_file(self, pid: str):
        """Âm thanh nghe thử theo mã (chỉ mã hex do chính backend sinh; không nhận đường dẫn từ client)."""
        import re as _re
        from .service_templates import Raw
        if not _re.fullmatch(r"[0-9a-f]{16}", pid or ""):
            raise _err("PREVIEW_NOT_FOUND", "Không có bản nghe thử này.")
        f = self.cfg.path("runtime") / "previews" / "prosody" / f"{pid}.wav"
        if not f.is_file():
            raise _err("PREVIEW_NOT_FOUND", "Bản nghe thử không còn (đã bị dọn).", "Bấm Nghe thử để tạo lại.")
        return Raw(f.read_bytes(), "audio/wav")

    def tts_profile_detail(self, name: str) -> dict:
        prof = AU.load_tts_profile(self.cfg, name)
        facts = [{"path": p, "value": f["value"], "source": f["source"], "confidence": f["confidence"], "evidence": f.get("evidence", [])[:3], "note": f.get("note")}
                 for p, f in TS.walk_facts(prof)] if prof.get("schema") == TS.SCHEMA_VERSION else []
        return {"name": name, "engine": prof.get("engine"), "status": prof.get("status"), "facts": facts, "needs_user": prof.get("needs_user", []),
                "capabilities": prof.get("capabilities") or {}, "meta": prof.get("meta") or {}}

    def tts_onboard(self, reference: str) -> dict:
        reference = (reference or "").strip().strip('"')
        if not reference:
            raise _err("MISSING_REFERENCE", "Cần đường dẫn thư mục/repo hoặc link docs của engine TTS.", "Dán đường dẫn repo (git URL hoặc thư mục) của engine.")
        if self.tasks.running("tts_onboard"):
            raise _err("BUSY", "Đang phân tích một engine khác.", "Chờ xong rồi thử lại.")
        tid = self.tasks.start("tts_onboard", self._onboard, reference)
        return {"task_id": tid}

    def _onboard(self, reference: str) -> dict:
        import tempfile
        from ..tts import analyzer
        src = analyzer.fetch_reference(reference, Path(tempfile.mkdtemp(prefix="cf-tts-ref-")))
        res = analyzer.analyze(src, None)
        out = self.cfg.path("runtime") / "onboarding" / str(res["engine"])
        paths = analyzer.write_onboarding(res, out)
        caps = res["capabilities"]
        return {"engine": res["engine"], "adapter_ready": bool(res["adapter"]["ready"]), "adapter_kind": res["adapter"]["kind"], "files": len(res["files"]),
                "capabilities": {k: caps.get(k) for k in ("max_chars", "languages", "sample_rate", "speed", "voice_cloning")},
                "needs_user": [{"key": n["key"], "reason": n["reason"]} for n in res["needs_user"]], "out_dir": str(out),
                "written": [str(p) for p in paths.values()],
                "next": "Kiểm chứng bằng chạy thật: python scripts/tts_tune.py --root . --profile " + str(paths.get("profile") or out / "profile.candidate.json")}

    # ================================================================================= source pools
    def pools(self) -> dict:
        render_cfg = self.cfg.data.get("render") or {}
        configured = render_cfg.get("pools") or {}
        adapter = self.orc.adapters.get("render")
        uses_pool = bool(adapter is not None and getattr(adapter, "requires_pool", False))
        specs = self.orc.pool_sync.specs() if uses_pool else {}
        rows = []
        for name, spec in configured.items():
            raw = Path(str(spec.get("raw_dir", "")))
            files = None
            problems = []
            if not raw.is_dir():
                problems.append("Thư mục video nguồn không tồn tại.")
            else:
                files = sum(1 for p in raw.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT) if raw.exists() else 0
                if files == 0:
                    problems.append("Thư mục không có video nào (mp4/mov/mkv…).")
            st = None
            if name in specs and adapter is not None and raw.is_dir():
                try:
                    st = adapter.pool_status(specs[name])
                except Exception as e:                                   # noqa: BLE001
                    problems.append(f"Không đọc được trạng thái đồng bộ: {e}")
            rs = self.orc.store.get_resource_status(f"pool:{name}")
            if rs and not rs["ok"]:
                problems.append(f"Lần đồng bộ gần nhất lỗi: {rs['detail']}")
            profile_use = []
            try:
                from ..render import profile as PF
                for pid in PF.DEFAULTS:
                    pr = PF.resolve(pid, render_cfg, None)
                    if pr.get("source_pool") == name:
                        profile_use.append(pid)
            except Exception:                                            # noqa: BLE001
                pass
            state = "problem" if problems else ("syncing" if st and st["syncing"] else "ready" if st and st["ready"] else "pending" if st else "idle")
            rows.append({"name": name, "raw_dir": str(raw), "orientation": AU.orientation_of(name, spec), "declared_orientation": spec.get("orientation"),
                         "files": files, "state": state, "todo": st["todo"] if st else None, "reason": st["reason"] if st else None, "used_by": profile_use,
                         "last_sync": rs["checked_at"] if rs else None, "problems": problems, "can_sync": bool(st is not None or (name in specs and raw.is_dir()))})
        return {"uses_pool": uses_pool, "render_adapter": self.cfg.data["adapters"].get("render"), "pools": rows,
                "syncing_task": self.tasks.running("pool_sync")}

    def pool_sync(self, name: str | None) -> dict:
        adapter = self.orc.adapters.get("render")
        if adapter is None or not getattr(adapter, "requires_pool", False):
            raise _err("NO_POOLS", "Adapter render hiện tại không dùng video nền.", "Chạy setup để bật ContentFlow.")
        running = self.tasks.running("pool_sync")
        if running:
            return {"task_id": running, "already": True}
        return {"task_id": self.tasks.start("pool_sync", self._sync, name), "already": False}

    def _sync(self, name: str | None) -> dict:
        res = self.orc.pool_sync.sync(name)
        return {k: ({"error": v["error"], "message": v.get("message")} if "error" in v else {"files": v.get("files"), "reused": v.get("reused")}) for k, v in res.items()}

    def pool_upsert(self, name: str, raw_dir: str, orientation: str | None) -> dict:
        import re
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", name or ""):
            raise _err("INVALID_POOL_NAME", "Tên pool chỉ gồm chữ không dấu, số, _ và -.", "Ví dụ: gameplay")
        raw = Path(raw_dir or "").expanduser()
        if not raw.is_dir():
            raise _err("INVALID_POOL_DIR", "Thư mục video nguồn không tồn tại.", "Chọn thư mục chứa video (mp4/mov/mkv).", path=str(raw))
        if orientation not in (None, "", "landscape", "portrait"):
            raise _err("INVALID_ORIENTATION", "Hướng khung hình phải là landscape hoặc portrait.")
        spec = {"raw_dir": str(raw.resolve())}
        if orientation:
            spec["orientation"] = orientation
        local = self._read_local()
        prev = _get(local, f"render.pools.{name}", {}) or {}
        _set(local, f"render.pools.{name}", {**prev, **spec})
        self._write_local(local)
        pools = self.cfg.data.setdefault("render", {}).setdefault("pools", {})
        pools[name] = {**pools.get(name, {}), **spec}
        return {"saved": True, "name": name}

    def pool_delete(self, name: str) -> dict:
        local = self._read_local()
        pools = _get(local, "render.pools", {}) or {}
        if name not in (self.cfg.data.get("render", {}).get("pools") or {}):
            raise _err("POOL_NOT_FOUND", f"Không có pool '{name}'.")
        if name in pools:
            pools.pop(name)
            self._write_local(local)
        elif name in (self.cfg.data.get("render", {}).get("pools") or {}):
            raise _err("POOL_IN_BASE_CONFIG", "Pool này khai báo trong config/config.json (được commit), không xoá từ giao diện.", "Sửa file cấu hình nếu muốn bỏ.")
        self.cfg.data["render"]["pools"].pop(name, None)
        return {"deleted": True}

    # ================================================================================= dữ liệu mẫu
    def make_samples(self) -> dict:
        from . import samples
        if self.tasks.running("samples"):
            raise _err("BUSY", "Đang tạo dữ liệu mẫu.", "Chờ vài giây.")
        return samples.make_samples(self.cfg)

    # ================================================================================= hạ tầng
    def runtime_status(self, runner_running: bool) -> dict:
        now = time.time()
        if self._uploader_cache is None or now - self._uploader_cache[0] > 15:
            pub = self.orc.adapters.get("publish")
            try:
                h = pub.health() if pub is not None else {"ok": False}
            except Exception:                                            # noqa: BLE001
                h = {"ok": False}
            self._uploader_cache = (now, {"adapter": self.cfg.data["adapters"].get("publish"), "ok": bool(h.get("ok"))})
        return {"runner": runner_running, "uploader": self._uploader_cache[1], "adapters": dict(self.cfg.data["adapters"]),
                "fake_adapters": sorted(k for k, v in self.cfg.data["adapters"].items() if v == "fake"), "python": sys.version.split()[0]}

    @staticmethod
    def pick_path(kind: str, title: str = "") -> dict:
        """Hộp thoại chọn file/thư mục NATIVE của hệ điều hành (trình duyệt không cho biết đường dẫn thật). Không có tkinter thì báo để người dùng dán đường dẫn."""
        try:
            import tkinter
            from tkinter import filedialog
        except ImportError:
            return {"path": None, "unsupported": True, "message": "Máy không có hộp thoại chọn file; hãy dán đường dẫn đầy đủ."}
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            p = filedialog.askdirectory(title=title or "Chọn thư mục") if kind == "folder" else filedialog.askopenfilename(
                title=title or "Chọn file", filetypes=[("Phụ đề / truyện / audio", "*.srt *.vtt *.txt *.wav *.mp3 *.m4a *.flac"), ("Tất cả", "*.*")])
        finally:
            root.destroy()
        return {"path": p or None, "unsupported": False}
