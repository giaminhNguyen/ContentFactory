"""Render profile: YouTube (16:9) và TikTok (9:16). Cấu hình, không hardcode vào code render.

Profile hiệu lực = mặc định trong file này ⊕ `config["render"]["profiles"][id]` (đã chốt vào snapshot của job) ⊕ `params.render[id]` của job.
Kích thước output của ContentFlow do **frame PNG** quyết định (xem docs/CURRENT_SYSTEM_AUDIT.md §3.4), nên mỗi profile có một frame
trong suốt đúng độ phân giải (sinh bằng `frames.ensure_frame`) và `viewport` phủ kín canvas; muốn khung viền riêng thì đặt `frame_path` + `viewport`.
"""
from __future__ import annotations

import copy
import re

from ..contracts import ErrorClass, StageError

DEFAULTS: dict = {
    "youtube": {"id": "youtube", "aspect_ratio": "16:9", "resolution": "1920x1080", "fps": 30, "source_pool": "gameplay",
                "selection_mode": "shuffle", "source_processing": "auto", "encoder": "auto", "deadline_s": 7200,
                "frame_path": None, "viewport": None, "config_overrides": {},
                "retry": {"max_attempts": 2, "backoff_s": [5.0, 30.0]},
                "thumbnail": {"enabled": True, "highlight": "auto", "highlight_text": "", "image": None, "config_overrides": {}}},
    "tiktok": {"id": "tiktok", "aspect_ratio": "9:16", "resolution": "1080x1920", "fps": 30, "source_pool": "gameplay_vertical",
               "selection_mode": "shuffle", "source_processing": "auto", "encoder": "auto", "deadline_s": 7200,
               "frame_path": None, "viewport": None, "config_overrides": {},
               "retry": {"max_attempts": 2, "backoff_s": [5.0, 30.0]}},
}
RES_RX = re.compile(r"^(\d{3,5})x(\d{3,5})$")
# phần của profile ảnh hưởng tới KẾT QUẢ video (vào khóa cache); deadline/retry thì không
KEY_FIELDS = ("id", "resolution", "fps", "source_pool", "selection_mode", "source_processing", "encoder", "frame_path", "viewport",
              "config_overrides", "template")
LEGACY_LAYOUT_KEYS = ("frame_path", "viewport", "config_overrides")     # bố cục kiểu cũ trong profile: thay bằng template (D-92)


def _merge(a: dict, b: dict) -> dict:
    for k, v in b.items():
        a[k] = _merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return a


def resolve(pid: str, config_render: dict | None, params_render: dict | None) -> dict:
    base = copy.deepcopy(DEFAULTS.get(pid, {"id": pid}))
    if pid not in DEFAULTS:
        raise StageError(ErrorClass.POLICY, "INVALID_RENDER_PROFILE", f"profile '{pid}' không tồn tại; hợp lệ: {sorted(DEFAULTS)}")
    p = _merge(_merge(base, copy.deepcopy(((config_render or {}).get("profiles") or {}).get(pid, {}))),
               copy.deepcopy((params_render or {}).get(pid, {})))
    errs = []
    m = RES_RX.match(str(p.get("resolution", "")))
    if not m:
        errs.append(f"resolution phải dạng WxH (nhận {p.get('resolution')!r})")
    else:
        p["width"], p["height"] = int(m.group(1)), int(m.group(2))
        a = re.match(r"^(\d+):(\d+)$", str(p.get("aspect_ratio", "")))
        if a and abs(p["width"] / p["height"] - int(a.group(1)) / int(a.group(2))) > 0.02:
            errs.append(f"aspect_ratio {p['aspect_ratio']} không khớp resolution {p['resolution']}")
    if not 1 <= int(p.get("fps", 0)) <= 120:
        errs.append("fps ngoài [1, 120]")
    if p.get("selection_mode") not in ("shuffle", "random", "sequential"):
        errs.append("selection_mode phải là shuffle | random | sequential")
    if p.get("source_processing") not in ("auto", "normal", "fast"):
        errs.append("source_processing phải là auto | normal | fast")
    if int(p["retry"]["max_attempts"]) < 1:
        errs.append("retry.max_attempts phải >= 1")
    if errs:
        raise StageError(ErrorClass.POLICY, "INVALID_RENDER_PROFILE", "; ".join(errs), {"errors": errs, "profile": pid})
    return p


def pool_spec(profile: dict, config_render: dict | None) -> dict | None:
    """Pool nguồn video của profile (None nếu profile không dùng pool). Raise POLICY nếu pool chưa được cấu hình."""
    name = profile.get("source_pool")
    if not name:
        return None
    pools = (config_render or {}).get("pools") or {}
    if name not in pools:
        raise StageError(ErrorClass.POLICY, "POOL_NOT_CONFIGURED",
                         f"source pool '{name}' chưa cấu hình: thêm render.pools.{name} = {{raw_dir: ...}} vào config", {"pool": name},
                         resource="input")
    spec = copy.deepcopy(pools[name])
    spec["name"] = name
    sync = spec.setdefault("sync", {})
    sync.setdefault("size", profile["resolution"])
    sync.setdefault("fps", profile["fps"])
    sync.setdefault("quality", "balanced")
    sync.setdefault("remove_audio", True)
    sync.setdefault("encoder", "auto")
    return spec


def has_legacy_layout(prof: dict) -> bool:
    """Profile còn mang bố cục kiểu cũ (frame/viewport/override ContentFlow) — chỉ còn dùng làm tương thích, nên chuyển sang template."""
    return (any(prof.get(k) for k in LEGACY_LAYOUT_KEYS) or bool((prof.get("thumbnail") or {}).get("config_overrides"))
            or prof.get("resolution") != DEFAULTS.get(prof.get("id"), {}).get("resolution", prof.get("resolution")))


def template_ref(snap: dict | None) -> dict | None:
    """Phần nhận dạng của snapshot template (vào khóa cache/report): đổi version, nội dung hoặc asset => khác."""
    return {"id": snap["id"], "version": snap["version"], "fingerprint": snap.get("fingerprint")} if snap else None


def apply_template(prof: dict, snap: dict | None) -> dict:
    """Template (đã chốt vào job) quyết định khung hình/độ phân giải/fps; layout kiểu cũ trong profile bị bỏ qua (có cảnh báo ở lúc tạo job)."""
    if not snap:
        return prof
    p = copy.deepcopy(prof)
    w, h = snap["summary"]["canvas"]
    fps = snap["summary"].get("fps")
    p.update(width=int(w), height=int(h), resolution=f"{w}x{h}", aspect_ratio=f"{w}:{h}", frame_path=None, viewport=None, config_overrides={},
             template=template_ref(snap))
    if fps:
        p["fps"] = int(fps)
    return p
