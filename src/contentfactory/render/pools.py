"""Source pool: thư mục video thô -> thư mục đã đồng bộ (H.264/yuv420p/CFR, cùng kích thước) bằng Source Sync của ContentFlow.

Đây là bước CHUẨN BỊ DÙNG CHUNG, không thuộc job nào: kết quả nằm ở `runtime/pools/<tên>/synced` và được mọi job render tái dùng. Chỉ làm lại khi
nguồn hoặc tùy chọn đồng bộ đổi (dấu vân tay: tên + size + mtime_ns của từng file thô + tùy chọn + phiên bản ContentFlow).

Khác với ContentFlow (chỉ "bỏ qua nếu file đích tồn tại và size > 0", ghi thẳng vào đích nên crash để lại file cụt mà lần sau bị tin nhầm),
ở đây chỉ TIN file đích mà trạng thái của ta (`.cf_pool.json`, ghi atomic sau khi xong) đã ghi lại với đúng size và nguồn không đổi.
File đích không được tin bị xóa rồi mới đồng bộ lại. Khóa liên tiến trình (`.sync.lock`, có pid + heartbeat) bảo đảm không hai nơi cùng đồng bộ.
Hàm thuần/tệp trong module này; việc chạy ffmpeg nằm ở `contentflow.py` + `sync_shim.py`.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from ..fsutil import atomic_write_json

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv")     # = video_utils.VIDEO_EXTENSIONS của ContentFlow
STATE = ".cf_pool.json"
LOCK = ".sync.lock"
LOCK_TTL_S = 120.0


def dest_name(src_name: str) -> str:
    return Path(src_name).stem + ".mp4"                       # ContentFlow: <stem>.mp4


def scan_raw(raw_dir: Path) -> dict[str, list[int]]:
    """{tên: [size, mtime_ns]} của video trong thư mục thô (KHÔNG đệ quy, giống ContentFlow)."""
    out = {}
    d = Path(raw_dir)
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                st = p.stat()
                out[p.name] = [st.st_size, st.st_mtime_ns]
    return out


def options_sig(sync: dict, version: str) -> str:
    return hashlib.sha256(json.dumps({"sync": sync, "version": version}, sort_keys=True, default=str).encode()).hexdigest()[:20]


def fingerprint(raw: dict, sync: dict, version: str) -> str:
    return hashlib.sha256(json.dumps({"raw": raw, "opt": options_sig(sync, version)}, sort_keys=True).encode()).hexdigest()[:24]


def read_state(synced_dir: Path) -> dict | None:
    try:
        d = json.loads((Path(synced_dir) / STATE).read_text(encoding="utf-8"))
        return d if d.get("schema") == 1 else None
    except (OSError, ValueError):
        return None


def write_state(synced_dir: Path, state: dict) -> None:
    atomic_write_json(Path(synced_dir) / STATE, {"schema": 1, **state})


def assess(raw: dict, sync: dict, version: str, state: dict | None, synced_dir: Path) -> dict:
    """{ready, fingerprint, trusted: [tên đích], untrusted: [tên đích cần xóa], todo: [tên nguồn], reason}.

    ready = mọi file thô có file đích được tin (hoặc đã biết hỏng ở đúng phiên bản file đó) và có ít nhất một file dùng được."""
    fp, sig = fingerprint(raw, sync, version), options_sig(sync, version)
    synced_dir = Path(synced_dir)
    existing = {p.name: p.stat().st_size for p in synced_dir.glob("*.mp4") if p.is_file()} if synced_dir.is_dir() else {}
    trusted: set[str] = set()
    bad_known: set[str] = set()
    if state and state.get("options") == sig:
        for src, sf in (state.get("sources") or {}).items():
            if raw.get(src) != sf:
                continue                                              # nguồn đổi/xóa: file đích cũ không còn đúng
            dn = dest_name(src)
            if dn in (state.get("dests") or {}) and existing.get(dn) == state["dests"][dn]:
                trusted.add(dn)
        for src, sf in (state.get("failed") or {}).items():
            if raw.get(src) == sf[:2]:
                bad_known.add(src)
    todo = [s for s in raw if dest_name(s) not in trusted and s not in bad_known]
    untrusted = sorted(set(existing) - trusted)
    ready = bool(raw) and not todo and bool(trusted)
    reason = ("ok" if ready else "chưa có trạng thái đồng bộ" if not state else "nguồn hoặc tùy chọn đã đổi" if todo else
              "không có file dùng được" if not trusted else "ok")
    return {"ready": ready, "fingerprint": fp, "trusted": sorted(trusted), "untrusted": untrusted, "todo": todo, "reason": reason}


# ------------------------------------------------------------------------------- khóa liên tiến trình
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class PoolLock:
    """Khóa bằng file O_EXCL kèm pid; coi là mồ côi nếu chủ đã chết hoặc quá TTL không heartbeat. Dùng được giữa thread và tiến trình."""

    def __init__(self, pool_dir: Path, ttl: float = LOCK_TTL_S) -> None:
        self.path = Path(pool_dir) / LOCK
        self.ttl = ttl
        self.held = False

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._stale():
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                return False
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "ts": time.time()}, f)
            self.held = True
            return True
        return False

    def _stale(self) -> bool:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(d.get("pid", 0))
            age = time.time() - self.path.stat().st_mtime
        except (OSError, ValueError):
            return True
        return (not pid_alive(pid)) or age > self.ttl

    def touch(self) -> None:
        if self.held:
            try:
                os.utime(self.path)
            except OSError:
                pass

    def release(self) -> None:
        if self.held:
            self.held = False
            try:
                self.path.unlink()
            except OSError:
                pass
