"""Orchestrator: lấy job từ hàng đợi từng stage, chạy handler, checkpoint, retry, resume, hold/auto-resume.

Bất biến:
- Chỉ DB giữ state. Handler chỉ thấy StageContext (artifact đầu vào đã kiểm + params) và adapter được tiêm.
- Kết quả stage chỉ có hiệu lực khi `store.succeed` commit (artifact + state cùng transaction).
- Tiến trình chết => lease hết hạn => `recover_expired` xếp job lại đúng stage; artifact stage trước không đổi.
- Job KHÔNG bắt buộc chạy hết chuỗi: `start_stage`/`target_stage`; stage có output hợp lệ thì skip (D-36).
- Lỗi tài nguyên TẠM THỜI làm job bị GIỮ (PAUSED_*), không FAILED (D-37). Auto Resume chỉ đưa job lại hàng đợi khi
  Resource Monitor nói tài nguyên đã sẵn sàng, có trần số lần không tiến triển (D-39).
- Job dùng config SNAPSHOT lúc tạo, không đọc lại config global ngữ nghĩa lúc chạy (D-41).
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from ..contracts import ArtifactRef, CancelToken, ErrorClass, StageContext, StageError, StageResult
from ..fsutil import atomic_write, atomic_write_json, sha256_file
from ..jobs import manifest as M
from ..jobs import pipeline as P
from ..jobs.db import Claim, JobStore
from ..jobs.plan import Plan, plan_job, plan_spec
from ..jobs.policy import RetryPolicy
from ..jobs.workspace import ensure_job_dirs, job_dir
from .config import Config, _merge
from .handlers import HANDLERS
from .log import EventLog
from .monitor import DiskProbe, NetworkProbe, ResourceMonitor
from ..jobs.sequences import SequenceManager
from . import auto as AU
from . import cleanup as CL
from . import channels as CH
from . import templates as TPL
from .pools import PoolSyncService
from .registry import build_adapters
from .snapshot import (adapters_hash, apply_patch, build_snapshot, config_hash, effective_config)
from .stages import StageContract
from .validation import validate_kind

KNOWN_KINDS = frozenset(k for s in P.STAGES for k in s.produces)


def _spec_error(msg: str, **detail) -> StageError:
    return StageError(ErrorClass.POLICY, "INVALID_JOBSPEC", msg, detail)


class Orchestrator:
    def __init__(self, cfg: Config, adapters: dict | None = None, echo: bool = False,
                 monitor: ResourceMonitor | None = None) -> None:
        self.cfg = cfg
        self.store = JobStore(cfg.path("db"))
        self.adapters = adapters if adapters is not None else build_adapters(cfg)
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.cancel = CancelToken()
        self.log = EventLog(cfg.path("runtime"), cfg.path("workspace"), echo)
        self.versions = M.read_modules_lock(cfg.root)
        cfg.path("output").mkdir(parents=True, exist_ok=True)
        # Bộ adapter mặc định dựng từ config LÚC KHỞI TẠO; job có snapshot khác thì được dựng bộ riêng (cache theo hash).
        self._default_hash = adapters_hash(build_snapshot(cfg, auto_resume=True, start_stage=None, target_stage=None)["semantic"])
        self._job_adapters: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.monitor = monitor or self._default_monitor()
        self.monitor.input_check = self.monitor.input_check or self._inputs_ok
        self.monitor.adapters_health = self.monitor.adapters_health or self._adapters_health
        self._last_tick = 0.0
        self._last_cleanup = 0.0
        self._manifest_lock = threading.Lock()
        self._guard_logged: dict[str, float] = {}
        self.pool_sync = PoolSyncService(self)
        self.sequence = SequenceManager(self.store)          # Sequence Manager dùng chung (trạng thái project, không phải cấu hình)

    # -- dựng mặc định --------------------------------------------------------------------------
    def _default_monitor(self) -> ResourceMonitor:
        m = self.cfg.data.get("monitor", {})
        disk = DiskProbe(self.cfg.path("workspace"), 0)
        hosts = [tuple(h) for h in m.get("network_hosts", [["1.1.1.1", 443], ["8.8.8.8", 53]])]
        probes = {"network": NetworkProbe(hosts, float(m.get("network_timeout_s", 3.0))), "disk": disk}
        return ResourceMonitor(self.store, probes, base_s=float(m.get("base_s", 30)), max_s=float(m.get("max_s", 300)),
                               disk=disk, disk_min_free_gb=m.get("disk_min_free_gb", {"default": 0.5}))

    def _adapters_health(self, stage: P.Stage) -> tuple[bool, str]:
        bad = []
        for name in stage.adapters:
            try:
                h = self.adapters[name].health()
            except Exception as e:
                h = {"ok": False, "error": repr(e)}
            if not h.get("ok"):
                bad.append(name)
        return not bad, ("adapter chưa sẵn sàng: " + ",".join(bad)) if bad else "adapter sẵn sàng"

    def _inputs_ok(self, job: dict) -> tuple[bool, str]:
        stage = P.BY_QUEUE.get(job["state"])
        if stage is None:
            return True, "không còn stage để chạy"
        try:
            StageContract(stage).validate_inputs(self.store.inputs(job["id"], stage.requires),
                                                 job_dir(self.cfg.path("workspace"), job["id"]))
        except StageError as e:
            return False, f"{e.code}: {e.message}"
        return True, "input hợp lệ"

    # -- snapshot / policy ----------------------------------------------------------------------
    def _policy(self, snapshot: dict | None) -> RetryPolicy:
        retry = (snapshot or {}).get("semantic", {}).get("retry") or self.cfg["retry"]
        return RetryPolicy.from_config({**self.cfg["retry"], **retry})

    def _adapters_for(self, snapshot: dict | None) -> dict:
        if not snapshot:
            return self.adapters
        h = adapters_hash(snapshot["semantic"])
        if h == self._default_hash:
            return self.adapters
        with self._lock:
            if h not in self._job_adapters:
                self._job_adapters[h] = build_adapters(effective_config(self.cfg, snapshot))
            return self._job_adapters[h]

    # -- tạo job ------------------------------------------------------------------------------
    def submit(self, params: dict, priority: int = 0, *, mode: str | None = None, start_stage: str | None = None,
               target_stage: str | None = None, inputs: dict | None = None, from_job: str | dict | None = None,
               auto_resume: bool | None = None, pipeline: dict | None = None) -> str:
        """Tạo job. `mode` ∈ P.MODES (FULL, SUBTITLE_ONLY, STORY_ONLY, THROUGH_TTS, TTS_ONLY, VIDEO_ONLY) hoặc đặt trực tiếp
        `start_stage`/`target_stage`, hoặc `pipeline` = pipeline spec v2 {requested_stages: [...]} (chọn stage tùy ý; dependency tự suy ra,
        không dùng chung với mode/start/target). `inputs` = artifact đưa từ ngoài vào (kind -> đường dẫn | [đường dẫn] | dict cho metadata);
        `from_job` = dùng lại artifact của job khác. Spec không hợp lệ bị từ chối NGAY (không tạo job nửa vời)."""
        # Channel preset (Auto Mode): job_defaults < preset của kênh/tự chọn < params người dùng nhập. Kênh đọc MỘT lần, chốt vào snapshot (D-41, D-46).
        chan_id = str(params.get("channel") or self.cfg["job_defaults"].get("channel") or "default")
        channel = CH.load_channel(self.cfg, chan_id)
        preset, decisions = AU.preset_params(self.cfg, channel, params, self.adapters)
        merged = _merge(_merge(copy.deepcopy(self.cfg["job_defaults"]), copy.deepcopy(preset)), copy.deepcopy(params))
        decisions += AU.select_pools(self.cfg, merged, channel, self.adapters)
        items = self._prepare_imports(inputs or {}, from_job, merged)
        plan = self._plan(merged, {i["kind"] for i in items}, mode, start_stage, target_stage, pipeline)
        if plan.errors:
            raise _spec_error("; ".join(plan.errors), errors=plan.errors)
        spec = self._stored_pipeline(pipeline, plan)
        tkinds = self._template_kinds(plan, spec)
        if tkinds is None or tkinds:                                                    # Template: chốt version cụ thể + snapshot lúc tạo job (D-92)
            tpls, tdec = TPL.select_templates(self.cfg, merged, channel, self.adapters, tkinds)
            if tpls:
                merged["templates"] = tpls
            decisions += tdec
        if decisions:
            merged["auto"] = decisions                                                   # minh bạch: cái gì được tự chọn và vì sao (không ảnh hưởng stage_key)
        resolved = bool(self.cfg.data.get("auto_resume_default", True)) if auto_resume is None else bool(auto_resume)
        snap = build_snapshot(self.cfg, auto_resume=resolved, start_stage=plan.start_stage, target_stage=plan.target_stage)
        snap["semantic"]["channel_config"] = channel
        if not merged.get("watermark") and channel.get("watermark") and Path(channel["watermark"]).is_file():
            merged["watermark"] = channel["watermark"]                                  # watermark là channel asset (HANDOFF §10)
        job_id = self.store.create_job(merged, priority, state=P.STAGES[plan.start_idx].queue_state,
                                       start_stage=plan.start_stage, target_stage=plan.target_stage,
                                       auto_resume=resolved, snapshot=snap, config_hash=config_hash(snap["semantic"]), pipeline=spec)
        try:
            jd = ensure_job_dirs(self.cfg.path("workspace"), job_id)
            self._register_imports(job_id, jd, items)
        except BaseException:
            self.store.discard_job(job_id)
            shutil.rmtree(job_dir(self.cfg.path("workspace"), job_id), ignore_errors=True)
            raise
        for d in decisions:
            self.log.emit("auto_decision", job_id=job_id, **d)
        self.log.emit("job_created", job_id=job_id, start_stage=plan.start_stage, target_stage=plan.target_stage,
                      auto_resume=resolved, imports=sorted({i["kind"] for i in items}), plan_run=plan.run, plan_skip=plan.skip)
        self._manifest(job_id)
        return job_id

    def plan(self, params: dict | None = None, *, mode: str | None = None, start_stage: str | None = None,
             target_stage: str | None = None, inputs: dict | None = None, from_job: str | dict | None = None,
             pipeline: dict | None = None) -> Plan:
        """Xem trước kế hoạch (không tạo job, không copy file)."""
        merged = _merge(copy.deepcopy(self.cfg["job_defaults"]), copy.deepcopy(params or {}))
        items = self._prepare_imports(inputs or {}, from_job, merged)
        return self._plan(merged, {i["kind"] for i in items}, mode, start_stage, target_stage, pipeline)

    def _plan(self, merged: dict, provided: set[str], mode: str | None, start_stage: str | None, target_stage: str | None,
              pipeline: dict | None) -> Plan:
        has_input = bool((merged.get("input") or {}).get("value"))
        if pipeline is not None:
            if mode is not None or start_stage is not None or target_stage is not None:
                raise _spec_error("pipeline không dùng chung với mode/start_stage/target_stage")
            return plan_spec(pipeline, provided, has_input)
        if mode is not None:
            if mode not in P.MODES:
                raise _spec_error(f"mode không hợp lệ: {mode!r}; hợp lệ: {sorted(P.MODES)}")
            m_start, m_target = P.MODES[mode]
            start_stage, target_stage = start_stage or m_start, target_stage or m_target
        return plan_job(start_stage, target_stage, provided, has_input)

    @staticmethod
    def _stored_pipeline(pipeline: dict | None, plan: Plan) -> dict | None:
        """Spec lưu theo job (None với job kiểu mode/start/target cũ). `run` là kết quả plan lúc tạo: runtime chỉ cần đọc, không tính lại."""
        if pipeline is None:
            return None
        return {"version": 2, "requested_stages": plan.requested, "options": dict(pipeline.get("options") or {}), "run": list(plan.run)}

    @staticmethod
    def _template_kinds(plan: Plan, spec: dict | None) -> set[str] | None:
        """Kind template cần chốt: job cũ = tất cả khi đích tới render (None); job có pipeline spec = chỉ nhánh render nằm trong kế hoạch."""
        if spec is None:
            return None if plan.target_idx >= P.INDEX["render_youtube"] else set()
        return ({"youtube", "thumbnail"} if "render_youtube" in spec["run"] else set()) | ({"tiktok"} if "render_tiktok" in spec["run"] else set())

    # -- import artifact ----------------------------------------------------------------------
    def _prepare_imports(self, inputs: dict, from_job: str | dict | None, params: dict) -> list[dict]:
        items: list[dict] = []
        for kind, spec in inputs.items():
            if kind not in KNOWN_KINDS:
                raise _spec_error(f"kind không hợp lệ: {kind!r}; hợp lệ: {sorted(KNOWN_KINDS)}")
            if kind == "metadata" and isinstance(spec, dict):               # chỉ cần tiêu đề: tự dựng metadata
                if not str(spec.get("title") or "").strip():
                    raise StageError(ErrorClass.POLICY, "IMPORT_INVALID", "metadata cần title", {"kind": kind})
                content = {"provider": "manual", "status": "ok", "source_type": "manual",
                           "language": params.get("language", "vi"), **spec}
                items.append({"kind": kind, "content": content, "meta": {"title": content["title"],
                              "language": content["language"]}, "origin": {"manual": True}})
                continue
            paths = [spec] if isinstance(spec, (str, Path)) else list(spec)
            for n, raw in enumerate(paths, 1):
                src = Path(raw)
                issues = validate_kind(kind, src)
                if issues:
                    raise StageError(ErrorClass.POLICY, "IMPORT_INVALID", f"{src}: {','.join(issues)}",
                                     {"kind": kind, "issues": issues})
                meta = {"index": n} if kind in P.INDEXED_KINDS else {}
                items.append({"kind": kind, "src": src, "meta": meta, "origin": {"path": str(src)}})
        if from_job is not None:
            spec = {"job_id": from_job} if isinstance(from_job, str) else dict(from_job)
            sid = spec["job_id"]
            if self.store.get_job(sid) is None:
                raise _spec_error(f"from_job không tồn tại: {sid}")
            sjd = job_dir(self.cfg.path("workspace"), sid)
            wanted = set(spec.get("kinds") or [])
            have = {i["kind"] for i in items}
            for a in self.store.artifacts(sid):
                if (wanted and a["kind"] not in wanted) or a["kind"] in have:
                    continue
                p = sjd / a["path"]
                if not p.is_file() or sha256_file(p) != a["sha256"] or validate_kind(a["kind"], p, json.loads(a["meta"])):
                    raise StageError(ErrorClass.POLICY, "IMPORT_INVALID", f"{sid}:{a['path']} không còn hợp lệ", {"kind": a["kind"]})
                items.append({"kind": a["kind"], "src": p, "rel": a["path"], "meta": json.loads(a["meta"]),
                              "origin": {"from_job": sid, "stage": a["stage"], "sha256": a["sha256"]}})
        return items

    def _register_imports(self, job_id: str, jd: Path, items: list[dict]) -> None:
        refs: list[ArtifactRef] = []
        for n, it in enumerate(items):
            if "content" in it:
                dst = jd / "import" / it["kind"] / "source.json"
                atomic_write_json(dst, it["content"])
            else:
                rel = it.get("rel")
                dst = (jd / "import" / f"from_{it['origin']['from_job']}" / rel) if rel else \
                    (jd / "import" / it["kind"] / f"{n:02d}_{it['src'].name}")
                atomic_write(dst, lambda tmp, s=it["src"]: shutil.copyfile(s, tmp))     # copy: workspace vẫn cô lập
            refs.append({"path": dst.resolve().relative_to(jd.resolve()).as_posix(), "kind": it["kind"],
                         "sha256": sha256_file(dst), "bytes": dst.stat().st_size,
                         "meta": {**it["meta"], "imported": it["origin"]}})
        if refs:
            self.store.add_artifacts(job_id, "import", refs)

    # -- hành động explicit trên job ------------------------------------------------------------
    def retry(self, job_id: str) -> str:
        stage = self.store.retry_failed(job_id)
        self.log.emit("job_retry_requested", job_id=job_id, stage=stage)
        self._manifest(job_id)
        return stage

    def resume(self, job_id: str, now: bool = False) -> str:
        """Resume / Resume Now: trả 'resumed' | 'still_down' | 'not_held'. `now=True` ép đo lại tài nguyên (bỏ qua cooldown);
        tài nguyên vẫn hỏng thì giữ nguyên hold, không đốt retry."""
        job = self.store.get_job(job_id)
        if job is None:
            raise ValueError(f"không có job {job_id}")
        if not job["hold_reason"]:
            return "not_held"
        ok, detail = self.monitor.ready(job, force=now)
        if not ok:
            self.log.emit("job_resume_refused", "warning", job_id, reason=job["hold_reason"], detail=detail)
            return "still_down"
        self.store.release_hold(job_id, auto=False)
        self.log.emit("job_resumed", job_id=job_id, manual=True, from_reason=job["hold_reason"])
        self._manifest(job_id)
        return "resumed"

    def rerender_part(self, job_id: str, part: int) -> str:
        """Ép render lại ĐÚNG một part TikTok (xóa output + khóa của part đó): job phải đang xếp hàng/bị giữ/FAILED ở stage render_tiktok.
        Job đã đi qua stage này thì chưa rewind được (D-56). FAILED thì đưa về hàng như `retry`. Trả trạng thái job sau đó."""
        job = self.store.get_job(job_id)
        if job is None:
            raise ValueError(f"không có job {job_id}")
        st = P.BY_NAME["render_tiktok"]
        queued = job["state"] == st.queue_state
        failed_here = job["state"] == P.FAILED and job["failed_stage"] == "render_tiktok"
        if not (queued or failed_here):
            raise ValueError(f"job {job_id} đang ở {job['state']}: chỉ render lại part khi job xếp hàng/bị giữ/FAILED ở render_tiktok")
        base = job_dir(self.cfg.path("workspace"), job_id) / st.workdir / f"part_{int(part):02d}.mp4"
        for f in (base, base.with_name(base.name + ".key.json")):
            f.unlink(missing_ok=True)
        self.log.emit("render_part_invalidated", job_id=job_id, part=int(part))
        if failed_here:
            self.retry(job_id)
        self._manifest(job_id)
        return self.store.get_job(job_id)["state"]

    def set_auto_resume(self, job_id: str, value: bool) -> None:
        self.store.set_auto_resume(job_id, value)
        self._manifest(job_id)

    def set_target(self, job_id: str, target_stage: str) -> None:
        """Mở rộng/đổi đích của job (explicit). Stage đã xong không chạy lại; thiếu input cho đoạn mới thì từ chối."""
        job = self.store.get_job(job_id)
        if job is None:
            raise ValueError(f"không có job {job_id}")
        if job.get("pipeline") is not None:
            raise _spec_error("job dùng pipeline tùy chỉnh: không đổi được bằng target_stage (dùng pipeline revision)")
        have = {a["kind"] for a in self.store.artifacts(job_id)}
        pos = P.position(job["state"])
        plan = plan_job(P.STAGES[min(pos, len(P.STAGES) - 1)].name if pos is not None else None, target_stage, have,
                        bool((job["params"].get("input") or {}).get("value")))
        if plan.errors:
            raise _spec_error("; ".join(plan.errors), errors=plan.errors)
        if P.INDEX[target_stage] >= P.INDEX["render_youtube"] and not job["params"].get("templates"):
            self._select_templates_for(job)                                             # job tạo khi đích chưa tới render: chốt template lúc mở rộng (sai thì báo, chưa đổi đích)
        self.store.set_target(job_id, target_stage)
        self._manifest(job_id)

    def _select_templates_for(self, job: dict) -> None:
        channel = (job.get("config_snapshot") or {}).get("semantic", {}).get("channel_config") or CH.load_channel(self.cfg, str(job["params"].get("channel") or "default"))
        tpls, tdec = TPL.select_templates(self.cfg, job["params"], channel, self.adapters)
        if tpls:
            params = {**job["params"], "templates": tpls, "auto": list(job["params"].get("auto") or []) + tdec}
            self.store.set_params(job["id"], params, "templates chốt khi mở rộng đích: " + ", ".join(f"{k}={v['id']}@v{v['version']}" for k, v in tpls.items()))
            for d in tdec:
                self.log.emit("auto_decision", job_id=job["id"], **d)

    def retemplate(self, job_id: str, kind: str, template_id: str, policy="latest_published") -> dict:
        """Hành động EXPLICIT: chọn lại template cho MỘT kind (thumbnail | youtube | tiktok) của job và chốt snapshot mới. Output cũ của kind đó bị
        coi là hết hạn (stage_key đổi) nên stage render tương ứng chạy lại; Source/Story/TTS/Audio không bị đụng."""
        job = self.store.get_job(job_id)
        if job is None:
            raise ValueError(f"không có job {job_id}")
        api = TPL._api(self.adapters)
        if api is None:
            raise _spec_error("adapter render hiện tại không có hệ thống template")
        templates = TPL.retemplate(api, job["params"], kind, template_id, policy)
        snap = templates[kind]
        self.store.set_params(job_id, {**job["params"], "templates": templates}, f"retemplate {kind} -> {snap['id']}@v{snap['version']}")
        self.log.emit("job_retemplated", job_id=job_id, kind=kind, template=snap["id"], version=snap["version"])
        self._manifest(job_id)
        return snap

    def set_job_config(self, job_id: str, patch: dict) -> int:
        """Đổi config NGỮ NGHĨA của đúng job này (explicit, ghi revision). Trả revision mới."""
        job = self.store.get_job(job_id)
        if job is None or not job.get("config_snapshot"):
            raise ValueError(f"job {job_id} không có config snapshot")
        snap = apply_patch(job["config_snapshot"], patch)
        rev = job["config_revision"] + 1
        self.store.set_snapshot(job_id, snap, config_hash(snap["semantic"]), rev)
        self.log.emit("job_config_changed", job_id=job_id, revision=rev, keys=sorted(patch))
        self._manifest(job_id)
        return rev

    # -- vòng lặp chính ------------------------------------------------------------------------
    def run(self, until_idle: bool = True, stop: threading.Event | None = None) -> None:
        self.cancel = CancelToken()                 # token riêng cho mỗi lần run: instance dùng lại được sau khi đã dừng
        self._last_tick = 0.0
        for j in self.store.list_jobs():            # manifest là dẫn xuất: dựng lại nếu crash giữa commit và ghi file
            if j["state"] not in P.TERMINAL:
                self._manifest(j["id"])
        hb_stop = threading.Event()
        threading.Thread(target=self._heartbeat, args=(hb_stop,), daemon=True).start()
        self.pool_sync.start()                      # Source Sync nền, dùng chung: không chiếm slot của stage nào
        executor = ThreadPoolExecutor(max_workers=int(self.cfg.data.get("max_workers", 8)))
        futures: set[Future] = set()
        self.log.emit("orchestrator_started", owner=self.owner)
        try:
            while not (stop and stop.is_set()):
                futures = {f for f in futures if not f.done()}
                for job_id, action in self.store.recover_expired(self.cfg["retry"]["max_interruptions"]):
                    self.log.emit("lease_recovered", "warning", job_id, action=action)
                    self._manifest(job_id)
                self._guarded(self._monitor_tick)
                self._guarded(self._cleanup_tick)
                self._schedule(executor, futures)
                # until_idle: thoát khi không còn job ACTIVE; job bị giữ không giữ tiến trình lại (dùng --forever để theo dõi)
                if until_idle and not futures and self.store.nonterminal_count() == 0:
                    break
                time.sleep(self.cfg["poll_s"])
        except KeyboardInterrupt:
            pass
        finally:
            self.cancel.set()                       # handler hợp tác trả CANCELLED => job về hàng, không mất retry
            self.pool_sync.stop()
            executor.shutdown(wait=True)
            hb_stop.set()
            self.log.emit("orchestrator_stopped", owner=self.owner)

    # -- nội bộ -------------------------------------------------------------------------------
    def cleanup(self, dry_run: bool = False) -> dict:
        """Auto Cleanup (D-83): dọn trung gian/cache/workspace cũ; không bao giờ đụng output/ của người dùng."""
        return CL.run(self, dry_run=dry_run)

    def _cleanup_tick(self) -> None:
        cfg = self.cfg.data.get("cleanup", {})
        now = time.time()
        if not cfg.get("enabled", True) or now - self._last_cleanup < float(cfg.get("interval_s", 600)):
            return
        self._last_cleanup = now
        try:
            self.cleanup()
        except Exception as e:                               # noqa: BLE001 - dọn dẹp lỗi không được làm sập scheduler
            self.log.emit("cleanup_error", "warning", error=repr(e))

    def _heartbeat(self, stop: threading.Event) -> None:
        while not stop.wait(self.cfg["heartbeat_s"]):
            try:
                self.store.heartbeat(self.owner, self.cfg["lease_s"])
            except Exception as e:                  # DB bận tạm thời: lần sau thử lại
                self.log.emit("heartbeat_error", "warning", error=repr(e))

    def _guarded(self, fn) -> None:
        """Việc nền (theo dõi tài nguyên, dọn dẹp) lỗi không được làm sập vòng lặp lập lịch: ghi log (tối đa mỗi 60 s mỗi loại) rồi chạy tiếp."""
        try:
            fn()
        except Exception as e:                                   # noqa: BLE001
            now = time.time()
            if now - self._guard_logged.get(fn.__name__, 0) > 60:
                self._guard_logged[fn.__name__] = now
                self.log.emit("background_task_error", "error", task=fn.__name__, error=repr(e), traceback=traceback.format_exc())

    def _monitor_tick(self) -> None:
        """Theo dõi tài nguyên của các job bị giữ (luôn, kể cả Auto Resume OFF) và tự resume job có Auto Resume ON."""
        now = time.time()
        if now - self._last_tick < float(self.cfg.data.get("monitor", {}).get("tick_s", 1.0)):
            return
        self._last_tick = now
        for job in self.store.held_jobs():
            if job["needs_user"]:
                continue
            ok, detail = self.monitor.ready(job)    # cập nhật resource_status; cooldown nằm trong monitor
            auto = job["auto_resume"] if job["auto_resume"] is not None else bool(self.cfg.data.get("auto_resume_default", True))
            if not (ok and auto):
                continue
            policy = self._policy(job["config_snapshot"])
            res = self.store.release_hold(job["id"], auto=True, max_no_progress=policy.max_auto_resumes_without_progress)
            self.log.emit("job_auto_resume" if res == "released" else "job_auto_resume_blocked",
                          "info" if res == "released" else "warning", job["id"], from_reason=job["hold_reason"], result=res)
            self._manifest(job["id"])

    def _schedule(self, executor: ThreadPoolExecutor, futures: set[Future]) -> None:
        cap = int(self.cfg.data.get("max_workers", 8))
        for stage in reversed(P.STAGES):            # ưu tiên stage sau để hút pipeline (HANDOFF §14)
            free = cap - len(futures)
            if free <= 0:
                return
            for claim in self.store.claim(stage, free, self.cfg.limit(P.resource_of(stage)),
                                          self.owner, self.cfg["lease_s"]):
                futures.add(executor.submit(self._execute, claim))

    def _manifest(self, job_id: str) -> None:
        jd = job_dir(self.cfg.path("workspace"), job_id)
        if jd.exists():
            with self._manifest_lock:                       # dựng + ghi trong cùng một khóa: người ghi sau luôn thấy trạng thái mới hơn (không để bản cũ đè bản mới)
                M.write(self.store, jd, job_id, self.versions)

    def _progress_fn(self, job_id: str, stage: str):
        last = [0.0]

        def progress(done, total=None, detail: str = "", force: bool = False, **extra) -> None:
            now = time.time()
            if not force and not (total is not None and done >= total) and now - last[0] < 0.05:   # chặn ghi DB dồn dập (force: đổi trạng thái, không bỏ)
                return
            last[0] = now
            try:
                self.store.set_checkpoint(job_id, stage, {"done": done, "total": total, "detail": detail, **extra})
            except Exception as e:
                self.log.emit("checkpoint_error", "warning", job_id, stage=stage, error=repr(e))
        return progress

    def _execute(self, claim: Claim) -> None:
        stage, job_id = claim.stage, claim.job_id
        log = self.log.bind(job_id, stage.name, claim.attempt)
        contract = StageContract(stage)
        try:
            jd = ensure_job_dirs(self.cfg.path("workspace"), job_id)
            if claim.pipeline is not None and stage.name not in claim.pipeline["run"]:        # không được yêu cầu: đi qua máy trạng thái, không chạy
                imported = any(a["stage"] == "import" and a["kind"] in stage.produces for a in self.store.artifacts(job_id))
                reason = "provided" if imported else "not_requested"
                if self.store.succeed(claim, self.owner, [], {"skipped": reason}, status="skipped"):
                    log("stage_skipped", reason=reason)
                return
            inputs, package_kinds = contract.scope_inputs(self.store.inputs(job_id, stage.requires + stage.optional), claim.pipeline)
            ready, _missing = contract.can_run({k for k, v in inputs.items() if v})
            key = contract.stage_key(claim.params, claim.snapshot, inputs) if ready else None
            if key:
                self.store.set_run_key(claim.run_id, key)
            reason = contract.skip_reason(self.store, job_id, jd, key, claim.target_idx, claim.pipeline)
            if reason:                              # output đã hợp lệ (hoặc được cung cấp sẵn): KHÔNG chạy lại
                if self.store.succeed(claim, self.owner, [], {"skipped": reason}, status="skipped"):
                    log("stage_skipped", reason=reason)
                return
            contract.validate_inputs(inputs, jd, also=package_kinds)
            self.monitor.preflight(stage)
            sem = (claim.snapshot or {}).get("semantic", {})
            ctx = StageContext(job_id=job_id, stage=stage.name, attempt=claim.attempt, stage_key=key or "",
                               workspace=jd, stage_dir=jd / stage.workdir, params=claim.params, inputs=inputs,
                               config={"output_dir": str(self.cfg.path("output")),
                                       "tts_cache_dir": str(self.cfg.path("runtime") / "cache" / "tts"),
                                       "source": sem.get("source", self.cfg.data.get("source", {})),
                                       "render": sem.get("render", self.cfg.data.get("render", {})),
                                       "channel_config": sem.get("channel_config"),
                                       "publishing": sem.get("publishing", self.cfg.data.get("publishing", {}))},
                               cancel=self.cancel, log=log, progress=self._progress_fn(job_id, stage.name))
            log("stage_started", stage_key=(key or "")[:12])
            t0 = time.time()
            adapters = {**self._adapters_for(claim.snapshot), "sequence": self.sequence}
            result = HANDLERS[stage.name](ctx, **{n: adapters[n] for n in stage.adapters})
            arts = self._seal(stage, result, jd, contract)
            if self.store.succeed(claim, self.owner, arts, result.data):
                log("stage_succeeded", seconds=round(time.time() - t0, 3), artifacts=len(arts), data=result.data)
            else:
                log("lease_lost", "warning", note="kết quả bị bỏ vì lease đã hết hạn và job đã được nhận lại")
        except StageError as e:
            self._on_error(claim, e, log)
        except Exception as e:
            log("stage_exception", "error", error=repr(e), traceback=traceback.format_exc())
            self._on_error(claim, StageError(ErrorClass.POLICY, "UNEXPECTED", repr(e)), log)
        finally:
            self._manifest(job_id)

    def _on_error(self, claim: Claim, e: StageError, log) -> None:
        if e.error_class == ErrorClass.CANCELLED:
            self.store.release(claim, self.owner)
            log("stage_released", "warning")
            return
        outcome = self.store.handle_error(claim, self.owner, e, self._policy(claim.snapshot))
        log("stage_failed" if outcome in ("failed", "lost") else f"stage_{outcome}",
            "error" if outcome == "failed" else "warning", outcome=outcome, error=e.to_dict())

    @staticmethod
    def _seal(stage: P.Stage, result: StageResult, jd: Path, contract: StageContract | None = None) -> list[ArtifactRef]:
        base, out = jd.resolve(), []
        for d in result.artifacts:
            p = (jd / d.path).resolve()
            try:
                rel = p.relative_to(base).as_posix()
            except ValueError:
                raise StageError(ErrorClass.POLICY, "BAD_ARTIFACT_PATH", d.path) from None
            if d.kind not in stage.produces:
                raise StageError(ErrorClass.POLICY, "UNDECLARED_KIND", f"{stage.name} không khai báo '{d.kind}'")
            if not p.is_file():
                raise StageError(ErrorClass.POLICY, "MISSING_OUTPUT", rel)
            out.append({"path": rel, "kind": d.kind, "sha256": sha256_file(p), "bytes": p.stat().st_size, "meta": d.meta})
        for kind in stage.produces:
            if not any(a["kind"] == kind for a in out):
                raise StageError(ErrorClass.POLICY, "MISSING_OUTPUT", f"stage {stage.name} thiếu artifact '{kind}'")
        (contract or StageContract(stage)).validate_outputs(out, jd)
        return out
