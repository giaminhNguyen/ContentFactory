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

from ..contracts import ArtifactRef, CancelToken, ErrorClass, JobCancelToken, StageContext, StageError, StageResult
from ..fsutil import atomic_write, atomic_write_json, sha256_file
from ..jobs import manifest as M
from ..jobs import pipeline as P
from ..jobs.db import CONTROL_CANCELLED, CONTROL_DELETED, CONTROL_PAUSED, CONTROL_RUNNING, Claim, JobStore
from ..jobs.plan import Plan, plan_job, plan_spec, progress_floor
from ..jobs.policy import RetryPolicy
from ..jobs.workspace import ensure_job_dirs, job_dir
from .config import Config, _merge
from .handlers import HANDLERS
from .log import EventLog
from .monitor import DiskProbe, NetworkProbe, ResourceMonitor
from ..jobs.sequences import SequenceManager
from ..media import image_pool as IP
from ..story import guidance as GD
from . import auto as AU
from . import cleanup as CL
from . import channels as CH
from . import revisions as REV
from . import watermarks as WM
from . import templates as TPL
from .pools import PoolSyncService
from .registry import build_adapters
from ..tts import prosody as PRO
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
        self._tokens: dict[str, JobCancelToken] = {}                 # token của các job đang chạy stage (để Pause/Cancel phản hồi tức thì)
        self.pool_sync = PoolSyncService(self)
        self.image_pools = IP.ImagePools(cfg, self.store)
        self._batches = None
        self.sequence = SequenceManager(self.store)          # Sequence Manager dùng chung (trạng thái project, không phải cấu hình)
        self.watermarks = WM.Watermarks(cfg, self.store, self.adapters.get("audio"))     # Watermark Library theo kênh (revision bất biến, active, tham chiếu của job)

    def batch_service(self):
        """Channel Run (D-101); tạo lười để import không vòng. Dùng chung một thể hiện (giữ discovery tiêm cho test)."""
        if self._batches is None:
            from .batches import BatchService
            self._batches = BatchService(self)
        return self._batches

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
            if name == "sequence":                                   # thành phần nội bộ (SequenceManager): không phải adapter, không có health
                continue
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
               auto_resume: bool | None = None, pipeline: dict | None = None, batch_id: str | None = None, source_key: str | None = None) -> str:
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
        src = self._youtube_source(merged)                                               # nguồn chuẩn của job (không gọi mạng): link xem video + id dùng chống xử lý trùng
        if src and "source" not in merged:
            merged["source"] = src
        source_key = source_key or (f"youtube:{src['video_id']}" if src else None)
        if "story_guidance" in merged:                                                    # đề xuất truyện riêng của job: kiểm ngay (quá dài/rỗng bị từ chối, không cắt âm thầm); inherit = không lưu gì
            g = GD.parse(merged.pop("story_guidance"))
            if g["mode"] != GD.DEFAULT_MODE:
                merged["story_guidance"] = g
        if merged.get("prosody"):
            PRO.resolve_prosody(merged["prosody"])                                    # sai thì từ chối ngay lúc tạo job (không để lỗi nửa chừng ở stage TTS)
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
        if not merged.get("watermark"):
            wm = self.watermarks.resolve_active(channel)                                # watermark là channel asset (HANDOFF §10): chốt revision + sha256 vào job, đổi kênh sau đó không đổi job
            if wm:
                merged["watermark"], merged["watermark_ref"] = wm["path"], wm["ref"]
        job_id = self.store.create_job(merged, priority, state=P.STAGES[plan.start_idx].queue_state,
                                       start_stage=plan.start_stage, target_stage=plan.target_stage,
                                       auto_resume=resolved, snapshot=snap, config_hash=config_hash(snap["semantic"]), pipeline=spec,
                                       batch_id=batch_id, source_key=source_key, channel_id=chan_id)
        try:
            jd = ensure_job_dirs(self.cfg.path("workspace"), job_id)
            self._register_imports(job_id, jd, items)
            self._chot_thumbnail_source(job_id, merged, channel, plan.run, from_job)
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

    # -- Image Pool: chốt ảnh thumbnail cho job (Phase 8, D-105) ----------------------------------------------------------
    @staticmethod
    def _thumbnail_pool(channel: dict) -> tuple[str, str | None] | None:
        th = channel.get("thumbnail") or {}
        return (th["image_pool"], th.get("selection_mode")) if th.get("image_pool") else None

    def _thumbnail_params(self, job_id: str, params: dict, channel: dict, run, carry_from: str | None = None) -> dict | None:
        """Params mới có `thumbnail_source` nếu job có nhánh thumbnail + kênh cấu hình pool ảnh + chưa chốt; None nếu không cần đổi.
        Chốt MỘT lần: rút từ túi bền, sao chép vào workspace + sha256. Job nhân bản (`carry_from`) mang theo đúng ảnh của job gốc nếu bản sao còn."""
        if "render_youtube" not in run:
            return None
        ws = self.cfg.path("workspace")
        old = params.get("thumbnail_source")
        jd = job_dir(ws, job_id)
        if old:                                                                           # params nhân bản từ job khác: chép ảnh sang workspace của job mới
            if (jd / old["file"]).is_file():
                return None
            src = job_dir(ws, carry_from) / old["file"] if carry_from else None
            if src and src.is_file() and IP.sha256_file(src) == old["sha256"]:
                dst = jd / old["file"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                return None
        pool = self._thumbnail_pool(channel)
        if not pool:
            return None
        snap = self.image_pools.assign(pool[0], jd, mode=pool[1])
        dec = {"what": "thumbnail.image", "value": f"{snap['pool']}/{snap['source_relpath']}",
               "why": f"Ảnh thumbnail lấy từ pool ảnh '{snap['pool']}' của kênh (chế độ {snap['selection_mode']}); đã sao chép vào job nên retry dùng đúng ảnh này."}
        return {**params, "thumbnail_source": snap, "auto": list(params.get("auto") or []) + [dec]}

    def _chot_thumbnail_source(self, job_id: str, merged: dict, channel: dict, run, from_job) -> None:
        carry = from_job["job_id"] if isinstance(from_job, dict) else (from_job if isinstance(from_job, str) else None)
        params = self._thumbnail_params(job_id, merged, channel, run, carry)
        if params is not None:
            self.store.set_params(job_id, params, f"thumbnail source: {params['thumbnail_source']['pool']}/{params['thumbnail_source']['source_relpath']}")
            merged.update(params)

    def reroll_thumbnail(self, job_id: str, *, apply_policy: str = "after_current_safe_point") -> dict:
        """Hành động EXPLICIT “Đổi ảnh thumbnail”: rút ảnh KHÁC từ pool của job rồi cập nhật bằng revision (cùng impact/invalidation như mọi cập nhật):
        chỉ render_youtube (ảnh) và output đóng gói lại; Source/Story/TTS/Audio/TikTok giữ nguyên. Job đã hoàn tất/đã đăng KHÔNG bị đổi tại chỗ
        (impact chặn và gợi ý nhân bản) — không bao giờ âm thầm sửa bản đã đăng. Bấm đúp khi đang chờ áp dụng chỉ giữ MỘT thay đổi."""
        job = self.store.get_job(job_id)
        if job is None:
            raise ValueError(f"không có job {job_id}")
        old = job["params"].get("thumbnail_source")
        if not old:
            raise _spec_error("job này không dùng pool ảnh thumbnail (kênh chưa chọn pool lúc tạo job)", errors=["no_thumbnail_source"])
        pend = self.store.pending_revision(job_id)
        if pend and (pend["change"].get("params_patch") or {}).get("thumbnail_source"):
            return {"revision": pend["revision"], "status": "pending", "already": True, "thumbnail_source": pend["change"]["params_patch"]["thumbnail_source"],
                    "impact": pend.get("impact")}
        probe = self.preview_update(job_id, params_patch={"thumbnail_source": {"sha256": "reroll"}})        # chặn TRƯỚC khi rút ảnh (không lãng phí lượt túi)
        if not probe["ok"]:
            raise _spec_error("; ".join(probe["errors"]), errors=probe["errors"], blocked=probe.get("blocked"), clone_suggested=probe.get("clone_suggested"))
        new = self.image_pools.assign(old["pool"], job_dir(self.cfg.path("workspace"), job_id), mode=old.get("selection_mode"),
                                      avoid_sha=old["sha256"], rerolls=int(old.get("rerolls", 0)) + 1)
        res = self.request_update(job_id, params_patch={"thumbnail_source": new}, apply_policy=apply_policy)
        self.log.emit("thumbnail_rerolled", job_id=job_id, pool=new["pool"], source=new["source_relpath"], rerolls=new["rerolls"])
        return {**res, "thumbnail_source": new}

    @staticmethod
    def _youtube_source(params: dict) -> dict | None:
        inp = params.get("input") or {}
        if inp.get("kind") != "youtube_url":
            return None
        try:
            from ..source.youtube import parse_video_id
            vid = parse_video_id(str(inp.get("value") or ""))
        except StageError:
            return None
        return {"provider": "youtube", "video_id": vid, "video_url": f"https://www.youtube.com/watch?v={vid}"}

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
        if job["control_state"] == CONTROL_CANCELLED:
            return "cancelled"
        if job["control_state"] == CONTROL_PAUSED:                  # Tiếp tục sau Tạm dừng: chỉ gỡ pause thủ công, KHÔNG gỡ hold tài nguyên
            self.store.set_control(job_id, CONTROL_RUNNING)
            self.log.emit("job_unpaused", job_id=job_id, held=job["hold_reason"])
            self._manifest(job_id)
            return "unpaused"
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

    # -- điều khiển của người dùng: pause / cancel (D-99) ------------------------------------------------
    def pause_job(self, job_id: str, origin: str = "USER") -> str:
        """Tạm dừng AN TOÀN: đơn vị đang chạy (segment TTS, part TikTok…) được hoàn tất và ghi checkpoint rồi stage dừng; không stage/đơn vị mới được nhận.
        Khác hold tài nguyên (Auto Resume không bao giờ gỡ nó) và khác Cancel. Idempotent. Trả 'changed' | 'unchanged' | 'complete' | 'failed' | 'cancelled'."""
        res = self.store.set_control(job_id, CONTROL_PAUSED, origin)
        if res == "changed":
            self.log.emit("job_paused", job_id=job_id, origin=origin)
            tok = self._tokens.get(job_id)
            if tok:
                tok.request_pause()
            self._manifest(job_id)
        return res

    def cancel_job(self, job_id: str) -> str:
        """Hủy job (ý định riêng, không tự chạy lại): dừng đơn vị đang chạy NGAY nếu adapter hỗ trợ; artifact và lịch sử được giữ.
        Trả 'changed' | 'unchanged' | 'complete'. Job đã hoàn tất không bị đổi."""
        res = self.store.set_control(job_id, CONTROL_CANCELLED)
        if res == "changed":
            self.log.emit("job_cancelled", "warning", job_id)
            tok = self._tokens.get(job_id)
            if tok:
                tok.request_abort()
            self._manifest(job_id)
        return res

    def delete_job(self, job_id: str) -> str:
        """Xóa job (Sửa job → Danger zone). Tombstone nguyên tử: job biến khỏi danh sách, runner không nhận nữa, stage đang chạy bị abort HỢP TÁC (checkpoint đã hoàn tất
        không bị phá). Workspace nội bộ chỉ bị dọn khi KHÔNG còn worker giữ lease (ngay nếu job không chạy, ở tick sau nếu đang chạy). `output/` của người dùng
        KHÔNG BAO GIỜ bị xóa. Idempotent. Trả 'deleted' | 'already' | 'gone'."""
        res = self.store.mark_deleted(job_id)
        if res == "deleted":
            self.log.emit("job_deleted", "warning", job_id)
            tok = self._tokens.get(job_id)
            if tok:
                tok.request_abort()
        if res != "gone":
            self._guarded(self._finalize_deleted)
        return res

    def _finalize_deleted(self) -> None:
        """Dọn workspace nội bộ của job đã xóa khi không còn lease, và nhả số Full Audio nếu chưa đăng. Chỉ đụng workspace/<job>, không đụng output/."""
        ws = self.cfg.path("workspace")
        for j in self.store.deleted_jobs():
            if j["lease_owner"]:
                continue
            jd = job_dir(ws, j["id"])
            if jd.exists():
                shutil.rmtree(jd, ignore_errors=True)
                try:
                    self.sequence.release(j["id"])
                except ValueError:                                  # đã đăng với số này: giữ nguyên
                    pass

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
        """Mở rộng/đổi đích của job và để nó chạy tiếp (đường của `cf target`, dedupe Channel Run). Là `update_target` không giữ job đã xong."""
        self.update_target(job_id, target_stage, hold_completed=False)

    def update_target(self, job_id: str, target_stage: str, *, hold_completed: bool = True) -> dict:
        """Thao tác DUY NHẤT đổi đích pipeline của một job (Sửa job → Cập nhật pipeline; UI/API/CLI cùng đi qua đây).
        Chỉ đổi ĐÍCH TƯƠNG LAI: không lùi qua progress floor (stage đã bắt đầu/đã xong), không đổi start_stage, không xóa artifact/lịch sử, không kill stage đang chạy.
        Kế hoạch mới được tính bằng planner với artifact đang có (thiếu input cho đoạn mới thì từ chối, chưa đổi gì). Job chạy dở: runner đọc đích mới ở lần nhận
        stage kế tiếp. Job ĐÃ XONG mà đích mở rộng: lưu pipeline mới nhưng giữ job tạm dừng (origin EDIT) để KHÔNG tự chạy (`hold_completed`).
        Trả {"result": changed|unchanged, old_target, new_target, held}. Lỗi domain là StageError (JOB_NOT_FOUND, JOB_CANCELLED, PIPELINE_TARGET_INVALID,
        PIPELINE_TARGET_BEFORE_PROGRESS, JOB_UPDATE_CONFLICT)."""
        if target_stage not in P.INDEX:
            raise StageError(ErrorClass.POLICY, "PIPELINE_TARGET_INVALID", f"Bước đích không hợp lệ: {target_stage!r}.", {"valid": [s.name for s in P.STAGES]}, resource="input")
        for _ in range(5):                                                                  # CAS: runner có thể vừa chuyển stage giữa lúc tính kế hoạch và lúc ghi
            job = self.store.get_job(job_id)
            if job is None or job["control_state"] == CONTROL_DELETED:
                raise StageError(ErrorClass.POLICY, "JOB_NOT_FOUND", f"Không có job {job_id}.", {"hint": "Quay lại danh sách job."}, resource="input")
            floor = progress_floor(job, self.store.stage_runs(job_id))
            if P.INDEX[target_stage] < floor:                                                # chặn TRƯỚC khi chốt template/ảnh (store kiểm lại trong transaction)
                raise StageError(ErrorClass.POLICY, "PIPELINE_TARGET_BEFORE_PROGRESS", "Không đặt đích trước bước job đã chạy tới.",
                                 {"floor": P.STAGES[floor].name, "target": target_stage, "hint": "Chỉ chọn được bước hiện tại hoặc các bước phía sau."}, resource="input")
            plan, spec = self.target_plan(job, target_stage)
            if plan.errors:
                raise StageError(ErrorClass.POLICY, "PIPELINE_TARGET_INVALID", "; ".join(plan.errors),
                                 {"errors": plan.errors, "hint": "Bước này cần dữ liệu mà job chưa có."}, resource="input")
            params = self._params_for_target(job, plan, spec, target_stage)
            res = self.store.update_target(job_id, target_stage, expect_state=job["state"], pipeline=spec, params=params, hold_completed=hold_completed)
            if res["result"] == "conflict":
                continue
            if res["result"] == "changed":
                self.log.emit("job_target_updated", job_id=job_id, old=res["old_target"], new=res["new_target"], held=res["held"])
                self._manifest(job_id)
            return res
        raise StageError(ErrorClass.POLICY, "JOB_UPDATE_CONFLICT", "Job vừa đổi trạng thái, thử lại sau giây lát.", {"hint": "Bấm Lưu lại."}, resource="input")

    def target_plan(self, job: dict, target_stage: str, have: set[str] | None = None) -> tuple[Plan, dict | None]:
        """(kế hoạch, pipeline spec mới) cho job nếu đích là `target_stage`. Job kiểu start/target: spec None (chỉ đổi đích). Job có pipeline tùy chỉnh: giữ các stage
        đã yêu cầu nằm trước đích và thêm đích — không xóa lựa chọn của người dùng."""
        if have is None:
            have = {a["kind"] for a in self.store.artifacts(job["id"])}
        has_input = bool((job["params"].get("input") or {}).get("value"))
        start = job.get("start_stage")
        if job.get("pipeline") is None:
            return plan_job(start, target_stage, have, has_input), None
        idx = P.INDEX[target_stage]
        req = sorted({r for r in job["pipeline"]["requested_stages"] if P.INDEX[r] <= idx} | {target_stage}, key=P.INDEX.__getitem__)
        plan = plan_spec({"version": 2, "requested_stages": req, "options": job["pipeline"].get("options") or {}}, have, has_input, floor=start)
        return plan, {"version": 2, "requested_stages": plan.requested, "options": dict(job["pipeline"].get("options") or {}), "run": list(plan.run)}

    def _params_for_target(self, job: dict, plan: Plan, spec: dict | None, target_stage: str) -> dict | None:
        """Params mới nếu đích mở rộng tới render mà job chưa chốt template/thumbnail (chốt MỘT lần, lúc mở rộng); None nếu không cần đổi. Sai thì báo, chưa đổi đích."""
        run = spec["run"] if spec else plan.run
        if spec is not None:
            return self._templates_for_run(job, job["params"], run, changed=False)
        if P.INDEX[target_stage] >= P.INDEX["render_youtube"] and not job["params"].get("templates"):
            return self._legacy_templates(job)
        return None

    def _legacy_templates(self, job: dict) -> dict | None:
        channel = (job.get("config_snapshot") or {}).get("semantic", {}).get("channel_config") or CH.load_channel(self.cfg, str(job["params"].get("channel") or "default"))
        params = job["params"]
        tp = self._thumbnail_params(job["id"], params, channel, ["render_youtube"])                         # mở rộng đích tới render: chốt luôn ảnh thumbnail
        if tp is not None:
            params = tp
        tpls, tdec = TPL.select_templates(self.cfg, params, channel, self.adapters)
        if tpls:
            params = {**params, "templates": tpls, "auto": list(params.get("auto") or []) + tdec}
            for d in tdec:
                self.log.emit("auto_decision", job_id=job["id"], **d)
        return params if params is not job["params"] else None

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

    # -- cập nhật pipeline/config của job đang sống: impact -> revision -> áp dụng tại điểm an toàn (D-99) ----------------
    def preview_update(self, job_id: str, *, pipeline: dict | None = None, config_patch: dict | None = None, params_patch: dict | None = None) -> dict:
        """Impact của thay đổi (stage nào KEEP/REUSE/RUN/RERUN/REMOVE_FROM_PLAN/CURRENT_CONTINUE, job lùi về đâu). Không đổi gì."""
        job = self.store.get_job(job_id)
        if job is None:
            raise ValueError(f"không có job {job_id}")
        return REV.compute_impact(self, job, pipeline=pipeline, config_patch=config_patch, params_patch=params_patch)

    def request_update(self, job_id: str, *, pipeline: dict | None = None, config_patch: dict | None = None, params_patch: dict | None = None,
                       apply_policy: str = "after_current_safe_point", created_by: str = "user") -> dict:
        """Ghi một revision `pending` rồi áp dụng ngay nếu job đang ở điểm an toàn (không có stage chạy), ngược lại chờ:
        `after_current_safe_point` (mặc định) = sau khi đơn vị/stage hiện tại xong; `pause_and_apply` = dừng ở ranh giới checkpoint kế tiếp rồi áp dụng và chạy tiếp.
        Không bao giờ đổi pipeline giữa một đơn vị đang chạy. Bấm đúp cùng một thay đổi chỉ tạo một revision. Trả {revision, status, impact, ...}."""
        if apply_policy not in REV.POLICIES:
            raise _spec_error(f"apply_policy không hợp lệ: {apply_policy!r}; hợp lệ: {list(REV.POLICIES)}")
        impact = self.preview_update(job_id, pipeline=pipeline, config_patch=config_patch, params_patch=params_patch)
        if not impact["ok"]:
            raise _spec_error("; ".join(impact["errors"]), errors=impact["errors"], blocked=impact.get("blocked"), clone_suggested=impact.get("clone_suggested"))
        change = {"pipeline": {"requested_stages": impact["pipeline"]["requested_stages"]} if pipeline else None,
                  "config_patch": config_patch or None, "params_patch": params_patch or None}
        rev, created = self.store.create_revision(job_id, change, impact, apply_policy, created_by)
        if created:
            self.log.emit("pipeline_revision_requested", job_id=job_id, revision=rev["revision"], policy=apply_policy, rewind_to=impact["rewind_to"])
        tok = self._tokens.get(job_id)
        if apply_policy == "pause_and_apply" and tok:
            tok.request_pause()
        status = self.apply_pending(job_id)
        return {"revision": rev["revision"], "created": created, "status": status, "apply_policy": apply_policy, "impact": impact}

    def apply_pending(self, job_id: str) -> str:
        """Áp revision đang chờ nếu job ở điểm an toàn (không có stage đang chạy/lease). Trả 'applied' | 'pending' | 'rejected' | 'none'.
        Impact được tính lại tại thời điểm áp dụng (job có thể đã đi tiếp từ lúc yêu cầu) và ghi vào DB cùng transaction đổi pipeline;
        áp hai lần chỉ có một lần có hiệu lực."""
        rev = self.store.pending_revision(job_id)
        job = self.store.get_job(job_id)
        if rev is None or job is None:
            return "none"
        if job["state"] in P.BY_RUNNING or job.get("lease_owner"):
            return "pending"
        ch = rev["change"]
        impact = REV.compute_impact(self, job, pipeline=ch.get("pipeline"), config_patch=ch.get("config_patch"), params_patch=ch.get("params_patch"))
        if not impact["ok"]:
            return self._reject_revision(job_id, rev, impact)
        run = impact["pipeline"]["run"]
        snapshot = chash = None
        if ch.get("config_patch"):
            snapshot = apply_patch(job["config_snapshot"], ch["config_patch"])
            chash = config_hash(snapshot["semantic"])
        params = _merge(copy.deepcopy(job["params"]), copy.deepcopy(ch["params_patch"])) if ch.get("params_patch") else None
        try:
            params = self._templates_for_run(job, params if params is not None else job["params"], run, changed=params is not None)
        except StageError as e:
            return self._reject_revision(job_id, rev, {**impact, "ok": False, "errors": [e.message]})
        rewind, state = impact["rewind_to"], job["state"]
        new_state = state
        if state == P.FAILED:
            failed = job["failed_stage"]
            if rewind or failed not in run:                   # job lỗi: chỉ xếp lại khi pipeline mới thật sự đổi việc phải làm
                new_state = P.BY_NAME[rewind or failed].queue_state
        elif rewind:
            new_state = P.BY_NAME[rewind].queue_state
        old_start = P.INDEX[job["start_stage"]] if job.get("start_stage") else 0
        res = self.store.apply_revision(rev, expect_state=state, pipeline=impact["pipeline"],
                                        start_stage=P.STAGES[min(old_start, P.INDEX[run[0]])].name, target_stage=P.STAGES[max(P.INDEX[r] for r in run)].name,
                                        new_state=new_state, snapshot=snapshot, config_hash=chash, params=params, impact=impact)
        if res == "applied":
            self.log.emit("pipeline_revision_applied", job_id=job_id, revision=rev["revision"], rewind_to=rewind, state=new_state)
            self._manifest(job_id)
            return "applied"
        return "pending" if res == "not_safe" else "none"

    def _reject_revision(self, job_id: str, rev: dict, impact: dict) -> str:
        self.store.reject_revision(rev["id"], impact)
        self.log.emit("pipeline_revision_rejected", "warning", job_id, revision=rev["revision"], errors=impact["errors"])
        self._manifest(job_id)
        return "rejected"

    def _templates_for_run(self, job: dict, params: dict, run: list[str], changed: bool) -> dict | None:
        """Pipeline mới có nhánh render mà job chưa chốt template (vd tạo job chỉ TikTok rồi thêm YouTube): chốt lúc áp dụng, giống lúc tạo job.
        Trả params mới nếu có thay đổi, None nếu không đổi gì (và `changed` False)."""
        need = ({"youtube", "thumbnail"} if "render_youtube" in run else set()) | ({"tiktok"} if "render_tiktok" in run else set())
        missing = need - set(params.get("templates") or {})
        channel = (job.get("config_snapshot") or {}).get("semantic", {}).get("channel_config") or CH.load_channel(self.cfg, str(params.get("channel") or "default"))
        tp = self._thumbnail_params(job["id"], params, channel, run)
        if tp is not None:
            params, changed = tp, True
        if missing:
            tpls, tdec = TPL.select_templates(self.cfg, params, channel, self.adapters, missing)
            if tpls:
                params = {**params, "templates": {**(params.get("templates") or {}), **tpls}, "auto": list(params.get("auto") or []) + tdec}
                changed = True
        return params if changed else None

    def _apply_pending_revisions(self) -> None:
        for jid in dict.fromkeys(self.store.jobs_with_pending_revision()):
            self.apply_pending(jid)

    def clone_job(self, job_id: str, *, rerun_from: str | None = None, pipeline: dict | None = None, params_patch: dict | None = None) -> str:
        """“Chạy lại với thay đổi” (D-99): job đã hoàn tất/đã hủy KHÔNG bị đổi tại chỗ (output cũ giữ nguyên). Tạo job MỚI, dùng lại artifact còn hợp lệ
        của job cũ (from_job) ở các stage đứng TRƯỚC `rerun_from` (None = dùng lại tất cả: chỉ phần pipeline mới thêm mới chạy)."""
        src = self.store.get_job(job_id)
        if src is None:
            raise ValueError(f"không có job {job_id}")
        if rerun_from is not None and rerun_from not in P.INDEX:
            raise _spec_error(f"rerun_from không hợp lệ: {rerun_from!r}; hợp lệ: {[s.name for s in P.STAGES]}")
        imported = {a["kind"] for a in self.store.artifacts(job_id) if a["stage"] == "import"}
        spec = pipeline or {"requested_stages": REV.current_pipeline(src, imported)["requested_stages"]}
        keep = P.STAGES[:P.INDEX[rerun_from]] if rerun_from else P.STAGES
        kinds = sorted({k for s in keep for k in s.produces})
        params = {k: v for k, v in copy.deepcopy(src["params"]).items() if k not in ("ui", "auto")}      # `templates` giữ nguyên: snapshot cũ => kết quả tái lập được
        if params_patch:
            params = _merge(params, copy.deepcopy(params_patch))
        new = self.submit(params, pipeline={"version": 2, "requested_stages": spec["requested_stages"]}, from_job={"job_id": job_id, "kinds": kinds},
                          auto_resume=src.get("auto_resume"))
        self.log.emit("job_cloned", job_id=new, source=job_id, rerun_from=rerun_from)
        return new

    # -- vòng lặp chính ------------------------------------------------------------------------
    def run(self, until_idle: bool = True, stop: threading.Event | None = None) -> None:
        self.cancel = CancelToken()                 # token riêng cho mỗi lần run: instance dùng lại được sau khi đã dừng
        self._last_tick = 0.0
        for j in self.store.list_jobs():            # manifest là dẫn xuất: dựng lại nếu crash giữa commit và ghi file
            if j["state"] not in P.TERMINAL:
                self._manifest(j["id"])
        self._guarded(self.recover_batches)                  # item batch chưa có job (crash giữa lúc tạo) được hoàn tất trước khi chạy
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

    def recover_batches(self) -> None:
        n = self.batch_service().ensure_created()
        if n:
            self.log.emit("batch_recovered", items=n)

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
        self._guarded(self._finalize_deleted)
        for job in self.store.held_jobs():
            if job["needs_user"] or job["control_state"] != CONTROL_RUNNING:    # Auto Resume không bao giờ override Tạm dừng/Hủy của người dùng
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
        self._guarded(self._apply_pending_revisions)
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
        token = JobCancelToken(self.cancel, lambda: self.store.job_signal(job_id))
        self._tokens[job_id] = token
        try:
            token.check()                           # đã bị Tạm dừng/Hủy giữa lúc nhận job và lúc bắt đầu: nhả lại, không chạy
            jd = ensure_job_dirs(self.cfg.path("workspace"), job_id)
            if claim.pipeline is not None and stage.name not in claim.pipeline["run"]:        # không được yêu cầu: đi qua máy trạng thái, không chạy
                imported = any(a["stage"] == "import" and a["kind"] in stage.produces for a in self.store.artifacts(job_id))
                reason = "provided" if imported else "not_requested"
                if self.store.succeed(claim, self.owner, [], {"skipped": reason}, status="skipped"):
                    log("stage_skipped", reason=reason)
                return
            inputs, package_kinds = contract.scope_inputs(self.store.inputs(job_id, stage.requires + stage.optional), claim.pipeline)
            ready, _missing = contract.can_run({k for k, v in inputs.items() if v})
            extra = self.stage_extra(stage.name, claim.params)                    # ngữ cảnh quyết định lúc chạy (vd đề xuất truyện hiệu lực), chốt vào lần chạy này
            key = contract.stage_key(claim.params, claim.snapshot, inputs, GD.key_extra(extra.get("story_guidance"))) if ready else None
            self.store.set_run_key(claim.run_id, key, {"guidance": extra["story_guidance"]} if extra.get("story_guidance") else None)
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
                               cancel=token, log=log, progress=self._progress_fn(job_id, stage.name), extra=extra)
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
            self._tokens.pop(job_id, None)
            self._manifest(job_id)

    def stage_extra(self, stage: str, params: dict) -> dict:
        """Ngữ cảnh do orchestrator quyết định NGAY TRƯỚC khi chạy một stage (không nằm trong params): hiện chỉ `story` — đề xuất truyện hiệu lực
        (đề xuất riêng của job > mặc định hiện tại trong Cài đặt > không có), đọc MỘT chỗ duy nhất ở đây."""
        if stage != "story":
            return {}
        return {"story_guidance": GD.resolve(params, (self.cfg.data.get("story") or {}).get("guidance"), now=time.time())}

    def _on_error(self, claim: Claim, e: StageError, log) -> None:
        if e.error_class == ErrorClass.CANCELLED:
            self.store.release(claim, self.owner)
            tok = self._tokens.get(claim.job_id)
            log("stage_released", "warning", reason=(tok.reason if tok else "") or "shutdown")      # pause | abort | shutdown
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
