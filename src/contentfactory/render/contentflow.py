"""ContentFlowRender: RenderAdapter thật, bọc `media_worker` của ContentFlow qua subprocess (JSON-lines) — KHÔNG import code ContentFlow và
không copy logic của nó. ContentFlow lo chọn nền/ghép/encode; orchestrator lo hàng đợi, retry, checkpoint, cache, kiểm tra kết quả.

  render_video    -> `python -m media_worker run` type=render   (output_dir = thư mục đích, output_name = tên file đích; idempotency_key do Manager đặt)
  render_thumbnail-> type=thumbnail
  prepare_pool    -> Source Sync dùng chung (worker không có job type sync): `sync_shim.py` chạy bằng Python của ContentFlow, xem pools.py
  status          -> `media_worker status` (đối soát sau crash)

Lỗi worker ánh xạ sang StageError: RESOURCE (FFMPEG_MISSING/GPU_UNAVAILABLE -> runtime, DISK_FULL -> disk), POLICY (MISSING_INPUT -> resource=input
để job bị giữ chờ người cung cấp; INVALID_CONFIG -> FAILED), TRANSIENT (FFMPEG_FAILED/TIMEOUT/INTERNAL_ERROR), CANCELLED.
Kết quả được kiểm bằng ffprobe (kích thước, độ dài khớp audio); video sai bị xóa (để worker không "replay" một output hỏng) rồi báo TRANSIENT.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..contracts import ErrorClass, StageContext, StageError
from ..fsutil import atomic_write_json, sha256_file
from . import pools as PL
from .frames import ensure_frame

RESOURCE_OF = {"FFMPEG_MISSING": "runtime", "GPU_UNAVAILABLE": "runtime", "DISK_FULL": "disk"}
CLASS_OF = {"TRANSIENT": ErrorClass.TRANSIENT, "RESOURCE": ErrorClass.RESOURCE, "POLICY": ErrorClass.POLICY, "CANCELLED": ErrorClass.CANCELLED}
SHIM = Path(__file__).with_name("sync_shim.py")


def ext_job_id(key: str) -> str:
    """Giống `media_worker.external_job_id`: xác định từ idempotency_key (dùng để đặt file hủy)."""
    return "mw-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def map_worker_error(err: dict, where: str = "render") -> StageError:
    code, klass, msg = err.get("code", "INTERNAL_ERROR"), err.get("class", "TRANSIENT"), err.get("message", "")
    cls = CLASS_OF.get(klass, ErrorClass.TRANSIENT)
    resource = RESOURCE_OF.get(code, "runtime") if cls == ErrorClass.RESOURCE else ("input" if code == "MISSING_INPUT" else None)
    return StageError(cls, code, f"{where}: {msg}"[:600], {"worker_code": code, "worker_class": klass}, resource=resource)


class ContentFlowRender:
    requires_pool = True                         # stage render cần source pool đã đồng bộ

    def __init__(self, spec: dict | None = None) -> None:
        s = spec or {}
        self.root = Path(s.get("root") or "modules/ContentFlow")
        self.python = s.get("python") or sys.executable
        self.base_dir = Path(s.get("base_dir") or self.root)
        self.pools_dir = Path(s.get("pools_dir") or "runtime/pools")
        self.frames_dir = Path(s.get("frames_dir") or self.base_dir / "frames")
        self.ffprobe = s.get("ffprobe") or "ffprobe"
        self.sync_wait_s = float(s.get("sync_wait_s", 3600))
        self.verify = bool(s.get("verify_output", True))
        self._version: str | None = None
        self._health: dict | None = None

    # ------------------------------------------------------------------------------------------ sức khỏe / phiên bản
    def _worker(self, args: list[str], timeout: float = 30, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([self.python, "-m", "media_worker", *args], cwd=str(cwd or self.root), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=self._env())

    @staticmethod
    def _env() -> dict:
        return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

    def health(self) -> dict:
        try:
            r = self._worker(["health"])
            d = json.loads((r.stdout or "").strip().splitlines()[-1])
        except (OSError, ValueError, IndexError, subprocess.SubprocessError) as e:
            return {"ok": False, "error": f"không chạy được media_worker: {e!r}"}
        ok = bool(d.get("ok")) and "render" in d.get("capabilities", [])
        return {"ok": ok, "worker_version": d.get("version"), "capabilities": d.get("capabilities"),
                **({} if ok else {"error": "worker không có khả năng render (thiếu ffmpeg/ffprobe, hoặc thiếu Pillow cho ContentFlow?)"})}

    def _spawn_error(self, e: OSError) -> StageError:
        """Không khởi động được worker: nói rõ cấu hình nào sai (thư mục ContentFlow hay Python), là vấn đề tài nguyên (giữ job), không phải lỗi vĩnh viễn."""
        if not self.root.is_dir():
            msg = f"thư mục ContentFlow không tồn tại: {self.root} (tools.contentflow.root; chạy setup để clone module)"
        else:
            msg = f"không chạy được {self.python!r} (tools.contentflow.python): {e}"
        return StageError(ErrorClass.RESOURCE, "CONTENTFLOW_MISSING", msg, resource="runtime")

    def version(self) -> str:
        """Phiên bản ContentFlow (git HEAD + phiên bản worker): vào khóa cache và dấu vân tay pool."""
        if self._version is None:
            head = "nogit"
            try:
                head = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()[:12] or "nogit"
            except (OSError, subprocess.SubprocessError):
                pass
            wv = (self._safe_health() or {}).get("worker_version") or "?"
            self._version = f"{head}+w{wv}"
        return self._version

    def _safe_health(self) -> dict | None:
        if self._health is None:
            self._health = self.health()
        return self._health

    # ------------------------------------------------------------------------------------------ chạy một worker request
    def _run_worker(self, jtype: str, key: str, out_dir: Path, params: dict, inputs: list[dict], deadline_s: float, ctx, on_progress=None) -> dict:
        out_dir.mkdir(parents=True, exist_ok=True)
        ext = ext_job_id(key)
        req = {"protocol": 1, "job_id": getattr(ctx, "job_id", "job"), "type": jtype, "idempotency_key": key,
               "output_dir": str(out_dir.resolve()), "params": params, "inputs": inputs, "deadline_s": deadline_s,
               "attempt": getattr(ctx, "attempt", 1)}
        req_file = out_dir / f".req.{ext}.json"
        atomic_write_json(req_file, req)
        errf = out_dir / f".stderr.{ext}.log"
        events: list[dict] = []
        q: queue.Queue = queue.Queue()
        with open(errf, "wb") as ef:
            try:
                p = subprocess.Popen([self.python, "-m", "media_worker", "run", "--request", str(req_file), "--base-dir", str(self.base_dir.resolve())],
                                     cwd=str(self.root), stdout=subprocess.PIPE, stderr=ef, stdin=subprocess.DEVNULL, env=self._env())
            except OSError as e:
                raise self._spawn_error(e) from None

            def read() -> None:
                for raw in iter(p.stdout.readline, b""):
                    q.put(raw)
                q.put(None)
            threading.Thread(target=read, daemon=True).start()
            cancel = getattr(ctx, "cancel", None)
            cancelled_at = None
            closed = False
            while not (closed and p.poll() is not None):
                try:
                    raw = q.get(timeout=0.2)
                except queue.Empty:
                    raw = False
                if raw is None:
                    closed = True
                elif raw:
                    try:
                        ev = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    events.append(ev)
                    if ev.get("event") == "progress" and on_progress:
                        on_progress(float(ev.get("pct", 0.0)))
                if cancel is not None and cancel.is_set() and cancelled_at is None:
                    cancelled_at = time.time()
                    (out_dir / f"cancel.{ext}").write_text("cancel", encoding="utf-8")        # worker theo dõi file này
                if cancelled_at and time.time() - cancelled_at > 15 and p.poll() is None:
                    self._kill(p)
            p.wait()
            p.stdout.close()
        (out_dir / f"cancel.{ext}").unlink(missing_ok=True)
        req_file.unlink(missing_ok=True)
        if cancelled_at:
            raise StageError(ErrorClass.CANCELLED, "CANCELLED", "render bị hủy")
        terminal = next((e for e in reversed(events) if e.get("event") in ("completed", "failed")), None)
        tail = errf.read_text(encoding="utf-8", errors="replace")[-500:] if errf.exists() else ""
        errf.unlink(missing_ok=True)
        if terminal and terminal["event"] == "failed":
            raise map_worker_error(terminal.get("error") or {}, jtype)
        if not terminal:
            if p.returncode == 2:
                raise StageError(ErrorClass.POLICY, "RENDER_USAGE", f"media_worker từ chối request: {tail}")
            raise StageError(ErrorClass.TRANSIENT, "RENDER_WORKER_DIED", f"media_worker thoát (rc={p.returncode}) không có sự kiện kết thúc: {tail}")
        return {"events": events, "artifacts": (terminal.get("outputs") or {}).get("artifacts", []),
                "replayed": not any(e.get("event") == "started" for e in events)}

    @staticmethod
    def _kill(p: subprocess.Popen) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        else:
            p.kill()

    # ------------------------------------------------------------------------------------------ kiểm tra kết quả
    def _probe(self, path: Path) -> dict | None:
        exe = shutil.which(self.ffprobe) or (self.ffprobe if Path(self.ffprobe).exists() else None)
        if not exe:
            return None
        r = subprocess.run([exe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        if r.returncode != 0:
            return {"error": r.stderr.strip()[-200:]}
        d = json.loads(r.stdout or "{}")
        v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
        a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
        dur = (d.get("format") or {}).get("duration")
        return {"width": int(v["width"]) if v else 0, "height": int(v["height"]) if v else 0, "has_video": bool(v), "has_audio": bool(a),
                "duration": float(dur) if dur not in (None, "N/A") else 0.0}

    def _check_video(self, path: Path, width: int, height: int, audio_dur: float | None) -> dict:
        info = self._probe(path) if self.verify else None
        if info is None:
            return {"verified": False}
        bad = []
        if info.get("error"):
            bad.append(f"không đọc được: {info['error']}")
        else:
            if not info["has_video"] or not info["has_audio"]:
                bad.append("thiếu luồng video hoặc audio")
            if (info["width"], info["height"]) != (width, height):
                bad.append(f"kích thước {info['width']}x{info['height']}, cần {width}x{height}")
            if audio_dur and abs(info["duration"] - audio_dur) > max(1.0, audio_dur * 0.01):
                bad.append(f"dài {info['duration']:.2f}s, audio dài {audio_dur:.2f}s")
        if bad:
            path.unlink(missing_ok=True)                    # xóa để worker không "replay" một output hỏng ở lần retry
            raise StageError(ErrorClass.TRANSIENT, "RENDER_OUTPUT_INVALID", f"{path.name}: " + "; ".join(bad), {"issues": bad})
        return {"verified": True, **{k: info[k] for k in ("width", "height", "duration")}}

    # ------------------------------------------------------------------------------------------ RenderAdapter
    def render_video(self, req: dict, ctx: StageContext) -> dict:
        prof, audio, out = req["profile"], Path(req["audio"]), Path(req["output"])
        key = req.get("key") or hashlib.sha256(f"{sha256_file(audio)}|{json.dumps(prof, sort_keys=True, default=str)}|{out.name}".encode()).hexdigest()
        frame = Path(prof["frame_path"]) if prof.get("frame_path") else ensure_frame(self.frames_dir, prof["width"], prof["height"])
        vp = prof.get("viewport") or {"x": 0, "y": 0, "width": prof["width"], "height": prof["height"]}
        cfg = {"video_generator": {"viewport": vp, "video": {"fps": prof["fps"]}}}
        _merge(cfg, prof.get("config_overrides") or {})
        inputs = [{"type": "audio", "path": str(audio.resolve()), "sha256": req.get("audio_sha256") or sha256_file(audio)},
                  {"type": "frame", "path": str(frame.resolve())}]
        pool = req.get("pool")
        params = {"fps": prof["fps"], "selection_mode": prof["selection_mode"], "source_processing": prof["source_processing"],
                  "encoder": prof["encoder"], "output_name": out.name, "config": cfg}
        if pool and pool.get("dir"):
            inputs.append({"type": "video_dir", "path": str(Path(pool["dir"]).resolve())})
        res = self._run_worker("render", key, out.parent, params, inputs, float(prof.get("deadline_s", 7200)), ctx, req.get("on_progress"))
        if not out.is_file() or out.stat().st_size == 0:
            raise StageError(ErrorClass.TRANSIENT, "RENDER_NO_OUTPUT", f"worker báo xong nhưng không có {out}")
        adur = self._probe(audio)
        chk = self._check_video(out, prof["width"], prof["height"], adur["duration"] if adur and not adur.get("error") else None)
        return {"warnings": [], "replayed": res["replayed"], "worker_events": len(res["events"]), **chk}

    def render_thumbnail(self, req: dict, ctx: StageContext) -> Path:
        out, th = Path(req["output"]), req.get("config_overrides") or {}
        key = req.get("key") or hashlib.sha256(json.dumps([req["title"], req["channel_name"], out.name], default=str).encode()).hexdigest()
        inputs = []
        if req.get("image"):
            inputs.append({"type": "image", "path": str(Path(req["image"]).resolve())})
        params = {"channel": req["channel_name"], "title": req["title"], "highlight_mode": req.get("highlight", "auto"),
                  "highlight_text": req.get("highlight_text", ""), "output_name": out.name, "config": th}
        self._run_worker("thumbnail", key, out.parent, params, inputs, 600.0, ctx)
        if not out.is_file() or out.stat().st_size == 0:
            raise StageError(ErrorClass.TRANSIENT, "THUMBNAIL_NO_OUTPUT", f"worker báo xong nhưng không có {out}")
        return out

    def status(self, idempotency_key: str, output_dir: Path) -> dict:
        r = self._worker(["status", "--output-dir", str(output_dir), "--key", idempotency_key])
        return json.loads((r.stdout or "{}").strip().splitlines()[-1])

    # ------------------------------------------------------------------------------------------ Source Sync dùng chung
    def pool_dirs(self, name: str) -> tuple[Path, Path]:
        d = self.pools_dir / name
        return d, d / "synced"

    def pool_status(self, pool: dict) -> dict:
        d, synced = self.pool_dirs(pool["name"])
        raw = PL.scan_raw(Path(pool["raw_dir"]))
        a = PL.assess(raw, pool["sync"], self.version(), PL.read_state(synced), synced)
        busy = (d / PL.LOCK).exists() and not PL.PoolLock(d)._stale()
        return {"name": pool["name"], "ready": a["ready"], "syncing": busy, "reason": a["reason"], "fingerprint": a["fingerprint"],
                "raw_files": len(raw), "todo": len(a["todo"]), "dir": str(synced)}

    def prepare_pool(self, pool: dict, ctx=None) -> dict:
        """Bảo đảm pool đã đồng bộ. Không đổi thì trả về ngay (chỉ liệt kê file + đọc trạng thái); đang có nơi khác đồng bộ thì chờ (hủy được)."""
        d, synced = self.pool_dirs(pool["name"])
        raw_dir = Path(pool["raw_dir"])
        if not raw_dir.is_dir():
            raise StageError(ErrorClass.POLICY, "MISSING_INPUT", f"thư mục nguồn của pool '{pool['name']}' không tồn tại: {raw_dir}", resource="input")
        cancel = getattr(ctx, "cancel", None)
        ver = self.version()
        t0 = time.time()
        lock = PL.PoolLock(d)
        while True:
            raw = PL.scan_raw(raw_dir)
            if not raw:
                raise StageError(ErrorClass.POLICY, "MISSING_INPUT", f"pool '{pool['name']}': không có video nào trong {raw_dir}", resource="input")
            a = PL.assess(raw, pool["sync"], ver, PL.read_state(synced), synced)
            if a["ready"]:
                return {"dir": str(synced), "fingerprint": a["fingerprint"], "reused": True, "files": len(a["trusted"])}
            if lock.try_acquire():
                break
            if cancel is not None:
                cancel.check()
            if time.time() - t0 > self.sync_wait_s:
                raise StageError(ErrorClass.TRANSIENT, "POOL_SYNC_TIMEOUT", f"chờ đồng bộ pool '{pool['name']}' quá {self.sync_wait_s:.0f}s", resource="runtime")
            time.sleep(0.25)
        try:
            raw = PL.scan_raw(raw_dir)                     # đọc lại sau khi có khóa: nơi khác có thể vừa xong hoặc nguồn vừa đổi
            a = PL.assess(raw, pool["sync"], ver, PL.read_state(synced), synced)
            if a["ready"]:
                return {"dir": str(synced), "fingerprint": a["fingerprint"], "reused": True, "files": len(a["trusted"])}
            synced.mkdir(parents=True, exist_ok=True)
            for name in a["untrusted"]:                    # file đích không có trong trạng thái đã ghi: có thể là file cụt do crash
                (synced / name).unlink(missing_ok=True)
            (synced / "source_profile.json").unlink(missing_ok=True)
            result = self._run_sync(pool, d, synced, ctx, lock)
            raw2 = PL.scan_raw(raw_dir)
            failed = {r["name"]: raw2.get(r["name"], [0, 0]) + [r["message"][:200]] for r in result["files"] if r["status"] == "error"}
            ok_names = {r["name"] for r in result["files"] if r["status"] in ("ok", "skipped")}
            dests = {PL.dest_name(n): (synced / PL.dest_name(n)).stat().st_size for n in ok_names if (synced / PL.dest_name(n)).is_file()}
            if not dests:
                raise StageError(ErrorClass.POLICY, "POOL_SYNC_FAILED", f"pool '{pool['name']}': không file nào đồng bộ được: " +
                                 "; ".join(f"{k}: {v[2]}" for k, v in failed.items())[:400], {"failed": failed})
            PL.write_state(synced, {"fingerprint": PL.fingerprint(raw2, pool["sync"], ver), "options": PL.options_sig(pool["sync"], ver),
                                    "sources": {n: raw2[n] for n in raw2 if PL.dest_name(n) in dests}, "dests": dests,
                                    "failed": {k: v for k, v in failed.items() if k in raw2}, "synced_at": time.time(), "version": ver,
                                    "encoder": result.get("encoder")})
            return {"dir": str(synced), "fingerprint": PL.fingerprint(raw2, pool["sync"], ver), "reused": False, "files": len(dests),
                    "failed": sorted(failed), "encoder": result.get("encoder")}
        finally:
            lock.release()

    def _run_sync(self, pool: dict, pool_dir: Path, synced: Path, ctx, lock: PL.PoolLock) -> dict:
        sy = pool["sync"]
        cancel_file = pool_dir / ".sync.cancel"
        cancel_file.unlink(missing_ok=True)
        req = {"cf_root": str(self.root.resolve()), "source_folder": str(Path(pool["raw_dir"]).resolve()), "output_folder": str(synced.resolve()),
               "size": sy["size"], "fps": sy.get("fps"), "quality": sy["quality"], "encoder": sy["encoder"], "remove_audio": sy["remove_audio"],
               "cancel_file": str(cancel_file)}
        rf = pool_dir / ".sync_req.json"
        atomic_write_json(rf, req)
        cancel = getattr(ctx, "cancel", None)
        events: list[dict] = []
        with tempfile_stderr(pool_dir) as ef:
            try:
                p = subprocess.Popen([self.python, str(SHIM), str(rf)], cwd=str(self.root), stdout=subprocess.PIPE, stderr=ef, stdin=subprocess.DEVNULL,
                                     env=self._env())
            except OSError as e:
                raise self._spawn_error(e) from None
            q: queue.Queue = queue.Queue()

            def read() -> None:
                for raw in iter(p.stdout.readline, b""):
                    q.put(raw)
                q.put(None)
            threading.Thread(target=read, daemon=True).start()
            closed, cancelled_at, last_touch = False, None, 0.0
            while not (closed and p.poll() is not None):
                try:
                    raw = q.get(timeout=0.2)
                except queue.Empty:
                    raw = False
                if raw is None:
                    closed = True
                elif raw:
                    try:
                        events.append(json.loads(raw.decode("utf-8", "replace")))
                    except ValueError:
                        pass
                if time.time() - last_touch > 10:
                    lock.touch()
                    last_touch = time.time()
                if cancel is not None and cancel.is_set() and cancelled_at is None:
                    cancelled_at = time.time()
                    cancel_file.write_text("cancel", encoding="utf-8")
                if cancelled_at and time.time() - cancelled_at > 20 and p.poll() is None:
                    self._kill(p)
            p.wait()
            p.stdout.close()
            ef.flush()
            ef.seek(0)
            tail = ef.read()[-400:].decode("utf-8", "replace")
        cancel_file.unlink(missing_ok=True)
        rf.unlink(missing_ok=True)
        if cancelled_at:
            raise StageError(ErrorClass.CANCELLED, "CANCELLED", "đồng bộ pool bị hủy")
        res = next((e for e in reversed(events) if e.get("event") in ("result", "failed")), None)
        if res and res["event"] == "failed":
            raise map_worker_error(res, "source sync")
        if not res:
            raise StageError(ErrorClass.TRANSIENT, "POOL_SYNC_DIED", f"sync_shim thoát (rc={p.returncode}) không có kết quả: {tail}")
        return res


def _merge(a: dict, b: dict) -> dict:
    for k, v in b.items():
        a[k] = _merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return a


class tempfile_stderr:
    """File tạm trong thư mục pool để hứng stderr của shim (không dùng PIPE để khỏi nghẽn)."""

    def __init__(self, d: Path) -> None:
        self.path = Path(d) / ".sync_stderr.log"

    def __enter__(self):
        self.f = open(self.path, "w+b")
        return self.f

    def __exit__(self, *a):
        self.f.close()
        self.path.unlink(missing_ok=True)
