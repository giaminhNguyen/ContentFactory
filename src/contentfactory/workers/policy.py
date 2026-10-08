"""Retry/fallback/cooldown policy cho routing (W1.7 + W1.9 + W1.10).

Một nơi duy nhất định nghĩa mặc định; routing config chỉ lưu phần KHÁC mặc định.
Router, manager, simulator cùng đọc từ đây — không spread hằng số.
"""
from __future__ import annotations

from .errors import WorkerErrorClass

# retry_on: số lần retry CÙNG worker cho mỗi loại lỗi (0 = không retry, chuyển worker kế ngay)
DEFAULT_POLICY: dict = {
    "max_total_attempts": 5,          # tổng số lần thử cho một task
    "max_distinct_workers": 3,        # số worker khác nhau tối đa được dùng
    "retry_on": {k.value: n for k, n in (
        (WorkerErrorClass.TEMPORARY, 2),
        (WorkerErrorClass.TIMEOUT, 1),
        (WorkerErrorClass.INVALID_OUTPUT, 1),
        (WorkerErrorClass.UNKNOWN, 1),
        (WorkerErrorClass.QUOTA, 0),
        (WorkerErrorClass.AUTH, 0),
    )},
    "cooldown_after": 3,              # N lỗi liên tiếp -> cooldown
    "cooldown_s": 300.0,
    "max_no_progress": 4,             # W2.6: cùng một lỗi lặp N lần trên một task không tiến triển -> dừng
}

_RETRY_KEYS = tuple(k.value for k in WorkerErrorClass)


def validate(cfg: dict | None) -> dict:
    """Chốt policy hợp lệ: khoá lạ bị chặn ngay (tránh gõ sai im lặng)."""
    cfg = dict(cfg or {})
    unknown = set(cfg) - set(DEFAULT_POLICY)
    if unknown:
        raise ValueError(f"policy có khoá lạ: {sorted(unknown)}; hợp lệ: {sorted(DEFAULT_POLICY)}")
    retry = dict(cfg.get("retry_on", {}))
    bad = set(retry) - set(_RETRY_KEYS)
    if bad:
        raise ValueError(f"retry_on có loại lỗi lạ: {sorted(bad)}; hợp lệ: {_RETRY_KEYS}")
    for k in ("max_total_attempts", "max_distinct_workers", "cooldown_after", "max_no_progress"):
        if k in cfg and int(cfg[k]) < 1:
            raise ValueError(f"{k} phải >= 1")
    if "cooldown_s" in cfg and float(cfg["cooldown_s"]) < 0:
        raise ValueError("cooldown_s phải >= 0")
    return merge(cfg)


def merge(cfg: dict | None) -> dict:
    """Chính sách đầy đủ = mặc định + phần người dùng cấu hình (retry_on gộp theo khoá)."""
    cfg = dict(cfg or {})
    out = dict(DEFAULT_POLICY)
    retry = dict(DEFAULT_POLICY["retry_on"])
    retry.update({k: int(v) for k, v in dict(cfg.get("retry_on", {})).items()})
    out.update({k: v for k, v in cfg.items() if k != "retry_on"})
    out["retry_on"] = retry
    return out


def retries_for(policy: dict, kind: str) -> int:
    """Số lần retry cùng worker cho một loại lỗi (đã merge)."""
    return int(policy.get("retry_on", {}).get(kind, DEFAULT_POLICY["retry_on"].get(kind, 0)))
