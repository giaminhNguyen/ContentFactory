"""Image Pool (Agent Plan Phase 8, D-105): thư mục ảnh làm NGUỒN ẢNH cho thumbnail. Template vẫn là chủ của bố cục; pool chỉ cấp ảnh.

Ba phần tách bạch:
  * quét/kiểm tra (`scan`, `sniff`)     — đọc phần đầu file để xác nhận đúng JPEG/PNG/WebP (không tin đuôi file), đo kích thước, không theo symlink;
  * chọn ảnh (`pick`)                   — hàm thuần, tất định khi cho `rng` cố định: shuffle (túi xáo trộn, không lặp tới khi hết rồi xáo lại), random, sequential;
  * chốt cho job (`ImagePools.assign`)  — rút một ảnh từ trạng thái túi LƯU BỀN (SQLite, nguyên tử giữa các job/batch) rồi SAO CHÉP vào workspace của job
    + sha256. Retry/restart/chạy lại không bao giờ chọn lại; thư mục nguồn đổi về sau không làm đổi thumbnail của job.
"""
from __future__ import annotations

import bisect
import hashlib
import os
import random
import struct
import tempfile
import time
from pathlib import Path

from ..contracts import ErrorClass, StageError

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MODES = ("shuffle", "random", "sequential")
DEFAULT_MODE = "shuffle"
MAX_BYTES = 40 * 1024 * 1024
MIN_SIDE = 64                 # nhỏ hơn: không dùng được làm ảnh thumbnail
SOFT_SIDE = 600               # nhỏ hơn: dùng được nhưng cảnh báo (bị phóng to, mờ)
MAX_DEPTH = 2                 # thư mục gốc + 2 cấp thư mục con
MAX_FILES = 5000
EXT_OF = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}


def _err(code: str, msg: str, hint: str = "", **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, msg, {"hint": hint, **detail}, resource="input")


# ------------------------------------------------------------------------------------------------ kiểm tra file
def sniff(path: Path) -> dict:
    """{'format','width','height'} theo NỘI DUNG file; ValueError(lý do) nếu không phải ảnh JPEG/PNG/WebP đọc được."""
    size = path.stat().st_size
    if size < 32:
        raise ValueError("file quá nhỏ, không phải ảnh")
    if size > MAX_BYTES:
        raise ValueError(f"file quá lớn ({size // (1024 * 1024)} MB > {MAX_BYTES // (1024 * 1024)} MB)")
    with open(path, "rb") as f:
        head = f.read(32)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            w, h = struct.unpack(">II", head[16:24])
            fmt = "png"
        elif head[:3] == b"\xff\xd8\xff":
            fmt, (w, h) = "jpeg", _jpeg_size(f)
        elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            fmt, (w, h) = "webp", _webp_size(head)
        else:
            raise ValueError("nội dung không phải JPEG/PNG/WebP (có thể đã đổi đuôi file)")
    if not w or not h:
        raise ValueError("không đọc được kích thước ảnh (file hỏng?)")
    if min(w, h) < MIN_SIDE:
        raise ValueError(f"ảnh quá nhỏ ({w}×{h})")
    return {"format": fmt, "width": int(w), "height": int(h)}


def _jpeg_size(f) -> tuple[int, int]:
    f.seek(2)
    while True:
        b = f.read(1)
        if not b:
            raise ValueError("JPEG bị cắt cụt")
        if b != b"\xff":
            continue
        m = f.read(1)
        while m == b"\xff":
            m = f.read(1)
        if not m:
            raise ValueError("JPEG bị cắt cụt")
        if m[0] in (0xD8, 0x01) or 0xD0 <= m[0] <= 0xD7:
            continue
        seg = f.read(2)
        if len(seg) < 2:
            raise ValueError("JPEG bị cắt cụt")
        n = struct.unpack(">H", seg)[0]
        if m[0] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            d = f.read(5)
            if len(d) < 5:
                raise ValueError("JPEG bị cắt cụt")
            h, w = struct.unpack(">HH", d[1:5])
            return w, h
        f.seek(n - 2, 1)


