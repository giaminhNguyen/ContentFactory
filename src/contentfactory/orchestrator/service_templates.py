"""Facade cho giao diện Template (Template Studio) và ô chọn template của kênh. Mọi nghiệp vụ template nằm ở ContentFlow (D-92): đây chỉ chuyển lời gọi,
dịch lỗi sang tiếng Việt dễ hiểu, và phục vụ file ảnh/video xem trước theo TÊN (không nhận đường dẫn tùy ý).

Một khóa khóa-ghi (`_busy`) chặn bấm đúp Save/Publish: thao tác ghi trên cùng một template chạy lần lượt, lần thứ hai thấy trạng thái mới và trả kết quả idempotent.
"""
from __future__ import annotations

import re
import tempfile
import threading
from pathlib import Path

from ..contracts import ErrorClass, StageError
from . import channels as CH
from .template_ops import TemplateOps

SAFE_NAME = re.compile(r"^[A-Za-z0-9][\w\-.]{0,120}$")
PREVIEW_KINDS = ("previews", "test_render")
CTYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".mp4": "video/mp4"}


class Raw:
    """Phản hồi nhị phân (ảnh/video xem trước)."""

    def __init__(self, body: bytes, ctype: str) -> None:
        self.body, self.ctype = body, ctype


def _err(code: str, msg: str, hint: str = "") -> StageError:
    return StageError(ErrorClass.POLICY, code, msg, {"hint": hint}, resource="input")


