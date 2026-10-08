"""Facade Worker Runtime cho CLI / doctor / webui (W1.15,W1.16): mọi đọc-ghi worker đi qua đây.

UI và lệnh CLI dùng chung một bộ JSON qua `worker_dump`/`attempt_dump`, không phải biết sqlite hay driver.
Không leo thang quyền: không có logic account-rotation/token-scraping; attempt không lộ session/token value.
"""
from __future__ import annotations

import time

from ..workers.drivers import CLASSES
from ..workers.models import MODEL_PROFILES
from ..workers.registry import WorkerRegistry
from ..workers.store import WorkerStore

REAL_DRIVERS = ("claude_cli", "codex_cli", "gemini_cli", "opencode_cli")


def worker_dump(w) -> dict:
    ok, reason = w.routable()
    health_state, health_reason = w.health()
    return {
        "id": w.id, "name": w.name, "driver_id": w.driver_id, "executable": w.executable,
        "status": w.status.value, "enabled": w.enabled, "source": w.source,
        "models": [m.id for m in w.models], "default_model": w.default_model,
        "profiles": dict(w.profiles), "concurrency": w.concurrency, "timeout_s": w.timeout_s,
        "idle_timeout_s": w.idle_timeout_s, "hard_timeout_s": w.hard_timeout_s,
        "failure_streak": w.failure_streak, "cooldown_s": round(max(0.0, w.cooldown_until - time.time()), 1),
        "circuit": w.circuit().value, "circuit_opened_at": w.circuit_opened_at,
        "health": health_state.value, "health_reason": health_reason,
        "last_error": {"kind": w.last_error_kind, "code": w.last_error_code, "at": w.last_error_at}
            if w.last_error_at else None,
        "last_success_at": w.last_success_at,
        "routable": {"ok": ok, "reason": reason},
        "version": w.meta.get("version") or "", "auth": w.meta.get("auth") or "unknown",
        "detail": w.meta.get("detail") or "",
    }


def pool_dump(p) -> dict:
    return {"name": p.name, "display_name": p.display_name, "strategy": p.strategy.value,
            "members": list(p.members), "enabled": p.enabled}


def attempt_dump(a) -> dict:
    return {
        "attempt_id": a.attempt_id, "work_type": a.work_type, "worker_id": a.worker_id,
        "worker_name": a.worker_name, "driver_id": a.driver_id, "model_id": a.model_id, "pool": a.pool,
        "state": a.state.value, "started_at": a.started_at, "ended_at": a.ended_at,
        "duration_s": round(a.duration_s, 2), "error_kind": a.error_kind, "error_code": a.error_code,
        "error_message": a.error_message, "promoted": a.promoted,
        "session": bool(a.session_id),                                  # không bao giờ lộ session/token value
        "cost_usd": round(a.cost_usd, 4), "validation": list(a.validation), "workspace": a.workspace,
    }


