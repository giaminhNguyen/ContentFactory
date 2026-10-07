"""Facade giao diện cho Watermark Library: tạo bằng TTS / tải file lên, sửa (revision mới), tạo lại, chọn làm watermark đang dùng, xóa/lưu trữ, nghe thử.

Nghiệp vụ lưu trữ nằm ở `watermarks.py`; đọc văn bản bằng TTS dùng CHÍNH `TTSManager` của stage TTS (`describe_text`/`synthesize_text`: cùng profile resolver, adapter, planner,
cache chunk, QA, retry) — không có danh sách giọng/adapter/credential riêng cho watermark. Auto = đúng logic chọn của narration (preset kênh → tự chọn theo ngôn ngữ + engine).
"""
from __future__ import annotations

import re
import shutil
import tempfile
import threading
from pathlib import Path

from ..contracts import CancelToken, ErrorClass, StageError
from ..tts import schema as TS
from ..tts.manager import TTSManager
from . import auto as AU
from . import channels as CH
from . import watermarks as WM
from .service_templates import Raw

MAX_TEXT_CHARS = 1000
NAME_MAX = 80


def _err(code: str, message: str, hint: str = "", **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, message, {**detail, **({"hint": hint} if hint else {})}, resource="input")


_BUSY: set[tuple[str, str]] = set()                 # dùng chung giữa các thể hiện Service (mỗi request có thể dựng Service riêng)
_GUARD = threading.Lock()


