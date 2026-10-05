"""Fake adapter cho Phase 1: chạy end-to-end mà không cần engine thật.

Chèn lỗi / độ trễ qua job.params["fake"][<điểm>] (xem `hook`):
  {"sleep_s": 30, "attempt": 1}              ngủ (huỷ được) chỉ ở attempt 1, để test kill/resume
  {"fail_until_attempt": 2, "error_class": "TRANSIENT", "code": "X"}   lỗi cho tới hết attempt 2
  thêm "resource": "network|token|quota|disk|runtime|credential|input", "retry_after_s": N, "resume_after_s": N
  {"fail_while_file": "<path>", ...}          lỗi chừng nào file đó còn tồn tại (test mô phỏng "tài nguyên đã hồi phục")
Điểm: source, story, tts_chunk_<n>, render_youtube, render_tiktok, render_tiktok_part_<n>, publish.
Audio là WAV thật (stdlib `wave`) để QA/ghép/cắt part có ý nghĩa; video/thumbnail chỉ là byte giả.
"""
from __future__ import annotations

import hashlib
import json
import random
import shutil
import time
import wave
from pathlib import Path

from ..contracts import ErrorClass, StageContext, StageError
from ..fsutil import atomic_write, atomic_write_bytes, atomic_write_json, atomic_write_text, sha256_file

RATE = 8000


def hook(ctx: StageContext, point: str) -> None:
    cfg = (ctx.params.get("fake") or {}).get(point)
    if not cfg:
        return
    if cfg.get("sleep_s") and cfg.get("attempt") in (None, ctx.attempt):
        ctx.log("fake_sleep", point=point, seconds=cfg["sleep_s"])
        ctx.cancel.wait(cfg["sleep_s"])
    still_down = bool(cfg.get("fail_while_file")) and Path(cfg["fail_while_file"]).exists()   # tài nguyên do test điều khiển
    if ctx.attempt <= cfg.get("fail_until_attempt", 0) or still_down:
        raise StageError(ErrorClass(cfg.get("error_class", "TRANSIENT")), cfg.get("code", "FAKE_FAILURE"),
                         f"injected at {point}, attempt {ctx.attempt}", resource=cfg.get("resource"),
                         retry_after_s=cfg.get("retry_after_s"),
                         resume_after=time.time() + cfg["resume_after_s"] if cfg.get("resume_after_s") else None)


WORDS = ("đêm khuya gió mưa hẻm nhỏ bước chân xa dần ngọn đèn vàng cánh cửa gỗ tiếng động lạ người đàn ông áo đen "
         "im lặng bóng tối kéo dài lá khô rơi con mèo trắng bờ sông lạnh sương mù tiếng chuông chùa vọng lại").split()


def _paragraph(k: int) -> str:
    """Đoạn giả có nội dung khác nhau thật sự (assembler sẽ loại đoạn gần trùng)."""
    rnd = random.Random(k)
    return f"Đoạn {k}. " + " ".join(rnd.choice(WORDS) for _ in range(45)) + "."


def record_call(ctx: StageContext, name: str) -> None:
    with open(ctx.stage_dir / "calls.log", "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{name} attempt={ctx.attempt}\n")


def _write_wav(path: Path, frames: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames)


