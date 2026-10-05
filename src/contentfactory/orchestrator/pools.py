"""Source Sync chạy NỀN và DÙNG CHUNG (HANDOFF §13): đồng bộ pool video thô một lần, mọi job render tái dùng; chỉ làm lại khi nguồn/tùy chọn đổi.

Thread riêng (không chiếm slot của stage nào): lúc `Orchestrator.run()` bắt đầu rồi mỗi `render.pool_sync_interval_s` giây, với từng pool mà profile
YouTube/TikTok đang dùng, gọi `render.prepare_pool` (adapter tự bỏ qua nếu dấu vân tay không đổi). Lỗi chỉ ghi nhận (resource_status `pool:<tên>` + log),
không làm sập orchestrator; job render cần pool chưa sẵn sàng sẽ tự gọi `prepare_pool` (chờ nếu đang đồng bộ, hoặc báo lỗi có kiểu).
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from ..contracts import ErrorClass, StageError
from ..render import profile as PF


class PoolSyncService:
    def __init__(self, orc) -> None:
        self.orc = orc
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------------------------ pool nào cần đồng bộ
    def specs(self) -> dict[str, dict]:
        """Pool đang được profile dùng (suy ra size/fps mặc định từ profile). Profile/pool cấu hình sai bị bỏ qua và ghi log."""
        cfg = self.orc.cfg.data.get("render", {})
        out: dict[str, dict] = {}
        for pid in PF.DEFAULTS:
            try:
                prof = PF.resolve(pid, cfg, None)
                spec = PF.pool_spec(prof, cfg)
            except StageError as e:
                self.orc.log.emit("pool_spec_invalid", "warning", profile=pid, error=e.code, message=e.message)
                continue
            if spec and spec["name"] not in out:
                out[spec["name"]] = spec
        return out

    def enabled(self) -> bool:
        r = self.orc.adapters.get("render")
        rc = self.orc.cfg.data.get("render", {})
        return bool(r is not None and getattr(r, "requires_pool", False) and rc.get("pool_sync_background", True) and rc.get("pools"))

    # ------------------------------------------------------------------------------------------ đồng bộ một pool / tất cả
    def sync(self, name: str | None = None) -> dict[str, dict]:
        render = self.orc.adapters["render"]
        res: dict[str, dict] = {}
        for pname, spec in self.specs().items():
            if name and pname != name:
                continue
            with self._lock:
                t0 = time.time()
                try:
                    info = render.prepare_pool(spec, SimpleNamespace(cancel=self.orc.cancel, log=lambda *a, **k: None))
                    ok, detail = True, f"{info.get('files')} file, {'không đổi' if info.get('reused') else 'đã đồng bộ'}"
                    if not info.get("reused"):
                        self.orc.log.emit("pool_sync_done", pool=pname, files=info.get("files"), seconds=round(time.time() - t0, 1),
                                          failed=info.get("failed"))
                    res[pname] = info
                except StageError as e:
                    if e.error_class == ErrorClass.CANCELLED:
                        raise
                    ok, detail = False, f"{e.code}: {e.message}"[:300]
                    self.orc.log.emit("pool_sync_failed", "warning", pool=pname, error=e.code, message=e.message[:300])
                    res[pname] = {"error": e.code, "message": e.message}
            now = time.time()
            self.orc.store.put_resource_status(f"pool:{pname}", ok, detail, now,
                                               now + float(self.orc.cfg.data["render"].get("pool_sync_interval_s", 300)), None, 0 if ok else 1)
        return res

    # ------------------------------------------------------------------------------------------ nền
    def start(self) -> None:
        if self._thread or not self.enabled():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pool-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        interval = float(self.orc.cfg.data["render"].get("pool_sync_interval_s", 300))
        while not self._stop.is_set():
            try:
                self.sync()
            except StageError:                                     # CANCELLED khi orchestrator dừng
                return
            except Exception as e:                                 # noqa: BLE001 - nền không được làm sập tiến trình
                self.orc.log.emit("pool_sync_error", "error", error=repr(e))
            self._stop.wait(interval)
