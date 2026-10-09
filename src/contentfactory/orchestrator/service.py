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
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from ..contracts import ErrorClass, StageError, clean_title, thumb_channel_line
from ..fsutil import atomic_write_json
from ..jobs import pipeline as P
from ..jobs.plan import plan_spec, spec_for_mode
from ..jobs.workspace import job_dir
from ..media import image_pool as IPOOL
from ..source import discovery as DISC
from ..story import guidance as GD
from ..story import mode as SM
from ..story import presets as SP
from ..story_remix import estimate as EST
from ..output import metadata as MD
from ..tts import prosody as PRO
from . import auto as AU
from . import channels as CH
from . import diagnose as DG
from . import ops
from . import preflight as PF
from . import revisions as REV
from . import templates as TPL
from .service_jobedit import JobEditService
from .service_watermarks import WatermarkService
from .service_templates import Raw

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"}
SUBTITLE_EXT = {".srt", ".vtt", ".json"}
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
FILTERS = {"all": None, "running": {"running", "queued"}, "waiting": {"waiting", "paused"}, "attention": {"attention", "failed"}, "completed": {"completed"}}
ACTION_VI = {"KEEP": "Giữ nguyên", "REUSE": "Dùng lại", "RUN": "Sẽ chạy", "RERUN": "Chạy lại", "REMOVE_FROM_PLAN": "Bỏ khỏi kế hoạch",
             "CURRENT_CONTINUE": "Đang chạy, làm nốt", "BLOCKED": "Không áp dụng được", "OFF": "Không chạy"}
DEDUPE_WINDOW_S = 600
# Timeline của bước trong job (Phase 9): trạng thái chuẩn hoá + nhóm nhánh để UI tách YouTube/TikTok thay vì giả vờ mọi thứ đều cần.
TIMELINE_LABEL = {"DONE": "Xong", "REUSED": "Dùng lại", "AVAILABLE": "Có sẵn", "RUNNING": "Đang chạy", "QUEUED": "Chờ tới lượt", "PAUSED": "Tạm dừng",
                  "FAILED": "Lỗi", "NOT_REQUESTED": "Không yêu cầu", "INVALIDATED": "Cần chạy lại"}
BRANCH = {"render_youtube": "youtube", "render_tiktok": "tiktok", "output": "package", "publish": "youtube"}

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
COLLECTION_KINDS = {"youtube_channel": "Kênh YouTube", "youtube_playlist": "Playlist YouTube"}
KIND_LABEL = {**COLLECTION_KINDS, "youtube_url": "Link YouTube", "transcript_file": "Phụ đề / transcript", "story_text": "Truyện (story.txt)", "audio": "Audio có sẵn",
              "project": "Project đã có", "unknown": "Không nhận dạng được"}
# Loại đầu vào -> (artifact đã có sẵn, có params.input để stage source chạy). Dùng cho plan của pipeline tùy chỉnh.
INPUT_PROVIDES = {"youtube_url": (set(), True), "transcript_file": (set(), True), "story_text": ({"story_text", "metadata"}, False),
                  "audio": ({"audio_master", "metadata"}, False)}
KIND_VI = {"subtitle_raw": "phụ đề thô", "transcript_structured": "phụ đề đã dựng câu", "transcript": "phụ đề đã làm sạch", "metadata": "thông tin video",
           "story_text": "truyện", "story_report": "báo cáo truyện", "audio_master": "audio giọng đọc", "tts_manifest": "bản kê giọng đọc",
           "audio_timeline": "mốc thời gian audio", "narration_master": "audio chuẩn hóa", "audio_youtube": "audio cho YouTube", "audio_tiktok": "audio cho TikTok",
           "audio_report": "báo cáo audio", "video_youtube": "video YouTube", "thumbnail": "thumbnail", "youtube_render_report": "báo cáo render YouTube",
           "video_tiktok": "video TikTok", "tiktok_render_report": "báo cáo render TikTok", "output_package": "gói output", "publish_metadata": "tiêu đề/mô tả đăng",
           "publish_result": "kết quả đăng"}
