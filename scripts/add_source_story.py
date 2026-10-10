"""Thêm truyện GỐC (story_source.txt) vào các gói output đã dựng trước khi logic mới có. Idempotent: gói đã có thì bỏ qua.

Chạy: python scripts/add_source_story.py [--root DIR]. Chỉ COPY transcript nguồn của job (kiểm sha256 theo artifact), cập nhật project.json
(files, source_story, content_sig đúng công thức của publisher để chạy lại stage output vẫn 'reused') và README.txt. Không đụng video/story.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

REL = "story_source.txt"


def sig_of(pj: dict, d: Path) -> str:
    """Cùng công thức output/publisher.py: files (theo thứ tự plan) + texts + project."""
    order = ["story", "story_source", "youtube_video", "youtube_thumbnail", "tiktok_part"]
    plan = [f for r in order for f in pj["files"] if f["role"] == r]
    texts = {f["path"]: (d / f["path"]).read_text(encoding="utf-8") for f in pj["files"] if f["role"] in ("title", "description")}
    p = pj["project"]
    return hashlib.sha256(json.dumps({"files": [(f["path"], f["sha256"]) for f in plan], "texts": texts,
                                      "project": [p["title"], p["title_source"], p["channel_id"], p.get("sequence")]}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    root = Path(ap.parse_args().root).resolve()
    db = sqlite3.connect(f"file:{root / 'runtime' / 'contentfactory.db'}?mode=ro", uri=True)
    done = skipped = 0
    for d in sorted((root / "output").iterdir()):
        f = d / "project.json"
        if not (d.is_dir() and f.is_file()):
            continue
        pj = json.loads(f.read_text(encoding="utf-8"))
        if any(x["path"] == REL for x in pj["files"]):
            skipped += 1
            continue
        row = db.execute("select path, sha256 from artifacts where job_id=? and kind='transcript' order by rowid desc limit 1", (pj["job_id"],)).fetchone()
        src = root / "workspace" / f"job_{pj['job_id']}" / row[0] if row else None
        if not (src and src.is_file()):
            print(f"BO QUA {d.name}: không có transcript nguồn của job {pj['job_id']}", file=sys.stderr)
            continue
        dst = d / REL
        shutil.copyfile(src, dst)
        sha = hashlib.sha256(dst.read_bytes()).hexdigest()
        if sha != row[1]:
            dst.unlink()
            print(f"LOI {d.name}: sha256 transcript khác artifact", file=sys.stderr)
            continue
        entry = {"path": REL, "role": "story_source", "sha256": sha, "bytes": dst.stat().st_size, "source": {"workspace_path": row[0], "sha256": row[1]}}
        i = next((k for k, x in enumerate(pj["files"]) if x["role"] == "story"), -1) + 1
        pj["files"].insert(i, entry)
        pj["source_story"] = {"file": REL, "sha256": sha, "bytes": entry["bytes"]}
        pj["content_sig"] = sig_of(pj, d)
        f.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")
        rd = d / "README.txt"
        if rd.is_file():
            txt = rd.read_text(encoding="utf-8")
            line = "story_source.txt         : truyện GỐC (transcript nguồn) để đối chiếu"
            if "story.txt " in txt and line not in txt:
                lines = txt.split("\n")
                k = next(n for n, l in enumerate(lines) if l.startswith("story.txt "))
                lines.insert(k + 1, line)
                rd.write_text("\n".join(lines), encoding="utf-8")
        done += 1
        print(f"OK {d.name}")
    print(f"đã thêm: {done}, đã có từ trước: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
