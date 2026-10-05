"""Auto Tune: đo thực tế adapter TTS đang cấu hình (gọi engine THẬT — tốn thời gian/tiền, chỉ chạy khi bạn yêu cầu).

    python scripts/tts_tune.py --root . [--language vi] [--profile profile.candidate.json] [--out tuned.json] [--max-requests 40]

Adapter lấy từ config/config.json (adapters.tts + adapter_config.tts). Có --profile thì ghi kết quả đo vào profile (source=runtime_test).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contentfactory.orchestrator.config import load_config    # noqa: E402
from contentfactory.orchestrator.registry import build_adapters   # noqa: E402
from contentfactory.tts import schema                         # noqa: E402
from contentfactory.tts.autotune import AutoTuner, apply_to_profile   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--language", default="vi")
    ap.add_argument("--profile")
    ap.add_argument("--out")
    ap.add_argument("--max-requests", type=int, default=40)
    ap.add_argument("--timeout-s", type=float, default=60)
    a = ap.parse_args()
    adapter = build_adapters(load_config(Path(a.root)))["tts"]
    prof = json.loads(Path(a.profile).read_text(encoding="utf-8")) if a.profile else None
    tuner = AutoTuner(adapter, a.language, prof, max_requests=a.max_requests, timeout_s=a.timeout_s)
    report = tuner.run()
    print(report["summary"], f"({report['requests']} request)")
    if prof is not None:
        out = apply_to_profile(prof, report, run_id=Path(a.profile).stem)
        errs = schema.validate_annotated(out)
        assert not errs, errs
        Path(a.out or a.profile).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("đã ghi profile:", a.out or a.profile)
    return 0 if report["works"] else 1


if __name__ == "__main__":
    sys.exit(main())
