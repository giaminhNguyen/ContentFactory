"""WorkerTTS: TTS cục bộ qua worker SỐNG LÂU (scripts/tts_worker.py) — model nạp một lần, không phải mỗi segment như CommandTTS.

Một adapter chứa nhiều engine; engine của segment = `engine` trong TTS profile (tts_profiles/<tên>.json), nên mỗi kênh dùng được engine riêng
(`engine_id` = None => Auto Mode chọn profile của mọi engine đã khai):
    "adapters": {"tts": "contentfactory.adapters.worker_tts:WorkerTTS"},
    "adapter_config": {"tts": {"engines": {"vieneu": {"command": ["<python của engine>", "scripts/tts_worker.py", "vieneu"],
        "cwd": null, "env": {}, "timeout_s": 600, "load_timeout_s": 900, "capabilities": {"languages": ["vi", "en"], ...}}}}}
`cwd` mặc định = gốc repo (đường dẫn script tương đối). stderr của worker: runtime/logs/tts-worker-<engine>.log.
Worker khởi động ở lần synth đầu, sống tới khi ContentFactory thoát (hết stdin => worker tự thoát); lỗi giao thức/timeout/huỷ => giết, lần sau khởi động lại.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from ..contracts import ErrorClass, StageContext, StageError

ROOT = Path(__file__).resolve().parents[3]
_OOM_RX = re.compile(r"out of memory|CUDA error|MemoryError", re.I)
_POLICY_TYPES = {"ValueError", "KeyError", "TypeError", "FileNotFoundError"}       # sai voice/settings/reference: thử lại vô ích


class WorkerTTS:
    engine_id = None

    def __init__(self, spec: dict | None = None) -> None:
        self.engines = (spec or {}).get("engines") or {}
        if not self.engines or any(not e.get("command") for e in self.engines.values()):
            raise ValueError("WorkerTTS cần adapter_config.tts.engines = {tên: {command: [...]}}")
        self._locks = {n: threading.Lock() for n in self.engines}
        self._procs: dict[str, tuple[subprocess.Popen, queue.Queue]] = {}

    def capabilities(self) -> dict:
        caps = [e.get("capabilities") or {} for e in self.engines.values()]
        sig = json.dumps(self.engines, sort_keys=True, default=str)
        return {"languages": sorted({x for c in caps for x in c.get("languages", [])}),
                "voices": sorted({x for c in caps for x in c.get("voices", [])}),
                "voice_cloning": any(c.get("voice_cloning") for c in caps),
                "engine_version": "worker-" + hashlib.sha256(sig.encode()).hexdigest()[:10]}

    def health(self) -> dict:
        found = {n: Path(e["command"][0]).is_file() for n, e in self.engines.items()}
        return {"ok": all(found.values()), "engines": found}

    # -- worker ----------------------------------------------------------------------------------------
    def _log(self, name: str) -> Path:
        return ROOT / "runtime" / "logs" / f"tts-worker-{name}.log"

    def _tail(self, name: str) -> str:
        try:
            return self._log(name).read_bytes()[-600:].decode("utf-8", "replace")
        except OSError:
            return ""

    def _kill(self, name: str) -> None:
        p, _ = self._procs.pop(name, (None, None))
        if p:
            if p.poll() is None:
                p.kill()
            p.wait()
            p.stdin.close()
            p.stdout.close()

    def _recv(self, name: str, timeout: float, ctx: StageContext) -> dict:
        p, q = self._procs[name]
        t0 = time.time()
        while True:
            try:
                line = q.get(timeout=0.2)
                break
            except queue.Empty:
                if ctx.cancel.is_set():
                    self._kill(name)
                    ctx.cancel.check()
                if time.time() - t0 > timeout:
                    self._kill(name)
                    raise StageError(ErrorClass.TRANSIENT, "TTS_TIMEOUT", f"{name}: >{timeout:.0f}s", resource="runtime") from None
        if line is None:                                  # worker chết (thiếu thư viện, OOM lúc nạp model...)
            self._kill(name)
            tail = self._tail(name)
            cls = ErrorClass.RESOURCE if _OOM_RX.search(tail) or "No module named" in tail else ErrorClass.TRANSIENT
            raise StageError(cls, "TTS_WORKER_DIED", f"{name}: {tail}".strip(), resource="runtime")
        try:
            return json.loads(line)
        except ValueError:
            self._kill(name)
            raise StageError(ErrorClass.TRANSIENT, "TTS_WORKER_PROTOCOL", f"{name}: {line[:200]!r}", resource="runtime") from None

    def _ensure(self, name: str, ctx: StageContext) -> None:
        p = self._procs.get(name, (None,))[0]
        if p and p.poll() is None:
            return
        e = self.engines[name]
        log = self._log(name)
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as fe:
            p = subprocess.Popen(e["command"], cwd=e.get("cwd") or ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=fe,
                                 env={**os.environ, **{k: str(v) for k, v in (e.get("env") or {}).items()}, "PYTHONIOENCODING": "utf-8"})
        q: queue.Queue = queue.Queue()

        def pump() -> None:
            for raw in p.stdout:
                q.put(raw.decode("utf-8", "replace"))
            q.put(None)
        threading.Thread(target=pump, daemon=True).start()
        self._procs[name] = (p, q)
        ctx.log("tts_worker_start", engine=name, pid=p.pid)
        msg = self._recv(name, float(e.get("load_timeout_s", 900)), ctx)
        if not msg.get("ready"):
            self._kill(name)
            raise StageError(ErrorClass.TRANSIENT, "TTS_WORKER_PROTOCOL", f"{name}: chờ ready, nhận {msg}", resource="runtime")

    # -- adapter ---------------------------------------------------------------------------------------
    def synthesize(self, segment: dict, profile: dict, out_path: Path, ctx: StageContext) -> dict:
        name = profile.get("engine")
        if name not in self.engines:
            raise StageError(ErrorClass.POLICY, "UNKNOWN_TTS_ENGINE",
                             f"profile dùng engine {name!r}; adapter có {sorted(self.engines)} (chọn tts_profile cho kênh)")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(out_path.name + ".part")
        req = {"text": segment["text"], "out": str(tmp), "voice": profile.get("voice"), "language": profile.get("language"),
               "settings": profile.get("settings") or {}}
        try:
            with self._locks[name]:
                self._ensure(name, ctx)
                p, _ = self._procs[name]
                try:
                    p.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
                    p.stdin.flush()
                except OSError:
                    self._kill(name)
                    raise StageError(ErrorClass.TRANSIENT, "TTS_WORKER_DIED", f"{name}: {self._tail(name)}".strip(), resource="runtime") from None
                res = self._recv(name, float(self.engines[name].get("timeout_s", 600)), ctx)
            if not res.get("ok"):
                err = res.get("error", "")
                cls = (ErrorClass.RESOURCE if _OOM_RX.search(err) else
                       ErrorClass.POLICY if res.get("type") in _POLICY_TYPES else ErrorClass.TRANSIENT)
                raise StageError(cls, "TTS_ENGINE_ERROR", f"{name}: {res.get('type')}: {err}",
                                 resource="runtime" if cls == ErrorClass.RESOURCE else None)
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise StageError(ErrorClass.TRANSIENT, "TTS_NO_OUTPUT", f"{name} không ghi file kết quả")
            os.replace(tmp, out_path)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {"index": segment["index"], "duration_sec": 0.0}