NEEDS_TITLE = {"story_text", "audio"}                 # không có nguồn tiêu đề nào khác ⇒ người dùng phải đặt tên truyện
GENERIC_NAMES = {"story", "audio", "narration", "master", "audio_master", "transcript", "text", "untitled", "output", "input"}
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
        self.jobedit = JobEditService(orc, self)
        self.watermarks = WatermarkService(orc)

    # ================================================================================== đầu vào
    def detect_input(self, value: str, kind: str | None = None) -> dict:
        """Nhận dạng đầu vào và trả các chế độ chạy HỢP LỆ (UI không bao giờ hỏi start_stage)."""
        v = (value or "").strip().strip('"')
        out = {"value": v, "kind": "unknown", "label": KIND_LABEL["unknown"], "ok": False, "ambiguous": False, "alternatives": [], "modes": [],
               "needs_title": False, "details": {}, "problem": None}
        if not v:
            return out
        if re.fullmatch(r"@[\w.\-]{2,60}", v, re.U):                       # @tên-kênh trần = kênh YouTube
            v = f"https://www.youtube.com/{v}"
            out["value"] = v
        if re.match(r"^https?://", v, re.I):
            u = urlparse(v)
            host = (u.hostname or "").lower()
            if host not in YT_HOSTS:
                out["problem"] = "Chỉ hỗ trợ link YouTube (youtube.com hoặc youtu.be)."
                return out
            vid = (re.search(r"[?&]v=([\w-]{6,})", v) or re.search(r"youtu\.be/([\w-]{6,})", v) or re.search(r"/(?:shorts|embed|live)/([\w-]{6,})", v))
            if not vid:
                try:
                    info = DISC.classify(v)                                    # kênh / playlist: không tạo một job; mở luồng Channel Run
                except StageError:
                    info = None
                if info and info["kind"] in ("channel", "playlist"):
                    o = self._finish(out, f"youtube_{info['kind']}", {"id": info["id"], "canonical_url": info["canonical_url"], "host": host})
                    o["collection"] = True                                         # chọn kiểu chạy cho các job con như video đơn
                    o["modes"] = [{"id": m, "label": RUN_MODES[m][0], "description": RUN_MODES[m][1]} for m in KIND_MODES["youtube_url"]]
                    return o
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
        out.update(kind=kind, label=KIND_LABEL[kind], ok=True, details=details, needs_title=kind in NEEDS_TITLE and self._title_required(), auto_title=kind in NEEDS_TITLE and not self._title_required(),
                   modes=[{"id": m, "label": RUN_MODES[m][0], "description": RUN_MODES[m][1]} for m in KIND_MODES.get(kind, [])])
        return out

    # ================================================================================== kế hoạch / chạy
    @staticmethod
    def _custom(payload: dict) -> dict | None:
        """Pipeline tùy chỉnh từ payload {"pipeline": {"mode": "custom", "requested_stages": [...]}}; mode khác/không có = chế độ chạy theo loại đầu vào."""
        p = payload.get("pipeline")
        if isinstance(p, dict) and p.get("mode") == "custom":
            return {"version": 2, "requested_stages": p.get("requested_stages"), "options": {}}
        return None

    def _spec(self, det: dict, run: str | None, title: str | None, channel: str, kids: bool | None, auto_resume: bool | None,
              custom: dict | None = None) -> tuple[dict, dict]:
        """(params, submit_kwargs) từ đầu vào đã nhận dạng. Thiếu gì thì báo bằng StageError dễ hiểu."""
        if not det["ok"]:
            raise _err("INVALID_INPUT", det.get("problem") or "Đầu vào không hợp lệ", "Dán link YouTube hoặc chọn file phụ đề/truyện/audio.")
        if custom is not None:
            if det["kind"] not in INPUT_PROVIDES:
                raise _err("INVALID_RUN_MODE", f"Pipeline tùy chỉnh không dùng được với {det['label']}.", "Chọn link YouTube, phụ đề, truyện hoặc audio.")
            spec: dict = {}
            kw: dict = {"pipeline": custom}
        else:
            if run not in {m["id"] for m in det["modes"]}:
                raise _err("INVALID_RUN_MODE", f"Chế độ '{run}' không dùng được với {det['label']}.", "Chọn một trong các chế độ được đề xuất.")
            label, _, spec = RUN_MODES[run]
            kw = {k: v for k, v in spec.items() if k in ("mode", "target_stage", "start_stage")}
        kind, value = det["kind"], det["value"]
        params: dict = {"channel": channel}
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
        title_source = "user"
        if kind in NEEDS_TITLE:
            if not title:
                if self._title_required():
                    raise _err("MISSING_TITLE", "Cần đặt tên truyện (project.title) cho đầu vào này.", "Điền ô 'Tên truyện': nó dùng cho thumbnail, tiêu đề YouTube và tên thư mục output.")
                title, title_source = self._auto_title(value), "auto"                              # để trống: lấy từ tên file/thư mục
            inputs["metadata"] = {"title": title}
        if title:
            params["project"] = {"title": title, **({"title_source": title_source} if title_source != "user" else {})}
        if inputs:
            kw["inputs"] = inputs
        if kids is not None:
            params["made_for_kids"] = bool(kids)
        if auto_resume is not None:
            kw["auto_resume"] = bool(auto_resume)
        kw["_extend"] = spec.get("extend")
        return params, kw

    def _title_required(self) -> bool:
        return (self.cfg.data.get("publishing") or {}).get("title_policy") == "require"          # cấu hình "Bắt buộc": không tự đặt tên

    @staticmethod
    def _auto_title(path: str) -> str:
        """Tên truyện tự tạo từ tên file; tên chung chung (story.txt, audio.wav…) thì dùng tên thư mục chứa nó."""
        p = Path(path)
        name = p.stem if p.stem.lower() not in GENERIC_NAMES else p.parent.name or p.stem
        return clean_title(name)

    @staticmethod
    def _require_kids(ch: dict, params: dict, reaches_publish: bool) -> None:
        if reaches_publish and not isinstance((ch.get("publishing") or {}).get("made_for_kids"), bool) and "made_for_kids" not in params:
            raise _err("MISSING_MADE_FOR_KIDS", "Kênh chưa khai báo video có dành cho trẻ em hay không.", "Chọn Có/Không rồi chạy lại.")

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
        custom = self._custom(payload)
        run = None if custom else payload.get("run") or (det["modes"][0]["id"] if det["modes"] else None)
        res["run"] = run
        if not det["ok"]:
            if det.get("problem"):
                res["problems"].append({"code": "INVALID_INPUT", "message": det["problem"]})
            return res
        if det.get("collection"):                                              # kênh/playlist: kế hoạch + kiểm tra thuộc về từng video (UI gọi lại với link video đầu tiên)
            res["collection"] = True
            return res
        if not run and custom is None:
            return res
        try:
            params, kw = self._spec(det, run, payload.get("title"), channel_id, payload.get("kids"), None, custom)
        except StageError as e:
            res["problems"].append({"code": e.code, "message": e.message, "hint": (e.detail or {}).get("hint"), "field": "title" if e.code == "MISSING_TITLE" else None})
            return res
        preset, decisions = AU.preset_params(self.cfg, ch, params, self.orc.adapters)
        merged = {**self.cfg.data["job_defaults"], **preset, **params}
        decisions += AU.select_pools(self.cfg, merged, ch, self.orc.adapters)
        res["auto"] = decisions
        extend = kw.pop("_extend", None)
        kw.pop("auto_resume", None)
        plan = None
        try:
            plan = self.orc.plan(params, **{k: v for k, v in kw.items() if k in ("mode", "target_stage", "start_stage", "inputs", "pipeline")})
            target = plan.target_stage
            if extend:
                target = extend
            lo, hi = P.INDEX[plan.start_stage], P.INDEX[target or "publish"]
            if custom is not None:
                infos = {st.name: plan.states.get(st.name) or {"state": "not_requested", "by": [], "kinds": []} for st in P.STAGES}
                stages = [{"name": st.name, "label": DG.STAGE_LABEL[st.name], "role": infos[st.name]["state"], "by": infos[st.name]["by"],
                           "reason": self._stage_reason(infos[st.name]) if plan.states else "",
                           "state": {"selected": "run", "locked": "run", "provided": "skip"}.get(infos[st.name]["state"], "off")} for st in P.STAGES]
            else:
                stages = [{"name": st.name, "label": DG.STAGE_LABEL[st.name],
                           "state": ("skip" if st.name in plan.skip else "run") if lo <= k <= hi else "off"} for k, st in enumerate(P.STAGES)]
            res["plan"] = {"start": plan.start_stage, "target": target, "run": plan.run, "skip": plan.skip, "stages": stages}
            for e in plan.errors:
                res["problems"].append({"code": "INVALID_JOBSPEC", "message": e})
        except StageError as e:
            res["problems"].append({"code": e.code, "message": e.message})
        tkinds = None if plan is None else self.orc._template_kinds(plan, {"run": plan.run} if custom is not None else None)
        tpls_ok = None
        if res["plan"] and (tkinds is None or tkinds) and not (plan and plan.errors):                       # template của kênh dùng được không (báo sớm, trước khi bấm RUN)
            try:
                tpls, tdec = TPL.select_templates(self.cfg, merged, ch, self.orc.adapters, tkinds)
                res["auto"] = res["auto"] + tdec
                res["templates"] = {k: {"id": v["id"], "version": v["version"], "name": v["name"]} for k, v in tpls.items()}
                tpls_ok = res["templates"]
            except StageError as e:
                res["problems"].append({"code": e.code, "message": e.message, "hint": "Mở Kênh → Template và chọn template đã publish."})
        reaches_publish = ("publish" in plan.run) if (custom is not None and plan is not None) else (custom is None and self._target_reaches(run) >= P.INDEX["publish"])
        declared = (ch.get("publishing") or {}).get("made_for_kids")
        res["needs_kids"] = bool(reaches_publish and not isinstance(declared, bool) and payload.get("kids") is None)
        if res["needs_kids"]:
            res["problems"].append({"code": "MISSING_MADE_FOR_KIDS", "field": "kids",
                                    "message": "Kênh này chưa khai báo video có dành cho trẻ em hay không (khai báo bắt buộc của YouTube).", "hint": "Chọn Có/Không bên dưới; có thể ghi nhớ cho kênh."})
        res["preflight"] = PF.run(self.orc, plan.run, ch, merged, tpls_ok) if plan is not None and not plan.errors else None       # chỉ kiểm thứ kế hoạch này cần (D-106)
        res["can_run"] = not res["problems"] and not (res["preflight"] or {}).get("blocking")
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
            if det.get("collection"):
                raise _err("USE_CHANNEL_RUN", f"Đây là {det['label'].lower()}: cần chọn video rồi tạo Channel Run, không tạo một job.", "Dùng “Quét kênh/playlist” để chọn video.")
            channel_id = str(payload.get("channel") or self.cfg.data["job_defaults"].get("channel") or "default")
            custom = self._custom(payload)
            run = None if custom else payload.get("run") or (det["modes"][0]["id"] if det["modes"] else "")
            params, kw = self._spec(det, run, payload.get("title"), channel_id, payload.get("kids"), payload.get("auto_resume"), custom)
            ch = self._channel_or_error(channel_id)
            if custom is not None:
                cplan = self.orc.plan(params, pipeline=custom, inputs=kw.get("inputs"))
                reaches_publish = "publish" in cplan.run
            else:
                reaches_publish = self._target_reaches(run) >= P.INDEX["publish"]
            self._require_kids(ch, params, reaches_publish)
            sig = hashlib.sha1(json.dumps([det["value"], det["kind"], channel_id, run, (payload.get("title") or "").strip()]
                                          + ([custom["requested_stages"]] if custom else []), ensure_ascii=False).encode()).hexdigest()[:16]
            now = time.time()
            for j in self.orc.store.job_index()[:200]:
                if DG.ui_status({**j, "target_idx": j["target_idx"]}) in ("completed", "failed") or now - j["updated_at"] > DEDUPE_WINDOW_S * 6:
                    continue
                full = self.orc.store.get_job(j["id"])
                if (full["params"].get("ui") or {}).get("sig") == sig and now - full["created_at"] < DEDUPE_WINDOW_S * 6:
                    if rid:
                        self._remember(rid, full["id"])
                    return {"job_id": full["id"], "deduped": True, "reason": "same_input_running"}
            if payload.get("story_guidance") is not None:                                     # đề xuất truyện riêng của job (mặc định: dùng đề xuất trong Cài đặt)
                params["story_guidance"] = payload["story_guidance"]
            if payload.get("story_mode") is not None:                                          # chế độ truyện (Story hiện có | Story Remix); thiếu = mặc định trong Cài đặt
                params["story_mode"] = payload["story_mode"]
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

    # ================================================================================== pipeline (descriptor + plan)
    def pipeline_descriptor(self) -> dict:
        """Nguồn sự thật cho bộ chọn stage của UI: thứ tự/phụ thuộc lấy thẳng từ `P.STAGES` (frontend không có đồ thị riêng)."""
        stages = [{"id": s.name, "label": DG.STAGE_LABEL[s.name], "order": (i + 1) * 10, "requires": list(s.requires), "produces": list(s.produces),
                   "optional": list(s.optional), "resource": P.resource_of(s), "deliverable": s.deliverable,
                   "packages": [{"stage": p, "kinds": list(k)} for p, k in s.packages]} for i, s in enumerate(P.STAGES)]
        return {"version": 2, "stages": stages, "modes": {m: spec_for_mode(m) for m in P.MODES}}

    @staticmethod
    def _kinds_vi(kinds: list[str]) -> str:
        return ", ".join(KIND_VI.get(k, k) for k in kinds)

    def _stage_reason(self, info: dict) -> str:
        by = ", ".join(DG.STAGE_LABEL[b] for b in info["by"])
        if info["state"] == "selected":
            return "Bạn đã chọn bước này." + (f" {by} cũng cần nó." if by else "")
        if info["state"] == "locked":
            return f"Bắt buộc: {by} cần {self._kinds_vi(info['kinds'])}."
        if info["state"] == "provided":
            return f"Dùng lại {self._kinds_vi(info['kinds'])} đã có, bước này không chạy."
        return "Không chạy."

    def plan_pipeline(self, payload: dict) -> dict:
        """Xem trước kế hoạch của một pipeline spec (không tạo job): bước nào chạy / bắt buộc / dùng lại / bỏ qua, lỗi, và adapter cần chuẩn bị."""
        from ..jobs.plan import plan_spec
        kind = payload.get("input_kind") or "youtube_url"
        provided, has_input = INPUT_PROVIDES.get(kind, (set(), True))
        extra = {str(k) for k in (payload.get("provided_artifacts") or [])}
        known = {k for s in P.STAGES for k in s.produces}
        bad = sorted(extra - known)
        plan = plan_spec(payload.get("pipeline_spec"), set(provided) | (extra & known), has_input)
        errors = list(plan.errors) + ([f"artifact không hợp lệ: {bad}"] if bad else [])
        stages = []
        for i, s in enumerate(P.STAGES):
            info = plan.states.get(s.name) or {"state": "not_requested", "by": [], "kinds": []}
            stages.append({"id": s.name, "label": DG.STAGE_LABEL[s.name], "order": (i + 1) * 10, "state": info["state"], "by": info["by"],
                           "kinds": info["kinds"], "reason": self._stage_reason(info) if plan.states else ""})
        run = [s for s in P.STAGES if s.name in plan.run]
        return {"ok": not errors, "errors": errors, "requested": plan.requested, "run": plan.run, "reuse": plan.reuse, "stages": stages,
                "requirements": {"adapters": sorted({a for s in run for a in s.adapters}), "resources": sorted({P.resource_of(s) for s in run})}}

    # ================================================================================== danh sách / chi tiết job
    def title_of(self, j: dict) -> str:
        p = j["params"].get("project") or {}
        t = (p.get("title") or "").strip()
        if t and p.get("title_source") != "auto":
            return t
        cached = self._titles.get(j["id"])
        if cached:
            return cached
        arts = self.orc.store.artifacts(j["id"])
        for a in arts:                                                              # tên AI tự nghĩ (cùng quy tắc với project_of)
            if a["kind"] == "story_report":
                try:
                    st = str(json.loads((job_dir(self.cfg.path("workspace"), j["id"]) / a["path"]).read_text(encoding="utf-8")).get("story_title") or "").strip()
                except (OSError, ValueError):
                    st = ""
                if st:
                    self._titles[j["id"]] = st
                    return st
        if t:
            return t
        for a in arts:
            if a["kind"] == "metadata":
                try:
                    md = json.loads((job_dir(self.cfg.path("workspace"), j["id"]) / a["path"]).read_text(encoding="utf-8"))
                    t = clean_title(str(md.get("title") or ""))
                    if t:
                        if j["state"] in P.TERMINAL:                                 # job còn chạy: tên AI có thể xuất hiện sau bước story, đừng ghim tên nguồn
                            self._titles[j["id"]] = t
                        return t
                except (OSError, ValueError):
                    pass
        v = (j["params"].get("input") or {}).get("value") or ""
        return (Path(v).name if v and not v.startswith("http") else v) or f"Job {j['id']}"

    @staticmethod
    def _safe_yt(url) -> str | None:
        """Chỉ trả link https tới YouTube (đã được backend dựng từ id đã kiểm hoặc do uploader trả về); chuỗi lạ/javascript:/host khác => None."""
        if not isinstance(url, str):
            return None
        u = urlparse(url.strip())
        host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
        return url.strip() if u.scheme == "https" and host in ("youtube.com", "youtu.be") and not u.username else None

    def _links(self, j: dict, meta: bool = False) -> dict:
        """Link chuẩn do BACKEND giữ (D-101): video nguồn, kênh nguồn, video đã đăng. Frontend không tự đoán URL."""
        src = j["params"].get("source") or {}
        ch_url, ch_title = src.get("channel_url"), src.get("channel_title")
        if meta and not ch_url:
            for a in self.orc.store.artifacts(j["id"]):
                if a["kind"] == "metadata":
                    try:
                        md = json.loads((job_dir(self.cfg.path("workspace"), j["id"]) / a["path"]).read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        break
                    ch_url = DISC.channel_url(channel_id=md.get("channel_id")) or md.get("channel_url")
                    ch_title = ch_title or md.get("channel") or md.get("uploader")
                    break
        return {"source_video_url": self._safe_yt(src.get("video_url")), "source_channel_url": self._safe_yt(ch_url), "source_channel_title": ch_title, "published_video_url": None}

    def summary(self, j: dict) -> dict:
        st = DG.ui_status(j)
        stage = DG.stage_of(j)
        cp = (j.get("checkpoint") or {}).get(stage) if stage else None
        frac = self._fraction(j, cp)
        row = {"id": j["id"], "title": self.title_of(j), "channel": j["params"].get("channel") or "default", "status": st, "state": j["state"],
               "control": j.get("control_state") or "RUNNING", "pausing": (j.get("control_state") == "PAUSED" and j["state"] in P.BY_RUNNING),
               "stage": stage, "stage_label": DG.STAGE_LABEL.get(stage, "Hoàn tất" if st == "completed" else ""), "progress": j.get("progress"),
               "fraction": frac, "created_at": j["created_at"], "updated_at": j["updated_at"], "auto_resume": j.get("auto_resume"),
               "input_kind": (j["params"].get("input") or {}).get("kind") or "import", "next_action": None, "hold": None}
        if st in ("waiting", "attention", "failed", "paused"):
            d = DG.explain(self.orc, j["id"])
            row["hold"] = {"title": (d["hold"] or {}).get("title") or d["stage_label"], "reason": d.get("reason_code")}
            row["next_action"] = d["resume"]["actions"][0] if d["resume"]["actions"] else None
        row["links"] = self._links(j)
        row["batch_id"] = j.get("batch_id")
        if st == "completed":
            o = self.output_info(j["id"])
            row["output_dir"] = o.get("project_dir")
            row["youtube_url"] = o.get("youtube_url")
            row["links"]["published_video_url"] = self._safe_yt(o.get("youtube_url"))
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

    def list_jobs(self, status: str = "all", limit: int = 30, offset: int = 0, since: str | None = None, q: str | None = None, kind: str | None = None,
                  channel: str | None = None, days: int | None = None) -> dict:
        """Danh sách cấp cao (job đơn + Channel Run). Bộ lọc phụ (Phase 9): `q` tìm theo tiêu đề/URL/mã video/mã job (không phân biệt hoa thường, bỏ dấu cách thừa),
        `kind` single|channel, `channel` = kênh xuất bản, `days` = tạo trong N ngày gần đây. Số đếm các nhóm trạng thái tính SAU các bộ lọc phụ để khớp với danh sách."""
        q = " ".join(str(q or "").lower().split())
        kind = kind if kind in ("single", "channel") else None
        days = int(days) if days and int(days) > 0 else None
        fkey = hashlib.sha1(json.dumps([q, kind, channel or "", days], ensure_ascii=False).encode()).hexdigest()[:8] if (q or kind or channel or days) else ""
        version = self.orc.store.jobs_version() + (f"|{fkey}" if fkey else "")
        if since and since == version:
            return {"changed": False, "version": version}
        store = self.orc.store
        bs = self.orc.batch_service()
        by_batch, item_st = store.batch_jobs_all(), store.batch_item_statuses()
        cutoff = time.time() - days * 86400 if days else None
        top: list[tuple[float, str, str, str, dict | None]] = []                       # (created_at, loại, id, nhóm hiển thị, tóm tắt batch)
        singles = [] if kind == "channel" else [r for r in store.job_index() if not r.get("batch_id")]       # job con của Channel Run KHÔNG là hàng cấp cao
        if channel:
            singles = [r for r in singles if (r.get("channel_id") or "") == channel]
        if cutoff:
            singles = [r for r in singles if r["created_at"] >= cutoff]
        if q and singles:
            hay = {}
            for chunk in range(0, len(singles), 200):
                for j in store.jobs_by_ids([r["id"] for r in singles[chunk:chunk + 200]]):
                    hay[j["id"]] = j
            singles = [r for r in singles if r["id"] in hay and self._matches(q, [r["id"], r.get("source_key"), r.get("input_value"), self.title_of(hay[r["id"]])])]
        for r in singles:
            top.append((r["created_at"], "job", r["id"], DG.ui_status(r), None))
        for b in ([] if kind == "single" else store.list_batches()):
            if channel and b["output_channel_id"] != channel:
                continue
            if cutoff and b["created_at"] < cutoff:
                continue
            if q and not self._matches(q, [b["id"], b.get("source_title"), b.get("source_url"), b.get("source_id"), b.get("source_channel_title"),
                                           *[f for it in store.batch_items(b["id"]) for f in (it.get("title"), it.get("source_video_id"))]]):
                continue
            s = bs.summary(b, by_batch.get(b["id"], []), item_st.get(b["id"], []))
            top.append((b["created_at"], "batch", b["id"], s["ui_status"], s))
        top.sort(key=lambda t: t[0], reverse=True)
        groups = {k: 0 for k in FILTERS}
        keep = FILTERS.get(status)
        picked = []
        for t in top:
            groups["all"] += 1
            for name, members in FILTERS.items():
                if members and t[3] in members:
                    groups[name] += 1
            if keep is None or t[3] in keep:
                picked.append(t)
        page = picked[offset: offset + limit]
        full = {j["id"]: j for j in store.jobs_by_ids([t[2] for t in page if t[1] == "job"])}
        rows = [({**self.summary(full[t[2]]), "type": "job"} if t[1] == "job" else {**t[4], "status": t[4]["ui_status"], "batch_status": t[4]["status"]})   # hàng batch dùng nhóm hiển thị chung với job
                for t in page if t[1] == "batch" or t[2] in full]
        return {"changed": True, "version": version, "counts": groups, "jobs": rows, "total": len(picked), "offset": offset, "limit": limit,
                "has_more": offset + limit < len(picked)}

    LANE_LABEL = {"gpu": "Render (GPU)", "tts": "Giọng đọc (TTS)"}

    def dashboard(self) -> dict:
        """Bảng nhanh trả lời MỘT câu hỏi: có việc gì cần người dùng xử lý không? Đếm theo JOB (kể cả job con của Channel Run) vì đó mới là khối lượng thật."""
        store = self.orc.store
        rows = store.job_index()
        now = time.localtime()
        midnight = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
        c = {"running": 0, "queued": 0, "waiting": 0, "paused": 0, "attention": 0, "completed_today": 0}
        lanes_used: dict[str, int] = {}
        attention_ids = []
        for r in rows:
            st = DG.ui_status(r)
            if st == "running":
                c["running"] += 1
                stage = P.BY_RUNNING.get(r["state"])
                if stage is not None:
                    key = stage.resource or stage.name
                    lanes_used[key] = lanes_used.get(key, 0) + 1
            elif st == "queued":
                c["queued"] += 1
            elif st == "waiting":
                c["waiting"] += 1
            elif st == "paused":
                c["paused"] += 1
            elif st in ("attention", "failed"):
                c["attention"] += 1
                attention_ids.append(r["id"])
            elif st == "completed" and r["updated_at"] >= midnight:
                c["completed_today"] += 1
        limits = self.cfg.data.get("limits") or {}
        lanes = [{"id": k, "label": self.LANE_LABEL[k], "used": lanes_used.get(k, 0), "limit": int(limits.get(k, limits.get("default", 2)))} for k in ("gpu", "tts")]
        disk = []
        for label, p in (("Workspace", self.cfg.path("workspace")), ("Output", self.cfg.path("output"))):
            try:
                du = shutil.disk_usage(p if p.exists() else p.anchor or ".")
                disk.append({"id": label.lower(), "label": label, "free_gb": round(du.free / 2 ** 30, 1), "low": du.free / 2 ** 30 < 2.0})
            except OSError:
                pass
        needs = []
        for j in store.jobs_by_ids(attention_ids[:5]):
            s = self.summary(j)
            needs.append({"id": j["id"], "title": s["title"], "stage_label": s["stage_label"], "reason": (s.get("hold") or {}).get("title"), "status": s["status"]})
        batches_running = sum(1 for b in store.list_batches() if self.orc.batch_service().summary(b, store.batch_jobs_all().get(b["id"], []), store.batch_item_statuses().get(b["id"], []))["status"] == "running")
        return {**c, "lanes": lanes, "disk": disk, "needs_attention": needs, "batches_running": batches_running,
                "headline": ("Có việc cần bạn xử lý" if c["attention"] else "Không có việc cần bạn xử lý"), "version": store.jobs_version()}

    @staticmethod
    def _matches(q: str, fields: list) -> bool:
        return any(q in " ".join(str(f).lower().split()) for f in fields if f)

    def forget_job(self, job_id: str) -> None:
        with self._lock:
            self._titles.pop(job_id, None)
            self._outputs.pop(job_id, None)

    def job_detail(self, job_id: str) -> dict:
        j = self._job_or_error(job_id)
        runs = self.orc.store.stage_runs(job_id)
        d = DG.explain(self.orc, job_id)
        s = self.summary(j)
        imported = {a["kind"] for a in self.orc.store.artifacts(job_id) if a["stage"] == "import"}
        cur = REV.current_pipeline(j, imported)
        pend = self.orc.store.pending_revision(job_id)
        status = s["status"]
        s["links"] = {**self._links(j, meta=True), "published_video_url": s["links"]["published_video_url"]}
        b = self.orc.store.get_batch(j["batch_id"]) if j.get("batch_id") else None
        s["batch"] = {"id": b["id"], "title": b.get("source_title") or b["source_url"], "position": (j["params"].get("batch") or {}).get("position")} if b else None
        s.update(control={"state": j["control_state"], "origin": j.get("pause_origin"), "pausing": s["pausing"]},
                 pipeline_revision=j.get("pipeline_revision", 1), requested_stages=cur["requested_stages"],
                 pending_revision=({"revision": pend["revision"], "apply_policy": pend["apply_policy"], "created_at": pend["created_at"],
                                    "summary": self._impact_summary(pend["impact"] or {})} if pend else None),
                 actions={"pause": j["control_state"] == "RUNNING" and status not in ("completed", "failed", "cancelled"), "unpause": j["control_state"] == "PAUSED",
                          "cancel": j["control_state"] != "CANCELLED" and status != "completed", "edit": j["control_state"] != "CANCELLED", "delete": True,
                          "clone": status in ("completed", "cancelled", "failed"), "reroll_thumbnail": bool((j["params"].get("thumbnail_source")) and status not in ("completed", "cancelled")),
                          "prosody": any(a["kind"] == "speech_plan" for a in self.orc.store.artifacts(job_id))})
        s["thumbnail"] = self.thumbnail_info(j)
        s["story_guidance"] = self.story_guidance_view(j, runs)
        s["story_mode"] = SM.of_job(j["params"])
        s["rerun"] = self.rerun_summary(j)
        s["actions"]["rerun"] = j["control_state"] != "CANCELLED"
        pipeline = self._pipeline(j, runs, pend, d.get("human"), cur, imported)
        s["edit"] = self.jobedit.view(j, s, runs, pipeline)
        s.update(version=self.orc.store.jobs_version(), diagnosis=d, pipeline=pipeline, decisions=j["params"].get("auto", []),
                 mode={"start": j.get("start_stage"), "target": j.get("target_stage")}, params_public=self._public_params(j["params"]),
                 timeline_legend=TIMELINE_LABEL,
                 output=(lambda o: o if o.get("project_dir") else None)(self.output_info(job_id)),
                 attempts=[{"stage": r["stage"], "attempt": r["attempt"], "status": r["status"], "started_at": r["started_at"], "ended_at": r["ended_at"]} for r in runs][-40:])
        return s

    @staticmethod
    def _public_params(p: dict) -> dict:
        keep = ("input", "channel", "language", "project", "tiktok", "made_for_kids", "source", "watermark_ref")
        out = {k: p[k] for k in keep if k in p}
        if p.get("templates"):                                       # chỉ phần nhận dạng của snapshot (không đẩy cả tài liệu template ra giao diện)
            out["templates"] = {k: {"id": v.get("id"), "version": v.get("version"), "name": v.get("name"), "checksum": str(v.get("checksum") or "")[:12]}
                                for k, v in p["templates"].items() if isinstance(v, dict)}
        return out

    def _pipeline(self, j: dict, runs: list[dict], pend: dict | None = None, human: str | None = None, cur: dict | None = None, imported: set | None = None) -> list[dict]:
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
            if j.get("pipeline") is not None and st.name not in j["pipeline"]["run"]:             # pipeline tùy chỉnh: bước không được yêu cầu chỉ đi qua máy trạng thái
                state = "provided" if imported & set(st.produces) else "not_planned"
            elif i < start:
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
        return self._timeline(j, rows, pend, human, cur, imported)

    def _timeline(self, j: dict, rows: list[dict], pend: dict | None, human: str | None, cur: dict | None, imported: set | None) -> list[dict]:
        """Thêm vào mỗi bước: `timeline` (DONE | REUSED | AVAILABLE | RUNNING | QUEUED | PAUSED | FAILED | NOT_REQUESTED | INVALIDATED), `branch` (shared | youtube | tiktok | package)
        và `why` (vì sao ở trạng thái này, tiếng Việt). `state` cũ giữ nguyên (tương thích). Nhánh YouTube/TikTok tách riêng để không giả vờ mọi thứ đều cần."""
        states = {}
        try:
            if cur:
                pl = plan_spec({"version": 2, "requested_stages": cur["requested_stages"]}, imported or set(), bool((j["params"].get("input") or {}).get("value")))
                states = pl.states or {}
        except Exception:                                                              # noqa: BLE001 — giải thích chỉ là phụ trợ, không được làm hỏng trang job
            states = {}
        rerun = {s["id"]: s for s in ((pend or {}).get("impact") or {}).get("stages", []) if s.get("action") == "RERUN"}
        paused = j.get("control_state") == "PAUSED"
        lab = {st.name: DG.STAGE_LABEL[st.name] for st in P.STAGES}
        for r in rows:
            n, st = r["name"], r["state"]
            info = states.get(n) or {}
            by = [lab.get(b, b) for b in (info.get("by") or [])]
            if n in rerun and st in ("done", "reused"):
                tl, why = "INVALIDATED", "Kết quả cũ không còn đúng: " + (rerun[n].get("reason") or "một thay đổi đang chờ áp dụng") + ". Sẽ chạy lại."
            elif st == "done":
                tl, why = "DONE", "Đã chạy xong ở lần chạy này."
            elif st == "reused":
                tl, why = "REUSED", "Dùng lại kết quả hợp lệ có sẵn (tham số và đầu vào không đổi) nên không chạy lại."
            elif st == "provided":
                tl, why = "AVAILABLE", "Kết quả của bước này đã có sẵn (do bạn đưa vào hoặc từ job khác) nên không cần chạy."
            elif st == "running":
                tl, why = "RUNNING", "Đang chạy."
            elif st == "held":
                tl, why = "PAUSED", human or "Đang giữ lại vì tài nguyên chưa sẵn sàng; sẽ tự chạy tiếp khi sẵn sàng."
            elif st == "failed":
                tl, why = "FAILED", human or "Bước này lỗi: xem chẩn đoán phía trên."
            elif st == "not_planned":
                tl, why = "NOT_REQUESTED", "Không nằm trong kế hoạch và không bước nào bạn chọn cần kết quả của nó."
            else:
                tl = "PAUSED" if paused else "QUEUED"
                why = "Job đang tạm dừng theo yêu cầu của bạn; bước này sẽ chạy sau khi tiếp tục." if paused else "Chờ tới lượt."
            if info.get("state") == "locked" and by and tl not in ("NOT_REQUESTED", "AVAILABLE"):
                why += " Bước này có mặt vì " + ", ".join(by) + " cần kết quả của nó."
            r.update(timeline=tl, branch=BRANCH.get(n, "shared"), why=why)
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
               "not_held": "Job này không bị giữ.", "unpaused": "Đã tiếp tục job từ chỗ dừng.", "cancelled": "Job đã bị hủy nên không tiếp tục được; dùng “Chạy lại với thay đổi”."}[res]
        if res == "unpaused" and self.orc.store.get_job(job_id)["hold_reason"]:
            msg += " Job vẫn đang chờ tài nguyên nên sẽ chạy khi tài nguyên sẵn sàng."
        return {"result": res, "message": msg}

    def speech_plan(self, job_id: str, scope: str = "external") -> dict:
        """Nhịp đọc của một job (từ speech plan đã snapshot): QC + danh sách ranh giới kèm khóa để chỉnh tay (`params.prosody.overrides`).
        scope: external (khoảng nghỉ thật sự được chèn, giữa các nhóm) | all (cả ranh giới trong nhóm do engine tự xử lý)."""
        j = self._job_or_error(job_id)
        art = next((a for a in self.orc.store.artifacts(job_id) if a["kind"] == "speech_plan"), None)
        if art is None:
            return {"available": False, "reason": "Job chưa chạy tới bước giọng đọc nên chưa có nhịp đọc."}
        try:
            plan = json.loads((job_dir(self.cfg.path("workspace"), job_id) / art["path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"available": False, "reason": "Không đọc được speech plan của job."}
        last = {g["segments"][-1] for g in plan["groups"]}
        labels = {k["id"]: k["label"] for k in PRO.describe()["kinds"]}
        rows = [{"key": s.get("key"), "text": s["text"][-90:], "kind": s["boundary_after"], "kind_label": labels.get(s["boundary_after"], s["boundary_after"]),
                 "pause_ms": s["pause_after_ms"], "source": s.get("source"),
                 "manual": bool(s.get("manual_override")), "group": s.get("synthesis_group"), "realized": s.get("realized"), "external": s["id"] in last}
                for s in plan["segments"] if s["boundary_after"] != "end" and (scope == "all" or s["id"] in last)]
        overrides = {}
        if j["params"].get("prosody"):
            overrides = PRO.resolve_prosody(j["params"]["prosody"])["overrides"]
        return {"available": True, "mode": plan["mode"], "editable": plan["mode"] == "prosody", "profile": plan.get("profile"), "qc": plan["qc"], "warnings": plan.get("warnings", []),
                "overrides": overrides, "boundaries": rows[:2000], "truncated": len(rows) > 2000}

    BULK_ACTIONS = ("pause", "resume", "retry", "cancel", "update_pipeline", "template", "delete")

    def bulk(self, action: str, job_ids: list[str], args: dict | None = None) -> dict:
        """Hành động hàng loạt trên các job ĐÃ CHỌN. Backend kiểm TỪNG job (không tin giao diện); trả kết quả từng job + đếm để UI báo thành công một phần
        rõ ràng. Không có job nào bị bỏ lặng lẽ: mỗi job là `done` | `unchanged` | `skipped` (kèm lý do) | `error`."""
        if action not in self.BULK_ACTIONS:
            raise _err("INVALID_BULK_ACTION", f"Hành động không hợp lệ: {action!r}; hợp lệ: {list(self.BULK_ACTIONS)}")
        ids = list(dict.fromkeys(str(i) for i in (job_ids or [])))
        if not ids:
            raise _err("NOTHING_SELECTED", "Chưa chọn job nào.")
        if len(ids) > 500:
            raise _err("TOO_MANY_SELECTED", "Chọn tối đa 500 job mỗi lần.")
        args = args or {}
        if action == "update_pipeline" and args.get("target_stage") not in P.INDEX:
            raise _err("BULK_ARGS", "Thiếu hoặc sai bước đích (target_stage).")                          # lỗi tham số chung: báo một lần, không lặp cho từng job
        if action == "template" and not (args.get("kind") and args.get("template_id")):
            raise _err("BULK_ARGS", "Thiếu loại template hoặc mã template.")
        results = []
        for jid in ids:
            r = None
            if action == "delete" and re.fullmatch(r"B\d+", jid):                                  # Channel Run: xóa hết job con rồi xóa chính nó
                gone = not self._delete_batch(jid)
                results.append({"job_id": jid, "result": "unchanged" if gone else "done", **({"reason": "Channel Run đã được xóa từ trước."} if gone else {})})
                continue
            j = self.orc.store.get_job(jid)
            if j is None:
                results.append({"job_id": jid, "result": "error", "reason": "Không có job này."})
                continue
            try:
                if action == "pause":
                    r = self.orc.pause_job(jid)
                    ok = r == "changed"
                    why = {"complete": "Job đã hoàn tất.", "failed": "Job đang lỗi: chạy lại stage lỗi.", "cancelled": "Job đã bị hủy.", "unchanged": "Job đã tạm dừng từ trước."}.get(r)
                elif action == "resume":
                    r = self.orc.resume(jid)
                    ok = r in ("unpaused", "resumed")
                    why = {"cancelled": "Job đã bị hủy.", "not_held": "Job không bị tạm dừng/giữ.", "still_down": "Tài nguyên chưa sẵn sàng: job vẫn đang chờ."}.get(r)
                elif action == "retry":
                    if j["state"] != P.FAILED or j["control_state"] == "CANCELLED":
                        ok, why = False, "Chỉ chạy lại được job đang lỗi."
                    else:
                        self.orc.retry(jid)
                        ok, why = True, None
                elif action == "update_pipeline":
                    ok, why = self._bulk_update(jid, j, args or {})
                elif action == "template":
                    ok, why = self._bulk_template(jid, j, args or {})
                elif action == "delete":                                                 # đúng thao tác Xóa job: job đang chạy dừng ở điểm an toàn, output giữ nguyên
                    r = self.jobedit.delete(jid)["result"]
                    ok = r == "deleted"
                    r = "unchanged" if r == "already" else r
                    why = None if ok else "Job đã được xóa từ trước."
                else:
                    r = self.orc.cancel_job(jid)
                    ok = r == "changed"
                    why = {"complete": "Job đã hoàn tất.", "unchanged": "Job đã bị hủy từ trước."}.get(r)
            except (StageError, ValueError) as e:
                results.append({"job_id": jid, "result": "error", "reason": getattr(e, "message", str(e))})
                continue
            results.append({"job_id": jid, "result": "done" if ok else ("unchanged" if r == "unchanged" else "skipped"), **({"reason": why} if why else {})})
        counts = {k: sum(1 for r in results if r["result"] == k) for k in ("done", "unchanged", "skipped", "error")}
        return {"action": action, "counts": counts, "results": results}

    def _delete_batch(self, bid: str) -> bool:
        """Xóa Channel Run: đóng batch trước (không tạo thêm job con), rồi xóa từng job con đúng như Xóa job (output giữ nguyên). False nếu đã xóa/không có."""
        if not self.orc.store.delete_batch(bid):
            return False
        for it in self.orc.store.batch_items(bid):
            if it["status"] == "pending":
                self.orc.store.set_batch_item(bid, it["source_video_id"], status="removed")
        for j in self.orc.store.batch_job_index(bid):
            self.jobedit.delete(j["id"])
        self.orc.log.emit("batch_deleted", "warning", batch_id=bid)
        return True

    def _bulk_update(self, jid: str, j: dict, args: dict) -> tuple[bool, str | None]:
        """Cập nhật pipeline cho MỘT job trong lô: đúng thao tác `update_target` của Sửa job (progress floor kiểm TỪNG job; job đã xong được lưu và giữ, không tự chạy)."""
        try:
            r = self.orc.update_target(jid, args["target_stage"])
        except StageError as e:
            return False, e.message
        if r["result"] == "unchanged":
            return False, "Pipeline của job này đã đúng như vậy."
        return True, "Đã lưu; job xong sẽ chờ “Chạy tiếp”." if r["held"] else "Đã cập nhật."

    def _bulk_template(self, jid: str, j: dict, args: dict) -> tuple[bool, str | None]:
        """Đổi template (thumbnail | youtube | tiktok) cho job CHƯA kết thúc; job đã xong/hủy giữ nguyên (không sửa tại chỗ)."""
        kind, tid = str(args.get("kind") or ""), str(args.get("template_id") or "")
        if DG.ui_status(j) in ("completed", "cancelled"):
            return False, "Job đã kết thúc: không sửa tại chỗ. Dùng “Chạy lại với thay đổi”."
        if kind not in (j["params"].get("templates") or {}):
            return False, "Job này không dùng loại template đó."
        if j["state"] in P.BY_RUNNING:
            return False, "Job đang chạy: tạm dừng hoặc chờ xong bước hiện tại rồi thử lại."
        self.orc.retemplate(jid, kind, tid)
        return True, "Đã đổi template; bước dựng liên quan sẽ chạy lại."

    def _job_or_error(self, job_id: str) -> dict:
        j = self.orc.store.get_job(job_id)
        if j is None or j["control_state"] == "DELETED":
            raise _err("JOB_NOT_FOUND", f"Không có job {job_id}.", "Quay lại danh sách job.")
        return j

    def pause(self, job_id: str) -> dict:
        """Tạm dừng AN TOÀN: hoàn tất đơn vị đang chạy (segment/part) rồi dừng; không đổi kết quả đã có."""
        self._job_or_error(job_id)
        res = self.orc.pause_job(job_id)
        msg = {"changed": "Đã tạm dừng. Đơn vị đang chạy sẽ hoàn tất rồi job dừng lại; kết quả đã xong được giữ nguyên.", "unchanged": "Job đã ở trạng thái tạm dừng.",
               "complete": "Job đã hoàn tất, không có gì để tạm dừng.", "failed": "Job đang lỗi: dùng “Chạy lại stage lỗi”.", "cancelled": "Job đã bị hủy."}[res]
        return {"result": res, "message": msg}

    def cancel(self, job_id: str) -> dict:
        self._job_or_error(job_id)
        res = self.orc.cancel_job(job_id)
        msg = {"changed": "Đã hủy job. Kết quả đã có được giữ lại; job sẽ không tự chạy lại.", "unchanged": "Job đã bị hủy từ trước.",
               "complete": "Job đã hoàn tất nên không hủy được."}[res]
        return {"result": res, "message": msg}

    # ---- cập nhật pipeline/config + chạy lại với thay đổi
    @staticmethod
    def _update_args(payload: dict, clone: bool = False) -> dict:
        """Revision của job chỉ còn dành cho config/params (nhịp đọc, thumbnail, template). Đổi PIPELINE của job đang sống đi qua `Sửa job` (đổi đích, progress floor);
        chỉ “Chạy lại với thay đổi” (job MỚI) còn nhận `pipeline`."""
        pl = payload.get("pipeline")
        if pl is not None and not clone:
            raise _err("PIPELINE_USE_TARGET", "Đổi pipeline của job qua “Sửa job → Cập nhật pipeline”.", "Dùng PUT /api/jobs/<id>/target với target_stage.")
        return {"pipeline": {"requested_stages": pl.get("requested_stages")} if isinstance(pl, dict) else None,
                "config_patch": payload.get("config_patch") or None, "params_patch": payload.get("params_patch") or None}

    @staticmethod
    def _impact_summary(impact: dict) -> dict:
        """Tóm tắt bằng ngôn ngữ người dùng: Thay đổi này sẽ chạy / giữ nguyên / bỏ khỏi kế hoạch."""
        by = {a: [s["label"] for s in impact.get("stages", []) if s["action"] == a] for a in ACTION_VI}
        return {"will_run": by["RUN"] + by["RERUN"], "kept": by["KEEP"] + by["REUSE"], "removed": by["REMOVE_FROM_PLAN"], "continuing": by["CURRENT_CONTINUE"]}

    def _impact_view(self, impact: dict) -> dict:
        return {**impact, "stages": [{**s, "action_label": ACTION_VI[s["action"]]} for s in impact["stages"]], "summary_text": self._impact_summary(impact)}

    # ------------------------------------------------------------------------------------------ Image Pool (Phase 8)
    _REROLL_PROBE = {"thumbnail_source": {"sha256": "reroll"}}

    def thumbnail_info(self, j: dict) -> dict | None:
        """Ảnh thumbnail đã chốt từ pool (chỉ phần công khai) + việc “Đổi ảnh” làm được không và vì sao."""
        t = j["params"].get("thumbnail_source")
        if not t:
            return None
        why = None
        if j["control_state"] == "CANCELLED" or self.summary(j)["status"] in ("completed", "cancelled"):
            why = "Job đã kết thúc nên không sửa tại chỗ (bản đã dựng/đăng được giữ nguyên). Dùng “Chạy lại với thay đổi” để làm job mới với ảnh khác."
        return {"pool": t["pool"], "selection_mode": t["selection_mode"], "source_relpath": t["source_relpath"], "width": t.get("width"), "height": t.get("height"),
                "sha": t["sha256"][:12], "rerolls": t.get("rerolls", 0), "image_url": f"/api/jobs/{j['id']}/thumbnail-source",
                "can_reroll": why is None, "reroll_blocked": why}

    def reroll_preview(self, job_id: str) -> dict:
        self._job_or_error(job_id)
        return self._impact_view(self.orc.preview_update(job_id, params_patch=self._REROLL_PROBE))

    def reroll_thumbnail(self, job_id: str) -> dict:
        self._job_or_error(job_id)
        r = self.orc.reroll_thumbnail(job_id)
        pending = r["status"] == "pending"
        return {**{k: v for k, v in r.items() if k != "impact"}, "impact": self._impact_view(r["impact"]) if r.get("impact") else None,
                "message": ("Đã chọn ảnh khác; sẽ áp dụng ở điểm an toàn kế tiếp (không làm hỏng đơn vị đang chạy)." if pending else
                            "Đã đổi ảnh thumbnail. Chỉ thumbnail và gói output được dựng lại.")}

    def thumbnail_source_file(self, job_id: str) -> Raw:
        j = self._job_or_error(job_id)
        t = j["params"].get("thumbnail_source")
        if not t:
            raise _err("NO_THUMBNAIL_SOURCE", "Job này không dùng pool ảnh thumbnail.")
        p = IPOOL.resolve_source(job_dir(self.cfg.path("workspace"), job_id), t)
        return Raw(p.read_bytes(), {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[t["format"]])

    def preview_update(self, job_id: str, payload: dict) -> dict:
        self._job_or_error(job_id)
        return self._impact_view(self.orc.preview_update(job_id, **self._update_args(payload)))

    def request_update(self, job_id: str, payload: dict) -> dict:
        self._job_or_error(job_id)
        r = self.orc.request_update(job_id, **self._update_args(payload), apply_policy=payload.get("apply_policy") or "after_current_safe_point")
        msg = {"applied": "Đã áp dụng thay đổi. Job chạy tiếp theo pipeline mới.",
               "pending": "Đã ghi thay đổi. Sẽ áp dụng ở điểm an toàn kế tiếp (không làm hỏng đơn vị đang chạy).",
               "rejected": "Thay đổi bị từ chối khi áp dụng.", "none": "Không có thay đổi cần áp dụng."}[r["status"]]
        return {**r, "impact": self._impact_view(r["impact"]), "message": msg}

    def clone(self, job_id: str, payload: dict) -> dict:
        self._job_or_error(job_id)
        new = self.orc.clone_job(job_id, rerun_from=payload.get("rerun_from") or None, pipeline=self._update_args(payload, clone=True)["pipeline"],
                                 params_patch=payload.get("params_patch") or None)
        return {"job_id": new, "message": f"Đã tạo job #{new} từ kết quả còn hợp lệ của job #{job_id}; job cũ không bị thay đổi."}

    def update_target(self, job_id: str, payload: dict) -> dict:
        return self.jobedit.update_target(job_id, payload)

    def delete_job(self, job_id: str) -> dict:
        return self.jobedit.delete(job_id)

    def retry(self, job_id: str) -> dict:
        j = self.orc.store.get_job(job_id)
        if j is None or j["state"] != P.FAILED:
            raise _err("NOT_FAILED", "Chỉ chạy lại được job đang ở trạng thái lỗi.")
        stage = self.orc.retry(job_id)
        return {"stage": stage, "message": f"Đã xếp lại stage '{DG.STAGE_LABEL.get(stage, stage)}'; các stage trước giữ nguyên."}

    # ---- Selective Manual Rerun (D-113)
    def rerun_options(self, job_id: str) -> dict:
        """Stage nào chạy lại được, vì sao không, cần chọn thêm gì, đã chạy lại mấy lần — toàn bộ do backend quyết định."""
        self._job_or_error(job_id)
        return self.orc.rerun_service().options(job_id)

    def rerun_plan(self, job_id: str, payload: dict) -> dict:
        self._job_or_error(job_id)
        return self.orc.rerun_service().plan(job_id, payload.get("stages"))

    def rerun_start(self, job_id: str, payload: dict) -> dict:
        self._job_or_error(job_id)
        r = self.orc.rerun_service().start(job_id, payload.get("stages"), str(payload.get("request_id") or "") or None)
        names = ", ".join(s["label"] for s in r["stages"])
        return {**r, "rerun_session_id": r["id"],
                "message": ("Lượt chạy lại này đã được tạo trước đó." if r.get("deduped") else f"Đã xếp lượt chạy lại #{r['number']}: {names}. Các bước khác giữ nguyên.")}

    def reruns(self, job_id: str) -> dict:
        self._job_or_error(job_id)
        return self.orc.rerun_service().history(job_id)

    def rerun_summary(self, j: dict) -> dict | None:
        """Phần nhẹ cho trang job: phiên đang chạy, số lần chạy lại và cờ stale của từng bước (phụ trợ: lỗi ở đây không được làm hỏng trang job)."""
        try:
            svc = self.orc.rerun_service()
            an = svc.analyze(j)
            return {"active": svc.view_active(j["id"]), "counts": self.orc.store.reruns.counts(j["id"]), "stale": {n: a["stale"] for n, a in an["stages"].items()},
                    "stale_by": {n: a["stale_by"] for n, a in an["stages"].items() if a["stale"]}}
        except Exception:                                                              # noqa: BLE001
            return None

    def remix_plan(self, job_id: str) -> dict:
        """Kế hoạch Story Remix của job (đọc artifact do bước lập kế hoạch ghi): ý tưởng đã chọn + lý do loại, dàn nhân vật, cổng originality/nhịp thưởng, chi phí, đại cương."""
        j = self._job_or_error(job_id)
        mode = SM.of_job(j["params"])
        if mode["mode"] != "story_remix":
            return {"active": False}
        d = job_dir(self.cfg.path("workspace"), job_id) / "story" / "remix"

        def rd(name: str):
            try:
                return json.loads((d / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        sel, cands, outline, cost = rd("selection_report.json"), rd("premise_candidates.json"), rd("outline.json"), rd("cost_report.json")
        done = len(list((d / "chapters").glob("ch_*.md"))) if (d / "chapters").is_dir() else 0
        writer = rd("writer_report.json")
        stop = None
        for r in reversed(self.orc.store.stage_runs(job_id)):
            if r["stage"] == "story":
                if r["status"] == "failed" and r.get("error"):
                    try:
                        e = json.loads(r["error"])
                        stop = {"code": e.get("code"), "message": e.get("message"), "hint": (e.get("detail") or {}).get("hint", "")}
                    except ValueError:
                        stop = None
                break
        premises = None
        if sel and cands:
            premises = [{"id": p["id"], "logline": p["logline"], "total": sel["scores"].get(p["id"], {}).get("total"), "selected": p["id"] == sel["selected"],
                         "reason": next((r["reason"] for r in sel["rejected"] if r["id"] == p["id"]), "")} for p in cands["candidates"]]
        src = next((a for a in self.orc.store.artifacts(job_id) if a["kind"] == "transcript"), None)
        try:
            src_chars = (job_dir(self.cfg.path("workspace"), job_id) / src["path"]).stat().st_size // 2 if src else None        # byte → ký tự: ước lượng thô cho tiếng Việt UTF-8
        except OSError:
            src_chars = None
        est = EST.estimate(mode["story"], j["params"].get("story_profile") or {}, src_chars or 60_000, (self.cfg.data.get("story_remix") or {}).get("price_usd_per_mtok"))
        return {"active": True, "mode": mode, "estimate": est, "ready": bool(outline), "dna": rd("source_dna.json"), "premises": premises, "selection_min": sel and sel.get("min_select"), "cast": rd("character_cast.json"),
                "originality": rd("originality_report.json"), "quality": rd("quality_report.json"), "bible": (lambda b: b and {"title": b["title"], "themes": b["themes"]})(rd("story_bible.json")),
                "outline": outline and [{"n": c["n"], "title": c["title"], "payoff": c["payoff"] and c["payoff"]["type"], "cast": len(c["cast"])} for c in outline["chapters"]],
                "cost": cost and {k: v for k, v in cost.items() if k != "call_log"}, "chapters_done": done,
                "writer": writer and [{"n": c["n"], "chars": c["chars"], "repairs": c["repairs"], "issues": [i["message"] for i in c["issues"]]} for c in writer["chapters"]],
                "final_qa": rd("final_qa.json"), "stop": stop}

    _STORY_MODE_EDITABLE = {"review_accepted", "budget_usd", "quality_repair_max_passes"}

    def update_story_mode(self, job_id: str, body: dict) -> dict:
        """Cho phép sửa vài tuỳ chọn KHÔNG đổi nội dung của job Story Remix đang dừng (xem báo cáo/nâng ngân sách/thêm lượt sửa) rồi chạy tiếp từ bước dở: các bước đã xong giữ nguyên."""
        j = self._job_or_error(job_id)
        cur = SM.of_job(j["params"])
        if cur["mode"] != "story_remix":
            raise _err("NOT_REMIX_JOB", "Job không ở chế độ Story Remix.")
        patch = body.get("story") or {}
        bad = sorted(set(patch) - self._STORY_MODE_EDITABLE)
        if not isinstance(patch, dict) or bad:
            raise _err("INVALID_STORY_MODE", f"Chỉ sửa được: {', '.join(sorted(self._STORY_MODE_EDITABLE))}.", "Các tuỳ chọn khác thay đổi nội dung truyện: tạo job mới.")
        new = SM.parse({"mode": "story_remix", "story": {**cur["story"], **patch}, "character_universe": cur["character_universe"]}, self.cfg.data.get("story"))
        self.orc.store.update_params(job_id, lambda p: {**p, "story_mode": new}, "story mode: " + ", ".join(sorted(patch)))
        out = {"story_mode": new}
        if body.get("retry", True) and self.orc.store.get_job(job_id)["state"] == P.FAILED:
            out.update(self.retry(job_id))
        return out

    # ---- Chế độ truyện (Story Remix | Story hiện có)
    def story_mode_info(self) -> dict:
        """Mô tả cho UI: các mode (kèm khả dụng + lý do), schema trường, mặc định hệ thống. Cùng schema server dùng để kiểm."""
        d = SM.describe(self.cfg.data.get("story"))
        pr = SP.load(self._presets_file())
        d["presets"] = [{"name": n, "story": p["story"], "character_universe": p["character_universe"], "saved_at": p["saved_at"]} for n, p in sorted(pr["presets"].items())]
        d["default_preset"] = pr["default"] if d["available"] else None
        d["estimate_note"] = "Ước tính thô; xem /api/story-mode/estimate"
        return d

    def _presets_file(self):
        return self.cfg.path("runtime") / "story_presets.json"

    def story_preset_save(self, name: str, body: dict) -> dict:
        try:
            SP.save_preset(self._presets_file(), name, body["story_mode"] if "story_mode" in body else {k: v for k, v in body.items() if k != "make_default"}, self.cfg.data.get("story"))
            if body.get("make_default"):
                SP.set_default(self._presets_file(), name)
        except StageError as e:
            raise _err(e.code, e.message, (e.detail or {}).get("hint", "")) from None
        return self.story_mode_info()

    def story_preset_delete(self, name: str) -> dict:
        try:
            SP.delete_preset(self._presets_file(), name)
        except StageError as e:
            raise _err(e.code, e.message) from None
        return self.story_mode_info()

    def story_preset_default(self, body: dict) -> dict:
        try:
            SP.set_default(self._presets_file(), body.get("name") or None)
        except StageError as e:
            raise _err(e.code, e.message) from None
        return self.story_mode_info()

    def story_mode_estimate(self, payload: dict) -> dict:
        """Ước tính trước (thô, công khai giả định) số lượt gọi/token/USD của một job Story Remix với cấu hình hiện tại của form."""
        try:
            m = SM.parse({"mode": "story_remix", **{k: v for k, v in ((payload or {}).get("story_mode") or {}).items() if k != "mode"}}, self.cfg.data.get("story"))
        except StageError as e:
            raise _err(e.code, e.message, (e.detail or {}).get("hint", "")) from None
        chars = (payload or {}).get("source_chars")
        if chars is not None and (isinstance(chars, bool) or not isinstance(chars, int) or not 1000 <= chars <= 5_000_000):
            raise _err("INVALID_ESTIMATE", "source_chars phải là số nguyên 1000–5000000.")
        return EST.estimate(m["story"], (payload or {}).get("story_profile") or {}, chars or 60_000, (self.cfg.data.get("story_remix") or {}).get("price_usd_per_mtok"))

    def story_mode_effective(self, payload: dict) -> dict:
        """Cấu hình hiệu lực (kèm nguồn từng giá trị) cho lựa chọn hiện tại của form; sai ⇒ lỗi rõ ràng, không ép kiểu."""
        try:
            pre = (SP.load(self._presets_file())["presets"].get((payload or {}).get("preset") or "") or None)
            return SM.effective((payload or {}).get("story_mode"), self.cfg.data.get("story"), pre)
        except StageError as e:
            raise _err(e.code, e.message, (e.detail or {}).get("hint", "")) from None

    # ---- Story Guidance (D-112)
    def story_guidance_view(self, j: dict, runs: list[dict] | None = None) -> dict:
        """Đề xuất truyện của job cho giao diện: cấu hình (inherit/custom/none), mặc định hiện tại trong Cài đặt, thứ sẽ được dùng nếu Truyện chạy NGAY BÂY GIỜ,
        và đề xuất hiệu lực của lần chạy Truyện gần nhất (snapshot đã chốt — không đổi khi Cài đặt đổi)."""
        runs = self.orc.store.stage_runs(j["id"]) if runs is None else runs
        default = str((self.cfg.data.get("story") or {}).get("guidance") or "")
        cfg = GD.of_job(j["params"])
        eff = GD.resolve(j["params"], default)
        last = None
        for r in reversed(runs):
            g = json.loads(r.get("meta") or "{}").get("guidance") if r["stage"] == "story" else None
            if g:
                last = {"source": g["source"], "text": g["text"], "hash": g["hash"], "run": r["id"], "at": g.get("resolved_at") or r["started_at"], "status": r["status"]}
                break
        return {"mode": cfg["mode"], "text": cfg["text"], "default_text": GD.normalize(default), "max_len": GD.MAX_LEN,
                "effective": {"source": eff["source"], "text": eff["text"]}, "last_run": last,
                "drift": bool(last and (last["hash"] or "") != (eff["hash"] or ""))}

    def set_story_guidance(self, job_id: str, payload: dict) -> dict:
        """Đổi đề xuất truyện RIÊNG của job. Chỉ có tác dụng ở lần Truyện chạy kế tiếp (không đổi kết quả đã có, không làm gì bị coi là cũ); muốn áp dụng cho truyện
        đã có thì dùng “Chạy lại → Truyện”."""
        self._job_or_error(job_id)
        g = GD.parse({"mode": payload.get("mode"), "text": payload.get("text")})

        def fn(p: dict) -> dict:
            p = dict(p)
            p.pop("story_guidance", None)
            if g["mode"] != GD.DEFAULT_MODE:
                p["story_guidance"] = g
            return p
        self.orc.store.update_params(job_id, fn, f"story guidance -> {g['mode']}")
        j = self._job_or_error(job_id)
        return {**self.story_guidance_view(j), "message": {"inherit": "Job dùng đề xuất trong Cài đặt.", "custom": "Đã lưu đề xuất riêng cho job; áp dụng ở lần Truyện chạy kế tiếp.",
                                                          "none": "Job không dùng đề xuất truyện."}[g["mode"]]}

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
                "options": {"tts_profiles": profiles, "pools": pools, "privacy": ["private", "unlisted", "public"], "prosody": PRO.describe(),
                            "image_pools": sorted(self.orc.image_pools.specs()), "image_modes": list(IPOOL.MODES)}}

    def save_channel(self, channel_id: str, raw: dict, create: bool = False) -> dict:
        d = CH.channel_dir(self.cfg, channel_id)
        if create and (d / "channel.json").exists():
            raise _err("CHANNEL_EXISTS", f"Kênh '{channel_id}' đã có.")
        if not isinstance(raw, dict):
            raise _err("INVALID_CHANNEL_CONFIG", "Cấu hình kênh phải là object.")
        raw = {k: v for k, v in raw.items() if k not in ("id", "loaded_from")}
        MD.normalize_channel(raw, channel_id)                  # đúng validator của core: sai thì báo ngay, không ghi
        if (raw.get("preset") or {}).get("prosody"):
            PRO.resolve_prosody(raw["preset"]["prosody"])                           # nhịp đọc của kênh: kiểm bằng đúng validator của core
        _, terrs = TPL.normalize_section(raw.get("templates"))
        if terrs:
            raise _err("INVALID_CHANNEL_CONFIG", "; ".join(terrs))
        d.mkdir(parents=True, exist_ok=True)
        with self.orc.watermarks.lock(channel_id):                                  # cùng khóa với Watermark Library: không ghi đè active vừa đổi
            f = d / "channel.json"
            cur = {}
            if f.is_file() and not create:
                try:
                    cur = json.loads(f.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    cur = {}
            for k in ("watermark_ref", "legacy_watermark"):                          # watermark đang dùng do Watermark Library quản lý, không phải form kênh
                raw.pop(k, None)
                if cur.get(k) is not None:
                    raw[k] = cur[k]
            if cur.get("watermark_ref") and cur.get("watermark") is not None:
                raw["watermark"] = cur["watermark"]
            atomic_write_json(f, raw)
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
        return {"youtube_title": pm["youtube_title"], "description": pm["description"], "sequence": seq, "thumbnail": {"channel_name": thumb_channel_line(ch["name"], seq), "title": project["title"]},
                "privacy": (ch.get("publishing") or {}).get("privacy") or "private"}

    def save_channel_asset(self, channel_id: str, name: str, data: bytes) -> dict:
        d = CH.channel_dir(self.cfg, channel_id)
        safe = re.sub(r"[^\w.\-]", "_", Path(name).name)[:80]
        if not safe or safe.startswith("."):
            raise _err("INVALID_ASSET_NAME", "Tên file không hợp lệ.")
        if Path(safe).suffix.lower() not in AUDIO_EXT | {".png", ".jpg", ".jpeg"}:
            raise _err("INVALID_ASSET_TYPE", "Chỉ nhận file audio (watermark) hoặc ảnh.")
        if Path(safe).suffix.lower() in AUDIO_EXT:                                    # audio = watermark: đi vào Watermark Library (revision bất biến), không ghi đè file rời
            r = self.watermarks.create_upload(channel_id, Path(safe).stem, safe, data)
            return {"name": safe, "bytes": len(data), "watermark_id": r["item"]["id"], "revision": r["item"]["current_revision"]}
        d.mkdir(parents=True, exist_ok=True)
        (d / safe).write_bytes(data)
        return {"name": safe, "bytes": len(data)}
