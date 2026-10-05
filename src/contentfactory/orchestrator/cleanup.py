"""Auto Cleanup (D-83): dọn thứ hệ thống tự sinh ra mà không còn cần, KHÔNG BAO GIỜ đụng `output/` của người dùng (ngoại trừ thư mục `.tmp-<job>` mồ côi do chính pipeline tạo).

Hai tầng cho job (chỉ job ĐÃ KẾT THÚC; job đang chạy/xếp hàng/bị giữ không bao giờ bị đụng):
  1. Trung gian: sau khi job PUBLISHED, xóa mọi file KHÔNG phải artifact đã đăng ký (chunk TTS, audio đã dọn biên, file tăng tốc, state worker, stderr…). Artifact, manifest và log còn nguyên
     (nên `from_job` và upload lại vẫn dùng được trong thời gian giữ).
  2. Artifact: job PUBLISHED quá `artifact_keep_days`, job FAILED quá `failed_keep_days`, job đã đạt target (chưa PUBLISHED) quá `artifact_keep_days` ⇒ xóa nốt artifact,
     chỉ giữ `manifest.json` + `job.log.jsonl` + `.cleaned.json` (ghi lại đã xóa gì). Sau bước này không dùng được `from_job` với job đó (báo lỗi rõ ràng).
Cache dùng chung (`runtime/cache/tts`, `runtime/cache/source`) bị giới hạn dung lượng: xóa file dùng lâu nhất (mtime) trước. Pool nguồn đã đồng bộ KHÔNG bị xóa tự động.
Mọi đường dẫn bị xóa được kiểm là nằm trong workspace/ hoặc runtime/cache/ của repo.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..jobs import pipeline as P
from ..jobs.workspace import job_dir

KEEP_NAMES = {"manifest.json", "job.log.jsonl", ".cleaned.json"}
DAY = 86400.0


def _within(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _files(d: Path) -> list[Path]:
    return [p for p in d.rglob("*") if p.is_file()] if d.is_dir() else []


def _rmdirs(d: Path) -> None:
    for p in sorted((x for x in d.rglob("*") if x.is_dir()), key=lambda x: -len(x.parts)):
        try:
            p.rmdir()                                            # chỉ xóa được thư mục rỗng
        except OSError:
            pass


def plan(orc, now: float | None = None) -> list[dict]:
    """Danh sách hành động dọn dẹp (chưa thực hiện): {kind, path, bytes, why}."""
    cfg = orc.cfg.data.get("cleanup", {})
    now = now or time.time()
    ws, actions = orc.cfg.path("workspace"), []

    def add(kind: str, p: Path, why: str) -> None:
        try:
            actions.append({"kind": kind, "path": p, "bytes": p.stat().st_size, "why": why})
        except OSError:
            pass
    for j in orc.store.list_jobs():
        jd = job_dir(ws, j["id"])
        if not jd.is_dir() or (jd / ".cleaned.json").exists() and not [f for f in _files(jd) if f.name not in KEEP_NAMES]:
            continue
        if j.get("lease_owner") or j["state"] not in P.TERMINAL and not P.is_complete(j["state"], j.get("target_idx")):
            continue                                              # đang chạy / xếp hàng / bị giữ
        if j.get("hold_reason"):
            continue
        age_d = (now - j["updated_at"]) / DAY
        arts = {(jd / a["path"]).resolve() for a in orc.store.artifacts(j["id"])}
        published = j["state"] == P.PUBLISHED
        deep = (published and age_d >= cfg.get("artifact_keep_days", 14)) or (j["state"] == P.FAILED and age_d >= cfg.get("failed_keep_days", 30)) \
            or (not published and j["state"] != P.FAILED and age_d >= cfg.get("artifact_keep_days", 14))
        for f in _files(jd):
            if f.name in KEEP_NAMES:
                continue
            if deep:
                add("job_artifact", f, f"job {j['id']} {j['state']} đã {age_d:.0f} ngày")
            elif published and cfg.get("intermediates_after_publish", True) and f.resolve() not in arts:
                add("job_intermediate", f, f"trung gian của job {j['id']} đã đăng")
    for name in ("tts", "source"):                                # cache dùng chung: giữ trong giới hạn dung lượng
        d = orc.cfg.path("runtime") / "cache" / name
        limit = float((cfg.get("cache_gb") or {}).get(name, 0)) * 2 ** 30
        files = sorted(_files(d), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for f in files:
            if limit and total > limit:
                add("cache", f, f"cache {name} vượt {limit / 2 ** 30:.0f} GB")
                total -= f.stat().st_size
    out = orc.cfg.path("output")                                  # chỉ thư mục tạm mồ côi do chính pipeline dựng dở
    for t in out.glob(".tmp-*") if out.is_dir() else []:
        if t.is_dir() and now - t.stat().st_mtime > DAY and not any(x.get("lease_owner") and t.name == f".tmp-{x['id']}" for x in orc.store.list_jobs()):
            for f in _files(t):
                add("output_tmp", f, "thư mục dựng dở bị bỏ rơi > 1 ngày")
    return actions


def run(orc, dry_run: bool = False, now: float | None = None) -> dict:
    now = now or time.time()
    actions = plan(orc, now)
    ws, cache = orc.cfg.path("workspace"), orc.cfg.path("runtime") / "cache"
    out_root = orc.cfg.path("output")
    freed, removed, by_kind, jobs = 0, 0, {}, set()
    if not dry_run:
        for a in actions:
            p: Path = a["path"]
            ok_root = (_within(p, ws) or _within(p, cache) or (a["kind"] == "output_tmp" and _within(p, out_root) and ".tmp-" in str(p)))
            if not ok_root or (a["kind"] != "output_tmp" and _within(p, out_root)):
                continue                                          # lưới an toàn: không bao giờ xóa ngoài vùng của hệ thống
            try:
                p.unlink()
            except OSError:
                continue
            freed += a["bytes"]
            removed += 1
            by_kind[a["kind"]] = by_kind.get(a["kind"], 0) + 1
            if a["kind"].startswith("job_"):
                jobs.add(next(x for x in p.parts[::-1] if x.startswith("job_")))
        for jid in jobs:
            jd = ws / jid
            _rmdirs(jd)
            deep = any(a["kind"] == "job_artifact" and jid in str(a["path"]) for a in actions)
            if deep:
                (jd / ".cleaned.json").write_text(json.dumps({"cleaned_at": now, "removed": sum(1 for a in actions if jid in str(a["path"]))}), encoding="utf-8")
        for d in (cache / "tts", cache / "source"):
            if d.is_dir():
                _rmdirs(d)
        for t in out_root.glob(".tmp-*") if out_root.is_dir() else []:
            if t.is_dir():
                _rmdirs(t)
                try:
                    t.rmdir()
                except OSError:
                    pass
        if removed:
            orc.log.emit("cleanup_done", files=removed, freed_bytes=freed, by_kind=by_kind, jobs=sorted(jobs))
    return {"dry_run": dry_run, "candidates": len(actions), "removed": removed, "freed_bytes": freed if not dry_run else sum(a["bytes"] for a in actions),
            "by_kind": by_kind if not dry_run else _count(actions), "jobs": sorted(jobs),
            "actions": [{**a, "path": str(a["path"])} for a in actions[:200]]}


def _count(actions: list[dict]) -> dict:
    out: dict = {}
    for a in actions:
        out[a["kind"]] = out.get(a["kind"], 0) + 1
    return out
