"""OutputPublisher thật: dựng gói output cho NGƯỜI DÙNG (HANDOFF §16-17). `workspace/` thuộc hệ thống, `output/` thuộc người dùng.

  output/<ngày>_<slug>/            README.txt  project.json  story.txt
                                   youtube/{video.mp4, thumbnail.jpg, title.txt, description.txt}
                                   tiktok/part_01.mp4 part_02.mp4 …   (đúng thứ tự part; không temp/cache/chunk)

Bất biến:
- Chỉ COPY từ workspace và chỉ copy có kiểm sha256; pipeline KHÔNG BAO GIỜ đọc lại output/ (người dùng chép/di chuyển/xóa thoải mái, pipeline không hỏng).
- Dựng trong thư mục tạm cùng volume rồi rename: không bao giờ thấy gói nửa vời.
- File final KHÔNG bị pipeline sửa âm thầm. Chạy lại (retry/resume/rerender) cho cùng job:
    · nội dung y hệt gói đã có  ->  KHÔNG đụng tới gói đó (reused);
    · nội dung khác (rerender ra video khác, đổi tiêu đề...) ->  gói MỚI `<tên>-v2`, `-v3`… kèm `version`/`supersedes` trong project.json, gói cũ giữ nguyên.
- `project.json` ghi cho từng file: đường dẫn trong gói, sha256, kích thước, và artifact nguồn trong workspace (đường dẫn + sha256).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from ..contracts import ErrorClass, OutputPackage, OutputRequest, StageContext, StageError
from ..fsutil import atomic_write_json, atomic_write_text, sha256_file
from .slug import slugify

SCHEMA = 1


def _copy_verified(src: Path, dst: Path, expect_sha: str | None) -> tuple[str, int]:
    """Copy rồi kiểm sha256 của bản copy (đúng bằng sha của artifact đã niêm phong). Trả (sha256, bytes)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    got = sha256_file(dst)
    if expect_sha and got != expect_sha:
        raise StageError(ErrorClass.TRANSIENT, "COPY_VERIFY_FAILED", f"{dst.name}: sha256 bản copy khác artifact trong workspace (file nguồn đổi hoặc copy lỗi)")
    return got, dst.stat().st_size


def _text_entry(path: Path, text: str) -> dict:
    atomic_write_text(path, text)
    b = text.encode("utf-8")
    return {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}


