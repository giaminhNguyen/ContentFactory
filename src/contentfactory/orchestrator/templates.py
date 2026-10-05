"""Chọn template cho job (Phase 10, D-92…D-97). Chạy lúc TẠO job, cùng chỗ với Auto Mode (preset/pool):

  Channel Config chọn template ID  ->  job chốt version cụ thể  ->  snapshot (nội dung + checksum + asset) nằm trong params.templates
  ->  RenderAdapter chỉ gửi tham chiếu/snapshot cho ContentFlow (không có tọa độ).

`params.templates[kind]` (kind = thumbnail | youtube | tiktok) là snapshot đã resolve: retry/resume/rerender dùng ĐÚNG snapshot đó, không resolve lại
`latest_published`. Chỉ hành động explicit (`retemplate`) mới đổi nó. Layout kiểu cũ (frame_path/viewport/config_overrides) vẫn chạy khi không có template nào
được chọn rõ ràng — kèm cảnh báo deprecate — và bị bỏ qua (có ghi) khi đã chọn template.
"""
from __future__ import annotations

import copy

from ..contracts import ErrorClass, StageError
from ..render import profile as PF
from .config import Config

# khóa trong Channel Config -> (kind của job, kiểu template cần)
CHANNEL_KEYS = {"thumbnail": ("thumbnail", "thumbnail"), "youtube_video": ("youtube", "video"), "tiktok_video": ("tiktok", "video")}
KIND_TO_CHANNEL_KEY = {v[0]: k for k, v in CHANNEL_KEYS.items()}
DEFAULT_IDS = {"thumbnail": "thumb_default", "youtube_video": "youtube_default", "tiktok_video": "tiktok_default"}
POLICY_LATEST = "latest_published"


def normalize_ref(raw, where: str) -> tuple[dict | None, list[str]]:
    """Một lựa chọn template trong Channel Config: {id, version_policy: latest_published|<số>, fallback?}. Chuỗi trần = id + latest_published."""
    if isinstance(raw, str):
        raw = {"id": raw}
    if not isinstance(raw, dict):
        return None, [f"{where} phải là object {{id, version_policy}}"]
    errs = [f"{where}.{k}: khóa không hợp lệ (hợp lệ: id, version_policy, fallback)" for k in raw if k not in ("id", "version_policy", "fallback")]
    tid = raw.get("id")
    if not isinstance(tid, str) or not tid.strip():
        errs.append(f"{where}.id phải là id template (chuỗi không rỗng)")
    pol = raw.get("version_policy", POLICY_LATEST)
    if not (pol == POLICY_LATEST or (isinstance(pol, int) and not isinstance(pol, bool) and pol >= 1)):
        errs.append(f"{where}.version_policy phải là 'latest_published' hoặc số version >= 1 (nhận {pol!r})")
    fb = raw.get("fallback")
    if fb is not None and not (isinstance(fb, str) and fb.strip()):
        errs.append(f"{where}.fallback phải là id template")
    if errs:
        return None, errs
    out = {"id": tid.strip(), "version_policy": pol}
    if fb:
        out["fallback"] = fb.strip()
    return out, []


def normalize_section(sec) -> tuple[dict, list[str]]:
    if sec is None:
        return {}, []
    if not isinstance(sec, dict):
        return {}, ["templates phải là object"]
    out, errs = {}, []
    for k, v in sec.items():
        if k not in CHANNEL_KEYS:
            errs.append(f"templates.{k}: không hợp lệ; hợp lệ: {sorted(CHANNEL_KEYS)}")
            continue
        ref, e = normalize_ref(v, f"templates.{k}")
        errs += e
        if ref:
            out[k] = ref
    return out, errs


def _api(adapters: dict):
    r = adapters.get("render")
    return r.templates if getattr(r, "supports_templates", False) else None


def _is_snapshot(v) -> bool:
    return isinstance(v, dict) and isinstance(v.get("template"), dict) and "checksum" in v


def global_defaults(cfg: Config) -> dict:
    return {**DEFAULT_IDS, **((cfg.data.get("templates") or {}).get("defaults") or {})}


def _legacy(cfg: Config, merged: dict, kind: str) -> bool:
    pid = "youtube" if kind in ("youtube", "thumbnail") else "tiktok"
    prof = PF.resolve(pid, cfg.data.get("render"), merged.get("render"))
    if kind == "thumbnail":
        return bool((prof.get("thumbnail") or {}).get("config_overrides"))
    return any(prof.get(k) for k in PF.LEGACY_LAYOUT_KEYS) or prof["resolution"] != PF.DEFAULTS[pid]["resolution"]   # đổi độ phân giải = chỉnh khung hình kiểu cũ


