"""Facade giao diện cho Image Pool (Phase 8, D-105): liệt kê/quét/tạo/xoá pool ảnh thumbnail, phục vụ ảnh xem thử THEO CHỈ SỐ (không nhận đường dẫn).

Nghiệp vụ chọn ảnh + chốt cho job nằm ở `media/image_pool.py` và Orchestrator; ở đây chỉ là cấu hình pool (config.local.json, top-level `image_pools`)
và dữ liệu cho trang “Nguồn Media → Ảnh thumbnail”.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..contracts import ErrorClass, StageError
from ..media import image_pool as IP
from . import channels as CH
from . import ops
from .service_templates import Raw

CTYPE = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
PREVIEW_COUNT = 12
NAME_RE = re.compile(r"[A-Za-z0-9_\-]{1,40}")


def _err(code: str, msg: str, hint: str = "", **detail) -> StageError:
    return StageError(ErrorClass.POLICY, code, msg, {"hint": hint, **detail}, resource="input")


class ImagePoolService:
    def __init__(self, orc, admin) -> None:
        self.orc, self.cfg, self.admin = orc, orc.cfg, admin

    # ---------------------------------------------------------------------------------------------- đọc
    def used_by(self, name: str) -> list[dict]:
        out = []
        for c in ops.list_channels(self.cfg):
            if not c.get("ok"):
                continue
            th = (CH.load_channel(self.cfg, c["id"]).get("thumbnail") or {})
            if th.get("image_pool") == name:
                out.append({"channel": c["id"], "name": c.get("name") or c["id"], "selection_mode": th.get("selection_mode")})
        return out

    def row(self, name: str, spec: dict) -> dict:
        res = IP.scan(spec.get("folder", ""))
        problems = []
        if not res["exists"]:
            problems.append("Thư mục không tồn tại hoặc không truy cập được.")
        elif not res["valid"]:
            problems.append("Chưa có ảnh hợp lệ nào (jpg/png/webp).")
        mode = spec.get("selection_mode") or IP.DEFAULT_MODE
        return {"name": name, "folder": spec.get("folder", ""), "selection_mode": mode, "exists": res["exists"], "valid": res["valid"], "invalid": res["invalid"],
                "warnings": res["warnings"], "problems": problems, "scanned_at": res["scanned_at"], "used_by": self.used_by(name),
                "state": "problem" if problems else "ready",
                "capacity_note": ("Chỉ 1 ảnh hợp lệ: mọi job dùng chung ảnh này." if res["valid"] == 1 else None)}

    def overview(self) -> dict:
        specs = self.orc.image_pools.specs()
        return {"pools": [self.row(n, s) for n, s in sorted(specs.items())], "modes": [{"id": m, "label": MODE_LABEL[m], "help": MODE_HELP[m]} for m in IP.MODES], "default_mode": IP.DEFAULT_MODE}

    def scan(self, name: str) -> dict:
        """Quét lại + chi tiết: danh sách ảnh xem thử (theo chỉ số) và các file không hợp lệ kèm lý do."""
        spec = self.orc.image_pools.spec(name)
        res = IP.scan(spec.get("folder", ""))
        valid = [r for r in res["files"] if r["ok"]]
        return {**self.row(name, spec), "images": [{"index": i, "rel": r["rel"], "width": r["width"], "height": r["height"], "format": r["format"], "size": r["size"]}
                                                   for i, r in enumerate(valid[:PREVIEW_COUNT])],
                "invalid_files": [{"rel": r["rel"], "problem": r["problem"]} for r in res["files"] if not r["ok"]][:50]}

    def image(self, name: str, index: int) -> Raw:
        spec = self.orc.image_pools.spec(name)
        res = IP.scan(spec.get("folder", ""))
        valid = [r for r in res["files"] if r["ok"]]
        if not 0 <= index < len(valid):
            raise _err("IMAGE_NOT_FOUND", "Không có ảnh này trong pool.", "Quét lại pool rồi mở lại.")
        row = valid[index]
        p = Path(spec["folder"]) / row["rel"]
        return Raw(p.read_bytes(), CTYPE[row["format"]])

    # ---------------------------------------------------------------------------------------------- ghi
    def upsert(self, name: str, folder: str, selection_mode: str | None) -> dict:
        if not NAME_RE.fullmatch(name or ""):
            raise _err("INVALID_POOL_NAME", "Tên pool chỉ gồm chữ không dấu, số, _ và -.", "Ví dụ: anime_female")
        mode = selection_mode or IP.DEFAULT_MODE
        if mode not in IP.MODES:
            raise _err("BAD_SELECTION_MODE", "Chế độ chọn ảnh không hợp lệ.", f"Dùng một trong: {', '.join(IP.MODES)}.")
        d = Path(folder or "").expanduser()
        if not d.is_dir():
            raise _err("INVALID_POOL_DIR", "Thư mục ảnh không tồn tại.", "Chọn thư mục chứa ảnh jpg/png/webp.", path=str(d))
        spec = {"folder": str(d.resolve()), "selection_mode": mode}
        local = self.admin._read_local()
        pools = local.setdefault("image_pools", {})
        pools[name] = spec
        self.admin._write_local(local)
        self.cfg.data.setdefault("image_pools", {})[name] = spec
        return {"saved": True, **self.row(name, spec)}

    def delete(self, name: str) -> dict:
        if name not in self.orc.image_pools.specs():
            raise _err("IMAGE_POOL_NOT_FOUND", f"Không có pool ảnh '{name}'.")
        used = self.used_by(name)
        if used:
            names = ", ".join(u["name"] for u in used)
            raise _err("POOL_IN_USE", f"Không xoá được: kênh {names} đang dùng pool này.", "Mở trang Kênh, bỏ chọn pool (hoặc chọn pool khác) rồi xoá.", used_by=used)
        local = self.admin._read_local()
        if name in (local.get("image_pools") or {}):
            local["image_pools"].pop(name)
            self.admin._write_local(local)
        elif name in (self.cfg.data.get("image_pools") or {}):
            raise _err("POOL_IN_BASE_CONFIG", "Pool này khai báo trong config/config.json (được commit), không xoá từ giao diện.", "Sửa file cấu hình nếu muốn bỏ.")
        self.cfg.data["image_pools"].pop(name, None)
        self.orc.store.image_pool_forget(name)
        return {"deleted": True}


MODE_LABEL = {"shuffle": "Xáo trộn (không lặp)", "random": "Ngẫu nhiên", "sequential": "Tuần tự"}
MODE_HELP = {"shuffle": "Mỗi ảnh được dùng một lần rồi mới xáo lại: các job liên tiếp không trùng ảnh khi pool đủ lớn.",
             "random": "Chọn ngẫu nhiên, có thể lặp lại.",
             "sequential": "Theo thứ tự tên file, lần lượt từng ảnh rồi quay vòng."}
