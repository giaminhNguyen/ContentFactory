"""Chạy một fake job end-to-end (không cần TTS/Story/Render thật).

    python scripts/run_fake_job.py            # dùng thư mục gốc repo (workspace/, output/, runtime/)
    python scripts/run_fake_job.py --temp     # dùng thư mục tạm, không chạm repo

Kết quả: in trạng thái từng stage và đường dẫn gói output.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from contentfactory.orchestrator.cli import _print_status          # noqa: E402
from contentfactory.orchestrator.config import load_config         # noqa: E402
from contentfactory.orchestrator.runner import Orchestrator        # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--temp", action="store_true")
    a = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix="cf-fake-")) if a.temp else REPO
    if a.temp:
        (root / "modules.lock").write_text((REPO / "modules.lock").read_text(encoding="utf-8"), encoding="utf-8")
    orc = Orchestrator(load_config(root, {"poll_s": 0.05}))
    job_id = orc.submit({
        "input": {"kind": "youtube_url", "value": "https://youtu.be/fake"},
        "title": "Truyện ma đêm khuya: Căn nhà số 13",
        "made_for_kids": False,
        "tiktok": {"speed": 2.0, "target_part_sec": 1.5},      # nhỏ để ra nhiều part trong bản fake
    })
    orc.run(until_idle=True)
    _print_status(orc, job_id)
    receipt = orc.store.inputs(job_id, ("output_package",))["output_package"]
    if receipt:
        pkg = json.loads((root / "workspace" / f"job_{job_id}" / receipt[0]["path"]).read_text(encoding="utf-8"))
        print("output:", pkg["project_dir"])
        for f in pkg["files"]:
            print("  ", f)
    print("manifest:", root / "workspace" / f"job_{job_id}" / "manifest.json")
    return 0 if orc.store.get_job(job_id)["state"] == "PUBLISHED" else 1


if __name__ == "__main__":
    sys.exit(main())