class WorkerService:
    """Các phương thức trả dữ liệu JSON-able; nghiệp vụ nằm ở workers/*, đây chỉ là lớp dán + che secret."""

    def __init__(self, cfg, registry: WorkerRegistry | None = None) -> None:
        self.cfg = cfg
        if registry is None:
            store = WorkerStore(cfg.path("runtime") / "workers.db")
            registry = WorkerRegistry(store)
            for d in REAL_DRIVERS:
                registry.register_driver(d, CLASSES[d]())
        self.registry = registry

    @property
    def store(self) -> WorkerStore:
        return self.registry.store

    # -- danh sách / drivers --------------------------------------------------------------------
    def drivers(self) -> list[dict]:
        return [{"id": c.id, "label": c.label, "exe_names": list(c.exe_names)} for c in CLASSES.values()]

    def workers(self) -> list[dict]:
        return [worker_dump(w) for w in self.registry.list()]

    def scan(self) -> dict:
        r = self.registry.scan()
        return {**r, "workers": self.workers()}

    def probe(self, worker_id: str) -> dict:
        return worker_dump(self.registry.probe(worker_id))

    def probe_all(self) -> list[dict]:
        """Probe lại mọi worker, từng cái bọc lỗi — MỘT worker hỏng không làm hỏng báo cáo (doctor)."""
        out = []
        for w in self.registry.list():
            try:
                out.append(self.probe(w.id))
            except Exception as e:                                       # noqa: BLE001
                d = worker_dump(w)
                d["status"] = "BROKEN"
                d["probe_error"] = str(e)
                out.append(d)
        return out

    # -- thêm / sửa / xoá worker -----------------------------------------------------------------
    def add(self, name: str, driver_id: str, executable: str, models: list[str] | None = None,
            probe: bool = True) -> dict:
        return worker_dump(self.registry.add(name, driver_id, executable, models=models, probe=probe))

    def update(self, worker_id: str, **fields) -> dict:
        return worker_dump(self.registry.update(worker_id, **fields))

    def set_enabled(self, worker_id: str, enabled: bool) -> dict:
        return worker_dump(self.registry.set_enabled(worker_id, enabled))

    def remove(self, worker_id: str, force: bool = False) -> dict:
        return {"removed": worker_id, "pools": self.registry.remove(worker_id, force=force)}

    # -- pools -----------------------------------------------------------------------------------
    def pools(self) -> list[dict]:
        return [pool_dump(p) for p in self.registry.pools()]

    def create_pool(self, name: str, display_name: str = "", members: list[str] | None = None,
                    strategy: str = "priority", enabled: bool = True) -> dict:
        return pool_dump(self.registry.create_pool(name, display_name, members, strategy=strategy, enabled=enabled))

    def update_pool(self, name: str, **fields) -> dict:
        return pool_dump(self.registry.update_pool(name, **fields))

    def delete_pool(self, name: str, force: bool = False) -> dict:
        return {"deleted": name, "routing_removed": self.registry.delete_pool(name, force=force)}

    # -- routing ---------------------------------------------------------------------------------
    def routing(self) -> dict:
        return {"routing": self.registry.routing(), "profiles": list(MODEL_PROFILES),
                "pools": [p["name"] for p in self.pools()]}

    def set_routing(self, work_type: str, pool: str, model_profile: str = "", policy: dict | None = None) -> dict:
        return self.registry.set_routing(work_type, pool, model_profile, policy)

    def delete_routing(self, work_type: str) -> bool:
        return self.registry.delete_routing(work_type)

    def pick(self, work_type: str, exclude: tuple[str, ...] = ()) -> dict:
        target, why = self.registry.pick(work_type, exclude=tuple(exclude))
        return {"ok": target is not None, "reason": why,
                "target": None if target is None else {
                    "worker": worker_dump(target.worker), "model": target.model, "pool": target.pool}}

    def simulate(self, work_type: str) -> dict:
        """Simulator W1.UI.6: routing logic thuần (không gọi model thật), kèm lý do loại từng worker."""
        return self.registry.simulate(work_type)

    # -- attempts (timeline) ----------------------------------------------------------------------
    def attempts(self, job_id: str = "", work_type: str = "", stage: str = "",
                 worker_id: str = "", limit: int = 100) -> list[dict]:
        atts = self.store.attempts(job_id=job_id, work_type=work_type, limit=limit)
        if worker_id:
            atts = [a for a in atts if a.worker_id == worker_id]
        if stage:
            atts = [a for a in atts if a.stage == stage]
        return [attempt_dump(a) for a in atts]

    # -- W2.7 metrics ----------------------------------------------------------------------------
    def stats(self) -> dict:
        return {"workers": self.store.worker_stats(), "tasks": self.store.attempts_per_task()}

    # -- W2.UI.4 health summary -------------------------------------------------------------------
    def summary(self) -> dict:
        """Tổng hợp một dòng cho Health Center: worker khoẻ/lỗi + circuit đang mở."""
        counts: dict[str, int] = {}
        circuits: list[dict] = []
        quota: list[str] = []
        auth: list[str] = []
        for w in self.registry.list():
            state, reason = w.health()
            counts[state.value] = counts.get(state.value, 0) + 1
            if w.circuit().value in ("OPEN", "HALF_OPEN"):
                circuits.append({"worker_id": w.id, "name": w.name,
                                 "state": w.circuit().value, "reason": reason})
            if state.value == "QUOTA_BLOCKED":
                quota.append(w.name)
            if state.value == "AUTH_BLOCKED":
                auth.append(w.name)
        workers = self.registry.list()
        healthy = sum(1 for w in workers if not w.enabled or w.health()[0] is not None)  # đếm riêng
        return {"counts": counts,
                "healthy": sum(1 for w in workers if w.health()[0].value == "HEALTHY"),
                "total": len(workers), "circuits": circuits,
                "quota_blocked": quota, "auth_blocked": auth}

    # -- W2.UI.3 impact preview -------------------------------------------------------------------
    def impact(self, worker_id: str) -> dict:
        """Hệ quả nếu disable/xoá worker này: pool liên quan, work types bị ảnh hưởng, task đang chạy."""
        pools = [p.name for p in self.registry.store.pools() if worker_id in p.members]
        running = sum(1 for a in self.registry.store.running_attempts() if a.worker_id == worker_id)
        work_types = sorted(wt for wt, cfg in self.registry.routing().items() if cfg.get("pool") in pools)
        return {"worker_id": worker_id, "pools": pools, "work_types": work_types, "running": running}

    def pool_impact(self, name: str) -> dict:
        """Hệ quả nếu xoá pool: work types trỏ vào + attempt đang chạy trên worker của pool."""
        work_types = sorted(wt for wt, cfg in self.registry.routing().items() if cfg.get("pool") == name)
        p = self.registry.store.pool(name)
        running = sum(1 for a in self.registry.store.running_attempts()
                      if p is not None and a.worker_id in p.members)
        return {"pool": name, "work_types": work_types, "running": running}

    # -- W2.UI.1 worker detail --------------------------------------------------------------------
    def worker_detail(self, worker_id: str) -> dict:
        w = self.registry.get(worker_id)
        wd = worker_dump(w)
        wd["health_history"] = self.store.health_history(worker_id)
        stats = self.store.worker_stats().get(worker_id, {})
        wd["stats"] = stats
        wd["recent_attempts"] = [attempt_dump(a) for a in
                                 self.store.attempts(work_type="", limit=50)
                                 if a.worker_id == worker_id][:10]
        return wd