"""Auto Mode: suy ra những thứ người dùng không nên phải chọn (D-82). Chạy lúc TẠO job; kết quả được chốt vào params của job (nên vào snapshot/stage_key
đúng chỗ) và ghi lại thành danh sách "decisions" (cái gì, vì sao) để minh bạch — không có lựa chọn ngầm.

  - Channel preset (`channel.json → preset`): TTS profile ưa thích, pool nguồn cho YouTube/TikTok, override profile render, audio, TikTok, ngôn ngữ. Watermark và cấu hình đăng
    (account, privacy…) đã là trường của channel từ Phase 4/6. Ưu tiên: params người dùng nhập > preset của kênh > job_defaults.
  - Auto TTS profile: không chỉ định ⇒ chọn trong `tts_profiles/` profile hợp ngôn ngữ + engine đang dùng, ưu tiên `ready`, đã Auto Tune, không đòi người dùng thêm gì.
  - Auto source/profile: pool mặc định không tồn tại ⇒ chọn theo hướng khung hình (ngang/dọc) từ `orientation` hoặc tên pool; chỉ có một pool ⇒ dùng chung và cảnh báo.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from ..contracts import ErrorClass, StageError
from ..render import profile as PF
from ..tts import schema as TS
from .config import Config

PORTRAIT = re.compile(r"vertical|portrait|9[x:_-]?16|doc|dọc", re.I)
LANDSCAPE = re.compile(r"landscape|horizontal|16[x:_-]?9|ngang", re.I)


# ============================================================================== TTS profile
def tts_profiles_dir(cfg: Config) -> Path:
    d = Path(cfg.data.get("tts_profiles_dir", "tts_profiles"))
    return d if d.is_absolute() else cfg.root / d


def load_tts_profile(cfg: Config, name: str) -> dict:
    f = tts_profiles_dir(cfg) / f"{name}.json"
    if not f.is_file():
        raise StageError(ErrorClass.POLICY, "TTS_PROFILE_NOT_FOUND", f"không có TTS profile '{name}' ({f}); tạo bằng scripts/tts_onboard.py", resource="input")
    try:
        prof = json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise StageError(ErrorClass.POLICY, "INVALID_TTS_PROFILE", f"{f}: {e}", resource="input") from None
    if prof.get("schema") == TS.SCHEMA_VERSION:
        errs = TS.validate_annotated(prof)
        if errs:
            raise StageError(ErrorClass.POLICY, "INVALID_TTS_PROFILE", f"{f}: " + "; ".join(errs[:5]), {"errors": errs}, resource="input")
    return prof


def list_tts_profiles(cfg: Config) -> list[tuple[str, dict]]:
    out = []
    d = tts_profiles_dir(cfg)
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            out.append((f.stem, load_tts_profile(cfg, f.stem)))
        except StageError:
            continue                                                   # profile hỏng không được cản việc chọn profile khác
    return out


def select_tts_profile(cfg: Config, language: str, engine_id: str | None) -> tuple[str, str] | None:
    """(tên, lý do) của profile phù hợp nhất, hoặc None. Loại profile chưa dùng được (còn `needs_user` bắt buộc, ví dụ reference voice)."""
    best, best_rank = None, None
    for name, prof in list_tts_profiles(cfg):
        if prof.get("schema") != TS.SCHEMA_VERSION:
            continue
        if engine_id and prof.get("engine") and prof["engine"] != engine_id:
            continue
        langs = ((prof.get("capabilities") or {}).get("languages")) or []
        if langs and language.split("-")[0].lower() not in {x.split("-")[0].lower() for x in langs}:
            continue
        hard = [n for n in prof.get("needs_user", []) if n.get("reason") in ("required_reference_voice", "required_parameter", "credential")]
        if hard:
            continue
        tuned = bool(((prof.get("meta") or {}).get("autotune") or {}).get("works"))
        rank = (prof.get("status") == "ready", tuned, name)
        if best_rank is None or rank > best_rank:
            best, best_rank = (name, f"hợp ngôn ngữ '{language}'" + (", engine khớp" if engine_id else "") + (", đã Auto Tune" if tuned else "") +
                               (", status ready" if prof.get("status") == "ready" else ", status candidate")), rank
    return best


# ============================================================================== source pool
def orientation_of(name: str, spec: dict) -> str | None:
    o = (spec or {}).get("orientation")
    if o in ("landscape", "portrait"):
        return o
    if PORTRAIT.search(name):
        return "portrait"
    if LANDSCAPE.search(name):
        return "landscape"
    return None


def pick_pool(pid: str, current: str | None, pools: dict) -> tuple[str | None, str]:
    """Pool cho profile `pid`: giữ pool đang chỉ định nếu tồn tại; ngược lại chọn theo hướng khung hình; chỉ một pool thì dùng chung (kèm cảnh báo)."""
    if current and current in pools:
        return current, "đã cấu hình"
    want = "landscape" if pid == "youtube" else "portrait"
    match = [n for n, s in pools.items() if orientation_of(n, s) == want]
    if len(match) == 1:
        return match[0], f"pool duy nhất có hướng khung hình {want}"
    if len(pools) == 1:
        n = next(iter(pools))
        return n, f"chỉ có một pool: dùng chung cho cả YouTube và TikTok (cảnh báo: có thể bị crop; nên có pool ngang và pool dọc)"
    return None, "không suy ra được pool"


# ============================================================================== preset -> params
def preset_params(cfg: Config, channel: dict, explicit: dict, adapters: dict) -> tuple[dict, list[dict]]:
    """(params từ preset + lựa chọn tự động, decisions). Không ghi đè gì người dùng đã nhập (explicit)."""
    pre = copy.deepcopy(channel.get("preset") or {})
    out: dict = {}
    dec: list[dict] = []
    for k in ("language", "audio", "tiktok"):
        if k in pre:
            out[k] = pre[k]
            dec.append({"what": k, "value": pre[k], "why": f"preset của kênh '{channel['id']}'"})
    if pre.get("render"):
        out["render"] = copy.deepcopy(pre["render"])
        dec.append({"what": "render", "value": sorted(pre["render"]), "why": f"preset của kênh '{channel['id']}'"})
    if "tts" in explicit:
        pass                                                           # người dùng đã chỉ định: giữ nguyên
    elif pre.get("tts") is not None:
        out["tts"] = pre["tts"]
        dec.append({"what": "tts", "value": "inline", "why": f"preset của kênh '{channel['id']}'"})
    elif pre.get("tts_profile"):
        out["tts"] = load_tts_profile(cfg, pre["tts_profile"])
        dec.append({"what": "tts_profile", "value": pre["tts_profile"], "why": f"profile ưa thích của kênh '{channel['id']}'"})
    elif cfg.data.get("auto", {}).get("tts_profile_selection", True):
        language = explicit.get("language") or pre.get("language") or cfg.data["job_defaults"].get("language", "vi")
        sel = select_tts_profile(cfg, language, getattr(adapters.get("tts"), "engine_id", None))
        if sel:
            out["tts"] = load_tts_profile(cfg, sel[0])
            dec.append({"what": "tts_profile", "value": sel[0], "why": "tự chọn: " + sel[1]})
    default_prosody = (cfg.data.get("prosody") or {}).get("default_profile")
    if "prosody" in explicit:
        pass
    elif pre.get("prosody"):
        out["prosody"] = pre["prosody"]
        dec.append({"what": "prosody", "value": (pre["prosody"] or {}).get("profile", "natural"), "why": f"nhịp đọc ưa thích của kênh '{channel['id']}'"})
    elif default_prosody:
        out["prosody"] = {"profile": default_prosody}
        dec.append({"what": "prosody", "value": default_prosody, "why": "nhịp đọc mặc định của máy (tất định, không dùng LLM)"})
    return out, dec


def select_pools(cfg: Config, merged: dict, channel: dict, adapters: dict) -> list[dict]:
    """Điền `render.<profile>.source_pool` (preset của kênh, hoặc tự chọn). Sửa `merged` tại chỗ; trả decisions. Bỏ qua khi adapter render không dùng pool."""
    if not getattr(adapters.get("render"), "requires_pool", False):
        return []
    pools = (cfg.data.get("render") or {}).get("pools") or {}
    dec: list[dict] = []
    pre_pools = (channel.get("preset") or {}).get("pools") or {}
    for pid in ("youtube", "tiktok"):
        cur_params = ((merged.get("render") or {}).get(pid) or {}).get("source_pool")
        prof = PF.resolve(pid, cfg.data.get("render"), merged.get("render"))
        if cur_params:                                                   # người dùng/preset render đã chỉ định pool
            continue
        if pre_pools.get(pid):
            name, why = pre_pools[pid], f"pool ưa thích của kênh '{channel['id']}'"
        elif cfg.data.get("auto", {}).get("pool_selection", True) and pools:
            name, why = pick_pool(pid, prof.get("source_pool"), pools)
            if name == prof.get("source_pool"):
                continue                                                 # pool mặc định của profile đã tồn tại: không cần ghi đè
        else:
            continue
        if name:
            merged.setdefault("render", {}).setdefault(pid, {})["source_pool"] = name
            dec.append({"what": f"pool.{pid}", "value": name, "why": why})
    return dec
