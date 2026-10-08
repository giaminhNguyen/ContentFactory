"""Registry worker: quét CLI mới, quản lý vòng đời (thêm/sửa/probe/disable/xoá).

Nhận `WorkerStore` (đã mở sqlite riêng) + tập driver tiêm sẵn (test chèn driver giả).
Không biết cách pipeline dùng worker — phần đó ở `WorkerManager` (C4+).
"""
from __future__ import annotations

import time
from pathlib import Path

from .drivers import CLASSES, build as build_driver
from .drivers.base import BaseDriver
from .errors import WorkerInUse
from .models import Worker, WorkerModel, WorkerStatus, new_id
from .store import WorkerStore

DISABLED = WorkerStatus.DISABLED


class WorkerRegistry:
    def __init__(self, store: WorkerStore, drivers: dict[str, BaseDriver] | None = None) -> None:
        self.store = store
        self._drivers: dict[str, BaseDriver] = dict(drivers or {})

    # -- driver ----------------------------------------------------------------------------
    def driver(self, driver_id: str) -> BaseDriver:
        if driver_id not in self._drivers:
            self._drivers[driver_id] = build_driver(driver_id)
        return self._drivers[driver_id]

    def driver_ids(self) -> list[str]:
        return sorted(set(CLASSES) | set(self._drivers))

    # -- đọc --------------------------------------------------------------------------------
    def get(self, worker_id: str) -> Worker:
        w = self.store.worker(worker_id)
        if w is None:
            raise KeyError(f"không có worker {worker_id!r}")
        return w

    def list(self) -> list[Worker]:
        return self.store.workers()

    # -- thêm / sửa --------------------------------------------------------------------------
    @staticmethod
    def _normalize_executable(executable: str) -> str:
        """So sánh executable không phân biệt hoa/thường + dấu phân cách (Windows)."""
        return (executable or "").strip().replace("\\", "/").lower()

    def add(self, name: str, driver_id: str, executable: str, *, source: str = "manual",
            models: list[str] | None = None, probe: bool = True) -> Worker:
        if driver_id not in self.driver_ids():
            raise ValueError(f"driver {driver_id!r} không tồn tại; hợp lệ: {self.driver_ids()}")
        if not (executable or "").strip():
            raise ValueError("executable bắt buộc")
        norm = self._normalize_executable(executable)
        for w in self.store.workers():
            if w.driver_id == driver_id and self._normalize_executable(w.executable) == norm:
                raise ValueError(f"worker đã tồn tại cho executable này: {w.id} ({w.name})")
        w = Worker(
            id=new_id("wkr"), name=name.strip() or f"{driver_id}:{Path(executable).name}",
            driver_id=driver_id, executable=executable.strip(), source=source,
            models=[WorkerModel(id=m, source="manual") for m in (models or [])],
        )
        if probe:
            self.store.save_worker(w)
            w = self.probe(w.id)
        return self.store.save_worker(w)

    def update(self, worker_id: str, **fields) -> Worker:
        w = self.get(worker_id)
        allowed = {"name", "executable", "enabled", "models", "profiles", "concurrency",
                   "timeout_s", "driver_id"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"trường không cho sửa: {sorted(bad)}")
        if "models" in fields and fields["models"] and isinstance(fields["models"][0], str):
            fields["models"] = [WorkerModel(id=m, source="manual") for m in fields["models"]]
        return self.store.save_worker(w.with_updates(**fields))

    def set_enabled(self, worker_id: str, enabled: bool) -> Worker:
        w = self.get(worker_id)
        w = w.with_updates(enabled=enabled,
                           status=(DISABLED if not enabled else WorkerStatus.DETECTED))
        return self.store.save_worker(w)

    # -- probe -------------------------------------------------------------------------------
    def probe(self, worker_id: str) -> Worker:
        """Chạy probe an toàn của driver, cập nhật status/version/models. Không xoá lịch sử khi CLI mất."""
        w = self.get(worker_id)
        r = self.driver(w.driver_id).probe(w.executable)
        status = r.status if w.enabled else DISABLED
        meta = dict(w.meta)
        meta.update({"version": r.version, "auth": r.auth, "detail": r.detail})
        meta.update(r.meta)
        models = w.models
        if r.models and {m.id for m in models} != set(r.models):
            known = {m.id: m for m in models}
            models = [known.get(m, WorkerModel(id=m, source="driver")) for m in r.models]
        return self.store.save_worker(w.with_updates(status=status, executable=r.executable or w.executable,
                                                     models=models, meta=meta))

    # -- discovery ----------------------------------------------------------------------------
    def scan(self, probe: bool = True) -> dict:
        """Quét mọi driver tìm CLI trên PATH. Không cần restart: worker mới được thêm ngay.

        Trả về thống kê cho UI: thêm mấy worker, driver nào thấy/mất.
        """
        found: dict[str, list] = {}
        missing: list[str] = []
        added: list[str] = []
        existing: list[str] = []
        for driver_id in self.driver_ids():
            try:
                hits = self.driver(driver_id).discover()
            except Exception:                          # driver hỏng không được làm hỏng cả lần quét
                found[driver_id] = []
                missing.append(driver_id)
                continue
            found[driver_id] = [h.executable for h in hits]
            if not hits:
                missing.append(driver_id)
            for hit in hits:
                norm = self._normalize_executable(hit.executable)
                dup = any(w.driver_id == driver_id and self._normalize_executable(w.executable) == norm
                          for w in self.store.workers())
                if dup:
                    existing.append(driver_id)
                    continue
                w = Worker(id=new_id("wkr"), name=f"{driver_id}:{Path(hit.executable).name}",
                           driver_id=driver_id, executable=hit.executable, source="scan")
                w = self.store.save_worker(w)
                added.append(w.id)
                if probe:
                    self.probe(w.id)
        return {"added": added, "existing": existing, "missing": missing, "found": found,
                "scanned_at": time.time()}

    # -- xoá -----------------------------------------------------------------------------------
    def pools_using(self, worker_id: str) -> list[str]:
        return [p.name for p in self.store.pools() if worker_id in p.members]

    def remove(self, worker_id: str, force: bool = False) -> list[str]:
        """Xoá worker. Đang trong pool => nêu dependency, force=True sẽ gỡ khỏi các pool đó trước."""
        pools = self.pools_using(worker_id)
        if pools and not force:
            raise WorkerInUse(worker_id, pools)
        if pools:
            for p in self.store.pools():
                if worker_id in p.members:
                    self.store.save_pool(p.with_updates(members=[m for m in p.members if m != worker_id]))
        self.store.delete_worker(worker_id)
        return pools
