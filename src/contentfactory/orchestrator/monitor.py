"""Resource Monitor (HANDOFF §15B, D-38): kiểm tra TẤT ĐỊNH (không AI) xem tài nguyên đang chặn một job đã hồi phục chưa.

Framework gồm:
  - `ResourceProbe` (contracts): một kiểm tra có timeout trả (ok, chi tiết). Có sẵn: DiskProbe, NetworkProbe, CallableProbe;
    probe riêng của provider (quota/rate-limit, GPU, credential) đăng ký thêm mà không sửa core.
  - cooldown: mỗi resource có `next_check_at`; hỏng liên tiếp thì giãn dần (base * 2^n, trần max_s); không polling dày.
  - hold theo thời gian (PAUSED_TOKEN/QUOTA): hồi phục khi tới `resume_after` (Retry-After / thời điểm reset do provider báo);
    KHÔNG đoán mức quota/token còn lại.
  - PAUSED_RESOURCE/CREDENTIAL không có probe riêng thì dùng `health()` của các adapter mà stage bị giữ dùng.
Monitor chỉ CẬP NHẬT TRẠNG THÁI và trả lời `ready`; việc đưa job vào hàng đợi thuộc Auto Resume (runner).
"""
from __future__ import annotations

import shutil
import socket
import time
from pathlib import Path
from typing import Callable

from ..contracts import ErrorClass, ResourceProbe, StageError
from ..jobs import pipeline as P
from ..jobs.db import JobStore

REASON_RESOURCE = {P.PAUSED_NETWORK: "network", P.PAUSED_DISK: "disk", P.PAUSED_RESOURCE: "runtime",
                   P.PAUSED_CREDENTIAL: "credential"}
GB = 2 ** 30


class DiskProbe:
    resource = "disk"

    def __init__(self, path: Path, min_free_bytes: int, usage: Callable = shutil.disk_usage) -> None:
        self.path, self.min_free, self.usage = Path(path), int(min_free_bytes), usage

    def check(self) -> tuple[bool, str]:
        free = self.usage(self.path if self.path.exists() else self.path.anchor or ".").free
        return free >= self.min_free, f"trống {free / GB:.2f} GB, cần ≥ {self.min_free / GB:.2f} GB"


class NetworkProbe:
    resource = "network"

    def __init__(self, hosts: list[tuple[str, int]], timeout: float = 3.0, connect: Callable = socket.create_connection) -> None:
        self.hosts, self.timeout, self.connect = hosts, timeout, connect

    def check(self) -> tuple[bool, str]:
        errs = []
        for host, port in self.hosts:
            try:
                self.connect((host, port), timeout=self.timeout).close()
                return True, f"kết nối được {host}:{port}"
            except OSError as e:
                errs.append(f"{host}:{port} {type(e).__name__}")
        return False, "không kết nối được: " + ", ".join(errs)


class CallableProbe:
    def __init__(self, resource: str, fn: Callable[[], tuple[bool, str] | bool]) -> None:
        self.resource, self.fn = resource, fn

    def check(self) -> tuple[bool, str]:
        r = self.fn()
        return (r, "") if isinstance(r, bool) else r


