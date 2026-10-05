"""Chính sách xử lý lỗi stage (D-37, D-40): retry có backoff, hay GIỮ job (hold), hay thất bại vĩnh viễn.

Hàm thuần, không đụng DB, nên test được bằng bảng. Ánh xạ (MODULE_CONTRACTS §11.4):

  TRANSIENT còn ngân sách            -> retry sau max(backoff có jitter, Retry-After)
  TRANSIENT, Retry-After quá lớn     -> hold (resume_after = now + Retry-After)
  TRANSIENT hết ngân sách + resource -> hold theo resource; không có resource -> FAILED_PERMANENT
  RESOURCE                           -> hold ngay theo resource
  AUTH                               -> PAUSED_CREDENTIAL
  POLICY + resource == "input"       -> PAUSED_MISSING_INPUT; còn lại FAILED_PERMANENT
  AMBIGUOUS                          -> FAILED_PERMANENT (cần người xác nhận)
  CANCELLED                          -> trả về hàng (xử lý ở runner)
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from ..contracts import ErrorClass, StageError
from . import pipeline as P

RESOURCE_TO_HOLD = {
    "network": P.PAUSED_NETWORK, "provider": P.PAUSED_NETWORK,
    "token": P.PAUSED_TOKEN, "quota": P.PAUSED_QUOTA, "disk": P.PAUSED_DISK,
    "runtime": P.PAUSED_RESOURCE, "credential": P.PAUSED_CREDENTIAL, "input": P.PAUSED_MISSING_INPUT,
}


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3                     # ngân sách retry của lớp TRANSIENT
    backoff_s: tuple[float, ...] = (2, 10, 60)   # dãy backoff gốc; phần tử cuối lặp lại
    jitter: float = 0.2                       # ±20%
    cap_s: float = 300.0                      # trần delay
    floor_s: float = 1.0                      # sàn delay (không retry dồn dập)
    retry_after_hold_threshold_s: float = 600.0   # Retry-After lớn hơn ngưỡng này thì GIỮ job thay vì ngủ trong hàng
    max_auto_resumes_without_progress: int = 5
    token_hold_default_s: float = 900.0       # hết token mà không biết thời điểm reset: thử lại sau
    quota_hold_default_s: float = 3600.0

    @staticmethod
    def from_config(r: dict) -> "RetryPolicy":
        d = RetryPolicy()
        return RetryPolicy(
            max_attempts=int(r.get("max_attempts", d.max_attempts)),
            backoff_s=tuple(r.get("backoff_s", d.backoff_s)),
            jitter=float(r.get("jitter", d.jitter)), cap_s=float(r.get("cap_s", d.cap_s)),
            floor_s=float(r.get("floor_s", d.floor_s)),
            retry_after_hold_threshold_s=float(r.get("retry_after_hold_threshold_s", d.retry_after_hold_threshold_s)),
            max_auto_resumes_without_progress=int(r.get("max_auto_resumes_without_progress",
                                                        d.max_auto_resumes_without_progress)),
            token_hold_default_s=float(r.get("token_hold_default_s", d.token_hold_default_s)),
            quota_hold_default_s=float(r.get("quota_hold_default_s", d.quota_hold_default_s)))

    def delay(self, used: int, retry_after_s: float | None = None, rand=random.random) -> float:
        """delay = clamp(backoff, floor, cap) ± jitter, rồi không bao giờ nhỏ hơn Retry-After của provider."""
        base = self.backoff_s[min(used, len(self.backoff_s) - 1)] if self.backoff_s else 0.0
        base = min(max(base, self.floor_s), self.cap_s)
        d = base * (1 + self.jitter * (2 * rand() - 1)) if self.jitter else base
        d = max(d, self.floor_s)
        return max(d, retry_after_s or 0.0)


@dataclass(frozen=True)
class Outcome:
    action: str                    # retry | hold | failed
    reason: str | None = None      # hold_reason khi action == hold
    delay: float = 0.0             # giây, khi action == retry
    resume_after: float | None = None   # epoch, khi action == hold
    note: str = ""


def hold_reason(err: StageError) -> str | None:
    """Lý do hold nếu `err` là lỗi tài nguyên tạm thời, ngược lại None."""
    if err.error_class == ErrorClass.AUTH:
        return P.PAUSED_CREDENTIAL
    if err.error_class == ErrorClass.RESOURCE:
        return RESOURCE_TO_HOLD.get(err.resource or "runtime", P.PAUSED_RESOURCE)
    if err.error_class == ErrorClass.POLICY and err.resource == "input":
        return P.PAUSED_MISSING_INPUT
    return None


def _resume_after(err: StageError, reason: str, policy: RetryPolicy, now: float) -> float | None:
    if err.resume_after:
        return err.resume_after
    if err.retry_after_s:
        return now + err.retry_after_s
    if reason == P.PAUSED_TOKEN:
        return now + policy.token_hold_default_s
    if reason == P.PAUSED_QUOTA:
        return now + policy.quota_hold_default_s
    return None                              # điều kiện hồi phục do Resource Monitor xác định


def outcome_for(err: StageError, used: int, policy: RetryPolicy, now: float, rand=random.random) -> Outcome:
    reason = hold_reason(err)
    if reason:
        return Outcome("hold", reason, resume_after=_resume_after(err, reason, policy, now), note=f"hold: {err.code}")
    if err.error_class == ErrorClass.TRANSIENT:
        too_long = err.retry_after_s is not None and err.retry_after_s > policy.retry_after_hold_threshold_s
        if too_long:                         # đợi quá lâu: không ngủ trong hàng đợi mà GIỮ job
            r = RESOURCE_TO_HOLD.get(err.resource or "network", P.PAUSED_NETWORK)
            return Outcome("hold", r, resume_after=now + err.retry_after_s, note=f"hold (Retry-After): {err.code}")
        if used + 1 < policy.max_attempts:
            d = policy.delay(used, err.retry_after_s, rand)
            return Outcome("retry", delay=d, note=f"retry in {d:.2f}s: {err.code}")
        if err.resource:                     # hết ngân sách nhưng nguyên nhân là tài nguyên => không phải lỗi vĩnh viễn
            r = RESOURCE_TO_HOLD.get(err.resource, P.PAUSED_RESOURCE)
            return Outcome("hold", r, resume_after=_resume_after(err, r, policy, now), note=f"hold (hết retry): {err.code}")
    return Outcome("failed", note=f"failed: {err.code}")
