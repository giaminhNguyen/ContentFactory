"""Render Manager: điều phối trên RenderAdapter (adapter chỉ biết "một audio -> một video").

  YouTube : audio_youtube (đã có watermark) -> 1 video 16:9 + thumbnail
  TikTok  : mỗi audio part -> 1 video 9:16 (part_NN.mp4), TRẠNG THÁI TỪNG PART được ghi vào checkpoint và báo cáo

Quy tắc:
  - Không render lại video đã hợp lệ: mỗi output có sidecar `<file>.key.json` {key, size}; key = audio sha256 + phần profile ảnh hưởng kết quả +
    dấu vân tay pool + phiên bản ContentFlow (+ chỉ số part). Resume/retry chỉ làm các output chưa hợp lệ. Video nền ngẫu nhiên không seed
    (D-08): coi video đã render là artifact, không tái sinh để so sánh.
  - Retry RIÊNG từng output (TRANSIENT, theo profile.retry). Lỗi TRANSIENT của một part KHÔNG chặn các part còn lại: render hết rồi mới báo
    `RENDER_PARTS_FAILED` với danh sách part lỗi, nên retry của job chỉ làm đúng các part đó. Lỗi khác (RESOURCE/AUTH/POLICY) đi thẳng lên
    cơ chế hold/fail của job vì thường ảnh hưởng mọi part.
  - Source pool là bước chuẩn bị DÙNG CHUNG (`prepare_pool`): ở đây chỉ gọi, adapter trả ngay nếu nguồn không đổi.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..contracts import ArtifactRef, ErrorClass, StageContext, StageError, StageResult
from ..fsutil import atomic_write_json
from . import profile as PF


def _h(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


class RenderManager:
    def __init__(self, render) -> None:
        self.render = render

    # ------------------------------------------------------------------------------------------ chuẩn bị
    def _profile(self, ctx: StageContext, pid: str) -> dict:
        return PF.resolve(pid, ctx.config.get("render"), ctx.params.get("render"))

    def _pool(self, ctx: StageContext, prof: dict) -> dict | None:
        if not getattr(self.render, "requires_pool", False):
            return None
        spec = PF.pool_spec(prof, ctx.config.get("render"))
        if spec is None:
            return None
        info = self.render.prepare_pool(spec, ctx)
        ctx.log("render_pool_ready", pool=spec["name"], reused=info.get("reused"), files=info.get("files"))
        return {**info, "name": spec["name"]}

    def _version(self) -> str:
        v = getattr(self.render, "version", None)
        return v() if callable(v) else "unversioned"

    @staticmethod
    def _valid(out: Path, key: str) -> dict | None:
        try:
            meta = json.loads(out.with_name(out.name + ".key.json").read_text(encoding="utf-8"))
            if meta["key"] == key and out.is_file() and out.stat().st_size == meta["size"] > 0:
                return meta
        except (OSError, ValueError, KeyError):
            pass
        return None

    @staticmethod
    def _stamp(out: Path, key: str, info: dict) -> None:
        atomic_write_json(out.with_name(out.name + ".key.json"), {"key": key, "size": out.stat().st_size, "info": info})

    @staticmethod
    def _invalidate(out: Path) -> None:
        for f in (out, out.with_name(out.name + ".key.json")):
            f.unlink(missing_ok=True)

    def _attempt(self, ctx: StageContext, label: str, retry: dict, fn):
        """Chạy fn() với retry RIÊNG cho output này (chỉ TRANSIENT). Trả (kết quả, số lần thử, danh sách mã lỗi đã gặp)."""
        errors: list[str] = []
        n_max = int(retry["max_attempts"])
        for n in range(1, n_max + 1):
            ctx.cancel.check()
            try:
                return fn(), n, errors
            except StageError as e:
                if e.error_class != ErrorClass.TRANSIENT:
                    e.detail = {**e.detail, "output": label}
                    raise
                errors.append(e.code)
                if n >= n_max:
                    e.detail = {**e.detail, "output": label, "attempts": n, "errors": errors}
                    raise
                wait = float(retry["backoff_s"][min(n - 1, len(retry["backoff_s"]) - 1)]) if retry["backoff_s"] else 0.0
                ctx.log("render_retry", "warning", output=label, try_no=n, error=e.code, wait_s=wait)
                ctx.cancel.wait(wait)
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------------------------------ YouTube
    def youtube(self, ctx: StageContext) -> StageResult:
        prof = self._profile(ctx, "youtube")
        meta = ctx.read_json("metadata")
        aref = ctx.inputs["audio_youtube"][0]
        pool = self._pool(ctx, prof)
        ver = self._version()
        base = {k: prof.get(k) for k in PF.KEY_FIELDS}
        vkey = _h("video", aref["sha256"], base, (pool or {}).get("fingerprint"), ver)
        video, thumb = ctx.stage_dir / "video.mp4", ctx.stage_dir / "thumbnail.jpg"
        states = {"video": {"state": "pending", "attempts": 0}, "thumbnail": {"state": "pending", "attempts": 0}}
        n_out = 2

        def note(**extra) -> None:
            done = sum(1 for s in states.values() if s["state"] in ("done", "reused"))
            ctx.progress(done, n_out, "youtube", force="current" not in extra, outputs=states, **extra)

        if self._valid(video, vkey):
            states["video"] = {"state": "reused", "attempts": 0}
        else:
            self._invalidate(video)
            states["video"]["state"] = "rendering"
            note()
            req = {"audio": ctx.path(aref), "audio_sha256": aref["sha256"], "profile": prof, "output": video, "pool": pool, "key": vkey,
                   "on_progress": lambda p: note(current=round(p, 3))}
            info, n, errs = self._attempt(ctx, "video", prof["retry"], lambda: self.render.render_video(req, ctx))
            self._stamp(video, vkey, info or {})
            states["video"] = {"state": "done", "attempts": n, **({"retry_errors": errs} if errs else {})}
        note()
        th = prof.get("thumbnail") or {}
        tkey = _h("thumb", meta["title"], ctx.params.get("channel", ""), th, ver)
        if self._valid(thumb, tkey):
            states["thumbnail"] = {"state": "reused", "attempts": 0}
        else:
            self._invalidate(thumb)
            states["thumbnail"]["state"] = "rendering"
            note()
            treq = {"title": meta["title"], "channel_name": ctx.params.get("channel", ""), "output": thumb, "image": th.get("image"),
                    "highlight": th.get("highlight", "auto"), "highlight_text": th.get("highlight_text", ""),
                    "config_overrides": th.get("config_overrides") or {}, "key": tkey}
            _, n, errs = self._attempt(ctx, "thumbnail", prof["retry"], lambda: self.render.render_thumbnail(treq, ctx))
            self._stamp(thumb, tkey, {})
            states["thumbnail"] = {"state": "done", "attempts": n, **({"retry_errors": errs} if errs else {})}
        note()
        report = {"schema": 1, "profile": base, "pool": {k: (pool or {}).get(k) for k in ("name", "fingerprint", "reused")}, "version": ver,
                  "audio_sha256": aref["sha256"], "outputs": states}
        rf = atomic_write_json(ctx.stage_dir / "render_report.json", report)
        return StageResult([ctx.draft(video, "video_youtube"), ctx.draft(thumb, "thumbnail"), ctx.draft(rf, "youtube_render_report")],
                           {"video": states["video"]["state"], "thumbnail": states["thumbnail"]["state"], "profile": "youtube"})

    # ------------------------------------------------------------------------------------------ TikTok
    def tiktok(self, ctx: StageContext) -> StageResult:
        prof = self._profile(ctx, "tiktok")
        refs: list[ArtifactRef] = sorted(ctx.inputs["audio_tiktok"], key=lambda r: r["meta"]["index"])
        pool = self._pool(ctx, prof)
        ver = self._version()
        base = {k: prof.get(k) for k in PF.KEY_FIELDS}
        states: dict[str, dict] = {str(r["meta"]["index"]): {"state": "pending", "attempts": 0} for r in refs}
        total = len(refs)

        def note(**extra) -> None:
            done = sum(1 for s in states.values() if s["state"] in ("done", "reused"))
            ctx.progress(done, total, "parts", force="current" not in extra, parts=states, **extra)   # đổi trạng thái part luôn được ghi; % thì giảm tần suất

        note()
        failed: dict[int, StageError] = {}
        for ref in refs:
            ctx.cancel.check()
            i = ref["meta"]["index"]
            out = ctx.stage_dir / f"part_{i:02d}.mp4"
            key = _h("tiktok-part", i, ref["sha256"], base, (pool or {}).get("fingerprint"), ver)
            if self._valid(out, key):
                states[str(i)] = {"state": "reused", "attempts": 0}
                note()
                continue
            self._invalidate(out)
            states[str(i)]["state"] = "rendering"
            note(current_part=i)
            req = {"audio": ctx.path(ref), "audio_sha256": ref["sha256"], "profile": prof, "output": out, "pool": pool, "key": key, "part": i,
                   "on_progress": lambda p, i=i: note(current_part=i, current=round(p, 3))}
            try:
                info, n, errs = self._attempt(ctx, f"part {i}", prof["retry"], lambda: self.render.render_video(req, ctx))
            except StageError as e:
                if e.error_class != ErrorClass.TRANSIENT:
                    states[str(i)] = {"state": "failed", "attempts": int(e.detail.get("attempts", 1)), "error": e.code}
                    note()
                    raise                                        # RESOURCE/AUTH/POLICY: thường ảnh hưởng mọi part, không tiếp tục
                states[str(i)] = {"state": "failed", "attempts": int(e.detail.get("attempts", 1)), "error": e.code}
                failed[i] = e
                ctx.log("render_part_failed", "error", part=i, error=e.code)
                note()
                continue                                         # part khác vẫn render; retry của job chỉ làm lại part lỗi
            self._stamp(out, key, info or {})
            states[str(i)] = {"state": "done", "attempts": n, **({"retry_errors": errs} if errs else {})}
            note()
        report = {"schema": 1, "profile": base, "pool": {k: (pool or {}).get(k) for k in ("name", "fingerprint", "reused")}, "version": ver,
                  "parts": states}
        rf = atomic_write_json(ctx.stage_dir / "render_report.json", report)
        if failed:
            first = next(iter(failed.values()))
            raise StageError(ErrorClass.TRANSIENT, "RENDER_PARTS_FAILED",
                             f"part lỗi: {sorted(failed)} ({', '.join(sorted({e.code for e in failed.values()}))}); các part còn lại đã render xong",
                             {"failed_parts": sorted(failed), "errors": {str(k): v.code for k, v in failed.items()}, "parts": states},
                             resource=first.resource)
        arts = [ctx.draft(ctx.stage_dir / f"part_{r['meta']['index']:02d}.mp4", "video_tiktok", index=r["meta"]["index"]) for r in refs]
        arts.append(ctx.draft(rf, "tiktok_render_report"))
        return StageResult(arts, {"parts": total, "rendered": sum(1 for s in states.values() if s["state"] == "done"),
                                  "reused": sum(1 for s in states.values() if s["state"] == "reused"), "profile": "tiktok"})
