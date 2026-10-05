"""Daemon yt_uploader GIẢ (cùng API/JSON như docs/API.md của yt_uploader) để test PublishAdapter không cần Google/Go.

Hành vi của job điều khiển bằng `server.mode` lúc tạo job (hoặc `server.retry_mode` sau `retry`):
  ok               queued -> uploading -> completed (sau vài lần poll)
  warn             completed nhưng kèm last_error (thumbnail/playlist lỗi; video đã lên, không upload lại)
  fail:<code>      failed ngay lần poll đầu với last_error.code = <code> (error_class suy ra giống daemon thật)
  slow             giữ nguyên `uploading` cho tới khi gọi server.release() (mô phỏng upload lâu)
  paused           paused ngay (daemon khởi động lại giữa chừng)
`server.requests` ghi lại mọi request (method, path, body) để test kiểm tra không tạo job/retry thừa.
"""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CLASS = {"network_error": "TRANSIENT", "rate_limited": "TRANSIENT", "auth_required": "AUTH", "auth_revoked": "AUTH", "invalid_metadata": "POLICY",
         "quota_exceeded": "POLICY", "youtube_rejected": "POLICY", "invalid_file": "RESOURCE", "file_changed": "RESOURCE",
         "database_error": "RESOURCE", "AMBIGUOUS_UPLOAD": "AMBIGUOUS_PUBLISH"}


class FakeUploader:
    TOKEN = "test-token"

    def __init__(self, port: int = 0) -> None:
        self.jobs: dict[str, dict] = {}
        self.by_key: dict[str, str] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.mode, self.retry_mode = "ok", "ok"
        self.features = ["idempotency_key", "resume_probe"]
        self.lock = threading.Lock()
        self.gate = threading.Event()
        self.seq = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _auth(self):
                if self.headers.get("Authorization") != f"Bearer {outer.TOKEN}":
                    self._send(401, {"error": {"code": "unauthorized", "message": "bad token"}})
                    return False
                return True

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}") if n else {}

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                outer.requests.append(("GET", u.path, None))
                if u.path == "/api/v1/health":
                    return self._send(200, {"status": "ok", "worker": True, "protocol": 1, "features": outer.features})
                if not self._auth():
                    return
                if u.path == "/api/v1/jobs":
                    k = (q.get("idempotency_key") or [None])[0]
                    jobs = [outer.jobs[outer.by_key[k]]] if k in outer.by_key else []
                    return self._send(200, {"jobs": [outer.view(j) for j in jobs], "limit": 50, "offset": 0})
                if u.path.startswith("/api/v1/jobs/"):
                    j = outer.jobs.get(u.path.rsplit("/", 1)[1])
                    if not j:
                        return self._send(404, {"error": {"code": "not_found", "message": "no job"}})
                    outer.advance(j)
                    return self._send(200, outer.view(j))
                self._send(404, {"error": {"code": "not_found", "message": u.path}})

            def do_POST(self):
                u = urllib.parse.urlparse(self.path)
                body = self._body()
                outer.requests.append(("POST", u.path, body))
                if not self._auth():
                    return
                if u.path == "/api/v1/jobs":
                    return outer.create(self, body)
                if u.path.endswith("/retry"):
                    j = outer.jobs.get(u.path.split("/")[-2])
                    if not j:
                        return self._send(404, {"error": {"code": "not_found", "message": "no job"}})
                    if j["state"] not in ("failed", "paused", "cancelled"):
                        return self._send(409, {"error": {"code": "conflict", "message": "not retryable"}})
                    if (j.get("last_error") or {}).get("code") == "AMBIGUOUS_UPLOAD":
                        return self._send(409, {"error": {"code": "conflict", "message": "ambiguous; force needed"}})
                    j.update(state="queued", last_error=None, error_class=None, mode=outer.retry_mode, polls=0, attempts=j["attempts"] + 1)
                    return self._send(200, outer.view(j))
                self._send(404, {"error": {"code": "not_found", "message": u.path}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    # ------------------------------------------------------------------ logic
    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def release(self):
        self.gate.set()

    def create(self, h, b):
        def bad(code, c, m):
            h._send(code, {"error": {"code": c, "message": m}})
        key = b.get("idempotency_key")
        with self.lock:
            if key and key in self.by_key:
                return h._send(200, self.view(self.jobs[self.by_key[key]]))
            f = Path(b.get("file_path", ""))
            if not f.is_absolute() or not f.is_file() or f.stat().st_size == 0:
                return bad(400, "invalid_file", f"file not found: {f}")
            if not isinstance(b.get("made_for_kids"), bool):
                return bad(400, "invalid_metadata", "made_for_kids is required")
            if len(b.get("title", "")) > 100:
                return bad(400, "invalid_metadata", "title too long")
            t = b.get("thumbnail_path")
            if t and (not Path(t).is_file() or Path(t).stat().st_size > 2 * 1024 * 1024):
                return bad(400, "invalid_metadata", "thumbnail > 2 MiB or missing")
            self.seq += 1
            jid = f"job{self.seq}"
            self.jobs[jid] = {"id": jid, "state": "queued", "attempts": 1, "mode": self.mode, "polls": 0, "request": b, "idempotency_key": key,
                              "last_error": None, "video_id": None, "progress_percent": 0}
            if key:
                self.by_key[key] = jid
            h._send(201, self.view(self.jobs[jid]))

    def advance(self, j):
        if j["state"] in ("completed", "failed", "cancelled", "paused") or j["state"] == "queued" and False:
            return
        j["polls"] += 1
        mode = j["mode"]
        if mode.startswith("fail:"):
            code = mode.split(":", 1)[1]
            j.update(state="failed", last_error={"code": code, "message": f"injected {code}"}, error_class=CLASS.get(code))
        elif mode == "paused":
            j["state"] = "paused"
        elif mode == "slow":
            j.update(state="uploading", progress_percent=40)
            if self.gate.is_set():
                self.done(j, None)
        elif j["polls"] >= 2:
            self.done(j, {"code": "invalid_metadata", "message": "thumbnail failed"} if mode == "warn" else None)
        else:
            j.update(state="uploading", progress_percent=50)

    def done(self, j, err):
        vid = "vid" + j["id"][3:]
        j.update(state="completed", video_id=vid, last_error=err, progress_percent=100)
        if err:
            j["error_class"] = CLASS.get(err["code"])

    @staticmethod
    def view(j):
        v = {k: j[k] for k in ("id", "state", "attempts", "idempotency_key", "progress_percent") if j.get(k) is not None}
        r = j["request"]
        v.update({"file_path": r["file_path"], "title": r.get("title"), "made_for_kids": r.get("made_for_kids")})
        if j.get("video_id"):
            v.update(video_id=j["video_id"], video_url=f"https://www.youtube.com/watch?v={j['video_id']}")
        if j.get("last_error"):
            v["last_error"] = j["last_error"]
            if j.get("error_class"):
                v["error_class"] = j["error_class"]
        return v

    def count(self, method: str, path_contains: str) -> int:
        return sum(1 for m, p, _ in self.requests if m == method and path_contains in p)

    def creates(self) -> int:
        """Số lần POST /jobs (tạo job), không tính /jobs/{id}/retry."""
        return sum(1 for m, p, _ in self.requests if m == "POST" and p.endswith("/jobs"))
