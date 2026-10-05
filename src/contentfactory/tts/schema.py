"""Schema của TTS: capability, profile (có evidence/confidence cho từng giá trị), rule. Chỉ stdlib.

Hai dạng profile:
  - ANNOTATED (lưu trữ, do Analyzer/Auto Tune sinh): mỗi giá trị là một Fact
        {"value": 400, "source": "official_docs", "confidence": "high", "evidence": [{"ref": "README.md:41", "quote": "..."}]}
  - FLAT (hiệu lực, đưa cho Manager và adapter): giá trị trần. `resolve()` bóc Fact, trộn với mặc định, kẹp theo capability.
Giá trị trần trong profile do người dùng/job truyền vào được coi là source="user". Mặc định trong code là source="default"
(confidence "low": con số hợp lý chứ không phải sự thật về engine, xem DECISIONS D-57).
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from ..contracts import ErrorClass, StageError

EVIDENCE_SOURCES = ("official_docs", "source_code", "official_example", "runtime_test", "ai_inference", "user", "default")
CONFIDENCE = ("high", "medium", "low")
# Khi hai nguồn mâu thuẫn: nguồn xếp hạng cao hơn thắng (riêng giới hạn số thì Analyzer chọn giá trị THẬN TRỌNG nhất).
SOURCE_RANK = {"user": 7, "runtime_test": 6, "official_docs": 5, "official_example": 4, "source_code": 3,
               "ai_inference": 2, "default": 1}
CONF_RANK = {"high": 3, "medium": 2, "low": 1}
SCHEMA_VERSION = 1

CAPABILITY_FIELDS = {          # tên: (kiểu, mặc định)
    "max_chars": ((int, type(None)), None), "languages": (list, []), "voices": (list, []), "speed": (bool, False),
    "ssml": (bool, False), "streaming": (bool, False), "batch": (bool, False), "voice_cloning": (bool, False),
    "requires_reference_audio": (bool, False), "output_formats": (list, ["wav"]), "sample_rate": ((int, type(None)), None),
    "max_concurrency": (int, 1), "engine_version": (str, ""), "cache_settings": ((list, type(None)), None),
    "device": (list, []),
}

DEFAULT_FLAT: dict = {
    "profile_version": "1", "engine": None, "language": None, "voice": None, "model": None, "settings": {},
    "segment": {"preferred_chars": 400, "max_chars": 600, "min_chars": 60},
    "break_priority": ["paragraph", "sentence", "semicolon", "comma", "space"],
    "pause_ms": {"paragraph": 600, "sentence": 0, "dialogue": 350},      # sentence = nghỉ giữa hai segment CÙNG đoạn
    "joiner": " ",                                                       # "" cho ngôn ngữ không dùng dấu cách (zh/ja)
    "normalize": {"strip_markdown": True, "collapse_punct": True, "ellipsis": True,
                  "ensure_terminal_punct": False, "replacements": []},
    "qa": {"silence_ratio_max": 0.98, "duration_chars_per_sec": None, "min_duration_sec": 0.05},
    "retry": {"max_attempts": 3, "backoff_s": [0.5, 2.0]},
    "planner": {"ai_retries": 1},
}


# ---------------------------------------------------------------- Fact
def fact(value: Any, source: str = "default", confidence: str = "low", evidence: list[dict] | None = None,
         note: str = "") -> dict:
    f = {"value": value, "source": source, "confidence": confidence, "evidence": list(evidence or [])}
    if note:
        f["note"] = note
    return f


def is_fact(x: Any) -> bool:
    return isinstance(x, dict) and "value" in x and "source" in x and "confidence" in x


def unwrap(x: Any) -> Any:
    """Bóc Fact (đệ quy qua dict/list)."""
    if is_fact(x):
        return unwrap(x["value"])
    if isinstance(x, dict):
        return {k: unwrap(v) for k, v in x.items()}
    return x


def validate_fact(path: str, f: Any) -> list[str]:
    if not is_fact(f):
        return [f"{path}: không phải Fact (thiếu value/source/confidence)"]
    errs = []
    if f["source"] not in EVIDENCE_SOURCES:
        errs.append(f"{path}: source {f['source']!r} không hợp lệ; hợp lệ: {list(EVIDENCE_SOURCES)}")
    if f["confidence"] not in CONFIDENCE:
        errs.append(f"{path}: confidence {f['confidence']!r} không hợp lệ")
    ev = f.get("evidence", [])
    if not isinstance(ev, list) or any(not isinstance(e, dict) or "ref" not in e for e in ev):
        errs.append(f"{path}: evidence phải là list các {{ref, quote?}}")
    if f["source"] not in ("default", "user") and not ev:
        errs.append(f"{path}: source {f['source']!r} cần ít nhất một evidence")
    if f["source"] == "ai_inference" and f["confidence"] == "high":
        errs.append(f"{path}: suy luận AI không được đánh dấu confidence=high")
    return errs


def walk_facts(profile: dict, prefix: str = ""):
    """Duyệt các Fact trong profile annotated: yield (đường_dẫn, fact)."""
    for k, v in profile.items():
        p = f"{prefix}{k}"
        if is_fact(v):
            yield p, v
        elif isinstance(v, dict) and k not in ("needs_user", "capabilities", "meta"):
            yield from walk_facts(v, p + ".")


def validate_annotated(profile: dict) -> list[str]:
    errs = []
    if profile.get("schema") != SCHEMA_VERSION:
        errs.append(f"schema phải là {SCHEMA_VERSION}")
    if not profile.get("engine"):
        errs.append("thiếu engine")
    if profile.get("status") not in ("candidate", "ready"):
        errs.append("status phải là candidate | ready")
    for path, f in walk_facts(profile):
        errs += validate_fact(path, f)
    for n in profile.get("needs_user", []):
        if not (isinstance(n, dict) and n.get("key") and n.get("reason")):
            errs.append(f"needs_user sai dạng: {n!r}")
    return errs


# ---------------------------------------------------------------- Capability
def normalize_capabilities(raw: dict | None) -> dict:
    """Capability đầy đủ trường (adapter cũ chỉ khai vài trường vẫn dùng được). Raise POLICY nếu sai kiểu."""
    raw = raw or {}
    out = {}
    for name, (typ, default) in CAPABILITY_FIELDS.items():
        v = raw.get(name, copy.deepcopy(default))
        if not isinstance(v, typ) or (typ is int and isinstance(v, bool)):
            raise StageError(ErrorClass.POLICY, "BAD_CAPABILITY", f"capability {name!r}={v!r} sai kiểu")
        out[name] = v
    extra = {k: v for k, v in raw.items() if k not in CAPABILITY_FIELDS}
    return {**out, **extra}


# ---------------------------------------------------------------- Profile hiệu lực
def _merge(a: dict, b: dict) -> dict:
    for k, v in b.items():
        a[k] = _merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return a


def _lang_ok(lang: str, supported: list[str]) -> bool:
    return not supported or any(lang.split("-")[0].lower() == s.split("-")[0].lower() for s in supported)


def resolve(raw: dict | None, caps: dict, language: str | None = None, engine: str | None = None) -> dict:
    """Profile FLAT hiệu lực = mặc định ⊕ raw (đã bóc Fact) kẹp theo capability. Raise POLICY nếu không thể dùng."""
    caps = normalize_capabilities(caps)
    r = unwrap(copy.deepcopy(raw or {}))
    flat = _merge(copy.deepcopy(DEFAULT_FLAT), {k: v for k, v in r.items()
                                                if k not in ("schema", "status", "needs_user", "capabilities", "meta")})
    if "max_chars" in r:                                    # dạng cũ Phase 1: {"max_chars": N, "pause_ms": {...}}
        flat["segment"]["max_chars"] = int(r["max_chars"])
    flat["engine"] = flat["engine"] or engine
    flat["language"] = flat["language"] or language or "vi"
    seg = flat["segment"]
    hard = caps["max_chars"]
    if hard:
        seg["max_chars"] = min(int(seg["max_chars"]), hard)
    seg["preferred_chars"] = min(int(seg["preferred_chars"]), int(seg["max_chars"]))
    seg["min_chars"] = min(int(seg["min_chars"]), seg["preferred_chars"])
    errs = []
    if seg["max_chars"] < 20:
        errs.append(f"segment.max_chars={seg['max_chars']} quá nhỏ")
    if not _lang_ok(flat["language"], caps["languages"]):
        errs.append(f"engine không hỗ trợ ngôn ngữ {flat['language']!r}; hỗ trợ: {caps['languages']}")
    if caps["requires_reference_audio"] and not (flat["settings"].get("reference_audio") or flat["voice"]):
        errs.append("engine cần reference audio (settings.reference_audio) hoặc voice")
    for k, v in flat["pause_ms"].items():
        if not isinstance(v, (int, float)) or v < 0:
            errs.append(f"pause_ms.{k}={v!r} không hợp lệ")
    if errs:
        raise StageError(ErrorClass.POLICY, "INVALID_TTS_PROFILE", "; ".join(errs), {"errors": errs})
    return flat


def cache_identity(flat: dict, caps: dict) -> dict:
    """Phần của profile/engine ảnh hưởng tới ÂM THANH của một segment (dùng cho cache key).
    KHÔNG gồm: ngắt đoạn, pause, retry, qa (không đổi file audio của một segment)."""
    caps = normalize_capabilities(caps)
    keys = caps["cache_settings"]
    settings = flat["settings"] if keys is None else {k: flat["settings"].get(k) for k in keys}
    return {"engine": flat["engine"], "engine_version": caps["engine_version"], "model": flat["model"],
            "voice": flat["voice"], "language": flat["language"], "settings": settings,
            "profile_version": flat["profile_version"]}


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def segment_key(text: str, identity: dict) -> str:
    return stable_hash({"text": text, "id": identity})


def new_annotated(engine: str, status: str = "candidate") -> dict:
    return {"schema": SCHEMA_VERSION, "engine": engine, "status": status, "profile_version": fact("1"),
            "needs_user": [], "meta": {}}