def _webp_size(head: bytes) -> tuple[int, int]:
    kind = head[12:16]
    if kind == b"VP8X":
        return 1 + int.from_bytes(head[24:27], "little"), 1 + int.from_bytes(head[27:30], "little")
    if kind == b"VP8L":
        bits = int.from_bytes(head[21:25], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    if kind == b"VP8 ":
        return struct.unpack("<HH", head[26:30])[0] & 0x3FFF, struct.unpack("<HH", head[26:30])[1] & 0x3FFF
    raise ValueError("WebP không đọc được")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------ quét thư mục
def scan(folder: str | Path) -> dict:
    """Quét thư mục ảnh: chỉ file thường trong thư mục (không theo symlink, không file/thư mục ẩn, tối đa MAX_DEPTH cấp). Không đọc cả file ảnh."""
    root = Path(folder)
    out = {"folder": str(root), "exists": root.is_dir(), "files": [], "valid": 0, "invalid": 0, "warnings": [], "scanned_at": time.time()}
    if not root.is_dir():
        out["warnings"].append("Thư mục không tồn tại hoặc không truy cập được (ổ đĩa/mạng đang tắt?).")
        return out
    real_root = root.resolve()
    skipped_links = skipped_other = small = 0
    for dirpath, dirs, names in os.walk(root, followlinks=False):
        depth = len(Path(dirpath).relative_to(root).parts)
        keep = []
        for d in sorted(dirs):
            if os.path.islink(os.path.join(dirpath, d)):
                skipped_links += 1
            elif not d.startswith(".") and depth < MAX_DEPTH:
                keep.append(d)
        dirs[:] = keep
        for n in sorted(names):
            p = Path(dirpath) / n
            if n.startswith("."):
                continue
            if p.suffix.lower() not in IMAGE_EXT:
                skipped_other += 1
                continue
            if os.path.islink(p):
                skipped_links += 1
                continue
            if len(out["files"]) >= MAX_FILES:
                out["warnings"].append(f"Chỉ quét {MAX_FILES} ảnh đầu tiên.")
                break
            rel = p.relative_to(root).as_posix()
            row = {"rel": rel}
            try:
                if real_root not in p.resolve().parents:
                    raise ValueError("nằm ngoài thư mục pool")
                st = p.stat()
                info = sniff(p)
                row.update(ok=True, size=st.st_size, mtime_ns=st.st_mtime_ns, **info)
                if min(info["width"], info["height"]) < SOFT_SIDE:
                    small += 1
                out["valid"] += 1
            except (ValueError, OSError) as e:
                row.update(ok=False, problem=str(e))
                out["invalid"] += 1
            out["files"].append(row)
    out["files"].sort(key=lambda r: r["rel"])
    if skipped_links:
        out["warnings"].append(f"Bỏ qua {skipped_links} liên kết (symlink) để không đọc ra ngoài thư mục pool.")
    if small:
        out["warnings"].append(f"{small} ảnh nhỏ hơn {SOFT_SIDE}px (thumbnail sẽ bị phóng to, có thể mờ).")
    if out["invalid"]:
        out["warnings"].append(f"{out['invalid']} file có đuôi ảnh nhưng không hợp lệ (hỏng/sai định dạng/quá lớn).")
    if not out["valid"]:
        out["warnings"].append("Chưa có ảnh hợp lệ nào (jpg/png/webp).")
    out["other_files"] = skipped_other
    return out


def valid_rels(scanned: dict) -> list[str]:
    return sorted(r["rel"] for r in scanned["files"] if r["ok"])


# ------------------------------------------------------------------------------------------------ chọn ảnh (thuần)
def pick(state: dict | None, rels: list[str], mode: str, rng: random.Random, avoid=()) -> tuple[str, dict]:
    """Chọn một ảnh từ `rels` theo `mode` và trạng thái túi `state` (None = chưa có). Trả (rel, trạng_thái_mới). Không I/O, tất định khi `rng` cố định.
    `avoid`: ảnh nên tránh (ví dụ ảnh hiện tại khi đổi ảnh) — chỉ bị bỏ qua nếu còn ảnh khác để chọn."""
    if mode not in MODES:
        raise _err("BAD_SELECTION_MODE", f"Chế độ chọn ảnh '{mode}' không hợp lệ.", f"Dùng một trong: {', '.join(MODES)}.")
    rels = sorted(set(rels))
    if not rels:
        raise _err("IMAGE_POOL_EMPTY", "Pool không có ảnh hợp lệ để chọn.", "Thêm ảnh jpg/png/webp vào thư mục rồi quét lại.")
    avoid = set(avoid) if len(rels) > 1 else set()
    st = dict(state or {})
    last = st.get("last")
    if mode == "random":
        rel = rng.choice([r for r in rels if r not in avoid] or rels)
        return rel, {"mode": mode, "last": rel}
    if mode == "sequential":
        i = bisect.bisect_right(rels, last) if last is not None else 0
        for _ in range(len(rels)):
            if i >= len(rels):
                i = 0
            if rels[i] not in avoid:
                break
            i += 1
        return rels[i], {"mode": mode, "last": rels[i]}
    have = set(rels)
    drawn = [r for r in st.get("drawn", []) if r in have]
    remaining = [r for r in st.get("remaining", []) if r in have]
    for r in rels:                                                                  # ảnh mới thêm vào thư mục giữa chừng: chen vào phần còn lại
        if r not in drawn and r not in remaining:
            remaining.insert(rng.randint(0, len(remaining)), r)
    if not remaining:                                                               # hết một vòng: xáo lại, không để ảnh vừa dùng đứng đầu vòng mới
        drawn = []
        remaining = list(rels)
        rng.shuffle(remaining)
        if len(remaining) > 1 and remaining[0] in ({last} | avoid):
            j = next((k for k, r in enumerate(remaining) if r != last and r not in avoid), 0)
            remaining[0], remaining[j] = remaining[j], remaining[0]
    idx = next((k for k, r in enumerate(remaining) if r not in avoid), 0)
    rel = remaining.pop(idx)
    drawn.append(rel)
    return rel, {"mode": mode, "last": rel, "drawn": drawn, "remaining": remaining}


# ------------------------------------------------------------------------------------------------ chốt cho job
class ImagePools:
    """Cấu hình pool (top-level `image_pools`, KHÔNG nằm trong snapshot ngữ nghĩa nên đổi pool không đụng job cũ) + chọn ảnh + sao chép vào workspace."""

    def __init__(self, cfg, store, rng: random.Random | None = None) -> None:
        self.cfg, self.store = cfg, store
        self.rng = rng or random.SystemRandom()

    def specs(self) -> dict:
        return {k: v for k, v in (self.cfg.data.get("image_pools") or {}).items() if isinstance(v, dict)}

    def spec(self, name: str) -> dict:
        s = self.specs().get(name)
        if not s:
            raise _err("IMAGE_POOL_NOT_FOUND", f"Không có pool ảnh '{name}'.", "Tạo pool ở Nguồn Media → Ảnh thumbnail, hoặc bỏ chọn pool trong cấu hình kênh.")
        return s

    def scan(self, name: str) -> dict:
        s = self.spec(name)
        return scan(s.get("folder", ""))

    def usable(self, name: str) -> list[str]:
        res = self.scan(name)
        rels = valid_rels(res)
        if not rels:
            why = "Thư mục không truy cập được." if not res["exists"] else "Chưa có ảnh hợp lệ nào."
            raise _err("IMAGE_POOL_EMPTY", f"Pool ảnh '{name}' không dùng được: {why}", "Kiểm tra thư mục (ổ đĩa/mạng) và thêm ảnh jpg/png/webp, hoặc đổi pool trong cấu hình kênh.", pool=name)
        return rels

    def draw(self, name: str, mode: str | None = None, avoid=()) -> str:
        """Rút một ảnh từ trạng thái túi bền (nguyên tử giữa các job/batch chạy đồng thời). Trả đường dẫn tương đối trong pool."""
        mode = mode or self.spec(name).get("selection_mode") or DEFAULT_MODE
        rels = self.usable(name)
        return self.store.image_pool_update(name, lambda st: pick(st, rels, mode, self.rng, avoid))

    def assign(self, name: str, job_dir: Path, mode: str | None = None, avoid_sha: str | None = None, rerolls: int = 0) -> dict:
        """Chọn + sao chép ảnh vào `job_dir/inputs/thumbnail/` (nguyên tử) và trả snapshot lưu vào params của job. `avoid_sha`: không chọn lại ảnh này (đổi ảnh)."""
        spec = self.spec(name)
        mode = mode or spec.get("selection_mode") or DEFAULT_MODE
        root = Path(spec["folder"])
        avoid: set[str] = set()
        for _ in range(3):                                                           # ảnh vừa chọn có thể vừa bị xoá/hỏng: thử lại với ảnh khác
            rel = self.draw(name, mode, avoid)
            try:
                snap = materialize(root, rel, Path(job_dir) / "inputs" / "thumbnail")
            except (ValueError, OSError):
                avoid.add(rel)
                continue
            if avoid_sha and snap["sha256"] == avoid_sha and len(self.usable(name)) > 1:
                avoid.add(rel)
                continue
            return {"pool": name, "selection_mode": mode, "source_relpath": rel, "rerolls": rerolls, **snap}
        raise _err("IMAGE_POOL_EMPTY", f"Không chốt được ảnh hợp lệ từ pool '{name}'.", "Quét lại pool và kiểm tra các ảnh.", pool=name)


def materialize(root: Path, rel: str, dest_dir: Path) -> dict:
    """Sao chép ảnh `rel` của pool vào `dest_dir` thành `thumbnail_<sha12>.<ext>` (nguyên tử). Kiểm tra lại trên BẢN SAO: nội dung, kích thước, nằm trong pool."""
    p = (Path(root) / rel)
    real_root = Path(root).resolve()
    if os.path.islink(p) or real_root not in p.resolve().parents:
        raise ValueError("ảnh nằm ngoài thư mục pool hoặc là liên kết")
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".thumb_", suffix=".part", dir=dest_dir)
    try:
        with os.fdopen(fd, "wb") as out, open(p, "rb") as src:
            h = hashlib.sha256()
            for chunk in iter(lambda: src.read(1 << 20), b""):
                h.update(chunk)
                out.write(chunk)
        info = sniff(Path(tmp))
        sha = h.hexdigest()
        final = dest_dir / f"thumbnail_{sha[:12]}{EXT_OF[info['format']]}"
        os.replace(tmp, final)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {"sha256": sha, "file": final.relative_to(dest_dir.parent.parent).as_posix(), "size": final.stat().st_size, **info}


def resolve_source(job_dir: Path, snap: dict) -> Path:
    """Đường dẫn ảnh thumbnail đã chốt của job; kiểm sha256. Mất/hỏng ⇒ lỗi RÕ RÀNG, không tự chọn ảnh khác (kết quả phải tái lập)."""
    p = Path(job_dir) / snap["file"]
    if not p.is_file():
        raise _err("THUMBNAIL_SOURCE_MISSING", "Ảnh thumbnail đã chốt cho job không còn trong workspace.", "Bấm “Đổi ảnh thumbnail” để chọn ảnh mới từ pool.")
    if sha256_file(p) != snap["sha256"]:
        raise _err("THUMBNAIL_SOURCE_CHANGED", "Ảnh thumbnail đã chốt cho job bị thay đổi sau khi chốt.", "Bấm “Đổi ảnh thumbnail” để chọn lại ảnh từ pool.")
    return p
