"""Config snapshot theo job (HANDOFF §15C, D-41): job chụp lại CẤU HÌNH NGỮ NGHĨA lúc bắt đầu.

Đổi config global không được âm thầm đổi job đang chạy; đổi config của job chỉ bằng hành động explicit (`apply_patch`,
ghi revision). KHÔNG snapshot: đường dẫn máy, giới hạn đồng thời, lease/heartbeat (cấu hình của máy chạy) và secrets.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time

from .config import Config, _merge

# Cấu hình có ý nghĩa với KẾT QUẢ của job (adapter/provider, dựng câu, story, retry, mẫu output).
SEMANTIC_KEYS = ("adapters", "adapter_config", "source", "supervip", "youtube", "story_branch", "retry", "output")
# Phần quyết định adapter nào được dựng cho job (dùng để biết có tái dùng bộ adapter mặc định của orchestrator không).
ADAPTER_KEYS = ("adapters", "adapter_config", "source", "supervip", "youtube", "story_branch", "output")
_SECRET = re.compile(r"(secret|password|passwd|token|api[_-]?key|credential)", re.I)
REDACTED = "***REDACTED***"


def redact(obj):
    """Giá trị của khóa trông như secret (và không phải tên biến môi trường `*_env`) bị che: secret không vào snapshot."""
    if isinstance(obj, dict):
        return {k: (REDACTED if _SECRET.search(k) and not k.endswith("_env") and isinstance(v, str) and v else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def config_hash(semantic: dict) -> str:
    return _hash(semantic)


def adapters_hash(semantic: dict) -> str:
    return _hash({k: semantic.get(k) for k in ADAPTER_KEYS})


def build_snapshot(cfg: Config, *, auto_resume: bool, start_stage: str | None, target_stage: str | None,
                   now: float | None = None) -> dict:
    semantic = redact({k: copy.deepcopy(cfg.data[k]) for k in SEMANTIC_KEYS if k in cfg.data})
    return {"semantic": semantic, "auto_resume": auto_resume, "start_stage": start_stage, "target_stage": target_stage,
            "created_at": now or time.time()}


def apply_patch(snapshot: dict, patch: dict) -> dict:
    """Trả snapshot mới với `patch` (gộp sâu) áp lên phần ngữ nghĩa. Khóa ngoài SEMANTIC_KEYS bị từ chối."""
    bad = sorted(set(patch) - set(SEMANTIC_KEYS))
    if bad:
        raise ValueError(f"chỉ đổi được config ngữ nghĩa {list(SEMANTIC_KEYS)}; không hợp lệ: {bad}")
    out = copy.deepcopy(snapshot)
    _merge(out["semantic"], redact(copy.deepcopy(patch)))
    return out


def effective_config(base: Config, snapshot: dict) -> Config:
    """Config dùng để dựng adapter/ctx cho một job: phần máy lấy từ `base`, phần ngữ nghĩa lấy từ snapshot."""
    data = copy.deepcopy(base.data)
    data.update(copy.deepcopy(snapshot["semantic"]))
    return Config(base.root, data)
