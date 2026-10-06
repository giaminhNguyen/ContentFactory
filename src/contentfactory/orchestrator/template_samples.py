"""Nguồn mẫu cho xem trước / render thử của Template Studio (Phase 7, D-104).

Giao diện chỉ gửi một MÔ TẢ mẫu ({id, image, channel}); đường dẫn ảnh thật được dựng ở đây từ cấu hình và không bao giờ nhận từ client
(trước đây `sample` đi thẳng vào ContentFlow). Hai thứ khác nhau: nội dung chữ (3 mẫu thực tế, tên kênh lấy từ kênh thật nếu chọn) và ảnh nền
(ảnh chân dung có sẵn của ContentFlow hoặc khung hình trích từ video trong pool; pool ảnh thumbnail của Phase 8 sẽ cắm vào cùng chỗ `images()`).
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from ..contracts import ErrorClass, StageError
from . import channels as CH

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
FRAMES_PER_POOL = 3
SAMPLES = (
    {"id": "s1", "label": "Mẫu 1 · tiêu đề dài", "title": "Cô gái trở về năm 1998 và phát hiện bí mật của cả dòng họ", "channel": "Truyện Đêm Khuya", "sequence": 27},
    {"id": "s2", "label": "Mẫu 2 · tiêu đề vừa", "title": "Đêm mưa đó, anh ấy đã không quay lại", "channel": "Truyện Đêm Khuya", "sequence": 3},
    {"id": "s3", "label": "Mẫu 3 · tiêu đề ngắn", "title": "Nợ máu", "channel": "Truyện Đêm Khuya", "sequence": 112},
)


def _err(code: str, msg: str, hint: str = "") -> StageError:
    return StageError(ErrorClass.POLICY, code, msg, {"hint": hint}, resource="input")


class TemplateSamples:
    def __init__(self, cfg, cache_root) -> None:
        self.cfg, self._cache_root = cfg, cache_root                 # cache_root: callable -> Path (ContentFlow cache, chỉ biết khi cần)

    # ---- ffmpeg / pool ------------------------------------------------------------------------------------------
    def ffmpeg(self) -> str | None:
        f = (self.cfg.data.get("tools") or {}).get("ffmpeg") or "ffmpeg"
        return shutil.which(f) or (f if Path(f).is_file() else None)

    def pool_videos(self, name: str) -> list[Path]:
        spec = ((self.cfg.data.get("render") or {}).get("pools") or {}).get(name) or {}
        raw = Path(str(spec.get("raw_dir", "")))
        return sorted(p for p in raw.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT) if raw.is_dir() else []

    # ---- danh sách cho giao diện --------------------------------------------------------------------------------
    def describe(self, type: str) -> dict:
        images = [{"id": "builtin", "label": "Ảnh mẫu có sẵn"}]
        pools = []
        if type == "thumbnail":
            for name in ((self.cfg.data.get("render") or {}).get("pools") or {}):
                n = len(self.pool_videos(name))
                if n:
                    pools.append(name)
                    images += [{"id": f"frame:{name}:{i}", "label": f"Khung hình pool “{name}” #{i + 1}"} for i in range(min(FRAMES_PER_POOL, n))]
        ff = self.ffmpeg()
        te = {"enabled": True, "reason": None} if type == "thumbnail" or ff else {"enabled": False, "reason": "Render thử video cần ffmpeg nhưng chưa tìm thấy. Cài ffmpeg hoặc đặt tools.ffmpeg (Doctor chỉ cách); xem trước vẫn dùng được."}
        try:
            from . import ops
            channels = [{"id": c["id"], "name": c.get("name") or c["id"]} for c in ops.list_channels(self.cfg) if c.get("ok")]
        except Exception:                                                              # noqa: BLE001 — danh sách kênh chỉ để chọn mẫu, không được làm hỏng xem trước
            channels = []
        return {"samples": [{k: s[k] for k in ("id", "label", "title", "sequence")} for s in SAMPLES], "images": images, "channels": channels, "pools": pools,
                "test_render": te,
                "note": ("Video mẫu của template video là hình giả lập có chữ “SOURCE VIDEO” (ContentFlow không nhận video thật cho xem trước): vùng hiển thị, khung/overlay, chữ và vùng an toàn là thật."
                         if type == "video" else None)}

    # ---- dựng mẫu cho ContentFlow ---------------------------------------------------------------------------------
    def resolve(self, spec: dict | None, type: str) -> dict:
        spec = spec if isinstance(spec, dict) else {}
        base = next((s for s in SAMPLES if s["id"] == spec.get("id")), SAMPLES[0])
        out = {"channel": base["channel"], "title": base["title"]}
        cid = spec.get("channel")
        if cid:
            try:
                if not CH.channel_dir(self.cfg, str(cid)).is_dir():
                    raise _err("CHANNEL_NOT_FOUND", "")
                out["channel"] = CH.load_channel(self.cfg, str(cid))["name"]
            except StageError:
                raise _err("CHANNEL_NOT_FOUND", f"Không có kênh '{cid}' để lấy tên mẫu.", "Chọn “Tên mẫu” hoặc một kênh có thật.") from None
        img = spec.get("image") or "builtin"
        if type == "thumbnail" and img != "builtin":
            out["image"] = str(self._frame(img))
        return out

    def _frame(self, ref: str) -> Path:
        parts = str(ref).split(":")
        if len(parts) != 3 or parts[0] != "frame" or not parts[2].isdigit() or int(parts[2]) >= FRAMES_PER_POOL:
            raise _err("BAD_SAMPLE", "Nguồn ảnh mẫu không hợp lệ.", "Chọn lại ảnh mẫu trong danh sách.")
        vids = self.pool_videos(parts[1])
        if not vids:
            raise _err("NO_SAMPLE_MEDIA", f"Pool “{parts[1]}” không có video để lấy khung hình mẫu.", "Thêm video vào pool ở trang Nguồn Media, hoặc chọn “Ảnh mẫu có sẵn”.")
        src = vids[int(parts[2]) % len(vids)]
        ff = self.ffmpeg()
        if not ff:
            raise _err("NO_SAMPLE_MEDIA", "Chưa có ffmpeg nên không trích được khung hình mẫu.", "Chọn “Ảnh mẫu có sẵn”, hoặc cài ffmpeg (Doctor chỉ cách).")
        st = src.stat()
        key = hashlib.sha1(f"{src}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
        out = Path(self._cache_root()) / "cf_sample_frames" / f"{key}.jpg"
        if out.is_file():
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{key}.part.jpg")
        for ss in ("2", "0"):                                                           # video ngắn hơn 2 giây thì lấy khung đầu
            try:
                r = subprocess.run([ff, "-hide_banner", "-v", "error", "-y", "-ss", ss, "-i", str(src), "-frames:v", "1", "-vf", "scale='min(1600,iw)':-2", "-q:v", "3", str(tmp)],
                                   capture_output=True, text=True, timeout=30)
            except (subprocess.TimeoutExpired, OSError):
                continue
            if r.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
                tmp.replace(out)
                return out
        tmp.unlink(missing_ok=True)
        raise _err("NO_SAMPLE_MEDIA", f"Không trích được khung hình từ {src.name}.", "Thử một ảnh mẫu khác hoặc “Ảnh mẫu có sẵn”.")
