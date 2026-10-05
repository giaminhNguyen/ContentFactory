"""Giả lập `claude -p --input-format stream-json --output-format stream-json` để test ClaudeCliRunner bằng subprocess thật."""
import json
import os
import sys
import time

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
sys.stdin.readline()                                   # nhận message người dùng
if "FAKE_CLAUDE_ARGS_FILE" in os.environ:
    with open(os.environ["FAKE_CLAUDE_ARGS_FILE"], "w", encoding="utf-8") as f:
        f.write(json.dumps({"args": sys.argv[1:],
                            "claude_env": sorted(k for k in os.environ if k.startswith(("CLAUDE", "OMC_"))),
                            "cwd": os.getcwd()}))


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def result(text="xong lượt", **kw):
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": "SID", "result": text,
          "total_cost_usd": 0.0123, **kw})


emit({"type": "system", "subtype": "init", "session_id": "SID"})
if mode == "hang":
    time.sleep(60)
elif mode == "noresult":
    sys.stderr.write("boom\n")
    sys.exit(1)
elif mode == "auth":
    result("Not logged in · Please run /login", is_error=True, total_cost_usd=0)
elif mode in ("background", "background_silent"):
    # sub-agent chạy nền: result đầu chưa phải kết thúc; Claude thật phát thêm một result sau khi tác vụ nền xong
    emit({"type": "system", "subtype": "task_started", "task_id": "T1", "is_backgrounded": True})
    result("lượt đầu")
    time.sleep(0.6)
    emit({"type": "system", "subtype": "task_notification", "task_id": "T1"})
    if mode == "background":
        result("xong sau tác vụ nền")
    sys.stdin.read()                                    # chờ runner đóng stdin
else:
    result()
    sys.stdin.read()