def _read_frames(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def _silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(RATE * seconds)


def _tone(seconds: float) -> bytes:
    """Sóng vuông biên độ nhỏ (không phải im lặng) để QA im lặng/hỏng của TTS Manager có ý nghĩa."""
    half = (3000).to_bytes(2, "little", signed=True) * 40 + (-3000).to_bytes(2, "little", signed=True) * 40
    n = int(RATE * seconds)
    return (half * (n // 80 + 1))[: 2 * n]


HEALTH = {"ok": True, "fake": True}


class FakeSource:
    """SourceAdapter giả: ghi một phụ đề VTT thật nhỏ để Transcript Processor (code thật) chạy trên đó."""

    def acquire(self, src, out_dir: Path, ctx: StageContext):
        hook(ctx, "source")
        record_call(ctx, "source")
        out_dir.mkdir(parents=True, exist_ok=True)
        raw = out_dir / "subtitle_raw.vtt"
        atomic_write_text(raw, "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nBản ghi giả lập.\n")
        return {"source_url": src["value"], "source_type": "youtube", "provider": "fake", "video_id": "abcdefghijk",
                "title": ctx.params.get("title") or "Truyện thử nghiệm", "description": "Mô tả nguồn (không dùng)",
                "language": ctx.params.get("language", "vi"), "raw_subtitle_path": raw, "subtitle_format": "vtt",
                "subtitle_kind": "manual", "has_timestamps": True, "metadata": {}, "status": "ok", "error": None,
                "attempts": [{"provider": "fake", "status": "ok"}], "origin": "network"}

    def health(self) -> dict:
        return HEALTH


class FakeStory:
    def generate(self, bundle, profile: dict, out_dir: Path, ctx: StageContext):
        hook(ctx, "story")
        record_call(ctx, "story")
        n = int(profile.get("paragraphs", 6))
        paras = [_paragraph(k) for k in range(1, n + 1)]
        # 3 section CÓ heading và marker như engine thật hay sinh ra: Story Assembler phải gỡ sạch
        sections, per = [], max(1, n // 3)
        for s in range(3):
            body = paras[s * per:(s + 1) * per] if s < 2 else paras[2 * per:]
            p = out_dir / "sections" / f"section_{s + 1:02d}.md"
            atomic_write_text(p, f"Chương {s + 1}: Mở đầu\n\n" + "\n".join(body) + "\n\n---\n(Còn tiếp)\n")
            sections.append(p)
        return {"sections": sections, "stats": {"paragraphs": n}}

    def health(self) -> dict:
        return HEALTH


class FakeTTS:
    engine_id = "fake"

    def capabilities(self) -> dict:
        return {"max_chars": 600, "languages": ["vi"], "speed": False, "ssml": False, "sample_rate": RATE,
                "engine_version": "fake-1", "output_formats": ["wav"]}

    def synthesize(self, segment, profile: dict, out_path: Path, ctx: StageContext):
        hook(ctx, f"tts_chunk_{segment['index']}")
        record_call(ctx, f"tts_chunk_{segment['index']}")
        secs = max(0.2, len(segment["text"]) / 400)
        atomic_write(out_path, lambda tmp: _write_wav(tmp, _tone(secs)))
        return {"index": segment["index"], "duration_sec": secs}

    def health(self) -> dict:
        return HEALTH


class FakeAudio:
    def qa(self, audio: Path):
        try:
            with wave.open(str(audio), "rb") as w:
                dur = w.getnframes() / w.getframerate()
        except (wave.Error, EOFError, OSError):
            return {"ok": False, "duration_sec": 0.0, "issues": ["UNDECODABLE"]}
        return {"ok": dur > 0, "duration_sec": round(dur, 3), "issues": [] if dur > 0 else ["ZERO_DURATION"]}

    def qa_full(self, audio: Path, expect: dict, ctx: StageContext) -> dict:
        q = self.qa(audio)
        return {"ok": q["ok"], "errors": [{"code": c, "message": c} for c in q["issues"]], "warnings": [], "kind": expect.get("kind"),
                "measures": {"duration_sec": q["duration_sec"]}}

    def assemble(self, chunks, pauses_ms, out: Path, ctx: StageContext) -> dict:
        data, timeline, t = b"", [], 0.0
        for i, (c, p) in enumerate(zip(chunks, pauses_ms), 1):
            fr = _read_frames(c)
            d = len(fr) / 2 / RATE
            timeline.append({"index": i, "start_sec": round(t, 4), "end_sec": round(t + d, 4), "gap_after_sec": p / 1000,
                             "cut_sec": round(t + d + p / 2000, 4)})
            data += fr + _silence(p / 1000)
            t += d + p / 1000
        _write_wav(out, data)
        return {"duration_sec": len(data) / 2 / RATE, "timeline": timeline}

    def master(self, src: Path, out: Path, ctx: StageContext) -> dict:
        data = _read_frames(src)
        atomic_write(out, lambda tmp: _write_wav(tmp, data))
        return {"duration_sec": round(len(data) / 2 / RATE, 3), "fake": True}

    def build_youtube_audio(self, master: Path, watermark: Path | None, out: Path, ctx: StageContext) -> dict:
        data = (_read_frames(watermark) if watermark else b"") + _read_frames(master)
        atomic_write(out, lambda tmp: _write_wav(tmp, data))
        return {"duration_sec": round(len(data) / 2 / RATE, 3), "watermark": bool(watermark)}

    def build_tiktok_parts(self, master: Path, speed: float, target_part_sec: float, out_dir: Path,
                           ctx: StageContext, timeline=None) -> dict:
        # Fake: KHÔNG đổi tốc độ thật, chỉ chia theo độ dài nguồn tương ứng target*speed (xử lý thật: audio.processor.FfmpegAudio).
        data = _read_frames(master)
        step = max(2, int(RATE * target_part_sec * speed) * 2)
        step -= step % 2
        parts = []
        for i, off in enumerate(range(0, len(data), step), 1):
            p = out_dir / f"part_{i:02d}.wav"
            atomic_write(p, lambda tmp, o=off: _write_wav(tmp, data[o:o + step]))
            d = len(data[off:off + step]) / 2 / RATE
            parts.append({"path": p, "index": i, "start_sec": round(off / 2 / RATE, 3), "end_sec": round(off / 2 / RATE + d, 3),
                          "duration_sec": round(d, 3), "boundary": "fake", "forced": False, "mid_sentence": False})
        return {"parts": parts, "warnings": [], "stretch": {"engine": "fake"}, "split": {"boundaries": "fake"}}

    def health(self) -> dict:
        return HEALTH


def _canon(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


_OK = {"ok": True, "errors": [], "warnings": []}


def _terr(code: str, msg: str, **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, msg, detail, resource="input")


class FakeTemplateApi:
    """Bản trong bộ nhớ của hệ thống template ContentFlow (cùng hình dạng trả lời với `python -m templating`): đủ để test chọn template,
    version, publish/archive và snapshot mà không cần ContentFlow. Builtin giống bản thật: thumb_default, youtube_default, tiktok_default..."""

    BUILTIN = {"thumb_default": ("thumbnail", 1648, 928), "thumb_gold": ("thumbnail", 1648, 928), "youtube_default": ("video", 1920, 1080),
               "tiktok_default": ("video", 1080, 1920), "youtube_framed": ("video", 1920, 1080), "tiktok_framed": ("video", 1080, 1920)}

    def __init__(self) -> None:
        self.t: dict[str, dict] = {}
        for tid, (ty, w, h) in self.BUILTIN.items():
            doc = {"schema": 1, "id": tid, "name": tid.replace("_", " ").title(), "type": ty, "version": 1, "status": "published", "description": "",
                   "canvas": {"width": w, "height": h, **({"fps": 30} if ty == "video" else {})}, "elements": [{"id": "e", "type": "x", "z": 1}]}
            self.t[tid] = {"scope": "builtin", "type": ty, "versions": {1: doc}}

    def _tpl(self, tid: str) -> dict:
        if tid not in self.t:
            raise _terr("TEMPLATE_NOT_FOUND", f"template '{tid}' does not exist", template_id=tid)
        return self.t[tid]

    def _doc(self, tid: str, v) -> dict:
        vs = self._tpl(tid)["versions"]
        if v in ("latest", None):
            return vs[max(vs)]
        if v == "latest_published":
            pub = [n for n, d in vs.items() if d["status"] == "published"]
            if not pub:
                raise _terr("NO_PUBLISHED_VERSION", f"template '{tid}' has no published version", template_id=tid)
            return vs[max(pub)]
        if int(v) not in vs:
            raise _terr("TEMPLATE_VERSION_NOT_FOUND", f"template '{tid}' has no version {v}", template_id=tid)
        return vs[int(v)]

    def list_templates(self, type=None, status=None, scope=None, include_archived=False) -> dict:
        rows = []
        for tid, t in sorted(self.t.items()):
            if type and t["type"] != type:
                continue
            vs = t["versions"]
            pub = [n for n, d in vs.items() if d["status"] == "published"]
            if not pub and not include_archived and not any(d["status"] == "draft" for d in vs.values()):
                continue
            top = vs[max(pub or vs)]
            rows.append({"id": tid, "name": top["name"], "type": t["type"], "scope": t["scope"], "description": top["description"],
                         "latest_published": max(pub) if pub else None, "latest": max(vs),
                         "draft": next((n for n, d in vs.items() if d["status"] == "draft"), None),
                         "status": "published" if pub else top["status"], "canvas": top["canvas"],
                         "versions": [{"version": n, "status": d["status"]} for n, d in sorted(vs.items())]})
        return {"templates": rows}

    def versions(self, id: str) -> dict:
        return {"id": id, "versions": [{"id": id, "version": n, "status": d["status"], "checksum": _canon(d)}
                                       for n, d in sorted(self._tpl(id)["versions"].items())]}

    def get_template(self, id: str, version="latest", validate=True) -> dict:
        d = self._doc(id, version)
        return {"template": d, "scope": self._tpl(id)["scope"], "checksum": _canon(d), "versions": self.versions(id)["versions"], "validation": _OK}

    def validate(self, id=None, version=None, template=None) -> dict:
        return _OK

    def create_draft(self, type: str, id: str, name: str, description: str = "", width=None, height=None, scope: str = "user") -> dict:
        if id in self.t:
            raise _terr("TEMPLATE_ID_EXISTS", f"template id '{id}' already exists")
        w, h = width or (1648 if type == "thumbnail" else 1920), height or (928 if type == "thumbnail" else 1080)
        doc = {"schema": 1, "id": id, "name": name, "type": type, "version": 1, "status": "draft", "description": description,
               "canvas": {"width": w, "height": h}, "elements": [{"id": "e", "type": "x", "z": 1}]}
        self.t[id] = {"scope": "user", "type": type, "versions": {1: doc}}
        return {"template": doc, "validation": _OK}

    def new_draft(self, id: str, from_version=None) -> dict:
        t = self._tpl(id)
        if any(d["status"] == "draft" for d in t["versions"].values()):
            raise _terr("DRAFT_EXISTS", f"template '{id}' already has an open draft")
        doc = json.loads(json.dumps(self._doc(id, from_version if from_version is not None else "latest")))
        doc.update(version=max(t["versions"]) + 1, status="draft")
        t["versions"][doc["version"]] = doc
        return {"template": doc, "validation": _OK}

    def save_draft(self, id: str, version: int, template: dict) -> dict:
        d = self._doc(id, int(version))
        if d["status"] != "draft":
            raise _terr("TEMPLATE_IMMUTABLE", f"template '{id}' v{version} is {d['status']} and immutable")
        new = {**template, "id": id, "version": int(version), "status": "draft", "type": d["type"]}
        self._tpl(id)["versions"][int(version)] = new
        return {"template": new, "checksum": _canon(new), "validation": _OK}

    def publish(self, id: str, version: int) -> dict:
        d = self._doc(id, int(version))
        changed = d["status"] != "published"
        d["status"] = "published"
        return {"id": id, "version": int(version), "status": "published", "changed": changed, "checksum": _canon(d)}

    def archive(self, id: str, version=None) -> dict:
        done = []
        for n, d in self._tpl(id)["versions"].items():
            if (version is None or n == int(version)) and d["status"] == "published":
                d["status"] = "archived"
                done.append(n)
        return {"id": id, "archived": done}

    def delete_draft(self, id: str, version: int) -> dict:
        t = self._tpl(id)
        if t["versions"].get(int(version), {}).get("status") != "draft":
            raise _terr("TEMPLATE_IMMUTABLE", "only drafts can be deleted")
        del t["versions"][int(version)]
        if not t["versions"]:
            del self.t[id]
        return {"deleted": f"{id}@v{version}"}

    def duplicate(self, id: str, new_id: str, name=None, version=None) -> dict:
        base = self._doc(id, version if version is not None else "latest")
        doc = json.loads(json.dumps(base))
        doc.update(id=new_id, name=name or f"{base['name']} Copy", version=1, status="draft")
        self.t[new_id] = {"scope": "user", "type": base["type"], "versions": {1: doc}}
        return {"template": doc, "validation": _OK}

    def resolve(self, id: str, policy="latest_published", expect_type=None, allow_draft=False) -> dict:
        d = self._doc(id, policy)
        if d["status"] == "draft" and not allow_draft:
            raise _terr("TEMPLATE_IS_DRAFT", f"template '{id}' v{d['version']} is a draft")
        if expect_type and d["type"] != expect_type:
            raise _terr("TEMPLATE_WRONG_TYPE", f"template '{id}' is a {d['type']} template, not {expect_type}")
        c, cs = d["canvas"], _canon(d)
        return {"schema": 1, "id": id, "version": d["version"], "type": d["type"], "name": d["name"], "scope": self._tpl(id)["scope"],
                "status": d["status"], "policy": policy, "checksum": cs, "fingerprint": cs, "template": json.loads(json.dumps(d)), "assets": {},
                "summary": {"canvas": [c["width"], c["height"]], "fps": c.get("fps")}}

    def resolve_many(self, requests: list[dict]) -> dict:
        out = {}
        for r in requests:
            try:
                out[r["key"]] = self.resolve(r["id"], r.get("policy", "latest_published"), r.get("expect_type"))
            except StageError as e:
                e.detail.setdefault("key", r["key"])
                raise
        return out

    def list_assets(self, type=None, scope=None) -> dict:
        return {"assets": [], "types": ["background", "font", "frame", "logo", "mask", "overlay"]}

    def info(self) -> dict:
        return {"roots": {}, "counts": {"assets": 0, "templates": len(self.t)}, "problems": []}

    def preview(self, *a, **k) -> dict:
        raise _terr("PREVIEW_UNAVAILABLE", "fake render adapter has no preview")

    test_render = preview


class FakeRender:
    requires_pool = False                                   # fake không cần source pool
    supports_templates = True

    @property
    def templates(self) -> FakeTemplateApi:
        if getattr(self, "_templates", None) is None:                # lazy: lớp con (vd GatedRender trong test) có thể không gọi __init__
            self._templates = FakeTemplateApi()
        return self._templates

    def prepare_pool(self, pool, ctx=None) -> dict:
        return {"dir": None, "fingerprint": "fake", "reused": True}

    def pool_status(self, pool) -> dict:
        return {"name": pool.get("name"), "ready": True, "syncing": False}

    def version(self) -> str:
        return "fake-render-1"

    def render_video(self, req, ctx: StageContext) -> dict:
        pid = req["profile"]["id"]
        hook(ctx, f"render_{pid}")
        if req.get("part"):
            hook(ctx, f"render_{pid}_part_{req['part']}")
        record_call(ctx, f"render_{pid}:{req['output'].name}")
        sha = sha256_file(req["audio"])[:12]
        t = req.get("template")
        tpl = f"|template={t['id']}@v{t['version']}" if t else ""
        atomic_write_bytes(req["output"], f"FAKE-MP4|profile={pid}|audio_sha={sha}{tpl}\n".encode())
        return {"warnings": []}

    def render_thumbnail(self, req, ctx: StageContext) -> Path:
        t = req.get("template")
        tpl = f"|template={t['id']}@v{t['version']}" if t else ""
        record_call(ctx, "thumbnail")
        atomic_write_bytes(req["output"], f"FAKE-JPG|{req['title']}|{req['channel_name']}{tpl}\n".encode())
        return req["output"]

    def health(self) -> dict:
        return HEALTH


class FakePublish:
    platform = "youtube"

    def publish(self, req, ctx: StageContext):
        hook(ctx, "publish")
        record_call(ctx, "publish")
        vid = "fake-" + hashlib.sha1(req["idempotency_key"].encode()).hexdigest()[:10]   # cùng key => cùng video
        return {"state": "completed", "remote_id": vid, "remote_url": f"https://youtube.invalid/watch?v={vid}",
                "warnings": [], "job_id": "fake-job"}

    def find(self, idempotency_key: str):
        return None

    def health(self) -> dict:
        return HEALTH