class ResourceMonitor:
    def __init__(self, store: JobStore, probes: dict[str, ResourceProbe] | None = None, *, base_s: float = 30.0,
                 max_s: float = 300.0, clock: Callable[[], float] = time.time,
                 disk: DiskProbe | None = None, disk_min_free_gb: dict | None = None,
                 adapters_health: Callable[[P.Stage], tuple[bool, str]] | None = None,
                 input_check: Callable[[dict], tuple[bool, str]] | None = None) -> None:
        self.store, self.probes = store, dict(probes or {})
        self.base_s, self.max_s, self.clock = base_s, max_s, clock
        self.disk, self.disk_min_free_gb = disk, disk_min_free_gb or {"default": 0.5}
        self.adapters_health, self.input_check = adapters_health, input_check

    # -- đo --------------------------------------------------------------------------------
    def check(self, resource: str, force: bool = False) -> dict | None:
        """Chạy probe của `resource` nếu đã tới hạn (hoặc `force`), ghi kết quả; trả trạng thái mới nhất hoặc None nếu không có probe."""
        probe = self.probes.get(resource)
        if probe is None:
            return None
        now = self.clock()
        prev = self.store.get_resource_status(resource)
        if prev and not force and now < prev["next_check_at"]:
            return prev                                                  # chưa tới hạn: không đo dồn dập
        try:
            ok, detail = probe.check()
        except Exception as e:                                           # probe hỏng không được làm sập scheduler
            ok, detail = False, f"probe lỗi: {e!r}"
        failures = 0 if ok else (prev["failures"] + 1 if prev else 1)
        interval = self.base_s if ok else min(self.max_s, self.base_s * 2 ** min(failures - 1, 30))      # chặn số mũ: outage kéo dài hàng ngày không được làm tràn số float và sập vòng lặp
        self.store.put_resource_status(resource, ok, detail, now, now + interval, None, failures)
        return self.store.get_resource_status(resource)

    def statuses(self) -> list[dict]:
        return self.store.list_resource_status()

    # -- trả lời "job bị giữ này chạy lại được chưa?" ---------------------------------------
    def ready(self, job: dict, force: bool = False) -> tuple[bool, str]:
        reason, now = job["hold_reason"], self.clock()
        if reason in P.TIME_BASED_HOLDS:
            after = job.get("resume_after")
            if after and now < after:
                return False, f"chờ tới thời điểm reset ({after - now:.0f}s nữa)"
            probe = self.probes.get("quota" if reason == P.PAUSED_QUOTA else "token")
            if probe is not None:                                        # provider cung cấp tín hiệu thì dùng thêm
                st = self.check(probe.resource, force)
                if st and not st["ok"]:
                    return False, st["detail"]
            return True, "đã tới thời điểm reset"
        if reason == P.PAUSED_MISSING_INPUT:
            return self.input_check(job) if self.input_check else (False, "không có bộ kiểm tra input")
        if reason == P.PAUSED_DISK and self.disk is not None:
            return self._disk_ready(job, now)
        resource = REASON_RESOURCE.get(reason)
        st = self.check(resource, force) if resource else None
        if st is not None:
            return st["ok"], st["detail"]
        if reason in (P.PAUSED_RESOURCE, P.PAUSED_CREDENTIAL) and self.adapters_health:
            stage = P.BY_QUEUE.get(job["state"])
            if stage is not None:
                return self.adapters_health(stage)
        # không có gì để đo: cho thử lại một lần sau `base_s`, không hơn (không polling dày)
        waited = now - (job.get("hold_since") or now)
        return waited >= self.base_s, f"không có probe; đã chờ {waited:.0f}s"

    # -- đĩa: ngưỡng theo stage ----------------------------------------------------------------
    def _disk_need(self, stage_name: str) -> int:
        return int(float(self.disk_min_free_gb.get(stage_name, self.disk_min_free_gb.get("default", 0.5))) * GB)

    def _disk_free(self) -> int:
        return self.disk.usage(self.disk.path if self.disk.path.exists() else self.disk.path.anchor or ".").free

    def _disk_ready(self, job: dict, now: float) -> tuple[bool, str]:
        stage = P.BY_QUEUE.get(job["state"])
        need, free = self._disk_need(stage.name if stage else "default"), self._disk_free()
        ok, detail = free >= need, f"trống {free / GB:.2f} GB, stage cần ≥ {need / GB:.2f} GB"
        prev = self.store.get_resource_status("disk")
        self.store.put_resource_status("disk", ok, detail, now, now + self.base_s, None, 0 if ok else (prev["failures"] + 1 if prev else 1))
        return ok, detail

    # -- kiểm tra trước khi chạy stage -------------------------------------------------------
    def preflight(self, stage: P.Stage) -> None:
        """Đủ chỗ trống đĩa cho stage chưa? Thiếu => lỗi TÀI NGUYÊN (job bị giữ PAUSED_DISK, không phải FAILED)."""
        if self.disk is None:
            return
        need, free = self._disk_need(stage.name), self._disk_free()
        if free < need:
            raise StageError(ErrorClass.RESOURCE, "DISK_LOW", f"trống {free / GB:.2f} GB, stage {stage.name} cần ≥ {need / GB:.2f} GB",
                             resource="disk")
