"""YtUploaderPublish: PublishAdapter YouTube = HTTP client của daemon `yt-uploader serve --headless` (loopback, D-12). Không import code của yt_uploader.

Quy trình `publish()` (idempotent theo `idempotency_key` = stage_key, nên CRASH/RETRY KHÔNG BAO GIỜ đăng hai video):
  1. tìm job theo key (`GET /jobs?idempotency_key=`):
       chưa có            -> `POST /jobs` (video + thumbnail + metadata đã dựng)
       completed          -> trả kết quả cũ (không upload lại)
       failed/paused/cancelled với lỗi có thể thử lại (mạng, quota, auth, rate limit…) -> `POST /jobs/{id}/retry` (daemon probe session rồi RESUME, không upload từ đầu)
       failed với lỗi vĩnh viễn (metadata bị từ chối, file lỗi…) hoặc AMBIGUOUS_UPLOAD -> báo lỗi, KHÔNG tự retry (AMBIGUOUS cần người kiểm tra kênh rồi `retry?force=true`)
       đang chạy          -> chờ tiếp
  2. poll tới trạng thái cuối; hủy (shutdown) thì chỉ ngừng chờ, job vẫn chạy trong daemon và lần sau tìm lại đúng job đó.
Video và thumbnail được đọc từ WORKSPACE (không từ output/), nên người dùng đổi tên/di chuyển/xóa output/ không ảnh hưởng upload; thumbnail > 2 MiB được nén RA FILE TẠM
(file thumbnail final không bị sửa). Lỗi daemon ánh xạ: quota -> RESOURCE quota (giữ job tới lúc reset), auth -> AUTH credential, mạng/rate limit -> TRANSIENT,
metadata/youtube_rejected -> POLICY, invalid_file -> POLICY input, AMBIGUOUS -> AMBIGUOUS, daemon không chạy -> RESOURCE runtime (job bị giữ, tự tiếp tục khi daemon lên).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..contracts import ErrorClass, PublishRequest, PublishResult, StageContext, StageError

REQUIRED_FEATURES = ("idempotency_key", "resume_probe")
TERMINAL = ("completed", "failed", "cancelled")
ACTIVE = ("queued", "preparing", "uploading", "processing", "retry_wait")
# lỗi mà thử lại có ý nghĩa (nguyên nhân là tạm thời hoặc người dùng đã sửa: mạng, quota hết ngày, đăng nhập lại...)
RETRYABLE = {"network_error", "rate_limited", "quota_exceeded", "auth_required", "auth_revoked", "database_error", "cancelled", "internal_error", None}


def next_quota_reset(now: datetime | None = None) -> float:
    """Quota YouTube reset lúc 00:00 giờ Thái Bình Dương. Không có tzdata trên Windows nên lấy mốc muộn nhất (00:00 PST = 08:00 UTC) + 10 phút."""
    now = now or datetime.now(timezone.utc)
    t = now.replace(hour=8, minute=10, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t.timestamp()


def _ambiguous(job: dict) -> bool:
    return (job.get("last_error") or {}).get("code") == "AMBIGUOUS_UPLOAD" or job.get("error_class") == "AMBIGUOUS_PUBLISH"


def map_job_error(job: dict) -> StageError:
    le = job.get("last_error") or {}
    code, msg = le.get("code"), le.get("message", "")
    detail = {"uploader_job": job.get("id"), "uploader_code": code, "uploader_class": job.get("error_class")}
    text = f"yt_uploader: {code}: {msg}"[:600]
    if _ambiguous(job):                                          # kiểm TRƯỚC mọi mã khác: không để mã lỗi tạm (network...) che mất trạng thái "không rõ video đã lên chưa"
        return StageError(ErrorClass.AMBIGUOUS, "AMBIGUOUS_UPLOAD", text + " — video có thể đã lên YouTube: kiểm tra kênh rồi `retry?force=true` nếu chưa có",
                          detail)
    if code == "quota_exceeded":
        return StageError(ErrorClass.RESOURCE, "QUOTA_EXCEEDED", text, detail, resource="quota", resume_after=next_quota_reset())
    if code in ("auth_required", "auth_revoked"):
        return StageError(ErrorClass.AUTH, code.upper(), text, detail, resource="credential")
    if code == "network_error":
        return StageError(ErrorClass.TRANSIENT, "UPLOAD_NETWORK_ERROR", text, detail, resource="network")
    if code == "rate_limited":
        return StageError(ErrorClass.TRANSIENT, "UPLOAD_RATE_LIMITED", text, detail, resource="provider")
    if code in ("invalid_metadata", "youtube_rejected"):
        return StageError(ErrorClass.POLICY, code.upper(), text, detail)
    if code == "invalid_file":
        return StageError(ErrorClass.POLICY, "UPLOAD_INVALID_FILE", text, detail, resource="input")
    if code == "file_changed":
        return StageError(ErrorClass.POLICY, "UPLOAD_FILE_CHANGED", text + " (file đổi sau khi bắt đầu upload; cần `retry?force=true` thủ công)", detail)
    if code == "database_error":
        return StageError(ErrorClass.RESOURCE, "UPLOADER_DATABASE_ERROR", text, detail, resource="runtime")
    return StageError(ErrorClass.TRANSIENT, "UPLOAD_FAILED", text, detail)


class YtUploaderPublish:
    platform = "youtube"

    def __init__(self, spec: dict | None = None) -> None:
        s = spec or {}
        self.url = str(s.get("url") or "http://127.0.0.1:8973").rstrip("/")
        self.data_dir = s.get("data_dir")
        self._token = s.get("token")
        self.http_timeout = float(s.get("http_timeout_s", 30))
        self.poll_s = float(s.get("poll_s", 2.0))
        self.max_wait_s = float(s.get("max_wait_s", 6 * 3600))
        self.ffmpeg = s.get("ffmpeg") or "ffmpeg"
        self.thumb_limit = int(s.get("thumbnail_max_bytes", 2 * 1024 * 1024 - 4096))
        self.max_resumes = int(s.get("max_resumes", 3))

    # ------------------------------------------------------------------------------------------ HTTP
    def token(self) -> str:
        if self._token:
            return self._token
        if os.environ.get("YT_UPLOADER_API_TOKEN"):
            return os.environ["YT_UPLOADER_API_TOKEN"].strip()
        d = self.data_dir or os.environ.get("YT_UPLOADER_DATA_DIR") or (
            Path(os.environ["APPDATA"]) / "yt-uploader" if os.environ.get("APPDATA") else Path.home() / ".config" / "yt-uploader")
        f = Path(d) / "api_token"
        try:
            return f.read_text(encoding="utf-8").strip()
        except OSError:
            raise StageError(ErrorClass.AUTH, "UPLOADER_TOKEN_MISSING", f"không đọc được API token của yt_uploader ({f}); chạy `yt-uploader serve` một lần hoặc đặt "
                             f"tools.yt_uploader.data_dir/token", resource="credential") from None

    def _call(self, method: str, path: str, body: dict | None = None, query: dict | None = None, auth: bool = True) -> tuple[int, dict]:
        url = f"{self.url}/api/v1{path}" + (("?" + urllib.parse.urlencode(query)) if query else "")
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self.token()
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"{}")
            except ValueError:
                payload = {}
            return e.code, payload
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            raise StageError(ErrorClass.RESOURCE, "UPLOADER_UNREACHABLE", f"yt_uploader không phản hồi ở {self.url}: {e}; chạy `yt-uploader serve --headless`",
                             resource="runtime") from None

    def _api_error(self, status: int, payload: dict, what: str) -> StageError:
        e = payload.get("error") or {}
        code, msg = e.get("code", f"http_{status}"), e.get("message", "")
        detail = {"http": status, "uploader_code": code}
        text = f"{what}: {code}: {msg}"[:600]
        if status == 401 or code == "unauthorized":
            return StageError(ErrorClass.AUTH, "UPLOADER_UNAUTHORIZED", text + " (API token sai)", detail, resource="credential")
        if code in ("auth_required", "auth_revoked"):
            return StageError(ErrorClass.AUTH, code.upper(), text + " (cần đăng nhập tài khoản Google: `yt-uploader login`)", detail, resource="credential")
        if code == "invalid_file":
            return StageError(ErrorClass.POLICY, "UPLOAD_INVALID_FILE", text, detail, resource="input")
        if code in ("invalid_metadata", "invalid_request"):
            return StageError(ErrorClass.POLICY, code.upper(), text, detail)
        if status >= 500:
            return StageError(ErrorClass.TRANSIENT, "UPLOADER_SERVER_ERROR", text, detail)
        return StageError(ErrorClass.POLICY, "UPLOADER_REJECTED", text, detail)

    # ------------------------------------------------------------------------------------------ health / find
    def health(self) -> dict:
        try:
            st, d = self._call("GET", "/health", auth=False)
        except StageError as e:
            return {"ok": False, "error": e.message}
        feats = d.get("features") or []
        missing = [f for f in REQUIRED_FEATURES if f not in feats]
        try:
            self.token()
            tok = True
        except StageError:
            tok = False
        ok = st == 200 and d.get("status") == "ok" and not missing and tok
        return {"ok": ok, "features": feats, "token": tok, **({"missing_features": missing} if missing else {}),
                **({} if tok else {"error": "thiếu API token"})}

    def _find_job(self, key: str) -> dict | None:
        st, d = self._call("GET", "/jobs", query={"idempotency_key": key})
        if st != 200:
            raise self._api_error(st, d, "tra cứu job")
        jobs = d.get("jobs") or []
        return jobs[0] if jobs else None

    def find(self, idempotency_key: str) -> PublishResult | None:
        job = self._find_job(idempotency_key)
        return None if job is None else self._to_result(job, raise_on_failure=False)

    @staticmethod
    def _to_result(job: dict, raise_on_failure: bool = True) -> PublishResult:
        st = job.get("state")
        if st == "completed":
            le = job.get("last_error")
            return {"state": "completed", "remote_id": job.get("video_id"), "remote_url": job.get("video_url"),
                    "warnings": [f"{le['code']}: {le.get('message', '')}"] if le else [], "job_id": job.get("id"), "attempts": job.get("attempts")}
        if raise_on_failure and st in ("failed", "cancelled"):
            raise map_job_error(job) if st == "failed" else StageError(ErrorClass.TRANSIENT, "UPLOAD_CANCELLED", "job bị hủy trong yt_uploader")
        return {"state": "failed" if st in ("failed", "cancelled") else "pending", "remote_id": job.get("video_id"), "remote_url": job.get("video_url"),
                "error": map_job_error(job).to_dict() if st == "failed" else None, "job_id": job.get("id")}

    # ------------------------------------------------------------------------------------------ thumbnail ≤ 2 MiB
    def _fit_thumbnail(self, src: Path, ctx: StageContext) -> Path:
        if src.stat().st_size <= self.thumb_limit:
            return src
        exe = shutil.which(self.ffmpeg) or (self.ffmpeg if Path(self.ffmpeg).exists() else None)
        if not exe:
            raise StageError(ErrorClass.RESOURCE, "THUMBNAIL_TOO_LARGE", f"thumbnail {src.stat().st_size / 1e6:.1f} MB > 2 MiB của YouTube và không có ffmpeg để nén",
                             resource="runtime")
        out = ctx.stage_dir / "thumbnail_upload.jpg"                     # bản tạm cho upload; thumbnail final KHÔNG bị sửa
        for q in (3, 5, 8, 12, 18, 25, 31):
            for scale in ("1", "0.75", "0.5"):
                r = subprocess.run([exe, "-hide_banner", "-v", "error", "-y", "-i", str(src), "-vf", f"scale=trunc(iw*{scale}/2)*2:-2", "-q:v", str(q), str(out)],
                                   capture_output=True, timeout=120)
                if r.returncode == 0 and out.is_file() and out.stat().st_size <= self.thumb_limit:
                    ctx.log("thumbnail_compressed", from_bytes=src.stat().st_size, to_bytes=out.stat().st_size, q=q, scale=scale)
                    return out
        raise StageError(ErrorClass.POLICY, "THUMBNAIL_TOO_LARGE", "không nén được thumbnail xuống dưới 2 MiB", resource="input")

    # ------------------------------------------------------------------------------------------ publish
    def publish(self, req: PublishRequest, ctx: StageContext) -> PublishResult:
        key = req["idempotency_key"]
        job = self._find_job(key)
        if job is None:
            thumb = self._fit_thumbnail(Path(req["thumbnail"]), ctx) if req.get("thumbnail") else None
            job = self._create(req, thumb, key)
            ctx.log("upload_job_created", uploader_job=job.get("id"))
        else:
            ctx.log("upload_job_found", uploader_job=job.get("id"), state=job.get("state"))
            code = (job.get("last_error") or {}).get("code")
            if job.get("state") in ("failed", "paused", "cancelled") and code in RETRYABLE and not _ambiguous(job):
                job = self._retry(job)                       # lần chạy trước lỗi tạm thời: daemon probe session rồi RESUME (không upload từ đầu)
                ctx.log("upload_job_retry", "warning", uploader_job=job.get("id"), previous_code=code)
        return self._to_result(self._wait(job, ctx))

    def _create(self, req: PublishRequest, thumb: Path | None, key: str) -> dict:
        if not isinstance(req.get("made_for_kids"), bool):      # chốt chặn cuối: KHÔNG ép kiểu (bool("false") == True sẽ khai báo sai với YouTube)
            raise StageError(ErrorClass.POLICY, "MISSING_MADE_FOR_KIDS", f"made_for_kids phải là boolean thật, nhận {req.get('made_for_kids')!r}")
        body = {"file_path": str(Path(req["video"]).resolve()), "title": req["title"], "description": req["description"], "tags": list(req.get("tags") or []),
                "privacy": req.get("privacy", "private"), "made_for_kids": req["made_for_kids"], "idempotency_key": key}
        if thumb:
            body["thumbnail_path"] = str(Path(thumb).resolve())
        if req.get("category"):
            body["category"] = req["category"]
        if req.get("playlists"):
            body["playlists"] = list(req["playlists"])
        if req.get("account_id"):
            body["account_id"] = req["account_id"]
        st, d = self._call("POST", "/jobs", body)
        if st not in (200, 201):
            raise self._api_error(st, d, "tạo job upload")
        return d

    def _retry(self, job: dict) -> dict:
        st, d = self._call("POST", f"/jobs/{job['id']}/retry")
        if st != 200:
            raise self._api_error(st, d, "retry job upload")
        return d

    def _wait(self, job: dict, ctx: StageContext) -> dict:
        t0, resumes, last_log = time.time(), 0, 0.0
        while True:
            ctx.cancel.check()                              # shutdown: ngừng chờ, job vẫn chạy trong daemon; lần sau tìm lại đúng job này
            st = job.get("state")
            if st == "completed":
                return job
            if st in ("failed", "cancelled"):
                return job                                  # lỗi xảy ra TRONG lần chờ này: để _to_result báo lỗi có kiểu cho chính sách retry/hold của job
            if st == "paused":                              # daemon khởi động lại giữa chừng: tiếp tục (có giới hạn)
                if resumes >= self.max_resumes:
                    return job
                resumes += 1
                ctx.log("upload_job_retry", "warning", uploader_job=job.get("id"), state=st)
                job = self._retry(job)
                continue
            if time.time() - t0 > self.max_wait_s:
                raise StageError(ErrorClass.TRANSIENT, "UPLOAD_WAIT_TIMEOUT", f"chờ upload quá {self.max_wait_s:.0f}s (job vẫn chạy trong yt_uploader)",
                                 {"uploader_job": job.get("id")}, resource="network")
            if time.time() - last_log > 5:
                last_log = time.time()
                ctx.log("upload_progress", state=st, percent=job.get("progress_percent"))
            ctx.cancel.wait(self.poll_s)
            s2, d = self._call("GET", f"/jobs/{job['id']}")
            if s2 != 200:
                raise self._api_error(s2, d, "đọc job upload")
            job = d
