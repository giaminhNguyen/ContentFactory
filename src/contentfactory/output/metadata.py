"""Metadata Builder (D-43…D-47): dựng tiêu đề/mô tả đăng YouTube từ `project.title` + Channel Config + sequence. Thuần tất định, không AI.

- Template STRICT: chỉ {channel_name} {project_title} {sequence}; biến lạ, format spec ({sequence:03d}) hay conversion đều là lỗi; `{{`/`}}` là dấu ngoặc nhọn.
- Giới hạn YouTube (theo yt_uploader): title ≤ 100 ký tự, description ≤ 5000 byte UTF-8. Vượt ⇒ POLICY (`TITLE_TOO_LONG`/`DESCRIPTION_TOO_LONG`), KHÔNG cắt âm thầm,
  KHÔNG đổi `project.title`.
- `project.title` là field chính duy nhất; title YouTube, thumbnail, tên thư mục output, README, project.json đều derive từ nó.
"""
from __future__ import annotations

import copy
import string

from ..contracts import ErrorClass, StageError

TITLE_MAX_CHARS = 100
DESCRIPTION_MAX_BYTES = 5000
ALLOWED_FIELDS = ("channel_name", "project_title", "sequence")
DEFAULT_TITLE_TEMPLATE = "[Full Audio {sequence}] | {project_title}"
DEFAULT_DESCRIPTION_TEMPLATE = "{project_title}\n\n{channel_name}"
PUBLISHING_KEYS = {"privacy": str, "tags": list, "category": (str, type(None)), "playlists": list, "account_id": (str, type(None)),
                   "made_for_kids": (bool, type(None))}


def default_channel(channel_id: str) -> dict:
    return {"id": channel_id, "name": channel_id, "title_template": DEFAULT_TITLE_TEMPLATE, "description_template": DEFAULT_DESCRIPTION_TEMPLATE,
            "thumbnail": {}, "publishing": {}, "sequence": {"last_used": 0}, "watermark": None, "preset": {}, "loaded_from": None}


def normalize_channel(raw: dict | None, channel_id: str) -> dict:
    """Channel Config đầy đủ trường (thiếu thì lấy mặc định); sai kiểu/biến lạ trong template ⇒ POLICY INVALID_CHANNEL_CONFIG."""
    out = default_channel(channel_id)
    raw = copy.deepcopy(raw or {})
    errs = []
    for k in ("name", "title_template", "description_template", "watermark"):
        if k in raw and raw[k] is not None:
            if not isinstance(raw[k], str) or not raw[k].strip():
                errs.append(f"{k} phải là chuỗi không rỗng")
            else:
                out[k] = raw[k]
    pre = raw.get("preset")
    if pre is not None:
        errs += validate_preset(pre)
        if isinstance(pre, dict):
            out["preset"] = pre
    for k in ("thumbnail", "publishing", "sequence"):
        if k in raw:
            if not isinstance(raw[k], dict):
                errs.append(f"{k} phải là object")
            else:
                out[k] = {**out[k], **raw[k]}
    th = out.get("thumbnail") or {}                                                    # Image Pool của kênh (Phase 8): template vẫn giữ bố cục, pool chỉ cấp ảnh
    if th.get("image_pool") is not None and (not isinstance(th["image_pool"], str) or not th["image_pool"].strip()):
        errs.append("thumbnail.image_pool phải là tên pool (chuỗi không rỗng) hoặc bỏ trống")
    if th.get("selection_mode") is not None and th["selection_mode"] not in ("shuffle", "random", "sequential"):
        errs.append("thumbnail.selection_mode phải là shuffle | random | sequential")
    for k, typ in PUBLISHING_KEYS.items():
        if k in out["publishing"] and not isinstance(out["publishing"][k], typ):
            errs.append(f"publishing.{k} sai kiểu")
    if out["publishing"].get("privacy") not in (None, "private", "unlisted", "public"):
        errs.append("publishing.privacy phải là private | unlisted | public")
    lu = out["sequence"].get("last_used", 0)
    if not isinstance(lu, int) or isinstance(lu, bool) or lu < 0:
        errs.append("sequence.last_used phải là số nguyên >= 0")
    for k in ("title_template", "description_template"):
        try:
            check_template(out[k], k)
        except StageError as e:
            errs.append(e.message)
    if errs:
        raise StageError(ErrorClass.POLICY, "INVALID_CHANNEL_CONFIG", f"channel '{channel_id}': " + "; ".join(errs), {"errors": errs},
                         resource="input")
    return out


