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
    return {
        "id": w.id, "name": w.name, "driver_id": w.driver_id, "executable": w.executable,
        "status": w.status.value, "enabled": w.enabled, "source": w.source,
        "models": [m.id for m in w.models], "default_model": w.default_model,
        "profiles": dict(w.profiles), "concurrency": w.concurrency, "timeout_s": w.timeout_s,
        "failure_streak": w.failure_streak, "cooldown_s": round(max(0.0, w.cooldown_until - time.time()), 1),
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

    # -- attempts (timeline) ----------------------------------------------------------------------
    def attempts(self, job_id: str = "", work_type: str = "", stage: str = "", limit: int = 100) -> list[dict]:
        atts = self.store.attempts(job_id=job_id, work_type=work_type, limit=limit)
        if stage:
            atts = [a for a in atts if a.stage == stage]
        return [attempt_dump(a) for a in atts]