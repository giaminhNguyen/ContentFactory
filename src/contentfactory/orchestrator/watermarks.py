"""Watermark Library (End-to-End Task, Phần A): watermark là Channel Asset có thư viện + revision bất biến, KHÔNG phải một file `watermark.wav` bị ghi đè.

    channels/<kênh>/
        channel.json                    "watermark": "watermarks/wm_x/rev_0002.wav", "watermark_ref": {"id": "wm_x", "revision": 2}   (active; cả hai để tương thích ngược)
        watermarks/registry.json        danh tính + tên + nguồn + revision hiện tại + lưu trữ (metadata nhỏ, ghi atomic)
        watermarks/wm_x/rev_0001.wav    audio của revision (ghi `*.part` rồi đổi tên: chỉ revision có CẢ audio lẫn json mới hợp lệ)
        watermarks/wm_x/rev_0001.json   metadata revision: sha256, thời lượng, nguồn, fingerprint, văn bản + TTS (nếu tạo bằng TTS)

Quy tắc:
  - Revision đã tạo là bất biến: sửa text/TTS/thay file ⇒ revision MỚI; lỗi giữa chừng ⇒ revision active cũ nguyên vẹn.
  - Active được chọn tường minh (`activate`) và đổi mà không xóa watermark cũ. Revision đang được job tham chiếu (`params.watermark_ref`) không bao giờ bị xóa vật lý:
    watermark có tham chiếu chỉ được lưu trữ (ẩn), job cũ vẫn tái lập được.
  - Channel cũ chỉ có `"watermark": "watermark.wav"` vẫn chạy y nguyên: hiện trong thư viện như một mục `legacy` (không bắt migration); mọi upload MỚI đi vào thư viện.
  - AudioProcessor không biết registry: pipeline vẫn nhận một đường dẫn file + sha256 (job snapshot `watermark` + `watermark_ref`).
Ghi registry dưới khóa theo kênh (cùng tiến trình); hai tiến trình ghi đồng thời cùng kênh không được hỗ trợ (giao diện chỉ chạy một tiến trình ghi).
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from ..contracts import ErrorClass, StageError
from ..fsutil import atomic_write, atomic_write_json, sha256_file, wav_header
from . import channels as CH
from .config import Config

AUDIO_EXT = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac")
MAX_BYTES = 50 * 1024 * 1024
MIN_DURATION_S = 0.1
WM_ID = re.compile(r"wm_[a-z0-9]{8}")
REQUEST_KEEP = 50
LEGACY_ID = "legacy"
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _err(code: str, message: str, hint: str = "", **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, message, {**detail, **({"hint": hint} if hint else {})}, resource="input")


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def new_id() -> str:
    return "wm_" + uuid.uuid4().hex[:8]


def rev_name(rev: int, ext: str) -> str:
    return f"rev_{rev:04d}{ext}"


def probe_audio(path: Path, audio=None) -> dict:
    """Kiểm audio watermark (decode được, thời lượng > 0, không rỗng, không quá lớn). WAV đọc bằng stdlib; định dạng khác cần AudioProcessor (ffprobe).
    Trả {duration_sec, bytes, sha256}; raise WATERMARK_AUDIO_INVALID với lý do cụ thể."""
    path = Path(path)
    bad = lambda why: _err("WATERMARK_AUDIO_INVALID", f"File audio watermark không dùng được: {why}.", "Chọn file WAV/MP3/M4A/FLAC/OGG nghe được, dài hơn 0,1 giây.", reason=why)
    if path.suffix.lower() not in AUDIO_EXT:
        raise bad(f"định dạng {path.suffix or '?'} không được hỗ trợ")
    try:
        size = path.stat().st_size
    except OSError:
        raise bad("không đọc được file") from None
    if size <= 0:
        raise bad("file rỗng")
    if size > MAX_BYTES:
        raise bad(f"file quá lớn ({size // (1024 * 1024)} MB, tối đa {MAX_BYTES // (1024 * 1024)} MB)")
    dur = None
    if path.suffix.lower() == ".wav":
        try:
            dur = float(wav_header(path)["duration"])
        except ValueError as e:
            raise bad(f"không phải WAV hợp lệ ({e})") from None
    if audio is not None:
        q = audio.qa(path)
        if not q["ok"]:
            raise bad("không giải mã được hoặc thời lượng bằng 0 (" + ",".join(q.get("issues") or []) + ")")
        dur = float(q["duration_sec"]) if dur is None else dur
    if dur is None:
        raise bad("không đọc được thời lượng (cần ffmpeg để kiểm file không phải WAV)")
    if dur < MIN_DURATION_S:
        raise bad("quá ngắn hoặc rỗng")
    return {"duration_sec": round(dur, 3), "bytes": size, "sha256": sha256_file(path)}


class Watermarks:
    def __init__(self, cfg: Config, store=None, audio=None) -> None:
        self.cfg, self.store, self.audio = cfg, store, audio

    # ------------------------------------------------------------------------------------------ đường dẫn + registry
    def channel_dir(self, channel_id: str) -> Path:
        return CH.channel_dir(self.cfg, channel_id)

    def root(self, channel_id: str) -> Path:
        return self.channel_dir(channel_id) / "watermarks"

    def lock(self, channel_id: str) -> threading.RLock:
        return _lock_for(self.channel_dir(channel_id))

    def _registry(self, channel_id: str) -> dict:
        f = self.root(channel_id) / "registry.json"
        try:
            reg = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(reg.get("watermarks"), dict):
                reg.setdefault("requests", {})
                return reg
        except (OSError, ValueError):
            pass
        return {"schema": 1, "watermarks": {}, "requests": {}}

    def _save(self, channel_id: str, reg: dict) -> None:
        reg["requests"] = dict(list(reg.get("requests", {}).items())[-REQUEST_KEEP:])
        atomic_write_json(self.root(channel_id) / "registry.json", reg)

    @staticmethod
    def _check_id(wm_id: str) -> str:
        if not WM_ID.fullmatch(wm_id or ""):
            raise _err("WATERMARK_NOT_FOUND", "Không có watermark này.", "Chọn một watermark trong thư viện.")
        return wm_id

    def revisions(self, channel_id: str, wm_id: str) -> list[dict]:
        """Revision HỢP LỆ (có cả json lẫn audio còn đủ kích thước); `*.part` và json mồ côi bị bỏ qua."""
        d = self.root(channel_id) / self._check_id(wm_id)
        out = []
        for f in sorted(d.glob("rev_*.json")) if d.is_dir() else []:
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                audio = d / meta["file"]
                if meta["revision"] >= 1 and audio.is_file() and audio.stat().st_size == meta["bytes"]:
                    out.append(meta)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(out, key=lambda m: m["revision"])

    def revision(self, channel_id: str, wm_id: str, revision: int) -> dict:
        for m in self.revisions(channel_id, wm_id):
            if m["revision"] == revision:
                return m
        raise _err("WATERMARK_REVISION_NOT_FOUND", f"Không có bản {revision} của watermark này.", "Chọn một bản có trong danh sách.")

    def revision_path(self, channel_id: str, wm_id: str, meta: dict) -> Path:
        return self.root(channel_id) / wm_id / meta["file"]

    def _entry(self, channel_id: str, wm_id: str) -> dict:
        e = self._registry(channel_id)["watermarks"].get(self._check_id(wm_id))
        if e is None:
            raise _err("WATERMARK_NOT_FOUND", "Không có watermark này.", "Chọn một watermark trong thư viện.")
        return e

    # ------------------------------------------------------------------------------------------ channel.json (active)
    def _channel_raw(self, channel_id: str) -> dict:
        f = self.channel_dir(channel_id) / "channel.json"
        try:
            raw = json.loads(f.read_text(encoding="utf-8-sig")) if f.is_file() else {}
        except (OSError, ValueError) as e:
            raise _err("INVALID_CHANNEL_CONFIG", f"Không đọc được channel.json: {e}") from None
        return raw if isinstance(raw, dict) else {}

    def _write_channel(self, channel_id: str, raw: dict) -> None:
        atomic_write_json(self.channel_dir(channel_id) / "channel.json", raw)

    def active_ref(self, channel_id: str) -> dict | None:
        ref = self._channel_raw(channel_id).get("watermark_ref")
        return {"id": ref["id"], "revision": int(ref["revision"])} if isinstance(ref, dict) and ref.get("id") and ref.get("revision") else None

    def activate(self, channel_id: str, wm_id: str, revision: int | None = None) -> dict:
        """Chọn watermark (bản hiện tại hoặc một bản cụ thể) làm watermark đang dùng của kênh. Không xóa gì."""
        with self.lock(channel_id):
            if wm_id == LEGACY_ID:
                return self._activate_legacy(channel_id)
            entry = self._entry(channel_id, wm_id)
            if entry.get("archived"):
                raise _err("WATERMARK_ARCHIVED", "Watermark này đã được lưu trữ.", "Khôi phục nó trước khi dùng.")
            meta = self.revision(channel_id, wm_id, revision or entry["current_revision"])
            self._write_active(channel_id, wm_id, meta)
            return self.get(channel_id, wm_id)

    @staticmethod
    def _remember_legacy(raw: dict) -> None:
        """Trước khi `watermark` bị thay bằng đường dẫn thư viện: nhớ tên file kiểu cũ để vẫn chọn lại được (file không bị xóa)."""
        cur = raw.get("watermark")
        if isinstance(cur, str) and cur.strip() and not cur.replace("\\", "/").startswith("watermarks/") and "legacy_watermark" not in raw:
            raw["legacy_watermark"] = Path(cur).name

    def _write_active(self, channel_id: str, wm_id: str, meta: dict) -> None:
        raw = self._channel_raw(channel_id)
        self._remember_legacy(raw)
        raw["watermark"] = f"watermarks/{wm_id}/{meta['file']}"
        raw["watermark_ref"] = {"id": wm_id, "revision": meta["revision"]}
        self._write_channel(channel_id, raw)

    def deactivate(self, channel_id: str) -> None:
        """Bỏ watermark khỏi kênh (file/thư viện không bị xóa). Job mới chạy không watermark."""
        with self.lock(channel_id):
            raw = self._channel_raw(channel_id)
            if "watermark" in raw or "watermark_ref" in raw:
                self._remember_legacy(raw)
                raw.pop("watermark", None)
                raw.pop("watermark_ref", None)
                self._write_channel(channel_id, raw)

    def _legacy_file(self, channel_id: str) -> Path | None:
        """File watermark kiểu cũ (`watermark` tương đối, không nằm trong thư viện) của kênh, nếu còn tồn tại."""
        raw = self._channel_raw(channel_id).get("watermark")
        if not isinstance(raw, str) or not raw.strip() or raw.replace("\\", "/").startswith("watermarks/"):
            return None
        base = self.channel_dir(channel_id)
        p = Path(raw)
        p = p if p.is_absolute() else base / p
        return p if p.is_file() else None

    def _activate_legacy(self, channel_id: str) -> dict:
        legacy = self._legacy_file_any(channel_id)
        if legacy is None:
            raise _err("WATERMARK_NOT_FOUND", "File watermark cũ không còn.", "Tải watermark mới lên.")
        raw = self._channel_raw(channel_id)
        raw["watermark"] = legacy.name
        raw.pop("watermark_ref", None)
        self._write_channel(channel_id, raw)
        return self.legacy_item(channel_id)

    def _legacy_file_any(self, channel_id: str) -> Path | None:
        """File legacy kể cả khi đang không active (đã chuyển sang watermark khác): `channels/<kênh>/watermark.<ext>` hoặc tên đã ghi trong `legacy_watermark`."""
        f = self._legacy_file(channel_id)
        if f is not None:
            return f
        raw = self._channel_raw(channel_id)
        name = raw.get("legacy_watermark")
        if isinstance(name, str) and name and "/" not in name and "\\" not in name:
            p = self.channel_dir(channel_id) / name
            return p if p.is_file() else None
        for ext in AUDIO_EXT:
            p = self.channel_dir(channel_id) / f"watermark{ext}"
            if p.is_file():
                return p
        return None

    # ------------------------------------------------------------------------------------------ đọc
    def _view(self, channel_id: str, entry: dict, active: dict | None, detail: bool = False) -> dict:
        revs = self.revisions(channel_id, entry["id"])
        cur = next((m for m in revs if m["revision"] == entry["current_revision"]), None)
        used = self.used_by(entry["id"]) if self.store is not None else {}
        item = {"id": entry["id"], "name": entry["name"], "source": entry["source"], "archived": bool(entry.get("archived")),
                "created_at": entry["created_at"], "updated_at": entry["updated_at"], "current_revision": entry["current_revision"],
                "active": bool(active and active["id"] == entry["id"]), "active_revision": active["revision"] if active and active["id"] == entry["id"] else None,
                "valid": cur is not None, "duration_sec": cur["duration_sec"] if cur else None, "bytes": cur["bytes"] if cur else None,
                "revision_count": len(revs), "in_use_by_jobs": sum(used.values()),
                "tts": _tts_summary(cur) if cur else None, "text": ((cur or {}).get("tts") or {}).get("text")}
        if detail:
            item["revisions"] = [{"revision": m["revision"], "created_at": m["created_at"], "duration_sec": m["duration_sec"], "sha": m["sha256"][:12],
                                  "source": m["source"], "current": m["revision"] == entry["current_revision"],
                                  "active": bool(active and active["id"] == entry["id"] and active["revision"] == m["revision"]),
                                  "jobs": used.get(m["revision"], 0), "tts": _tts_summary(m), "text": (m.get("tts") or {}).get("text")} for m in revs]
        return item

    def legacy_item(self, channel_id: str) -> dict | None:
        f = self._legacy_file_any(channel_id)
        if f is None:
            return None
        active_legacy = self._legacy_file(channel_id) is not None and self.active_ref(channel_id) is None
        try:
            probe = probe_audio(f, self.audio)
        except StageError:
            probe = None
        st = f.stat()
        used = self.store.jobs_using_watermark_path(str(f)) if self.store is not None else 0
        return {"id": LEGACY_ID, "name": f"Watermark cũ ({f.name})", "source": "legacy", "archived": False, "created_at": st.st_ctime, "updated_at": st.st_mtime,
                "current_revision": 1, "active": active_legacy, "active_revision": 1 if active_legacy else None, "valid": probe is not None,
                "duration_sec": probe["duration_sec"] if probe else None, "bytes": st.st_size, "revision_count": 1, "in_use_by_jobs": used, "tts": None, "text": None}

    def list(self, channel_id: str, include_archived: bool = False) -> dict:
        active = self.active_ref(channel_id)
        reg = self._registry(channel_id)
        items = [self._view(channel_id, e, active) for e in reg["watermarks"].values() if include_archived or not e.get("archived")]
        items.sort(key=lambda i: (not i["active"], -i["updated_at"]))
        legacy = self.legacy_item(channel_id)
        if legacy:
            items.append(legacy) if not legacy["active"] else items.insert(0, legacy)
        return {"items": items, "active": next((i["id"] for i in items if i["active"]), None), "archived": sum(1 for e in reg["watermarks"].values() if e.get("archived"))}

    def get(self, channel_id: str, wm_id: str) -> dict:
        if wm_id == LEGACY_ID:
            item = self.legacy_item(channel_id)
            if item is None:
                raise _err("WATERMARK_NOT_FOUND", "Không có watermark này.", "Chọn một watermark trong thư viện.")
            return {**item, "revisions": []}
        return self._view(channel_id, self._entry(channel_id, wm_id), self.active_ref(channel_id), detail=True)

    def used_by(self, wm_id: str) -> dict[int, int]:
        """revision -> số job (chưa xóa) đang tham chiếu qua `params.watermark_ref`."""
        return self.store.jobs_using_watermark(wm_id) if self.store is not None else {}

    # ------------------------------------------------------------------------------------------ ghi: revision
    def commit_revision(self, channel_id: str, audio_tmp: Path, *, source: str, name: str | None = None, wm_id: str | None = None, meta: dict | None = None,
                        activate: bool = False, request_id: str | None = None) -> dict:
        """Ghi MỘT revision mới từ file audio đã chuẩn bị (`audio_tmp`, sẽ được kiểm lại): audio `*.part` -> đổi tên, json atomic, rồi mới cập nhật registry.
        `wm_id` None = watermark mới. Watermark đang active được chuyển sang revision mới (đã thành công). Lỗi ở bất kỳ bước nào KHÔNG đổi active/registry
        (file dở là `*.part`/json mồ côi, không bao giờ được coi là revision)."""
        probe = probe_audio(audio_tmp, self.audio)
        ext = Path(audio_tmp).suffix.lower()
        with self.lock(channel_id):
            reg = self._registry(channel_id)
            if request_id and reg["requests"].get(request_id) in reg["watermarks"]:
                return self.get(channel_id, reg["requests"][request_id])                       # bấm đúp/gửi lại: trả watermark đã tạo, không tạo thêm
            new = wm_id is None
            if new:
                wm_id = new_id()
                while wm_id in reg["watermarks"] or (self.root(channel_id) / wm_id).exists():
                    wm_id = new_id()
                entry = {"id": wm_id, "name": (name or "Watermark").strip()[:80] or "Watermark", "source": source, "current_revision": 0, "archived": False,
                         "created_at": time.time(), "updated_at": time.time()}
            else:
                entry = reg["watermarks"].get(self._check_id(wm_id))
                if entry is None:
                    raise _err("WATERMARK_NOT_FOUND", "Không có watermark này.")
            d = self.root(channel_id) / wm_id
            existing = [int(m.group(1)) for f in d.glob("rev_*") if (m := re.match(r"rev_(\d{4})\.", f.name))] if d.is_dir() else []
            rev = max([entry["current_revision"], *existing]) + 1
            dest = d / rev_name(rev, ext)
            atomic_write(dest, lambda tmp: shutil.copyfile(audio_tmp, tmp))
            if sha256_file(dest) != probe["sha256"]:
                dest.unlink(missing_ok=True)
                raise _err("WATERMARK_AUDIO_INVALID", "Ghi file audio bị lỗi, hãy thử lại.")
            rmeta = {"schema": 1, "id": wm_id, "revision": rev, "file": dest.name, "sha256": probe["sha256"], "bytes": probe["bytes"], "duration_sec": probe["duration_sec"],
                     "source": source, "created_at": time.time(), **(meta or {})}
            atomic_write_json(d / rev_name(rev, ".json"), rmeta)                               # revision có hiệu lực khi json này tồn tại
            was_active = (self.active_ref(channel_id) or {}).get("id") == wm_id
            entry["current_revision"] = rev
            entry["updated_at"] = time.time()
            if name and not new:
                entry["name"] = name.strip()[:80] or entry["name"]
            reg["watermarks"][wm_id] = entry
            if request_id:
                reg["requests"][request_id] = wm_id
            self._save(channel_id, reg)
            if activate or was_active:
                self._write_active(channel_id, wm_id, rmeta)
            return self.get(channel_id, wm_id)

    def rename(self, channel_id: str, wm_id: str, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise _err("WATERMARK_NAME_EMPTY", "Tên watermark không được để trống.")
        with self.lock(channel_id):
            reg = self._registry(channel_id)
            e = self._entry(channel_id, wm_id)
            e["name"], e["updated_at"] = name[:80], time.time()
            reg["watermarks"][wm_id] = e
            self._save(channel_id, reg)
        return self.get(channel_id, wm_id)

    def restore(self, channel_id: str, wm_id: str) -> dict:
        with self.lock(channel_id):
            reg = self._registry(channel_id)
            e = self._entry(channel_id, wm_id)
            e["archived"], e["updated_at"] = False, time.time()
            reg["watermarks"][wm_id] = e
            self._save(channel_id, reg)
        return self.get(channel_id, wm_id)

    def delete(self, channel_id: str, wm_id: str, unset_active: bool = False) -> dict:
        """Xóa watermark. Đang active ⇒ WATERMARK_IN_USE trừ khi `unset_active` (bỏ khỏi kênh trong cùng thao tác). Có job tham chiếu bất kỳ revision nào ⇒ chỉ LƯU TRỮ
        (ẩn; mọi revision giữ nguyên để job cũ tái lập được); không ai tham chiếu ⇒ xóa vật lý."""
        with self.lock(channel_id):
            if wm_id == LEGACY_ID:
                return self._delete_legacy(channel_id, unset_active)
            reg = self._registry(channel_id)
            entry = self._entry(channel_id, wm_id)
            active = self.active_ref(channel_id)
            if active and active["id"] == wm_id:
                if not unset_active:
                    raise _err("WATERMARK_IN_USE", "Watermark này đang là watermark của kênh.", "Chọn watermark khác hoặc bỏ watermark khỏi kênh trước khi xóa.", active=True)
                self.deactivate(channel_id)
            refs = self.used_by(wm_id)
            if refs:
                entry["archived"], entry["updated_at"] = True, time.time()
                reg["watermarks"][wm_id] = entry
                self._save(channel_id, reg)
                return {"result": "archived", "jobs": sum(refs.values()), "item": self.get(channel_id, wm_id)}
            reg["watermarks"].pop(wm_id, None)
            self._save(channel_id, reg)                                                         # bỏ khỏi registry TRƯỚC khi xóa file: crash giữa chừng chỉ để lại thư mục mồ côi
            shutil.rmtree(self.root(channel_id) / wm_id, ignore_errors=True)
            return {"result": "deleted", "jobs": 0}

    def _delete_legacy(self, channel_id: str, unset_active: bool) -> dict:
        f = self._legacy_file_any(channel_id)
        if f is None:
            raise _err("WATERMARK_NOT_FOUND", "Không có watermark này.")
        if self._legacy_file(channel_id) is not None and self.active_ref(channel_id) is None:
            if not unset_active:
                raise _err("WATERMARK_IN_USE", "Watermark này đang là watermark của kênh.", "Chọn watermark khác hoặc bỏ watermark khỏi kênh trước khi xóa.", active=True)
            self.deactivate(channel_id)
        used = self.store.jobs_using_watermark_path(str(f)) if self.store is not None else 0
        if used:
            return {"result": "archived", "jobs": used, "item": None}                           # job cũ còn trỏ vào file này: giữ lại, chỉ bỏ khỏi kênh
        f.unlink(missing_ok=True)
        return {"result": "deleted", "jobs": 0}

    # ------------------------------------------------------------------------------------------ đọc audio (preview) an toàn
    def audio_path(self, channel_id: str, wm_id: str, revision: int | None = None) -> tuple[Path, str]:
        """Đường dẫn audio của một revision đã quản lý (preview). Chỉ mã `wm_xxxxxxxx` + số revision do backend sinh — không nhận đường dẫn từ client."""
        mime = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac", ".ogg": "audio/ogg", ".aac": "audio/aac"}
        if wm_id == LEGACY_ID:
            f = self._legacy_file_any(channel_id)
            if f is None:
                raise _err("WATERMARK_NOT_FOUND", "Không có watermark này.")
            return f, mime[f.suffix.lower()]
        entry = self._entry(channel_id, wm_id)
        meta = self.revision(channel_id, wm_id, revision or entry["current_revision"])
        p = self.revision_path(channel_id, wm_id, meta)
        return p, mime.get(p.suffix.lower(), "application/octet-stream")

    # ------------------------------------------------------------------------------------------ cho job
    def resolve_active(self, channel: dict) -> dict | None:
        """Watermark của kênh cho JOB MỚI: {path, ref} hoặc None. Revision đã quản lý: kiểm file còn nguyên (sha256) rồi snapshot {id, revision, sha256, source}. File
        kiểu cũ: chỉ băm lúc tạo job (`legacy`). Ref trỏ vào revision đã mất ⇒ WATERMARK_MISSING (không lặng lẽ chạy mà thiếu watermark)."""
        cid = channel["id"]
        ref = channel.get("watermark_ref")
        if ref:
            wm_id, rev = ref["id"], int(ref["revision"])
            try:
                meta = self.revision(cid, wm_id, rev)
            except StageError:
                raise _err("WATERMARK_MISSING", f"Watermark đang dùng của kênh '{cid}' ({wm_id} bản {rev}) không còn trong thư viện.",
                           "Chọn lại watermark ở trang Kênh hoặc bỏ watermark.") from None
            p = self.revision_path(cid, wm_id, meta)
            if sha256_file(p) != meta["sha256"]:
                raise _err("WATERMARK_AUDIO_INVALID", f"File watermark {wm_id} bản {rev} đã bị thay đổi/hỏng.", "Tạo lại bản mới hoặc chọn watermark khác.")
            return {"path": str(p), "ref": {"id": wm_id, "revision": rev, "sha256": meta["sha256"], "source": meta["source"]}}
        wm = channel.get("watermark")
        if wm and Path(wm).is_file():
            return {"path": str(wm), "ref": {"id": None, "revision": None, "sha256": sha256_file(Path(wm)), "source": "legacy"}}
        return None


def _tts_summary(meta: dict | None) -> dict | None:
    t = (meta or {}).get("tts")
    return {k: t.get(k) for k in ("profile", "engine", "voice", "model", "language")} if t else None
