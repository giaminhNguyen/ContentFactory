"""Nạp Channel Config (D-46): `channels/<id>/channel.json` (hoặc `channel.yaml` nếu máy có PyYAML — lõi chỉ dùng stdlib nên JSON là định dạng chính, D-17).

Được đọc MỘT lần lúc tạo job và chốt vào config snapshot của job (D-41): sửa file sau đó không đổi job đang chạy. Thiếu file ⇒ cấu hình mặc định
(tên kênh = id, template mặc định). Sai ⇒ từ chối ngay lúc submit (`INVALID_CHANNEL_CONFIG`), không tạo job nửa vời.
`watermark` tương đối được đổi sang đường dẫn tuyệt đối theo thư mục kênh (HANDOFF §10: channels/<id>/watermark.wav).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..contracts import ErrorClass, StageError
from ..output import metadata as MD
from .config import Config


def channel_dir(cfg: Config, channel_id: str) -> Path:
    if not channel_id or any(c in channel_id for c in "/\\:") or channel_id in (".", ".."):
        raise StageError(ErrorClass.POLICY, "INVALID_CHANNEL_ID", f"channel id không hợp lệ: {channel_id!r}", resource="input")
    base = Path(cfg.data.get("channels_dir", "channels"))
    return (base if base.is_absolute() else cfg.root / base) / channel_id


def load_channel(cfg: Config, channel_id: str) -> dict:
    d = channel_dir(cfg, channel_id)
    raw, src = None, None
    for name in ("channel.json", "channel.yaml", "channel.yml"):
        f = d / name
        if not f.is_file():
            continue
        src = f
        try:
            if f.suffix == ".json":
                raw = json.loads(f.read_text(encoding="utf-8-sig"))
            else:
                try:
                    import yaml                      # tùy chọn: không phải phụ thuộc của lõi
                except ImportError:
                    raise StageError(ErrorClass.POLICY, "INVALID_CHANNEL_CONFIG", f"{f} là YAML nhưng máy chưa cài PyYAML; dùng channel.json", resource="input") from None
                raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError) as e:
            raise StageError(ErrorClass.POLICY, "INVALID_CHANNEL_CONFIG", f"không đọc được {f}: {e}", resource="input") from None
        if not isinstance(raw, dict):
            raise StageError(ErrorClass.POLICY, "INVALID_CHANNEL_CONFIG", f"{f}: gốc phải là object", resource="input")
        break
    ch = MD.normalize_channel(raw, channel_id)
    ch["id"] = channel_id
    ch["loaded_from"] = str(src) if src else None
    if ch.get("watermark"):
        w = Path(ch["watermark"])
        ch["watermark"] = str(w if w.is_absolute() else d / w)
    return ch
