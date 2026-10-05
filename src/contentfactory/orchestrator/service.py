"""Facade cho giao diện (D-89): mọi thứ UI cần đều đi qua đây; không có logic nghiệp vụ nào nằm trong frontend.

Hàm thuần dữ liệu (dict/list JSON-able), không biết HTTP — test được trực tiếp trên một Orchestrator.
  - Chạy: `detect_input` (nhận dạng đầu vào + chế độ hợp lệ), `preview_run` (kế hoạch + lựa chọn tự động, không tạo job), `create_run` (idempotent + chống trùng).
  - Job: `list_jobs` (phân trang, bộ lọc, đếm), `job_detail` (pipeline từng stage, giữ/lỗi + cách đi tiếp, output, log), hành động resume/retry/auto-resume/open-output.
  - Kênh: danh sách, đọc/ghi (kiểm tra bằng đúng validator của core), xem trước tiêu đề/mô tả, tải asset.
Phần quản trị (TTS, pool, cài đặt, doctor) ở `service_admin.py`.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from ..contracts import ErrorClass, StageError, clean_title
from ..fsutil import atomic_write_json
from ..jobs import pipeline as P
from ..jobs.workspace import job_dir
from ..output import metadata as MD
from . import auto as AU
from . import channels as CH
from . import diagnose as DG
from . import ops

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"}
SUBTITLE_EXT = {".srt", ".vtt", ".json"}
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
FILTERS = {"all": None, "running": {"running", "queued"}, "waiting": {"waiting"}, "attention": {"attention", "failed"}, "completed": {"completed"}}
DEDUPE_WINDOW_S = 600

# id -> (nhãn, mô tả, đặc tả với orchestrator.submit)
RUN_MODES: dict[str, tuple[str, str, dict]] = {
    "full": ("Toàn bộ pipeline", "Từ đầu vào tới video YouTube + TikTok, gói output và đăng.", {"mode": "FULL"}),
    "through_tts": ("Đến hết giọng đọc", "Phụ đề → truyện → audio giọng đọc, dừng trước khi dựng video.", {"mode": "THROUGH_TTS"}),
    "story": ("Chỉ viết truyện", "Phụ đề → truyện (story.txt), không đọc.", {"mode": "STORY_ONLY"}),
    "subtitle": ("Chỉ lấy phụ đề", "Chỉ tải và làm sạch phụ đề.", {"mode": "SUBTITLE_ONLY"}),
    "story_full": ("Đọc + dựng video + đóng gói + đăng", "Từ truyện có sẵn tới hết pipeline.", {}),
    "tts_only": ("Chỉ đọc truyện thành giọng", "Từ story.txt có sẵn tới audio giọng đọc.", {"mode": "TTS_ONLY"}),
    "video_after_tts": ("Đọc + dựng video", "Từ story.txt tới video YouTube + TikTok, chưa đóng gói.", {"target_stage": "render_tiktok"}),
    "audio_full": ("Dựng video + đóng gói + đăng", "Từ audio có sẵn tới hết pipeline.", {"mode": "VIDEO_ONLY", "extend": "publish"}),
    "audio_package": ("Dựng video + đóng gói output", "Từ audio có sẵn tới thư mục output, chưa đăng.", {"mode": "VIDEO_ONLY", "extend": "output"}),
    "audio_video": ("Chỉ dựng video (YouTube + TikTok)", "Từ audio có sẵn, dừng sau khi render xong.", {"mode": "VIDEO_ONLY"}),
    "audio_youtube": ("Chỉ dựng video YouTube", "Từ audio có sẵn, chỉ render YouTube.", {"target_stage": "render_youtube"}),
}
KIND_MODES = {
    "youtube_url": ["full", "through_tts", "story", "subtitle"],
    "transcript_file": ["full", "through_tts", "story"],
    "story_text": ["story_full", "tts_only", "video_after_tts"],
    "audio": ["audio_full", "audio_package", "audio_video", "audio_youtube"],
}
KIND_LABEL = {"youtube_url": "Link YouTube", "transcript_file": "Phụ đề / transcript", "story_text": "Truyện (story.txt)", "audio": "Audio có sẵn",
              "project": "Project đã có", "unknown": "Không nhận dạng được"}
NEEDS_TITLE = {"story_text", "audio"}                 # không có nguồn tiêu đề nào khác ⇒ người dùng phải đặt tên truyện
MAX_LOG_BYTES = 256 * 1024


def _err(code: str, message: str, hint: str = "", **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, message, {**detail, **({"hint": hint} if hint else {})}, resource="input")


class Service:
    def __init__(self, orc) -> None:
        self.orc = orc
        self.cfg = orc.cfg
        self._lock = threading.Lock()
        self._titles: dict[str, str] = {}
        self._outputs: dict[str, dict] = {}
        self._req_file = self.cfg.path("runtime") / "ui_requests.json"
        self._requests: dict[str, str] = self._load_requests()

    # ================================================================================== đầu vào
    def detect_input(self, value: str, kind: str | None = None) -> dict:
        """Nhận dạng đầu vào và trả các chế độ chạy HỢP LỆ (UI không bao giờ hỏi start_stage)."""
        v = (value or "").strip().strip('"')
        out = {"value": v, "kind": "unknown", "label": KIND_LABEL["unknown"], "ok": False, "ambiguous": False, "alternatives": [], "modes": [],
               "needs_title": False, "details": {}, "problem": None}
        if not v:
            return out
        if re.match(r"^https?://", v, re.I):
            u = urlparse(v)
            host = (u.hostname or "").lower()
            if host not in YT_HOSTS:
                out["problem"] = "Chỉ hỗ trợ link YouTube (youtube.com hoặc youtu.be)."
                return out
            vid = (re.search(r"[?&]v=([\w-]{6,})", v) or re.search(r"youtu\.be/([\w-]{6,})", v) or re.search(r"/(?:shorts|embed|live)/([\w-]{6,})", v))
            if not vid:
                out["problem"] = "Link YouTube này không có mã video (v=...)."
                return out
            return self._finish(out, "youtube_url", {"video_id": vid.group(1), "host": host})
        p = Path(v).expanduser()
        if not p.exists():
            out["problem"] = "Không tìm thấy file/thư mục này. Dán link YouTube hoặc đường dẫn đầy đủ."
            return out
        if p.is_dir():
            pj = p / "project.json"
            if pj.is_file():
                try:
                    d = json.loads(pj.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    d = {}
                det = {"title": (d.get("project") or {}).get("title"), "job_id": d.get("job_id"), "version": d.get("version"), "path": str(p)}
                o = self._finish(out, "project", det)
                o["ok"], o["modes"] = False, []
                o["problem"] = None
                return o
            out["problem"] = "Thư mục này không phải một project ContentFactory (thiếu project.json)."
            return out
        ext = p.suffix.lower()
        det = {"name": p.name, "bytes": p.stat().st_size, "path": str(p)}
        if ext in AUDIO_EXT:
            return self._finish(out, "audio", det)
        if ext in SUBTITLE_EXT - {".json"}:
            return self._finish(out, "transcript_file", det)
        if ext in (".txt", ".md", ".json"):
            guess = "story_text" if re.search(r"story|truyen|truyện", p.stem, re.I) else "transcript_file"
            kind = kind if kind in ("story_text", "transcript_file") else guess
            o = self._finish(out, kind, det)
            o["ambiguous"] = ext == ".txt"
            o["alternatives"] = [k for k in ("story_text", "transcript_file") if k != kind] if o["ambiguous"] else []
            return o
        out["problem"] = f"Không hỗ trợ định dạng {ext or '(không có đuôi)'}. Dùng .srt/.vtt/.txt (phụ đề, truyện) hoặc .wav/.mp3 (audio)."
        return out

    def _finish(self, out: dict, kind: str, details: dict) -> dict:
        out.update(kind=kind, label=KIND_LABEL[kind], ok=True, details=details, needs_title=kind in NEEDS_TITLE,
                   modes=[{"id": m, "label": RUN_MODES[m][0], "description": RUN_MODES[m][1]} for m in KIND_MODES.get(kind, [])])
        return out

    # ================================================================================== kế hoạch / chạy
    def _spec(self, det: dict, run: str, title: str | None, channel: str, kids: bool | None, auto_resume: bool | None) -> tuple[dict, dict]:
        """(params, submit_kwargs) từ đầu vào đã nhận dạng. Thiếu gì thì báo bằng StageError dễ hiểu."""
        if not det["ok"]:
            raise _err("INVALID_INPUT", det.get("problem") or "Đầu vào không hợp lệ", "Dán link YouTube hoặc chọn file phụ đề/truyện/audio.")
        if run not in {m["id"] for m in det["modes"]}:
            raise _err("INVALID_RUN_MODE", f"Chế độ '{run}' không dùng được với {det['label']}.", "Chọn một trong các chế độ được đề xuất.")
        label, _, spec = RUN_MODES[run]
        kind, value = det["kind"], det["value"]
        params: dict = {"channel": channel}
        kw: dict = {k: v for k, v in spec.items() if k in ("mode", "target_stage", "start_stage")}
        inputs: dict = {}
        title = (title or "").strip()
        if kind == "youtube_url":
            params["input"] = {"kind": "youtube_url", "value": value}
        elif kind == "transcript_file":
            params["input"] = {"kind": "transcript_file", "value": str(Path(value).expanduser().resolve())}
        elif kind == "story_text":
            inputs["story_text"] = str(Path(value).expanduser().resolve())
        elif kind == "audio":
            inputs["audio_master"] = str(Path(value).expanduser().resolve())
        if kind in NEEDS_TITLE:
            if not title:
                raise _err("MISSING_TITLE", "Cần đặt tên truyện (project.title) cho đầu vào này.", "Điền ô 'Tên truyện': nó dùng cho thumbnail, tiêu đề YouTube và tên thư mục output.")
            inputs["metadata"] = {"title": title}
        if title:
            params["project"] = {"title": title}
        if inputs:
            kw["inputs"] = inputs
        if kids is not None:
            params["made_for_kids"] = bool(kids)
        if auto_resume is not None:
            kw["auto_resume"] = bool(auto_resume)
        kw["_extend"] = spec.get("extend")
        return params, kw

    def _target_reaches(self, run: str) -> int:
        label, _, spec = RUN_MODES[run]
        t = spec.get("extend") or spec.get("target_stage") or (P.MODES[spec["mode"]][1] if "mode" in spec else None)
        return P.INDEX[t] if t else len(P.STAGES) - 1

    def _channel_or_error(self, channel_id: str) -> dict:
        return CH.load_channel(self.cfg, channel_id)

    def preview_run(self, payload: dict) -> dict:
        """Kế hoạch (stage nào chạy/bỏ qua) + những gì hệ thống sẽ tự chọn + việc còn thiếu. Không tạo job, không gọi mạng."""
        det = self.detect_input((payload.get("input") or {}).get("value", ""), (payload.get("input") or {}).get("kind"))
        channel_id = str(payload.get("channel") or self.cfg.data["job_defaults"].get("channel") or "default")
        res = {"detect": det, "channel": channel_id, "problems": [], "auto": [], "plan": None, "needs_kids": False, "sequence_next": None, "can_run": False}
        try:
            ch = self._channel_or_error(channel_id)
        except StageError as e:
            res["problems"].append({"code": e.code, "message": e.message})
            return res
        res["channel_name"] = ch["name"]
        res["sequence_next"] = int(ch["sequence"].get("last_used", 0)) + 1
        run = payload.get("run") or (det["modes"][0]["id"] if det["modes"] else None)
        res["run"] = run
        if not det["ok"]:
            if det.get("problem"):
                res["problems"].append({"code": "INVALID_INPUT", "message": det["problem"]})
            return res
        if not run:
            return res
        try:
            params, kw = self._spec(det, run, payload.get("title"), channel_id, payload.get("kids"), None)
        except StageError as e:
            res["problems"].append({"code": e.code, "message": e.message, "hint": (e.detail or {}).get("hint"), "field": "title" if e.code == "MISSING_TITLE" else None})
            return res
        preset, decisions = AU.preset_params(self.cfg, ch, params, self.orc.adapters)
        merged = {**self.cfg.data["job_defaults"], **preset, **params}
        decisions += AU.select_pools(self.cfg, merged, ch, self.orc.adapters)
        res["auto"] = decisions
        extend = kw.pop("_extend", None)
        kw.pop("auto_resume", None)
        try:
            plan = self.orc.plan(params, **{k: v for k, v in kw.items() if k in ("mode", "target_stage", "start_stage", "inputs")})
            target = plan.target_stage
            if extend:
                target = extend
            lo, hi = P.INDEX[plan.start_stage], P.INDEX[target or "publish"]
            res["plan"] = {"start": plan.start_stage, "target": target, "run": plan.run, "skip": plan.skip,
                           "stages": [{"name": st.name, "label": DG.STAGE_LABEL[st.name],
                                       "state": ("skip" if st.name in plan.skip else "run") if lo <= k <= hi else "off"} for k, st in enumerate(P.STAGES)]}
            for e in plan.errors:
                res["problems"].append({"code": "INVALID_JOBSPEC", "message": e})
        except StageError as e:
            res["problems"].append({"code": e.code, "message": e.message})
        reaches_publish = self._target_reaches(run) >= P.INDEX["publish"]
        declared = (ch.get("publishing") or {}).get("made_for_kids")
        res["needs_kids"] = bool(reaches_publish and not isinstance(declared, bool) and payload.get("kids") is None)
        if res["needs_kids"]:
            res["problems"].append({"code": "MISSING_MADE_FOR_KIDS", "field": "kids",
                                    "message": "Kênh này chưa khai báo video có dành cho trẻ em hay không (khai báo bắt buộc của YouTube).", "hint": "Chọn Có/Không bên dưới; có thể ghi nhớ cho kênh."})
        res["can_run"] = not res["problems"]
        res["privacy"] = (ch.get("publishing") or {}).get("privacy") or self.cfg.data["publishing"]["defaults"]["privacy"]
        res["warnings"] = []
        if det["kind"] == "youtube_url" and not (payload.get("title") or "").strip() and reaches_publish:
            res["warnings"].append("Chưa đặt tên truyện: hệ thống dùng tiêu đề video nguồn đã làm sạch (kèm cảnh báo trong gói output). Nên đặt tên riêng.")
        return res

    def create_run(self, payload: dict) -> dict:
        """Tạo job. Hai lớp chống trùng: `request_id` (bấm đúp/gửi lại cùng một yêu cầu) và chữ ký nội dung (cùng đầu vào đang chạy)."""
        rid = str(payload.get("request_id") or "")
        with self._lock:
            if rid and rid in self._requests and self.orc.store.get_job(self._requests[rid]):
                return {"job_id": self._requests[rid], "deduped": True, "reason": "request"}
            inp = payload.get("input") or {}
            det = self.detect_input(inp.get("value", ""), inp.get("kind"))
            channel_id = str(payload.get("channel") or self.cfg.data["job_defaults"].get("channel") or "default")
            run = payload.get("run") or (det["modes"][0]["id"] if det["modes"] else "")
            params, kw = self._spec(det, run, payload.get("title"), channel_id, payload.get("kids"), payload.get("auto_resume"))
            ch = self._channel_or_error(channel_id)
            reaches_publish = self._target_reaches(run) >= P.INDEX["publish"]
            if reaches_publish and not isinstance((ch.get("publishing") or {}).get("made_for_kids"), bool) and "made_for_kids" not in params:
                raise _err("MISSING_MADE_FOR_KIDS", "Kênh chưa khai báo video có dành cho trẻ em hay không.", "Chọn Có/Không rồi chạy lại.")
            sig = hashlib.sha1(json.dumps([det["value"], det["kind"], channel_id, run, (payload.get("title") or "").strip()], ensure_ascii=False).encode()).hexdigest()[:16]
            now = time.time()
            for j in self.orc.store.job_index()[:200]:
                if DG.ui_status({**j, "target_idx": j["target_idx"]}) in ("completed", "failed") or now - j["updated_at"] > DEDUPE_WINDOW_S * 6:
                    continue
                full = self.orc.store.get_job(j["id"])
                if (full["params"].get("ui") or {}).get("sig") == sig and now - full["created_at"] < DEDUPE_WINDOW_S * 6:
                    if rid:
                        self._remember(rid, full["id"])
                    return {"job_id": full["id"], "deduped": True, "reason": "same_input_running"}
            params["ui"] = {"sig": sig, "request_id": rid or None}
            extend = kw.pop("_extend", None)
            job_id = self.orc.submit(params, **kw)
            if extend:
                self.orc.set_target(job_id, extend)
            if payload.get("remember_kids") and "made_for_kids" in params:
                self._save_kids(channel_id, bool(params["made_for_kids"]))
            if rid:
                self._remember(rid, job_id)
            return {"job_id": job_id, "deduped": False}

    def _save_kids(self, channel_id: str, value: bool) -> None:
        f = CH.channel_dir(self.cfg, channel_id) / "channel.json"
        raw = json.loads(f.read_text(encoding="utf-8-sig")) if f.is_file() else {}
        raw.setdefault("publishing", {})["made_for_kids"] = value
        MD.normalize_channel(raw, channel_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(f, raw)

    def _load_requests(self) -> dict[str, str]:
        try:
            return json.loads(self._req_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _remember(self, rid: str, job_id: str) -> None:
        self._requests[rid] = job_id
        if len(self._requests) > 300:
            for k in list(self._requests)[:100]:
                self._requests.pop(k, None)
        try:
            atomic_write_json(self._req_file, self._requests)
        except OSError:
            pass

    # ================================================================================== danh sách / chi tiết job
    def title_of(self, j: dict) -> str:
        t = ((j["params"].get("project") or {}).get("title") or "").strip()
        if t:
            return t
        cached = self._titles.get(j["id"])
        if cached:
            return cached
        for a in self.orc.store.artifacts(j["id"]):
            if a["kind"] == "metadata":
                try:
                    md = json.loads((job_dir(self.cfg.path("workspace"), j["id"]) / a["path"]).read_text(encoding="utf-8"))
                    t = clean_title(str(md.get("title") or ""))
                    if t:
                        self._titles[j["id"]] = t
                        return t
                except (OSError, ValueError):
                    pass
        v = (j["params"].get("input") or {}).get("value") or ""
        return (Path(v).name if v and not v.startswith("http") else v) or f"Job {j['id']}"

    def summary(self, j: dict) -> dict:
        st = DG.ui_status(j)
        stage = DG.stage_of(j)
        cp = (j.get("checkpoint") or {}).get(stage) if stage else None
        frac = self._fraction(j, cp)
        row = {"id": j["id"], "title": self.title_of(j), "channel": j["params"].get("channel") or "default", "status": st, "state": j["state"],
               "stage": stage, "stage_label": DG.STAGE_LABEL.get(stage, "Hoàn tất" if st == "completed" else ""), "progress": j.get("progress"),
               "fraction": frac, "created_at": j["created_at"], "updated_at": j["updated_at"], "auto_resume": j.get("auto_resume"),
               "input_kind": (j["params"].get("input") or {}).get("kind") or "import", "next_action": None, "hold": None}
        if st in ("waiting", "attention", "failed"):
            d = DG.explain(self.orc, j["id"])
            row["hold"] = {"title": (d["hold"] or {}).get("title") or d["stage_label"], "reason": d.get("reason_code")}
            row["next_action"] = d["resume"]["actions"][0] if d["resume"]["actions"] else None
        if st == "completed":
            o = self.output_info(j["id"])
            row["output_dir"] = o.get("project_dir")
            row["youtube_url"] = o.get("youtube_url")
        return row

    def _fraction(self, j: dict, cp: dict | None) -> float:
        start = P.INDEX[j["start_stage"]] if j.get("start_stage") else 0
        target = j["target_idx"] if j.get("target_idx") is not None else len(P.STAGES) - 1
        planned = max(1, target - start + 1)
        pos = P.position(j["state"])
        if j["state"] == P.FAILED:
            pos = P.INDEX.get(j.get("failed_stage") or "", start)
        if pos is None:
            return 0.0
        done = min(planned, max(0, pos - start))
        part = 0.0
        if cp and cp.get("total") and j["state"] in P.BY_RUNNING:
            part = min(1.0, (cp.get("done") or 0) / cp["total"])
        return round(min(1.0, (done + part) / planned), 3)

    def list_jobs(self, status: str = "all", limit: int = 30, offset: int = 0, since: str | None = None) -> dict:
        version = self.orc.store.jobs_version()
        if since and since == version:
            return {"changed": False, "version": version}
        idx = self.orc.store.job_index()
        groups = {k: 0 for k in FILTERS}
        keep = FILTERS.get(status)
        ids = []
        for r in idx:
            g = DG.ui_status(r)
            groups["all"] += 1
            for name, members in FILTERS.items():
                if members and g in members:
                    groups[name] += 1
            if keep is None or g in keep:
                ids.append(r["id"])
        page = ids[offset: offset + limit]
        jobs = [self.summary(j) for j in self.orc.store.jobs_by_ids(page)]
        return {"changed": True, "version": version, "counts": groups, "jobs": jobs, "total": len(ids), "offset": offset, "limit": limit,
                "has_more": offset + limit < len(ids)}

    def job_detail(self, job_id: str) -> dict:
        j = self.orc.store.get_job(job_id)
        if j is None:
            raise _err("JOB_NOT_FOUND", f"Không có job {job_id}.", "Quay lại danh sách job.")
        runs = self.orc.store.stage_runs(job_id)
        d = DG.explain(self.orc, job_id)
        s = self.summary(j)
        s.update(version=self.orc.store.jobs_version(), diagnosis=d, pipeline=self._pipeline(j, runs), decisions=j["params"].get("auto", []),
                 mode={"start": j.get("start_stage"), "target": j.get("target_stage")}, params_public=self._public_params(j["params"]),
                 output=(lambda o: o if o.get("project_dir") else None)(self.output_info(job_id)),
                 attempts=[{"stage": r["stage"], "attempt": r["attempt"], "status": r["status"], "started_at": r["started_at"], "ended_at": r["ended_at"]} for r in runs][-40:])
        return s

    @staticmethod
    def _public_params(p: dict) -> dict:
        keep = ("input", "channel", "language", "project", "tiktok", "made_for_kids")
        return {k: p[k] for k in keep if k in p}

    def _pipeline(self, j: dict, runs: list[dict]) -> list[dict]:
        n = len(P.STAGES)
        start = P.INDEX[j["start_stage"]] if j.get("start_stage") else 0
        target = j["target_idx"] if j.get("target_idx") is not None else n - 1
        failed = j["state"] == P.FAILED
        pos = P.INDEX.get(j.get("failed_stage") or "", 0) if failed else (P.position(j["state"]) if P.position(j["state"]) is not None else 0)
        imported = {a["kind"] for a in self.orc.store.artifacts(j["id"]) if a["stage"] == "import"}
        rows = []
        for i, st in enumerate(P.STAGES):
            mine = [r for r in runs if r["stage"] == st.name]
            last = mine[-1] if mine else None
            if i < start:
                state = "provided" if imported & set(st.produces) else "not_planned"
            elif i > target:
                state = "not_planned"
            elif i < pos or j["state"] == P.PUBLISHED:
                state = "reused" if last and last["status"] == "skipped" else "done"
            elif i == pos:
                state = "failed" if failed else "held" if j.get("hold_reason") else "running" if j["state"] in P.BY_RUNNING else "waiting"
            else:
                state = "waiting"
            cp = (j.get("checkpoint") or {}).get(st.name) or {}
            items = [{"name": k, "state": v.get("state"), "error": v.get("error")} for k, v in (cp.get("parts") or cp.get("outputs") or {}).items()]
            dur = sum(((r["ended_at"] or time.time()) - r["started_at"]) for r in mine if r["ended_at"] or r["status"] == "running")
            rows.append({"name": st.name, "label": DG.STAGE_LABEL[st.name], "state": state, "attempts": len(mine),
                         "done": cp.get("done") if state in ("running", "held", "failed") else None, "total": cp.get("total") if state in ("running", "held", "failed") else None,
                         "detail": cp.get("detail") if state in ("running", "held", "failed") else None, "items": items if state != "waiting" else [],
                         "seconds": round(dur, 1) if dur else None})
        return rows

    # ================================================================================== output / log
    def output_info(self, job_id: str) -> dict:
        cached = self._outputs.get(job_id)
        if cached:
            return cached
        jd = job_dir(self.cfg.path("workspace"), job_id)
        info: dict = {}
        for a in self.orc.store.artifacts(job_id):
            try:
                if a["kind"] == "output_package":
                    pkg = json.loads((jd / a["path"]).read_text(encoding="utf-8"))
                    info.update(project_dir=pkg.get("project_dir"), version=pkg.get("version"), files=pkg.get("files", []), reused=pkg.get("reused"))
                elif a["kind"] == "publish_result":
                    r = json.loads((jd / a["path"]).read_text(encoding="utf-8"))
                    info.update(youtube_url=r.get("remote_url"), remote_id=r.get("remote_id"), uploaded_sequence=r.get("sequence"))
                elif a["kind"] == "publish_metadata":
                    m = json.loads((jd / a["path"]).read_text(encoding="utf-8"))
                    info.update(youtube_title=m.get("youtube_title"), description=m.get("description"), sequence=m.get("sequence"), warnings=m.get("warnings", []),
                                project_title=m.get("project_title"), title_source=m.get("title_source"), channel_name=m.get("channel_name"))
            except (OSError, ValueError):
                continue
        if info.get("project_dir"):
            info["exists"] = Path(info["project_dir"]).is_dir()
            tt = [f for f in info.get("files", []) if f.startswith("tiktok/")]
            info["tiktok_parts"] = len(tt)
            if info.get("youtube_url") or info.get("project_dir"):
                self._outputs[job_id] = info
        return info

    def open_output(self, job_id: str, opener=ops.open_path) -> dict:
        """Mở thư mục output của job. Chỉ mở đường dẫn do chính pipeline ghi và nằm trong thư mục output; không nhận đường dẫn từ client."""
        j = self.orc.store.get_job(job_id)
        if j is None:
            raise _err("JOB_NOT_FOUND", f"Không có job {job_id}.")
        info = self.output_info(job_id)
        d = info.get("project_dir")
        if not d:
            raise _err("NO_OUTPUT", "Job này chưa có gói output.", "Chờ job chạy tới bước đóng gói output.")
        p = Path(d)
        if not p.is_dir():
            raise _err("OUTPUT_MOVED", "Thư mục output đã bị di chuyển hoặc xóa.", "Nó thuộc về bạn nên hệ thống không tạo lại; chạy lại từ bước output nếu cần.", path=str(p))
        root = self.cfg.path("output").resolve()
        if root not in p.resolve().parents and p.resolve() != root:
            raise _err("OUTPUT_OUTSIDE", "Đường dẫn output không nằm trong thư mục output đã cấu hình.", path=str(p))
        opener(str(p))
        return {"opened": str(p)}

    def job_log(self, job_id: str, tail: int = 150, before: int | None = None) -> dict:
        """`tail` dòng cuối của job.log.jsonl (đọc từ cuối, giới hạn dung lượng); `before` = vị trí byte để lấy trang cũ hơn."""
        f = job_dir(self.cfg.path("workspace"), job_id) / "job.log.jsonl"
        if not f.is_file():
            return {"lines": [], "start": 0, "has_more": False}
        size = f.stat().st_size
        end = size if before is None else min(before, size)
        start = max(0, end - MAX_LOG_BYTES)
        with open(f, "rb") as fh:
            fh.seek(start)
            chunk = fh.read(end - start)
        lines = chunk.split(b"\n")
        if start > 0:
            lines = lines[1:]                                   # dòng đầu có thể bị cắt giữa chừng
        lines = [x for x in lines if x.strip()]
        take = lines[-tail:]
        consumed = sum(len(x) + 1 for x in take)
        out = []
        for x in take:
            try:
                r = json.loads(x)
                out.append({"ts": r.get("ts"), "level": r.get("level"), "event": r.get("event"), "stage": r.get("stage"), "text": self._log_text(r)})
            except ValueError:
                out.append({"ts": None, "level": "info", "event": "raw", "stage": None, "text": x.decode("utf-8", "replace")[:300]})
        new_start = end - consumed
        return {"lines": out, "start": max(0, new_start), "has_more": new_start > 0}

    @staticmethod
    def _log_text(r: dict) -> str:
        skip = {"ts", "level", "event", "job_id", "stage", "attempt", "traceback"}
        extra = {k: v for k, v in r.items() if k not in skip}
        s = json.dumps(extra, ensure_ascii=False, default=str) if extra else ""
        return s[:400]

    # ================================================================================== hành động trên job
    def resume(self, job_id: str, now: bool = False) -> dict:
        res = self.orc.resume(job_id, now=now)
        msg = {"resumed": "Đã tiếp tục job.", "still_down": "Nguyên nhân vẫn còn (tài nguyên chưa sẵn sàng); job được giữ nguyên.",
               "not_held": "Job này không bị giữ."}[res]
        return {"result": res, "message": msg}

    def retry(self, job_id: str) -> dict:
        j = self.orc.store.get_job(job_id)
        if j is None or j["state"] != P.FAILED:
            raise _err("NOT_FAILED", "Chỉ chạy lại được job đang ở trạng thái lỗi.")
        stage = self.orc.retry(job_id)
        return {"stage": stage, "message": f"Đã xếp lại stage '{DG.STAGE_LABEL.get(stage, stage)}'; các stage trước giữ nguyên."}

    def set_auto_resume(self, job_id: str, enabled: bool) -> dict:
        if self.orc.store.get_job(job_id) is None:
            raise _err("JOB_NOT_FOUND", f"Không có job {job_id}.")
        self.orc.set_auto_resume(job_id, bool(enabled))
        return {"auto_resume": bool(enabled), "message": "Đã bật Auto Resume." if enabled else "Đã tắt Auto Resume."}

    # ================================================================================== kênh
    def list_channels(self) -> dict:
        rows = ops.list_channels(self.cfg)
        default = self.cfg.data["job_defaults"].get("channel") or "default"
        return {"channels": rows, "default": default}

    def get_channel(self, channel_id: str) -> dict:
        d = CH.channel_dir(self.cfg, channel_id)
        f = d / "channel.json"
        raw = json.loads(f.read_text(encoding="utf-8-sig")) if f.is_file() else None
        if raw is None and not d.is_dir():
            raise _err("CHANNEL_NOT_FOUND", f"Không có kênh '{channel_id}'.", "Tạo kênh mới ở trang Kênh.")
        ch = CH.load_channel(self.cfg, channel_id)
        raw = raw if raw is not None else {}
        profiles = [{"name": n, "engine": p.get("engine"), "status": p.get("status")} for n, p in AU.list_tts_profiles(self.cfg)]
        pools = sorted(((self.cfg.data.get("render") or {}).get("pools") or {}).keys())
        files = sorted(x.name for x in d.iterdir() if x.is_file() and x.name != "channel.json") if d.is_dir() else []
        return {"id": channel_id, "raw": raw, "channel": {k: v for k, v in ch.items() if k != "loaded_from"}, "assets": files,
                "options": {"tts_profiles": profiles, "pools": pools, "privacy": ["private", "unlisted", "public"]}}

    def save_channel(self, channel_id: str, raw: dict, create: bool = False) -> dict:
        d = CH.channel_dir(self.cfg, channel_id)
        if create and (d / "channel.json").exists():
            raise _err("CHANNEL_EXISTS", f"Kênh '{channel_id}' đã có.")
        if not isinstance(raw, dict):
            raise _err("INVALID_CHANNEL_CONFIG", "Cấu hình kênh phải là object.")
        raw = {k: v for k, v in raw.items() if k not in ("id", "loaded_from")}
        MD.normalize_channel(raw, channel_id)                  # đúng validator của core: sai thì báo ngay, không ghi
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_json(d / "channel.json", raw)
        return {"saved": True, "id": channel_id}

    def create_channel(self, channel_id: str, name: str | None, kids: bool, last_used: int = 0) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", channel_id or ""):
            raise _err("INVALID_CHANNEL_ID", "Mã kênh chỉ gồm chữ không dấu, số, _ và -, tối đa 40 ký tự.", "Ví dụ: kenh_a")
        f = ops.channel_init(self.cfg, channel_id, name or channel_id, kids=kids, last_used=last_used)
        return {"created": True, "id": channel_id, "path": str(f)}

    def channel_preview(self, channel_id: str, title: str | None) -> dict:
        ch = CH.load_channel(self.cfg, channel_id)
        seq = int(ch["sequence"].get("last_used", 0)) + 1
        project = {"id": "preview", "title": (title or "Tên truyện mẫu").strip() or "Tên truyện mẫu", "title_source": "user", "channel_id": channel_id,
                   "channel_name": ch["name"], "language": "vi"}
        pm = MD.build(project, ch, seq)
        return {"youtube_title": pm["youtube_title"], "description": pm["description"], "sequence": seq, "thumbnail": {"channel_name": ch["name"], "title": project["title"]},
                "privacy": (ch.get("publishing") or {}).get("privacy") or "private"}

    def save_channel_asset(self, channel_id: str, name: str, data: bytes) -> dict:
        d = CH.channel_dir(self.cfg, channel_id)
        safe = re.sub(r"[^\w.\-]", "_", Path(name).name)[:80]
        if not safe or safe.startswith("."):
            raise _err("INVALID_ASSET_NAME", "Tên file không hợp lệ.")
        if Path(safe).suffix.lower() not in AUDIO_EXT | {".png", ".jpg", ".jpeg"}:
            raise _err("INVALID_ASSET_TYPE", "Chỉ nhận file audio (watermark) hoặc ảnh.")
        d.mkdir(parents=True, exist_ok=True)
        (d / safe).write_bytes(data)
        return {"name": safe, "bytes": len(data)}
