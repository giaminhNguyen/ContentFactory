"""ProviderChain: cài đặt SourceAdapter. Chọn provider theo thứ tự, fallback khi provider lỗi, cache và idempotency.

ContentFactory là orchestrator cấp cao: chain KHÔNG giữ state pipeline nào ngoài file trong workspace của job
(`subtitle_raw.meta.json` làm dấu vân tay) và cache dùng chung giữa các job (`<runtime>/cache/source/<key>/`).
State của job/stage nằm ở DB của ContentFactory; DB/queue của Subtitle_supperVip (nếu có) không bao giờ được chạm tới.

Thứ tự kiểm tra khi `acquire`:
  1. job: `subtitle_raw.meta.json` + file raw còn nguyên sha256 và cùng `cache_key`  -> dùng lại, không đụng mạng
  2. cache: cùng (loại nguồn, định danh, ngôn ngữ ưu tiên)                            -> copy vào job, không đụng mạng
  3. provider lần lượt (bỏ qua provider không khả dụng); lỗi "dứt khoát" về video (URL sai, video không tồn tại)
     dừng ngay, các lỗi khác chuyển sang provider kế
`refresh_source: true` trong params bỏ qua 1 và 2.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from pathlib import Path

from ..contracts import ErrorClass, SourceInput, SourceProvider, SourceResult, StageContext, StageError
from ..fsutil import atomic_write, atomic_write_json, sha256_file
from .youtube import parse_video_id

DEFINITIVE = {"NOT_YOUTUBE_URL", "BAD_VIDEO_ID", "VIDEO_UNAVAILABLE", "FILE_NOT_FOUND", "UNSUPPORTED_FORMAT",
              "EMPTY_SUBTITLE"}
RECORD = "subtitle_raw.meta.json"
RESULT_KEYS = ("source_url", "source_type", "provider", "video_id", "title", "description", "language",
               "subtitle_format", "subtitle_kind", "has_timestamps", "metadata", "status", "error", "attempts")


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _valid(rec: dict | None, key: str, raw: Path) -> bool:
    return bool(rec and rec.get("cache_key") == key and raw.is_file() and sha256_file(raw) == rec.get("raw_sha256"))


class ProviderChain:
    def __init__(self, providers: list[SourceProvider], cache_dir: Path | None = None, prefs: dict | None = None) -> None:
        self.providers = providers
        self.cache = Path(cache_dir) if cache_dir else None
        self.prefs = {"languages": ["vi", "en"], "allow_translation": False, **(prefs or {})}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def health(self) -> dict:
        per = {p.name: p.health() for p in self.providers}
        return {"ok": any(h.get("ok") for h in per.values()), "providers": per}

    # -- định danh nguồn --------------------------------------------------------------------------
    @staticmethod
    def _identity(src: SourceInput) -> str:
        kind = src["kind"]
        if kind == "youtube_url":
            return parse_video_id(src["value"])
        if kind == "transcript_file":
            p = Path(src["value"])
            if not p.is_file():
                raise StageError(ErrorClass.POLICY, "FILE_NOT_FOUND", str(p))
            return sha256_file(p)
        if kind == "text":
            return hashlib.sha256(src["value"].encode("utf-8")).hexdigest()
        raise StageError(ErrorClass.POLICY, "UNSUPPORTED_SOURCE", f"kind={kind!r}")

    # -- ghi/đọc bản ghi -------------------------------------------------------------------------
    @staticmethod
    def _record(res: SourceResult, raw: Path, key: str, origin_provider_time: float) -> dict:
        rec = {k: res.get(k) for k in RESULT_KEYS}
        rec.update(cache_key=key, raw_file=raw.name, raw_sha256=sha256_file(raw), fetched_at=origin_provider_time)
        return rec

    @staticmethod
    def _result(rec: dict, raw: Path, origin: str) -> SourceResult:
        res: SourceResult = {k: rec.get(k) for k in RESULT_KEYS}      # type: ignore[assignment]
        res["raw_subtitle_path"], res["origin"] = raw, origin
        return res

    @staticmethod
    def _place(src_file: Path, dst: Path) -> None:
        for other in dst.parent.glob("subtitle_raw.*"):                # bỏ raw cũ khác đuôi (lần thử trước, provider khác)
            if other != dst and not other.name.endswith(".meta.json"):
                other.unlink()
        atomic_write(dst, lambda tmp: shutil.copyfile(src_file, tmp))  # byte-identical

    # -- acquire -----------------------------------------------------------------------------------
    def acquire(self, src: SourceInput, out_dir: Path, ctx: StageContext) -> SourceResult:
        prefs = {**self.prefs, "languages": list(ctx.params.get("source_languages") or self.prefs["languages"])}
        refresh = bool(ctx.params.get("refresh_source"))
        providers = [p for p in self.providers if p.supports(src["kind"])]
        if not providers:
            raise StageError(ErrorClass.POLICY, "UNSUPPORTED_SOURCE", f"không provider nào hỗ trợ kind={src['kind']!r}")
        key = hashlib.sha256(json.dumps({"kind": src["kind"], "id": self._identity(src), "prefs": prefs},
                                        sort_keys=True).encode()).hexdigest()[:16]
        out_dir.mkdir(parents=True, exist_ok=True)
        # Hai job cùng một nguồn chạy song song: job thứ hai đợi job thứ nhất rồi dùng cache thay vì tải lần nữa.
        with self._lock_for(key):
            return self._acquire_locked(src, out_dir, ctx, providers, prefs, key, refresh)

    def _acquire_locked(self, src: SourceInput, out_dir: Path, ctx: StageContext, providers: list[SourceProvider],
                        prefs: dict, key: str, refresh: bool) -> SourceResult:
        record_path = out_dir / RECORD

        rec = _read_json(record_path)
        if rec and not refresh and _valid(rec, key, out_dir / rec.get("raw_file", "")):
            ctx.log("source_raw_reused", where="job")
            return self._result(rec, out_dir / rec["raw_file"], "job")

        entry = self.cache / key if self.cache else None
        if entry and not refresh:
            crec = _read_json(entry / "record.json")
            if _valid(crec, key, entry / (crec or {}).get("raw_file", "")):
                raw = out_dir / crec["raw_file"]
                self._place(entry / crec["raw_file"], raw)
                atomic_write_json(record_path, crec)
                ctx.log("source_raw_reused", where="cache", provider=crec.get("provider"))
                return self._result(crec, raw, "cache")

        res, attempts = self._try_providers(providers, src, out_dir, ctx, prefs)
        res = self._enrich(res, providers, src, ctx, attempts)
        res["attempts"] = attempts
        raw = out_dir / f"subtitle_raw.{res['subtitle_format']}"
        self._place(Path(res["raw_subtitle_path"]), raw)
        shutil.rmtree(out_dir / "_acq", ignore_errors=True)
        rec = self._record(res, raw, key, time.time())
        atomic_write_json(record_path, rec)
        if entry:
            entry.mkdir(parents=True, exist_ok=True)
            self._place(raw, entry / raw.name)
            atomic_write_json(entry / "record.json", rec)
        res["raw_subtitle_path"], res["origin"] = raw, "network"
        return res

    def _try_providers(self, providers: list[SourceProvider], src: SourceInput, out_dir: Path, ctx: StageContext,
                       prefs: dict) -> tuple[SourceResult, list[dict]]:
        attempts: list[dict] = []
        errors: list[StageError] = []
        for p in providers:
            if not p.available():
                attempts.append({"provider": p.name, "status": "skipped", "reason": "unavailable"})
                continue
            t0 = time.time()
            try:
                res = p.acquire(src, out_dir / "_acq" / p.name, ctx, prefs)
            except StageError as e:
                attempts.append({"provider": p.name, "status": "error", "error_class": e.error_class.value,
                                 "code": e.code, "message": e.message[:300], "seconds": round(time.time() - t0, 2)})
                ctx.log("source_provider_failed", "warning", provider=p.name, code=e.code, error_class=e.error_class.value)
                if e.code in DEFINITIVE:
                    raise StageError(e.error_class, e.code, e.message, {**e.detail, "attempts": attempts}) from None
                errors.append(e)
                continue
            attempts.append({"provider": p.name, "status": "ok", "seconds": round(time.time() - t0, 2)})
            ctx.log("source_provider_ok", provider=p.name, fmt=res.get("subtitle_format"), kind=res.get("subtitle_kind"))
            return res, attempts
        if not errors:
            raise StageError(ErrorClass.RESOURCE, "NO_SOURCE_PROVIDER", "không provider nào khả dụng", {"attempts": attempts})
        pick = next((e for e in errors if e.error_class == ErrorClass.TRANSIENT), errors[0])   # còn hy vọng retry thì ưu tiên báo TRANSIENT
        raise StageError(pick.error_class, pick.code, pick.message, {**pick.detail, "attempts": attempts})

    def _enrich(self, res: SourceResult, providers: list[SourceProvider], src: SourceInput, ctx: StageContext,
                attempts: list[dict]) -> SourceResult:
        """Provider chính thiếu title (supervip không có YOUTUBE_API_KEY) thì hỏi provider khác; vẫn thiếu thì dùng id."""
        if not res.get("title"):
            for p in providers:
                if p.name == res["provider"] or not p.available():
                    continue
                try:
                    extra = p.describe(src, ctx)
                except StageError as e:
                    attempts.append({"provider": p.name, "status": "describe_error", "code": e.code})
                    continue
                if extra.get("title"):
                    res["title"] = extra["title"]
                    res["description"] = res.get("description") or extra.get("description")
                    res["metadata"] = {**extra, **res.get("metadata", {}), "title_from": p.name}
                    break
        if not res.get("title"):
            res["title"] = res.get("video_id") or "untitled"
            res["metadata"] = {**res.get("metadata", {}), "title_from": "fallback"}
        return res