def select_templates(cfg: Config, merged: dict, channel: dict, adapters: dict) -> tuple[dict, list[dict]]:
    """(params.templates, decisions). Raise POLICY INVALID_CHANNEL_TEMPLATE khi template đã chọn không dùng được (không đổi âm thầm sang template khác)."""
    api = _api(adapters)
    if api is None:
        return {}, []
    cid = channel.get("id") or merged.get("channel") or "default"
    job_t = merged.get("templates") or {}
    chan_t = channel.get("templates") or {}
    defaults = global_defaults(cfg)
    out: dict = {}
    decisions: list[dict] = []
    refs: dict[str, tuple[dict, str]] = {}                     # kind -> (ref, nguồn)
    for ckey, (kind, ttype) in CHANNEL_KEYS.items():
        have = job_t.get(kind)
        if _is_snapshot(have):                                  # đã chốt (tạo lại từ job cũ / test): giữ nguyên, không resolve lại
            out[kind] = have
            continue
        if have is not None:
            ref, errs = normalize_ref(have, f"params.templates.{kind}")
            if errs:
                raise StageError(ErrorClass.POLICY, "INVALID_TEMPLATE_PARAM", "; ".join(errs), {"errors": errs}, resource="input")
            refs[kind] = (ref, "chỉ định cho job này")
        elif ckey in chan_t:
            refs[kind] = (chan_t[ckey], f"Channel Config của kênh '{cid}'")
        elif _legacy(cfg, merged, kind):
            decisions.append({"what": f"template.{kind}", "value": "legacy", "why": "cấu hình bố cục kiểu cũ (frame_path/viewport/config_overrides) vẫn đang dùng; "
                              "deprecated — chạy `cf templates migrate` để chuyển sang template"})
        else:
            refs[kind] = ({"id": defaults[ckey], "version_policy": POLICY_LATEST}, "mặc định")
    for kind, (ref, src) in list(refs.items()):                # đã chọn rõ ràng mà vẫn còn layout cũ => layout cũ bị bỏ qua (nói rõ, không âm thầm)
        if src != "mặc định" and _legacy(cfg, merged, kind):
            decisions.append({"what": f"template.{kind}.legacy_ignored", "value": True,
                              "why": "template đã được chọn nên frame_path/viewport/config_overrides kiểu cũ không còn tác dụng"})
    pending = dict(refs)
    for _ in range(len(pending) + 1):
        if not pending:
            break
        reqs = [{"key": k, "id": ref["id"], "policy": ref["version_policy"], "expect_type": CHANNEL_KEYS[KIND_TO_CHANNEL_KEY[k]][1]}
                for k, (ref, _s) in pending.items()]
        try:
            snaps = api.resolve_many(requests=reqs)
        except StageError as e:
            if e.code == "TEMPLATES_UNAVAILABLE":                # module ContentFlow cũ chưa có hệ thống template: chạy kiểu cũ nhưng nói rõ (doctor cũng báo)
                decisions.append({"what": "templates", "value": "unavailable", "why": e.message})
                return out, decisions
            key = (e.detail or {}).get("key")
            if key not in pending:
                raise _wrap(e, cid, "?", {"id": "?"}) from None
            ref, src = pending[key]
            if ref.get("fallback") and ref["id"] != ref["fallback"]:   # fallback chỉ khi KÊNH khai báo rõ ràng
                decisions.append({"what": f"template.{key}.fallback", "value": ref["fallback"],
                                  "why": f"template '{ref['id']}' không dùng được ({e.code}); dùng fallback đã khai báo trong {src}"})
                pending[key] = ({"id": ref["fallback"], "version_policy": POLICY_LATEST}, src + " (fallback)")
                refs[key] = pending[key]
                continue
            raise _wrap(e, cid, key, ref) from None
        for k, snap in snaps.items():
            ref, src = refs[k]
            out[k] = snap
            pol = ref["version_policy"]
            decisions.append({"what": f"template.{k}", "value": f"{snap['id']}@v{snap['version']}",
                              "why": f"{src}; {'bản published mới nhất' if pol == POLICY_LATEST else 'version ghim ' + str(pol)} lúc tạo job (checksum {snap['checksum'][:10]})"})
        break
    return out, decisions


def _wrap(e: StageError, cid: str, kind: str, ref: dict) -> StageError:
    label = {"thumbnail": "thumbnail", "youtube": "YouTube", "tiktok": "TikTok"}.get(kind, kind)
    if e.error_class != ErrorClass.POLICY:
        return e                                                # ContentFlow không chạy được: giữ nguyên lớp lỗi (RESOURCE)
    return StageError(ErrorClass.POLICY, "INVALID_CHANNEL_TEMPLATE",
                      f"kênh '{cid}': template {label} '{ref.get('id')}' không dùng được ({e.code}): {e.message}. "
                      f"Chọn template khác cho kênh (Kênh → Template) hoặc publish một version cho template đó.",
                      {"channel": cid, "kind": kind, "template_id": ref.get("id"), "template_error": e.code, **(e.detail or {})}, resource="input")


def retemplate(api, params: dict, kind: str, template_id: str, policy=POLICY_LATEST) -> dict:
    """Hành động EXPLICIT: resolve lại và thay snapshot của MỘT kind trong params của job. Trả params.templates mới."""
    if kind not in KIND_TO_CHANNEL_KEY:
        raise StageError(ErrorClass.POLICY, "INVALID_TEMPLATE_KIND", f"kind phải là {sorted(KIND_TO_CHANNEL_KEY)}", resource="input")
    ttype = CHANNEL_KEYS[KIND_TO_CHANNEL_KEY[kind]][1]
    snap = api.resolve(id=template_id, policy=policy, expect_type=ttype)
    out = copy.deepcopy(params.get("templates") or {})
    out[kind] = snap
    return out
