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
from .template_samples import TemplateSamples

SAFE_NAME = re.compile(r"^[A-Za-z0-9][\w\-.]{0,120}$")
PREVIEW_KINDS = ("previews", "test_render")
CTYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".mp4": "video/mp4"}


class Raw:
    """Phản hồi nhị phân (ảnh/video xem trước)."""

    def __init__(self, body: bytes, ctype: str) -> None:
        self.body, self.ctype = body, ctype


def _err(code: str, msg: str, hint: str = "", **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, msg, {"hint": hint, **detail}, resource="input")


# Lỗi vòng đời của ContentFlow (tiếng Anh) -> câu người dùng đọc được + việc nên làm (D-103). Mã giữ nguyên; bản gốc nằm ở detail["original"].
VI_ERRORS = {
    "TEMPLATE_READONLY": ("Template có sẵn chỉ đọc.", "Nhân bản template này để có bản sửa được."),
    "TEMPLATE_IMMUTABLE": ("Bản đã publish hoặc đã lưu trữ không sửa hay xoá được.", "Tạo bản nháp mới để sửa, hoặc Lưu trữ để ẩn khỏi kênh."),
    "TEMPLATE_IS_DRAFT": ("Đây là bản nháp nên không lưu trữ được.", "Dùng “Xoá bản nháp” nếu bạn không cần nó nữa."),
    "TEMPLATE_NOT_FOUND": ("Không có template này.", "Có thể nó đã bị xoá: tải lại danh sách."),
    "TEMPLATE_VERSION_NOT_FOUND": ("Không có version này của template.", "Tải lại để xem các version hiện có."),
    "DRAFT_EXISTS": ("Template đã có một bản nháp đang mở.", "Lưu/publish hoặc xoá bản nháp đó trước."),
    "TEMPLATE_ID_EXISTS": ("Mã template này đã tồn tại.", "Chọn mã khác."),
    "NO_PUBLISHED_VERSION": ("Template chưa publish version nào.", "Publish một version trước khi chọn cho kênh."),
    "ASSET_IN_USE": ("Asset đang được template dùng.", "Gỡ asset khỏi template rồi xoá."),
    "LOCK_TIMEOUT": ("Template đang được một thao tác khác xử lý.", "Thử lại sau giây lát."),
}
CHANNEL_KEY_LABEL = {"thumbnail": "thumbnail", "youtube_video": "YouTube", "tiktok_video": "TikTok"}


class _Vi:
    """Bọc client template: lỗi vòng đời đã biết được dịch sang tiếng Việt (mã giữ nguyên để giao diện/test dựa vào)."""

    def __init__(self, api) -> None:
        self._api = api

    def __getattr__(self, name: str):
        fn = getattr(self._api, name)
        if not callable(fn):
            return fn

        def call(*a, **kw):
            try:
                return fn(*a, **kw)
            except StageError as e:
                if e.error_class == ErrorClass.POLICY and e.code in VI_ERRORS:
                    msg, hint = VI_ERRORS[e.code]
                    raise StageError(ErrorClass.POLICY, e.code, msg, {**(e.detail or {}), "hint": hint, "original": e.message}, resource="input") from None
                raise
        return call