class BuiltinOutputPublisher:
    def __init__(self, cfg: dict | None = None) -> None:
        self.template = (cfg or {}).get("name_template", "{date}_{slug}")

    # ------------------------------------------------------------------------------------------ tìm gói của job
    @staticmethod
    def packages(root: Path, job_id: str) -> list[dict]:
        """Các gói đã có của job (theo project.json.job_id, kể cả khi người dùng đã đổi tên thư mục), version tăng dần."""
        out = []
        for d in sorted(root.iterdir()) if root.exists() else []:
            f = d / "project.json"
            if d.is_dir() and not d.name.startswith(".") and f.is_file():
                try:
                    pj = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if pj.get("job_id") == job_id:
                    out.append({"dir": d, "version": int(pj.get("version", 1)), "sig": pj.get("content_sig"), "project": pj})
        return sorted(out, key=lambda p: p["version"])

    def _free_dir(self, root: Path, base: str) -> Path:
        cand, n = root / base, 1
        while cand.exists():
            n += 1
            cand = root / f"{base}-{n}"
        return cand

    # ------------------------------------------------------------------------------------------ dựng gói
    def publish(self, req: OutputRequest, ctx: StageContext) -> OutputPackage:
        root = Path(req["output_root"])
        root.mkdir(parents=True, exist_ok=True)
        job_id, proj = req["job_id"], req["project"]
        parts = sorted(req["tiktok_parts"], key=lambda p: p["index"])
        width = max(2, len(str(len(parts))))
        thumb_ext = req["youtube_thumbnail"]["path"].suffix.lower() or ".jpg"
        plan = [("story.txt", "story", req["story"]), ("youtube/video.mp4", "youtube_video", req["youtube_video"]),
                (f"youtube/thumbnail{thumb_ext}", "youtube_thumbnail", req["youtube_thumbnail"])]
        plan += [(f"tiktok/part_{p['index']:0{width}d}.mp4", "tiktok_part", p) for p in parts]
        texts = {"youtube/title.txt": req["youtube_title"] + "\n", "youtube/description.txt": req["description"] + "\n"}
        sig = hashlib.sha256(json.dumps({"files": [(rel, e["sha256"]) for rel, _, e in plan], "texts": texts,
                                         "project": [proj["title"], proj["title_source"], proj["channel_id"], proj.get("sequence")]},
                                        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        existing = self.packages(root, job_id)
        latest = existing[-1] if existing else None
        if latest and latest["sig"] == sig:                      # đúng nội dung này đã có: không đụng tới gói của người dùng
            ctx.log("output_package_reused", dir=str(latest["dir"]), version=latest["version"])
            return {"project_dir": str(latest["dir"]), "version": latest["version"], "reused": True,
                    "supersedes": latest["project"].get("supersedes"), "files": [f["path"] for f in latest["project"].get("files", [])]}
        version = latest["version"] + 1 if latest else 1
        if latest:
            base = existing[0]["dir"].name
            final = self._free_dir(root, f"{base}-v{version}")
        else:
            base = self.template.format(date=datetime.now().strftime("%Y%m%d"), slug=slugify(proj["title"]))
            final = self._free_dir(root, base)
        supersedes = latest["dir"].name if latest else None
        tmp = root / f".tmp-{job_id}"
        if tmp.exists():
            shutil.rmtree(tmp)
        files: list[dict] = []
        try:
            for rel, role, e in plan:
                sha, size = _copy_verified(Path(e["path"]), tmp / rel, e.get("sha256"))
                row = {"path": rel, "role": role, "sha256": sha, "bytes": size, "source": {"workspace_path": e["source"], "sha256": e.get("sha256")}}
                if role == "tiktok_part":
                    row["index"] = e["index"]
                    if e.get("duration_sec") is not None:
                        row["duration_sec"] = e["duration_sec"]
                files.append(row)
            for rel, text in texts.items():
                files.append({"path": rel, "role": rel.split("/")[1].split(".")[0], **_text_entry(tmp / rel, text)})
            atomic_write_text(tmp / "README.txt", self._readme(req, version, supersedes, files, parts, width, final.name))
            files.append({"path": "README.txt", "role": "readme"})
            atomic_write_json(tmp / "project.json", self._project_json(req, version, supersedes, sig, files, parts))
            os.replace(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return {"project_dir": str(final), "version": version, "reused": False, "supersedes": supersedes,
                "files": sorted([f["path"] for f in files] + ["project.json"])}

    # ------------------------------------------------------------------------------------------ project.json / README
    @staticmethod
    def _project_json(req: OutputRequest, version: int, supersedes: str | None, sig: str, files: list[dict], parts: list[dict]) -> dict:
        by = {f["path"]: f for f in files}
        yt = [f for f in files if f["role"] in ("youtube_video", "youtube_thumbnail", "title", "description")]
        return {"schema": SCHEMA, "job_id": req["job_id"], "version": version, "supersedes": supersedes,
                "created": datetime.now().isoformat(timespec="seconds"), "content_sig": sig,
                "project": {k: req["project"].get(k) for k in ("id", "title", "title_source", "language", "channel_id", "channel_name", "sequence")},
                "story": {"file": "story.txt", "sha256": by["story.txt"]["sha256"], "bytes": by["story.txt"]["bytes"]},
                "youtube": {"video": next(f["path"] for f in yt if f["role"] == "youtube_video"),
                            "thumbnail": next(f["path"] for f in yt if f["role"] == "youtube_thumbnail"),
                            "title_file": "youtube/title.txt", "description_file": "youtube/description.txt", "title": req["youtube_title"]},
                "tiktok": {"count": len(parts), "parts": [{"index": f["index"], "file": f["path"], "duration_sec": f.get("duration_sec")}
                                                          for f in files if f["role"] == "tiktok_part"]},
                "files": files, "warnings": req.get("warnings", []),
                "note": "Thư mục này thuộc về bạn. Pipeline không đọc lại và không sửa nó; render lại sẽ tạo phiên bản mới bên cạnh."}

    @staticmethod
    def _readme(req: OutputRequest, version: int, supersedes: str | None, files: list[dict], parts: list[dict], width: int, dirname: str) -> str:
        p = req["project"]
        by = {f["path"]: f for f in files}

        def mb(rel: str) -> str:
            n = by[rel]["bytes"]
            return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.1f} KB"
        lines = [p["title"], "=" * max(8, min(len(p["title"]), 70)), "",
                 f"Kênh      : {p.get('channel_name') or p.get('channel_id')}", f"Số tập    : Full Audio {p['sequence']}" if p.get("sequence") else None,
                 f"Ngôn ngữ  : {p.get('language')}", f"Phiên bản : {version}" + (f"  (thay thế bản trước: {supersedes}; bản cũ được giữ nguyên)" if supersedes else ""),
                 "", "NỘI DUNG THƯ MỤC", "----------------",
                 "story.txt                : truyện đầy đủ, không đánh số chương",
                 "youtube/video.mp4        : video YouTube hoàn chỉnh (" + mb("youtube/video.mp4") + ")",
                 "youtube/thumbnail.*      : ảnh thumbnail",
                 "youtube/title.txt        : tiêu đề dùng khi đăng", "youtube/description.txt  : mô tả dùng khi đăng",
                 f"tiktok/part_*.mp4        : {len(parts)} video TikTok, đăng theo THỨ TỰ part", "project.json            : bản kê máy đọc được (đường dẫn, sha256, nguồn)", "",
                 "TIÊU ĐỀ YOUTUBE", "---------------", req["youtube_title"], "", "CÁC PART TIKTOK", "---------------"]
        for f in [x for x in files if x["role"] == "tiktok_part"]:
            d = f.get("duration_sec")
            lines.append(f"  {f['path'].split('/')[-1]}  " + (f"{int(d // 60)}:{int(d % 60):02d}  " if d else "") + mb(f["path"]))
        if req.get("warnings"):
            lines += ["", "LƯU Ý", "-----"] + [f"  - {w}" for w in req["warnings"]]
        lines += ["", "Thư mục này thuộc về bạn: hệ thống không đọc lại và không sửa nó. Chép/di chuyển/xóa tùy ý.",
                  "Render lại sẽ tạo thư mục phiên bản mới bên cạnh (…-v2), không ghi đè thư mục này."]
        return "\n".join(l for l in lines if l is not None) + "\n"