PRESET_KEYS = {"tts_profile": (str, type(None)), "tts": dict, "language": str, "pools": dict, "render": dict, "audio": dict, "tiktok": dict, "prosody": dict}


def validate_preset(pre) -> list[str]:
    """Channel preset (Auto Mode): thứ kênh nhớ để người dùng không phải chọn lại mỗi lần. Trả danh sách lỗi."""
    if not isinstance(pre, dict):
        return ["preset phải là object"]
    errs = [f"preset.{k}: khóa không hợp lệ; hợp lệ: {sorted(PRESET_KEYS)}" for k in pre if k not in PRESET_KEYS]
    errs += [f"preset.{k} sai kiểu" for k, t in PRESET_KEYS.items() if k in pre and not isinstance(pre[k], t)]
    for k in ("pools", "render"):
        for sub in (pre.get(k) or {}):
            if sub not in ("youtube", "tiktok"):
                errs.append(f"preset.{k}.{sub}: chỉ hỗ trợ youtube | tiktok")
    for sub, v in (pre.get("pools") or {}).items():
        if not isinstance(v, str) or not v:
            errs.append(f"preset.pools.{sub} phải là tên pool")
    return errs


def check_template(tpl: str, where: str = "template") -> None:
    try:
        parsed = list(string.Formatter().parse(tpl))
    except ValueError as e:
        raise StageError(ErrorClass.POLICY, "INVALID_TEMPLATE", f"{where}: {e}") from None
    for _, field, spec, conv in parsed:
        if field is None:
            continue
        if field not in ALLOWED_FIELDS:
            raise StageError(ErrorClass.POLICY, "INVALID_TEMPLATE", f"{where}: biến {{{field}}} không hợp lệ; hợp lệ: {list(ALLOWED_FIELDS)}")
        if spec or conv:
            raise StageError(ErrorClass.POLICY, "INVALID_TEMPLATE", f"{where}: không hỗ trợ định dạng/conversion cho {{{field}}}")


def render_template(tpl: str, values: dict, where: str = "template") -> str:
    check_template(tpl, where)
    return tpl.format(**{k: values[k] for k in ALLOWED_FIELDS})


def build(project: dict, channel: dict, sequence: int) -> dict:
    """PublishMetadata + cảnh báo. `project`: {id, title, title_source, channel_id, language}."""
    values = {"channel_name": channel["name"], "project_title": project["title"], "sequence": str(sequence)}
    title = render_template(channel["title_template"], values, "title_template").strip()
    desc = render_template(channel["description_template"], values, "description_template").strip()
    if not title or "\n" in title:
        raise StageError(ErrorClass.POLICY, "INVALID_TITLE", "tiêu đề YouTube rỗng hoặc có xuống dòng")
    if len(title) > TITLE_MAX_CHARS:
        raise StageError(ErrorClass.POLICY, "TITLE_TOO_LONG",
                         f"tiêu đề YouTube dài {len(title)} ký tự (> {TITLE_MAX_CHARS}); rút gọn project.title hoặc title_template, KHÔNG tự cắt",
                         {"title": title})
    if len(desc.encode("utf-8")) > DESCRIPTION_MAX_BYTES:
        raise StageError(ErrorClass.POLICY, "DESCRIPTION_TOO_LONG", f"mô tả dài {len(desc.encode('utf-8'))} byte (> {DESCRIPTION_MAX_BYTES})")
    warnings = []
    if project["title_source"] == "source_default":
        warnings.append("project.title chưa được đặt: đang dùng TIÊU ĐỀ CỦA VIDEO NGUỒN (placeholder). Đặt params.project.title trước khi đăng.")
    return {"schema": 1, "youtube_title": title, "description": desc, "sequence": sequence, "project_title": project["title"],
            "title_source": project["title_source"], "channel_id": project["channel_id"], "channel_name": channel["name"],
            "language": project["language"], "templates": {"title": channel["title_template"], "description": channel["description_template"]},
            "warnings": warnings}
