"""Chạy Source + Story THẬT cho một URL YouTube (các stage sau vẫn là fake).

    python scripts/run_real_job.py URL --dry-run            # chỉ kiểm tra điều kiện, KHÔNG gọi LLM
    python scripts/run_real_job.py URL --chapters 3          # chạy thật, dùng thư mục tạm

CẢNH BÁO CHI PHÍ: Story gọi Claude Code CLI (tài khoản của bạn) qua nhiều lượt cho oh-story
(analyze -> explore -> create -> handoff -> 开书 -> các lô <= 3 chương). Bắt đầu bằng --chapters 3
và đặt --max-budget-usd để chặn chi phí mỗi lượt.

Điều kiện: yt-dlp (pip install yt-dlp, hoặc --yt-dlp "python -m yt_dlp"), claude CLI đã đăng nhập, node, python.
Quyền của agent: mặc định acceptEdits + allowlist (transcript là dữ liệu không tin cậy). Nếu agent bị kẹt vì thiếu quyền,
thử --permission-mode bypassPermissions CHỈ khi chấp nhận rủi ro (workspace của job được cách ly trong thư mục tạm).
"""
import argparse
import json
import shlex
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from contentfactory.orchestrator.cli import _print_status          # noqa: E402
from contentfactory.orchestrator.config import load_config         # noqa: E402
from contentfactory.orchestrator.registry import build_adapters    # noqa: E402
from contentfactory.orchestrator.runner import Orchestrator        # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--chapters", type=int, default=3)
    ap.add_argument("--chapter-chars", type=int, default=3000)
    ap.add_argument("--language", default="vi")
    ap.add_argument("--yt-dlp", default="yt-dlp", help='lệnh gọi yt-dlp, vd "python -m yt_dlp"')
    ap.add_argument("--permission-mode", default="acceptEdits")
    ap.add_argument("--max-budget-usd", type=float, default=None, help="trần chi phí MỖI lượt agent")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--root", help="thư mục gốc (mặc định: thư mục tạm mới)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    root = Path(a.root) if a.root else Path(tempfile.mkdtemp(prefix="cf-real-"))
    (root / "config").mkdir(parents=True, exist_ok=True)
    if not (root / "modules.lock").exists():
        (root / "modules.lock").write_text((REPO / "modules.lock").read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load_config(root, {
        "adapters": {"source": "youtube", "story": "story_branch"},
        "youtube": {"yt_dlp_cmd": shlex.split(a.yt_dlp)},
        "story_branch": {"oh_story_root": str(REPO / "modules" / "oh-story-claudecode"),
                         "permission_mode": a.permission_mode, "max_budget_usd_per_turn": a.max_budget_usd,
                         "max_turns": a.max_turns},
    })
    adapters = build_adapters(cfg)
    health = {k: adapters[k].health() for k in ("source", "story")}
    print("root:", root)
    print("health:", json.dumps(health, ensure_ascii=False))
    if a.dry_run or not all(h["ok"] for h in health.values()):
        print("dry-run: không gọi LLM" if a.dry_run else "thiếu điều kiện: sửa theo health ở trên rồi chạy lại")
        return 0 if all(h["ok"] for h in health.values()) else 2

    orc = Orchestrator(cfg, adapters, echo=False)
    job = orc.submit({"input": {"kind": "youtube_url", "value": a.url}, "language": a.language, "made_for_kids": False,
                      "story_profile": {"chapters": a.chapters, "chapter_chars": a.chapter_chars}})
    print("job", job, "- đang chạy (có thể mất nhiều phút; log: runtime/logs/orchestrator.jsonl)")
    orc.run(until_idle=True)
    _print_status(orc, job)
    story = root / "workspace" / f"job_{job}" / "story" / "story.txt"
    if story.is_file():
        text = story.read_text(encoding="utf-8")
        print(f"story.txt: {story} ({len(text)} ký tự, {text.count(chr(10) * 2) + 1} đoạn)")
    return 0 if orc.store.get_job(job)["state"] in ("PUBLISHED", "UPLOAD_READY") else 1


if __name__ == "__main__":
    sys.exit(main())
