"""Vận hành template cho CLI và giao diện (Phase 10): liệt kê, chọn template cho kênh, kiểm tra, di chuyển cấu hình layout kiểu cũ.

ContentFlow là chủ sở hữu template/asset (D-92); file này chỉ gọi `render.templates` (TemplateClient) và sửa phần CHỌN trong Channel Config.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import time
from pathlib import Path

from ..contracts import ErrorClass, StageError
from ..fsutil import atomic_write_json
from ..render import profile as PF
from . import channels as CH
from . import templates as TPL
from .config import Config
from .registry import build_adapter


def _err(code: str, msg: str, **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, msg, detail, resource="input")


class TemplateOps:
    def __init__(self, cfg: Config, api=None) -> None:
        self.cfg = cfg
        if api is None:
            render = build_adapter(cfg, "render")
            if not getattr(render, "supports_templates", False):
                raise _err("TEMPLATES_UNSUPPORTED", "adapter render hiện tại không có hệ thống template (adapters.render phải là contentflow)")
            api = render.templates
        self.api = api

    # ------------------------------------------------------------------------------------------------ danh sách / chi tiết
    def usage(self, template_id: str) -> list[dict]:
        """Các kênh đang chọn template này."""
        out = []
        base = CH.channel_dir(self.cfg, "x").parent
        for d in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
            try:
                ch = CH.load_channel(self.cfg, d.name)
            except StageError:
                continue
            for key, ref in (ch.get("templates") or {}).items():
                if ref["id"] == template_id or ref.get("fallback") == template_id:
                    out.append({"channel": d.name, "key": key})
        return out

    def list(self, type: str | None = None, include_archived: bool = False) -> list[dict]:
        rows = self.api.list_templates(type=type, include_archived=include_archived)["templates"]
        defaults = TPL.global_defaults(self.cfg)
        for r in rows:
            r["used_by"] = self.usage(r["id"])
            r["default_for"] = [k for k, v in defaults.items() if v == r["id"]]
        return rows

    def options(self) -> dict:
        """Cái UI cần để dựng ô chọn: template ĐÃ PUBLISH theo từng khóa Channel Config."""
        out = {}
        for ckey, (_kind, ttype) in TPL.CHANNEL_KEYS.items():
            rows = [r for r in self.api.list_templates(type=ttype)["templates"] if r["latest_published"]]
            out[ckey] = [{"id": r["id"], "name": r["name"], "version": r["latest_published"], "scope": r["scope"], "canvas": r["canvas"],
                          "description": r["description"]} for r in rows]
        return {"options": out, "defaults": TPL.global_defaults(self.cfg)}

    # ------------------------------------------------------------------------------------------------ chọn cho kênh
    def set_channel_template(self, channel_id: str, key: str, template_id: str | None, policy="latest_published", fallback: str | None = None) -> dict:
        """Ghi lựa chọn template vào channel.json (template_id=None: bỏ chọn => dùng mặc định). Kiểm tra template tồn tại, đúng loại, đã publish."""
        if key not in TPL.CHANNEL_KEYS:
            raise _err("INVALID_TEMPLATE_KEY", f"khóa phải là một trong {sorted(TPL.CHANNEL_KEYS)}")
        d = CH.channel_dir(self.cfg, channel_id)
        f = d / "channel.json"
        raw = json.loads(f.read_text(encoding="utf-8-sig")) if f.is_file() else {}
        sec = dict(raw.get("templates") or {})
        if template_id is None:
            sec.pop(key, None)
        else:
            ref, errs = TPL.normalize_ref({"id": template_id, "version_policy": policy, **({"fallback": fallback} if fallback else {})}, f"templates.{key}")
            if errs:
                raise _err("INVALID_CHANNEL_TEMPLATE", "; ".join(errs))
            snap = self.api.resolve(id=ref["id"], policy=ref["version_policy"], expect_type=TPL.CHANNEL_KEYS[key][1])   # raise rõ ràng nếu không dùng được
            sec[key] = ref
        raw["templates"] = sec
        if not sec:
            raw.pop("templates")
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_json(f, raw)
        return {"channel": channel_id, "key": key, "template": sec.get(key)}

    # ------------------------------------------------------------------------------------------------ doctor
    def health(self) -> list[dict]:
        """[{level, message}] cho `cf doctor`: ContentFlow có hệ thống template không, template các kênh chọn còn dùng được không, còn layout cũ không."""
        out = []
        try:
            info = self.api.info()
        except StageError as e:
            return [{"level": "fail", "message": f"hệ thống template của ContentFlow không chạy được: {e.message}"}]
        if info.get("problems"):
            out.append({"level": "warn", "message": f"asset có vấn đề: {'; '.join(info['problems'][:3])}"})
        out.append({"level": "ok", "message": f"{info['counts']['templates']} template, {info['counts']['assets']} asset"})
        base = CH.channel_dir(self.cfg, "x").parent
        for d in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
            try:
                ch = CH.load_channel(self.cfg, d.name)
            except StageError:
                continue
            for key, ref in (ch.get("templates") or {}).items():
                try:
                    self.api.resolve(id=ref["id"], policy=ref["version_policy"], expect_type=TPL.CHANNEL_KEYS[key][1])
                except StageError as e:
                    out.append({"level": "fail", "message": f"kênh '{d.name}': template {key} '{ref['id']}' không dùng được ({e.code}): {e.message}"})
        for pid in ("youtube", "tiktok"):
            if PF.has_legacy_layout(PF.resolve(pid, self.cfg.data.get("render"), None)):
                out.append({"level": "warn", "message": f"profile render '{pid}' còn bố cục kiểu cũ (frame_path/viewport/config_overrides): "
                            "deprecated, chạy `cf templates migrate` để chuyển sang template"})
        return out

    # ------------------------------------------------------------------------------------------------ migrate
    def migrate(self, apply: bool = False) -> list[dict]:
        """Chuyển bố cục kiểu cũ (render.profiles.* và preset.render của kênh) thành template user đã publish, rồi trỏ mặc định/kênh tới đó.
        Không apply: chỉ liệt kê. Apply: sao lưu config.local.json / channel.json (.bak), tạo template, bỏ khóa cũ. Chạy lại là no-op."""
        actions: list[dict] = []
        local = self.cfg.root / "config" / "config.local.json"
        rawc = json.loads(local.read_text(encoding="utf-8-sig")) if local.is_file() else {}
        profiles = ((rawc.get("render") or {}).get("profiles")) or {}
        for pid, prof in profiles.items():
            for kind, spec in self._legacy_kinds(pid, prof):
                actions.append({"scope": "global", "where": f"render.profiles.{pid}", "kind": kind, "spec": spec, "id": f"legacy_{kind}"})
        base = CH.channel_dir(self.cfg, "x").parent
        chan_files = []
        for d in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
            f = d / "channel.json"
            if not f.is_file():
                continue
            raw = json.loads(f.read_text(encoding="utf-8-sig"))
            chan_files.append((d.name, f, raw))
            for pid, prof in (((raw.get("preset") or {}).get("render")) or {}).items():
                for kind, spec in self._legacy_kinds(pid, prof):
                    tid = re.sub(r"[^a-z0-9_]", "_", f"legacy_{d.name}_{kind}".lower())[:48]
                    actions.append({"scope": f"channel:{d.name}", "where": f"preset.render.{pid}", "kind": kind, "spec": spec, "id": tid})
        if not apply or not actions:
            return actions
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if local.is_file():
            shutil.copy2(local, local.with_name(f"config.local.json.{stamp}.bak"))
        for a in actions:
            self._create_legacy_template(a)
            ckey = TPL.KIND_TO_CHANNEL_KEY[a["kind"]]
            pid = "youtube" if a["kind"] in ("youtube", "thumbnail") else "tiktok"
            if a["scope"] == "global":
                prof = rawc["render"]["profiles"][pid]
                self._strip(prof, a["kind"])
                rawc.setdefault("templates", {}).setdefault("defaults", {})[ckey] = a["id"]
            else:
                cname = a["scope"].split(":", 1)[1]
                f, raw = next((f, r) for n, f, r in chan_files if n == cname)
                self._strip(raw["preset"]["render"][pid], a["kind"])
                raw.setdefault("templates", {})[ckey] = {"id": a["id"], "version_policy": "latest_published"}
        for pid in list(profiles):                                                            # dọn profile rỗng sau khi bỏ khóa cũ
            if not profiles[pid] or profiles[pid] == {"thumbnail": {}}:
                profiles.pop(pid)
        if local.is_file() or rawc:
            atomic_write_json(local, rawc)
        for name, f, raw in chan_files:
            if any(x["scope"] == f"channel:{name}" for x in actions):
                shutil.copy2(f, f.with_name(f"channel.json.{stamp}.bak"))
                atomic_write_json(f, raw)
        return actions

    @staticmethod
    def _legacy_kinds(pid: str, prof: dict):
        custom_res = pid in PF.DEFAULTS and prof.get("resolution") not in (None, PF.DEFAULTS[pid]["resolution"])
        if pid in ("youtube", "tiktok") and (custom_res or any(prof.get(k) for k in PF.LEGACY_LAYOUT_KEYS)):
            yield pid, {k: prof.get(k) for k in PF.LEGACY_LAYOUT_KEYS if prof.get(k)} | {"resolution": prof.get("resolution"), "fps": prof.get("fps")}
        if pid == "youtube" and (prof.get("thumbnail") or {}).get("config_overrides"):
            yield "thumbnail", {"config_overrides": prof["thumbnail"]["config_overrides"]}

    @staticmethod
    def _strip(prof: dict, kind: str) -> None:
        if kind == "thumbnail":
            (prof.get("thumbnail") or {}).pop("config_overrides", None)
            if not prof.get("thumbnail"):
                prof.pop("thumbnail", None)
        else:
            for k in (*PF.LEGACY_LAYOUT_KEYS, "resolution"):
                prof.pop(k, None)

    def _create_legacy_template(self, a: dict) -> None:
        spec, kind, tid = a["spec"], a["kind"], a["id"]
        bd = Path((self.cfg.data.get("tools", {}).get("contentflow") or {}).get("base_dir") or "config/contentflow")
        bd = str(bd if bd.is_absolute() else self.cfg.root / bd)                                   # đường dẫn tương đối kiểu cũ tính từ base_dir của ContentFlow
        if self._exists(tid):
            return                                                                              # đã migrate lần trước
        if kind == "thumbnail":
            self.api.migrate_legacy(config=spec["config_overrides"], type="thumbnail", id=tid, name=f"Legacy thumbnail ({a['scope']})",
                                    base_dir=bd)
        else:
            res = spec.get("resolution") or PF.DEFAULTS[kind]["resolution"]
            w, h = (int(x) for x in str(res).split("x"))
            extra = (spec.get("config_overrides") or {}).get("video_generator") or {}
            self.api.migrate_legacy(config={}, type="video", id=tid, name=f"Legacy {kind} layout ({a['scope']})", frame_path=spec.get("frame_path"), base_dir=str(self.cfg.root),
                                    viewport=spec.get("viewport") or extra.get("viewport"), canvas=[w, h], fps=spec.get("fps"))

    def _exists(self, tid: str) -> bool:
        try:
            self.api.versions(id=tid)
            return True
        except StageError as e:
            if e.code == "TEMPLATE_NOT_FOUND":
                return False
            raise