class TemplateService:
    def __init__(self, orc) -> None:
        self.orc, self.cfg = orc, orc.cfg
        self.ops = TemplateOps(orc.cfg, api=getattr(orc.adapters.get("render"), "templates", None)) if getattr(orc.adapters.get("render"), "supports_templates", False) else None
        self._lock = threading.RLock()
        self._locks: dict[str, threading.Lock] = {}

    @property
    def api(self):
        if self.ops is None:
            raise _err("TEMPLATES_UNSUPPORTED", "Adapter render hiện tại không có hệ thống template.", "Dùng adapters.render = contentflow.")
        return self.ops.api

    def _busy(self, key: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(key, threading.Lock())

    # ------------------------------------------------------------------------------------------ đọc
    def overview(self, type: str | None = None, archived: bool = False) -> dict:
        self.api
        rows = self.ops.list(type, include_archived=archived)
        return {"templates": rows, "defaults": self.ops.options()["defaults"]}

    def options(self) -> dict:
        self.api
        return self.ops.options()

    def get(self, template_id: str, version=None) -> dict:
        out = self.api.get_template(id=template_id, version=version if version is not None else "latest")
        out["used_by"] = self.ops.usage(template_id)
        return out

    def validate(self, template_id: str, template: dict | None = None, version=None) -> dict:
        return self.api.validate(id=template_id, version=version, template=template) if template is None else self.api.validate(template=template)

    # ------------------------------------------------------------------------------------------ ghi (vòng đời)
    def create(self, type: str, template_id: str, name: str, description: str = "", width=None, height=None) -> dict:
        if not name or not str(name).strip():
            raise _err("INVALID_NAME", "Đặt tên cho template.")
        with self._busy("create:" + template_id):
            return self.api.create_draft(type=type, id=template_id, name=str(name).strip(), description=description or "", width=width, height=height)

    def duplicate(self, template_id: str, new_id: str, name: str | None, version=None) -> dict:
        with self._busy("create:" + new_id):
            return self.api.duplicate(id=template_id, new_id=new_id, name=name, version=version)

    def new_draft(self, template_id: str, from_version=None) -> dict:
        with self._busy(template_id):
            return self.api.new_draft(id=template_id, from_version=from_version)

    def save(self, template_id: str, version: int, template: dict) -> dict:
        with self._busy(template_id):
            return self.api.save_draft(id=template_id, version=int(version), template=template)

    def publish(self, template_id: str, version: int) -> dict:
        with self._busy(template_id):
            return self.api.publish(id=template_id, version=int(version))

    def archive(self, template_id: str, version=None) -> dict:
        used = self.ops.usage(template_id)
        res = self.api.archive(id=template_id, version=version)
        res["warning"] = (f"Còn {len(used)} kênh đang chọn template này; job mới của các kênh đó sẽ báo lỗi cho tới khi bạn chọn template khác." if used else None)
        return res

    def delete_draft(self, template_id: str, version: int) -> dict:
        with self._busy(template_id):
            return self.api.delete_draft(id=template_id, version=int(version))

    # ------------------------------------------------------------------------------------------ xem trước / render thử
    def _artifact(self, res: dict) -> dict:
        """Đổi đường dẫn tuyệt đối trong kết quả của ContentFlow thành url theo tên (UI không biết đường dẫn máy)."""
        p = Path(res["path"])
        kind = p.parent.name if p.parent.name in PREVIEW_KINDS else p.parent.parent.name
        rel = p.name if p.parent.name in PREVIEW_KINDS else f"{p.parent.name}/{p.name}"
        out = {k: v for k, v in res.items() if k != "path"}
        out["url"] = f"/api/templates/files/{kind}/{rel}"
        return out

    def preview(self, template_id: str, template: dict | None = None, version=None, sample: dict | None = None) -> dict:
        return self._artifact(self.api.preview(id=template_id if template is None else None, version=version, template=template, sample=sample))

    def test_render(self, template_id: str, template: dict | None = None, version=None, sample: dict | None = None) -> dict:
        with self._busy("render:" + template_id):
            return self._artifact(self.api.test_render(id=template_id if template is None else None, version=version, template=template, sample=sample))

    def file(self, kind: str, rel: str) -> Raw:
        if kind not in PREVIEW_KINDS or not all(SAFE_NAME.match(x) for x in rel.split("/")) or rel.count("/") > 1:
            raise _err("FILE_NOT_FOUND", "Không có tệp xem trước này.")
        cache = Path(self.api.info()["roots"]["cache"]).resolve()
        f = (cache / kind / rel).resolve()
        if cache not in f.parents or not f.is_file() or f.suffix.lower() not in CTYPES:
            raise _err("FILE_NOT_FOUND", "Tệp xem trước đã bị dọn; hãy bấm Xem trước lại.")
        return Raw(f.read_bytes(), CTYPES[f.suffix.lower()])

    # ------------------------------------------------------------------------------------------ asset
    def assets(self, type: str | None = None) -> dict:
        return self.api.list_assets(type=type)

    def asset_file(self, asset_id: str) -> Raw:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{1,63}", asset_id or ""):
            raise _err("ASSET_NOT_FOUND", "Không có asset này.")
        p = Path(self.api.asset_path(id=asset_id)["path"])
        if p.suffix.lower() not in CTYPES:
            raise _err("ASSET_NOT_FOUND", "Asset này không phải ảnh để xem.")
        return Raw(p.read_bytes(), CTYPES[p.suffix.lower()])

    def import_asset(self, asset_id: str, type: str, name: str, data: bytes) -> dict:
        safe = re.sub(r"[^\w.\-]", "_", Path(name or "asset").name)[:80] or "asset"
        with tempfile.TemporaryDirectory(prefix="cf_asset_") as td:
            f = Path(td) / safe
            f.write_bytes(data)
            return self.api.import_asset(id=asset_id, type=type, file=str(f), name=Path(name).stem[:60] or asset_id)

    def delete_asset(self, asset_id: str, force: bool = False) -> dict:
        return self.api.delete_asset(id=asset_id, force=bool(force))

    # ------------------------------------------------------------------------------------------ kênh
    def set_channel_template(self, channel_id: str, key: str, template_id: str | None, version_policy="latest_published", fallback: str | None = None) -> dict:
        CH.channel_dir(self.cfg, channel_id)
        return self.ops.set_channel_template(channel_id, key, template_id, version_policy, fallback)

    def channel_resolution(self, channel_id: str) -> dict:
        """Mỗi khóa của kênh: template đang chọn (hoặc mặc định), version published hiện tại, và có dùng được không (cho Advanced/debug)."""
        from . import templates as TPL
        ch = CH.load_channel(self.cfg, channel_id)
        defaults = TPL.global_defaults(self.cfg)
        out = {}
        for key, (_kind, ttype) in TPL.CHANNEL_KEYS.items():
            ref = (ch.get("templates") or {}).get(key)
            eff = ref or {"id": defaults[key], "version_policy": "latest_published"}
            row = {"configured": ref, "effective": eff, "is_default": ref is None}
            try:
                snap = self.api.resolve(id=eff["id"], policy=eff["version_policy"], expect_type=ttype)
                row.update(ok=True, version=snap["version"], name=snap["name"], checksum=snap["checksum"], canvas=snap["summary"]["canvas"])
            except StageError as e:
                row.update(ok=False, error={"code": e.code, "message": e.message})
            out[key] = row
        return out