class TemplateService:
    def __init__(self, orc) -> None:
        self.orc, self.cfg = orc, orc.cfg
        self.ops = TemplateOps(orc.cfg, api=getattr(orc.adapters.get("render"), "templates", None)) if getattr(orc.adapters.get("render"), "supports_templates", False) else None
        self._lock = threading.RLock()
        self._locks: dict[str, threading.Lock] = {}
        self.samples = TemplateSamples(orc.cfg, lambda: Path(self.api.info()["roots"]["cache"]))

    @property
    def api(self):
        if self.ops is None:
            raise _err("TEMPLATES_UNSUPPORTED", "Adapter render hiện tại không có hệ thống template.", "Dùng adapters.render = contentflow.")
        return _Vi(self.ops.api)

    @staticmethod
    def _who(used: list[dict]) -> str:
        return ", ".join(f"{u['channel']} ({CHANNEL_KEY_LABEL.get(u['key'], u['key'])})" for u in used)

    def actions_for(self, r: dict) -> dict:
        """Hành động hợp lệ cho một hàng danh sách — BACKEND quyết định (giao diện chỉ vẽ nút và lý do bị tắt).
        builtin: chỉ Nhân bản · bản nháp của user: Sửa / Nhân bản / Xoá bản nháp · đã publish: Nhân bản / Bản nháp mới / Lưu trữ (KHÔNG xoá — job cũ cần tái lập) ·
        đã lưu trữ: Nhân bản / Khôi phục (= bản nháp mới từ version gần nhất, rồi publish lại)."""
        user, used = r["scope"] == "user", r.get("used_by") or []
        pub, draft = r.get("latest_published"), r.get("draft")
        archived = [v["version"] for v in r.get("versions", []) if v["status"] == "archived"]
        removes = bool(draft) and len(r.get("versions", [])) == 1                    # xoá bản nháp này thì cả template biến mất
        blocked = None
        if draft and removes and used:
            blocked = f"Không xoá được: kênh {self._who(used)} đang chọn template này. Đổi template của kênh trước, hoặc nhân bản thay vì xoá."
        warn = f"Còn {len(used)} kênh đang chọn template này; job mới của các kênh đó sẽ báo lỗi cho tới khi chọn template khác." if used else None
        return {"open": True, "duplicate": True,
                "delete_draft": {"enabled": blocked is None, "version": draft, "removes_template": removes, "blocked": blocked} if user and draft else None,
                "archive": {"enabled": True, "version": pub, "warning": warn} if user and pub else None,
                "new_draft": {"enabled": True, "from_version": pub} if user and pub and not draft else None,
                "restore": {"enabled": True, "from_version": archived[-1]} if user and not pub and not draft and archived else None}

    def _busy(self, key: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(key, threading.Lock())

    # ------------------------------------------------------------------------------------------ đọc
    def overview(self, type: str | None = None, archived: bool = False) -> dict:
        self.api
        rows = self.ops.list(type, include_archived=archived)
        for r in rows:
            r["actions"] = self.actions_for(r)
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
        if self.api.get_template(id=template_id, version="latest", validate=False)["scope"] != "user":
            raise _err("TEMPLATE_READONLY", "Template có sẵn chỉ đọc: không lưu trữ được.", "Nhân bản template này nếu bạn muốn một bản riêng.")
        used = self.ops.usage(template_id)
        res = self.api.archive(id=template_id, version=version)
        res["warning"] = (f"Còn {len(used)} kênh đang chọn template này; job mới của các kênh đó sẽ báo lỗi cho tới khi bạn chọn template khác." if used else None)
        return res

    def delete_draft(self, template_id: str, version: int) -> dict:
        """Xoá MỘT bản nháp của user. An toàn khi bấm đúp (lần hai: đã xoá). Bản đã publish/lưu trữ không bao giờ bị xoá ở đây (job cũ cần tái lập): dùng Lưu trữ.
        Nếu bản nháp là phiên bản duy nhất (xoá = template biến mất) mà kênh đang chọn nó thì từ chối và nói rõ kênh nào."""
        version = int(version)
        with self._busy(template_id):
            try:
                got = self.api.get_template(id=template_id, version=version, validate=False)
            except StageError as e:
                if e.code in ("TEMPLATE_NOT_FOUND", "TEMPLATE_VERSION_NOT_FOUND"):
                    return {"deleted": f"{template_id}@v{version}", "already_deleted": True}
                raise
            if got["scope"] != "user":
                raise _err("TEMPLATE_READONLY", "Template có sẵn chỉ đọc: không xoá được.", "Nhân bản template này nếu bạn muốn một bản riêng.")
            status = got["template"]["status"]
            if status != "draft":
                pub = status == "published"
                raise _err("TEMPLATE_NOT_DRAFT", f"Chỉ xoá được bản nháp; v{version} đã {'publish' if pub else 'lưu trữ'}.",
                           "Bản đã publish thì dùng “Lưu trữ” (job cũ vẫn dùng được version này)." if pub else
                           "Bản đã lưu trữ được giữ để job cũ tái lập; Nhân bản hoặc Khôi phục nếu cần dùng lại.", suggest="archive" if pub else None)
            if len(got["versions"]) == 1 and (used := self.ops.usage(template_id)):
                raise _err("TEMPLATE_IN_USE", f"Không xoá được: kênh {self._who(used)} đang chọn template này.",
                           "Mở trang Kênh → Template, chọn template khác (hoặc nhân bản template này), rồi xoá.", used_by=used)
            return self.api.delete_draft(id=template_id, version=version)

    def restore(self, template_id: str) -> dict:
        """Khôi phục template đã lưu trữ = tạo bản nháp mới từ version gần nhất (sửa nếu cần, rồi publish lại để chọn cho kênh)."""
        with self._busy(template_id):
            got = self.api.get_template(id=template_id, version="latest", validate=False)
            if got["scope"] != "user":
                raise _err("TEMPLATE_READONLY", "Template có sẵn không cần khôi phục.", "Nhân bản nếu muốn bản riêng.")
            if any(v["status"] == "published" for v in got["versions"]):
                raise _err("NOT_ARCHIVED", "Template này đang ở trạng thái đã publish, không cần khôi phục.", "Dùng “Bản nháp mới” để sửa.")
            return self.api.new_draft(id=template_id, from_version=None)

    # ------------------------------------------------------------------------------------------ xem trước / render thử
    def _artifact(self, res: dict) -> dict:
        """Đổi đường dẫn tuyệt đối trong kết quả của ContentFlow thành url theo tên (UI không biết đường dẫn máy)."""
        p = Path(res["path"])
        kind = p.parent.name if p.parent.name in PREVIEW_KINDS else p.parent.parent.name
        rel = p.name if p.parent.name in PREVIEW_KINDS else f"{p.parent.name}/{p.name}"
        out = {k: v for k, v in res.items() if k != "path"}
        out["url"] = f"/api/templates/files/{kind}/{rel}"
        return out

    def _type_of(self, template_id: str, template: dict | None, version) -> str:
        if isinstance(template, dict) and template.get("type") in ("thumbnail", "video"):
            return template["type"]
        return self.api.get_template(id=template_id, version=version if version is not None else "latest", validate=False)["template"]["type"]

    def preview_sources(self, type: str) -> dict:
        """Mẫu chọn được cho xem trước/render thử + render thử có dùng được không (kèm lý do). Không dựng ảnh: nhanh."""
        if type not in ("thumbnail", "video"):
            raise _err("BAD_TYPE", "Loại template phải là thumbnail hoặc video.")
        _ = self.api                                                             # ném TEMPLATES_UNSUPPORTED nếu adapter không có hệ thống template
        return self.samples.describe(type)

    def preview(self, template_id: str, template: dict | None = None, version=None, sample: dict | None = None) -> dict:
        """Xem trước NHANH (không ffmpeg, không video dài): tài liệu có thể là bản chưa lưu. `sample` là mô tả {id, image, channel}, dựng thành mẫu ở backend."""
        s = self.samples.resolve(sample, self._type_of(template_id, template, version))
        return self._artifact(self.api.preview(id=template_id if template is None else None, version=version, template=template, sample=s))

    def test_render(self, template_id: str, template: dict | None = None, version=None, sample: dict | None = None) -> dict:
        """Render THẬT bằng ContentFlow (khác xem trước): thumbnail → ảnh, video → clip ngắn. Cùng mẫu với xem trước để so được."""
        typ = self._type_of(template_id, template, version)
        te = self.samples.describe(typ)["test_render"]
        if not te["enabled"]:
            raise _err("TEST_RENDER_UNAVAILABLE", "Chưa render thử được.", te["reason"])
        s = self.samples.resolve(sample, typ)
        with self._busy("render:" + template_id):
            return self._artifact(self.api.test_render(id=template_id if template is None else None, version=version, template=template, sample=s))

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
