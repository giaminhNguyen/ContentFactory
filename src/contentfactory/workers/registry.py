"""Registry worker: quét CLI mới, quản lý vòng đời (thêm/sửa/probe/disable/xoá).

Nhận `WorkerStore` (đã mở sqlite riêng) + tập driver tiêm sẵn (test chèn driver giả).
Không biết cách pipeline dùng worker — phần đó ở `WorkerManager` (C4+).
"""
from __future__ import annotations

import time
from pathlib import Path

from . import policy as policy_mod
from .drivers import CLASSES, build as build_driver
from .drivers.base import BaseDriver
from .errors import PoolInUse, WorkerInUse
from .models import (
    MODEL_PROFILES,
    ExecutionTarget,
    PoolStrategy,
    Worker,
    WorkerModel,
    WorkerPool,
    WorkerStatus,
    new_id,
)
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

    def register_driver(self, driver_id: str, driver: BaseDriver) -> None:
        """Tiêm driver (test và driver tự đăng ký ngoài registry mặc định)."""
        self._drivers[driver_id] = driver

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

    # -- model config (W1.5) -----------------------------------------------------------------
    def set_models(self, worker_id: str, specs: list) -> Worker:
        """Đặt danh sách model của worker.

        `specs`: ["model-a", {"id": "model-b", "enabled": False}, {"id": "model-c", "default": True}]
        Exactly một default; profile trỏ model đã bị gỡ sẽ tự bỏ trống (không còn dangling).
        """
        w = self.get(worker_id)
        models: list[WorkerModel] = []
        seen: set[str] = set()
        default_id = ""
        for spec in specs:
            if isinstance(spec, WorkerModel):
                m = spec
            elif isinstance(spec, dict):
                m = WorkerModel(id=str(spec.get("id", "")).strip(), enabled=bool(spec.get("enabled", True)),
                                default=bool(spec.get("default", False)),
                                source=str(spec.get("source", "manual")))
            else:
                m = WorkerModel(id=str(spec).strip())
            if not m.id:
                raise ValueError("model id bắt buộc")
            if m.id in seen:
                raise ValueError(f"model trùng lặp: {m.id}")
            seen.add(m.id)
            if m.default:
                default_id = m.id
            models.append(m)
        if default_id:
            models = [WorkerModel(**{**m.__dict__, "default": m.id == default_id}) for m in models]
        known = {m.id for m in models}
        profiles = {k: v for k, v in w.profiles.items() if v and v in known}
        return self.store.save_worker(w.with_updates(models=models, profiles=profiles))

    def set_profiles(self, worker_id: str, mapping: dict) -> Worker:
        """Đặt ánh xạ profile -> model id của worker. Giá trị rỗng = bỏ qua profile đó."""
        w = self.get(worker_id)
        known = {m.id for m in w.models}
        out: dict = {}
        for key, value in (mapping or {}).items():
            if key not in MODEL_PROFILES:
                raise ValueError(f"profile lạ: {key!r}; hợp lệ: {MODEL_PROFILES}")
            value = str(value or "")
            if value and value not in known:
                raise ValueError(f"profile {key} trỏ model không có trong worker: {value}")
            out[key] = value
        return self.store.save_worker(w.with_updates(profiles=out))

    # -- pools (W1.6) -------------------------------------------------------------------------
    def pools(self) -> list[WorkerPool]:
        return self.store.pools()

    @staticmethod
    def _validate_members(members: list[str], store: WorkerStore) -> list[str]:
        have = {w.id for w in store.workers()}
        missing = [m for m in members if m not in have]
        if missing:
            raise ValueError(f"member không tồn tại (dangling): {missing}")
        if len(set(members)) != len(members):
            raise ValueError("member trùng lặp")
        return list(members)

    def create_pool(self, name: str, display_name: str = "", members: list[str] | None = None,
                    strategy: str = PoolStrategy.PRIORITY.value, enabled: bool = True) -> WorkerPool:
        name = (name or "").strip()
        if not name:
            raise ValueError("tên pool bắt buộc")
        if self.store.pool(name) is not None:
            raise ValueError(f"pool đã tồn tại: {name}")
        strat = PoolStrategy(strategy)
        p = WorkerPool(name=name, display_name=display_name or name, strategy=strat,
                       members=self._validate_members(list(members or []), self.store), enabled=enabled)
        return self.store.save_pool(p)

    def update_pool(self, name: str, **fields) -> WorkerPool:
        p = self.store.pool(name)
        if p is None:
            raise KeyError(f"không có pool {name!r}")
        new_name = str(fields.pop("new_name", "") or "").strip()
        allowed = {"display_name", "strategy", "members", "enabled"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"trường không cho sửa: {sorted(bad)}")
        if "strategy" in fields:
            fields["strategy"] = PoolStrategy(fields["strategy"])
        if "members" in fields:
            fields["members"] = self._validate_members(list(fields["members"]), self.store)
        if new_name and new_name != name:
            if self.store.pool(new_name) is not None:
                raise ValueError(f"pool đã tồn tại: {new_name}")
            renamed = p.with_updates(**fields, name=new_name)
            self.store.save_pool(renamed)
            self.store.delete_pool(name)
            return renamed
        return self.store.save_pool(p.with_updates(**fields))

    def delete_pool(self, name: str, force: bool = False) -> list[str]:
        """Xoá pool. Đang được work_type trỏ tới => nêu dependency; force=True xoá luôn dòng routing đó."""
        if self.store.pool(name) is None:
            raise KeyError(f"không có pool {name!r}")
        used = [wt for wt, cfg in self.store.routing().items() if cfg.get("pool") == name]
        if used and not force:
            raise PoolInUse(name, used)
        for wt in used:
            self.store.delete_routing(wt)
        self.store.delete_pool(name)
        return used

    # -- routing (W1.7) -----------------------------------------------------------------------
    def routing(self) -> dict[str, dict]:
        return self.store.routing()

    def set_routing(self, work_type: str, pool: str, model_profile: str = "",
                    policy: dict | None = None) -> dict:
        """work_type -> pool + model profile + retry/fallback policy. Đổi bằng config, không sửa code."""
        work_type = (work_type or "").strip()
        if not work_type:
            raise ValueError("work_type bắt buộc")
        if not self.store.pool(pool):
            raise ValueError(f"pool không tồn tại: {pool}")
        if model_profile and model_profile not in MODEL_PROFILES:
            raise ValueError(f"model_profile lạ: {model_profile!r}; hợp lệ: {MODEL_PROFILES}")
        return self.store.save_routing(work_type, {
            "pool": pool, "model_profile": model_profile,
            "policy": policy_mod.validate(policy or {}),
        })

    def delete_routing(self, work_type: str) -> bool:
        return self.store.delete_routing(work_type)

    # -- chọn worker cho một work_type (router) --------------------------------------------------
    def _routing_plan(self, work_type: str, now: float, exclude: tuple[str, ...] = ()
                      ) -> tuple[WorkerPool | None, list[tuple[Worker, str, str]], list[tuple[str, str, str]], str]:
        """Đi bộ routing giống hệt `pick`; trả (pool, eligible, blocked, lý do).

        blocked: (worker_id, tên, lý do). Dùng chung cho router thật và simulator để sim không lệch router.
        """
        routing = self.store.routing().get(work_type)
        if routing is None:
            return None, [], [], f"chưa cấu hình routing cho {work_type}"
        pool_name = routing.get("pool") or ""
        if not pool_name:
            return None, [], [], f"{work_type} chưa gán pool"
        pool = self.store.pool(pool_name)
        if pool is None:
            return None, [], [], f"pool {pool_name!r} không tồn tại (config trỏ vào pool đã xoá)"
        if not pool.enabled:
            return None, [], [], f"pool {pool_name!r} đang tắt"
        if not pool.members:
            return None, [], [], f"pool {pool_name!r} rỗng — không có worker nào để route"
        profile = routing.get("model_profile") or None
        workers = {w.id: w for w in self.store.workers()}
        running = self.store.running_counts() if pool.strategy is PoolStrategy.LEAST_BUSY else {}

        eligible: list[tuple[Worker, str, str]] = []       # (worker, model, lý do)
        blocked: list[tuple[str, str, str]] = []           # (worker_id, tên, lý do)
        excluded = set(exclude)
        for wid in pool.members:
            w = workers.get(wid)
            if w is None:
                blocked.append((wid, wid, "không còn tồn tại (dangling member)"))
                continue
            if wid in excluded:
                blocked.append((wid, w.name, "đã lỗi trong lần thử này"))
                continue
            ok, reason = w.routable(now)
            if not ok:
                blocked.append((wid, w.name, reason))
                continue
            model = w.model_for(profile)
            if model is None:
                blocked.append((wid, w.name, "chưa cấu hình model"))
                continue
            eligible.append((w, model, reason))
        if pool.strategy is PoolStrategy.LEAST_BUSY:
            eligible.sort(key=lambda t: (running.get(t[0].id, 0), pool.members.index(t[0].id)))
        return pool, eligible, blocked, ""

    def pick(self, work_type: str, now: float | None = None,
             exclude: tuple[str, ...] = ()) -> tuple[ExecutionTarget | None, str]:
        """Chọn worker cho work_type theo config. Luôn trả lý do (dùng cho simulator + log).

        `exclude`: worker đã fail trong lần thử này (fallback không quay lại worker vừa hỏng).
        """
        now = time.time() if now is None else now
        pool, eligible, blocked, why = self._routing_plan(work_type, now, tuple(exclude))
        if why:
            return None, why
        if not eligible:
            return None, "không worker nào đủ điều kiện: " + "; ".join(b[2] for b in blocked)
        profile = (self.store.routing().get(work_type) or {}).get("model_profile") or None
        if pool.strategy is PoolStrategy.LEAST_BUSY:
            running = self.store.running_counts()
            why = f"least_busy: {eligible[0][0].name} ({running.get(eligible[0][0].id, 0)} attempt đang chạy)"
        else:
            why = f"priority #{pool.members.index(eligible[0][0].id) + 1}: {eligible[0][0].name}"
        if profile:
            why += f", profile {profile}"
        if blocked:
            why += f" (bỏ qua: {'; '.join(f'{b[1]} — {b[2]}' for b in blocked)})"
        return ExecutionTarget(worker=eligible[0][0], model=eligible[0][1], pool=pool.name, reason=why), why

    def simulate(self, work_type: str, now: float | None = None) -> dict:
        """Simulator W1.UI.6: chạy đúng routing logic (không gọi model), trả thứ tự dùng + lý do loại."""
        now = time.time() if now is None else now
        pool, eligible, blocked, why = self._routing_plan(work_type, now)
        rows: list[dict] = []
        for i, (w, model, _reason) in enumerate(eligible):
            rows.append({
                "worker_id": w.id, "name": w.name, "driver_id": w.driver_id,
                "status": w.status.value, "model": model, "pool": pool.name,
                "rank": i + 1, "role": "selected" if i == 0 else f"backup#{i}",
                "blocked_reason": None,
            })
        for wid, name, reason in blocked:
            w = self.store.worker(wid)
            rows.append({
                "worker_id": wid, "name": name, "driver_id": w.driver_id if w else "",
                "status": w.status.value if w else "", "model": None,
                "pool": pool.name if pool else None, "rank": None, "role": "excluded",
                "blocked_reason": reason,
            })
        return {"work_type": work_type, "pool": pool.name if pool else None,
                "ok": bool(eligible) and not why, "reason": why or ("; ".join(r[2] for r in blocked) if not eligible else ""),
                "rows": rows}