class WatermarkService:
    def __init__(self, orc) -> None:
        self.orc, self.cfg, self.lib = orc, orc.cfg, orc.watermarks

    # ------------------------------------------------------------------------------------------ đọc
    def overview(self, channel_id: str, include_archived: bool = False) -> dict:
        CH.channel_dir(self.cfg, channel_id)                                                # id kênh hợp lệ
        lib = self.lib.list(channel_id, include_archived)
        return {**lib, "tts": self.tts_options(channel_id)}

    def get(self, channel_id: str, wm_id: str) -> dict:
        return self.lib.get(channel_id, wm_id)

    def tts_options(self, channel_id: str) -> dict:
        """Dữ liệu cho bộ chọn giọng (cùng nguồn với trang Giọng đọc): Auto + các TTS profile hiện có; không có danh sách giọng riêng cho watermark."""
        adapter = self.orc.adapters.get("tts")
        language = self._language(channel_id)
        sel = AU.select_tts_profile(self.cfg, language, getattr(adapter, "engine_id", None))
        profiles = [{"name": n, "engine": p.get("engine"), "status": p.get("status"), "voice": TS.unwrap(p.get("voice")), "model": TS.unwrap(p.get("model"))}
                    for n, p in AU.list_tts_profiles(self.cfg)]
        return {"available": adapter is not None and self.orc.adapters.get("audio") is not None, "language": language, "auto": {"profile": sel[0] if sel else None, "why": sel[1] if sel else None},
                "profiles": profiles, "preset_profile": ((self._channel(channel_id).get("preset") or {}).get("tts_profile"))}

    def audio(self, channel_id: str, wm_id: str, revision: int | None = None) -> Raw:
        path, mime = self.lib.audio_path(channel_id, wm_id, revision)
        return Raw(path.read_bytes(), mime)

    # ------------------------------------------------------------------------------------------ nội bộ
    def _channel(self, channel_id: str) -> dict:
        return CH.load_channel(self.cfg, channel_id)

    def _language(self, channel_id: str) -> str:
        return ((self._channel(channel_id).get("preset") or {}).get("language")) or self.cfg.data["job_defaults"].get("language", "vi")

    @staticmethod
    def _clean_name(name: str | None, required: bool = True) -> str | None:
        name = re.sub(r"\s+", " ", (name or "")).strip()
        if not name:
            if required:
                raise _err("WATERMARK_NAME_EMPTY", "Hãy đặt tên cho watermark.", "Ví dụ: Intro truyện đêm.")
            return None
        return name[:NAME_MAX]

    @staticmethod
    def _clean_text(text: str | None) -> str:
        t = (text or "").strip()
        if not t:
            raise _err("WATERMARK_TEXT_EMPTY", "Hãy nhập nội dung watermark.", "Nội dung này sẽ được đọc thành giọng; hệ thống không tự thêm câu nào.")
        if len(t) > MAX_TEXT_CHARS:
            raise _err("WATERMARK_TEXT_TOO_LONG", f"Nội dung watermark quá dài ({len(t)} ký tự, tối đa {MAX_TEXT_CHARS}).", "Watermark nên chỉ vài câu ngắn.")
        return t

    def _profile(self, channel_id: str, requested: str | None) -> tuple[dict | None, dict]:
        """(profile TTS, quyết định). `requested` = tên profile hoặc 'auto'/None. Auto: preset của kênh → tự chọn theo ngôn ngữ + engine (đúng như narration)."""
        language = self._language(channel_id)
        if requested and requested != "auto":
            return AU.load_tts_profile(self.cfg, requested), {"requested": requested, "profile": requested, "why": "bạn chọn"}
        pre = (self._channel(channel_id).get("preset") or {})
        if pre.get("tts") is not None:
            return pre["tts"], {"requested": "auto", "profile": None, "why": f"preset của kênh '{channel_id}' (inline)"}
        if pre.get("tts_profile"):
            return AU.load_tts_profile(self.cfg, pre["tts_profile"]), {"requested": "auto", "profile": pre["tts_profile"], "why": f"profile ưa thích của kênh '{channel_id}'"}
        if self.cfg.data.get("auto", {}).get("tts_profile_selection", True):
            sel = AU.select_tts_profile(self.cfg, language, getattr(self.orc.adapters.get("tts"), "engine_id", None))
            if sel:
                return AU.load_tts_profile(self.cfg, sel[0]), {"requested": "auto", "profile": sel[0], "why": "tự chọn: " + sel[1]}
        return None, {"requested": "auto", "profile": None, "why": "không có profile: dùng mặc định của engine"}

    def _manager(self) -> TTSManager:
        tts, audio = self.orc.adapters.get("tts"), self.orc.adapters.get("audio")
        if tts is None or audio is None:
            raise _err("TTS_UNAVAILABLE", "Chưa có engine giọng đọc để tạo watermark.", "Mở trang Giọng đọc để kiểm tra engine.")
        return TTSManager(tts, audio, self.orc.adapters.get("planner"))

    @staticmethod
    def _map_tts_error(e: StageError) -> StageError:
        if e.error_class == ErrorClass.CANCELLED:
            return e
        if e.code in ("TTS_PROFILE_NOT_FOUND", "INVALID_TTS_PROFILE", "EMPTY_TEXT", "WATERMARK_AUDIO_INVALID", "WATERMARK_TEXT_EMPTY"):
            return e
        unavailable = e.error_class in (ErrorClass.RESOURCE, ErrorClass.AUTH) or e.resource in ("network", "provider", "quota", "token", "runtime", "credential")
        return _err("TTS_UNAVAILABLE" if unavailable else "TTS_GENERATION_FAILED",
                    ("Engine giọng đọc chưa sẵn sàng: " if unavailable else "Không tạo được giọng đọc cho watermark: ") + (e.message or e.code),
                    "Kiểm tra engine/credential ở trang Giọng đọc rồi thử lại. Watermark đang dùng không bị ảnh hưởng.", cause=e.code)

    def _claim(self, key: tuple[str, str]) -> None:
        with _GUARD:
            if key in _BUSY:
                raise _err("WATERMARK_BUSY", "Watermark này đang được tạo.", "Đợi bản đang tạo xong rồi thử lại.")
            _BUSY.add(key)

    def _release(self, key: tuple[str, str]) -> None:
        with _GUARD:
            _BUSY.discard(key)

    # ------------------------------------------------------------------------------------------ tạo bằng TTS / sửa / tạo lại
    def create_tts(self, channel_id: str, name: str, text: str, selection: str | None = "auto", *, activate: bool = False, request_id: str | None = None) -> dict:
        name, text = self._clean_name(name), self._clean_text(text)
        return self._generate(channel_id, None, name, text, selection or "auto", activate, request_id)

    def update_tts(self, channel_id: str, wm_id: str, *, name: str | None = None, text: str | None = None, selection: str | None = None) -> dict:
        """Sửa watermark TTS: đổi tên (chỉ metadata) và/hoặc văn bản/giọng (revision MỚI; không đổi gì ảnh hưởng âm thanh ⇒ không tạo bản mới, không gọi engine)."""
        entry = self.lib.get(channel_id, wm_id)
        if entry["source"] != "tts":
            raise _err("WATERMARK_NOT_TTS", "Chỉ watermark tạo bằng giọng đọc mới sửa được nội dung.", "Với watermark tải lên, hãy thay file.")
        meta = self.lib.revision(channel_id, wm_id, entry["current_revision"])
        cur = meta.get("tts") or {}
        new_name = self._clean_name(name, required=False)
        new_text = self._clean_text(text) if text is not None else cur.get("text", "")
        sel = selection or cur.get("selection") or "auto"
        return self._generate(channel_id, wm_id, new_name, new_text, sel, False, None)

    def regenerate(self, channel_id: str, wm_id: str) -> dict:
        """“Tạo lại”: cùng văn bản + cùng giọng ⇒ nếu engine/profile không đổi thì dùng lại bản hiện tại (không tốn lượt provider); có đổi (vd profile được cập nhật) ⇒ bản mới."""
        return self.update_tts(channel_id, wm_id)

    def _generate(self, channel_id: str, wm_id: str | None, name: str | None, text: str, selection: str, activate: bool, request_id: str | None) -> dict:
        profile, decision = self._profile(channel_id, selection)
        mgr, language = self._manager(), self._language(channel_id)
        try:
            info = mgr.describe_text(text, profile, language)
        except StageError as e:
            raise self._map_tts_error(e) from None
        if wm_id is not None:
            item = self.lib.get(channel_id, wm_id)
            cur = self.lib.revision(channel_id, wm_id, item["current_revision"])
            if (cur.get("tts") or {}).get("fingerprint") == info["fingerprint"]:            # không đổi gì ảnh hưởng âm thanh: giữ bản hiện tại
                if name and name != item["name"]:
                    item = self.lib.rename(channel_id, wm_id, name)
                return {"result": "unchanged", "item": item}
        key = (channel_id, wm_id or f"new:{request_id or info['fingerprint']}")
        self._claim(key)
        work = Path(tempfile.mkdtemp(prefix="cf-watermark-"))
        try:
            try:
                out = mgr.synthesize_text(text, profile, work, language=language, cache_dir=self.cfg.path("runtime") / "cache" / "tts", cancel=CancelToken())
            except StageError as e:
                raise self._map_tts_error(e) from None
            meta = {"fingerprint": info["fingerprint"],
                    "tts": {"text": info["text"], "source_text": text, "selection": selection, "profile": decision["profile"], "profile_why": decision["why"],
                            "engine": info["engine"], "engine_version": info["engine_version"], "voice": info["voice"], "model": info["model"], "language": info["language"],
                            "profile_version": info["profile_version"], "settings": info["settings"], "fingerprint": info["fingerprint"],
                            "synthesized": out["synthesized"], "cache_hits": out["cache_hits"]}}
            item = self.lib.commit_revision(channel_id, out["path"], source="tts", name=name, wm_id=wm_id, meta=meta, activate=activate, request_id=request_id)
            self.orc.log.emit("watermark_generated", channel=channel_id, watermark=item["id"], revision=item["current_revision"], engine=info["engine"],
                              synthesized=out["synthesized"], cache_hits=out["cache_hits"])
            return {"result": "created" if wm_id is None else "revised", "item": item, "reused_cache": out["synthesized"] == 0}
        finally:
            shutil.rmtree(work, ignore_errors=True)
            self._release(key)

    # ------------------------------------------------------------------------------------------ upload
    def create_upload(self, channel_id: str, name: str, filename: str, data: bytes, *, activate: bool = False, request_id: str | None = None) -> dict:
        return {"result": "created", "item": self._commit_upload(channel_id, None, self._clean_name(name), filename, data, activate, request_id)}

    def replace_upload(self, channel_id: str, wm_id: str, filename: str, data: bytes, name: str | None = None) -> dict:
        item = self.lib.get(channel_id, wm_id)
        if item["source"] == "tts":
            raise _err("WATERMARK_NOT_UPLOAD", "Watermark này tạo bằng giọng đọc: hãy sửa nội dung thay vì thay file.")
        if wm_id == WM.LEGACY_ID:
            raise _err("WATERMARK_LEGACY", "Watermark kiểu cũ không thay file tại chỗ.", "Tải file mới lên như một watermark mới rồi chọn nó.")
        return {"result": "revised", "item": self._commit_upload(channel_id, wm_id, self._clean_name(name, required=False), filename, data, False, None)}

    def _commit_upload(self, channel_id: str, wm_id: str | None, name: str | None, filename: str, data: bytes, activate: bool, request_id: str | None) -> dict:
        safe = re.sub(r"[^\w.\-]", "_", Path(filename or "").name)[:80]
        ext = Path(safe).suffix.lower()
        if not safe or safe.startswith(".") or ext not in WM.AUDIO_EXT:
            raise _err("WATERMARK_AUDIO_INVALID", "Chỉ nhận file audio (WAV, MP3, M4A, FLAC, OGG).", "Chọn lại file watermark.")
        if len(data) > WM.MAX_BYTES:
            raise _err("WATERMARK_AUDIO_INVALID", "File watermark quá lớn.", f"Tối đa {WM.MAX_BYTES // (1024 * 1024)} MB.")
        work = Path(tempfile.mkdtemp(prefix="cf-watermark-"))
        try:
            tmp = work / f"upload{ext}"
            tmp.write_bytes(data)
            return self.lib.commit_revision(channel_id, tmp, source="upload", name=name or Path(safe).stem, wm_id=wm_id, meta={"upload": {"filename": safe}},
                                            activate=activate, request_id=request_id)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ------------------------------------------------------------------------------------------ quản lý
    def rename(self, channel_id: str, wm_id: str, name: str) -> dict:
        return self.lib.rename(channel_id, wm_id, self._clean_name(name))

    def activate(self, channel_id: str, wm_id: str, revision: int | None = None) -> dict:
        return self.lib.activate(channel_id, wm_id, revision)

    def deactivate(self, channel_id: str) -> dict:
        self.lib.deactivate(channel_id)
        return {"active": None}

    def delete(self, channel_id: str, wm_id: str, unset_active: bool = False) -> dict:
        return self.lib.delete(channel_id, wm_id, unset_active)

    def restore(self, channel_id: str, wm_id: str) -> dict:
        return self.lib.restore(channel_id, wm_id)
