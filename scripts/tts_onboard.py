"""Onboarding một engine TTS mới chỉ từ repo/docs/source (TTS Analyzer, không chạy mã của repo).

    python scripts/tts_onboard.py <thư-mục | git URL | URL docs> --out onboarding/<engine> [--engine NAME]

Sinh: profile.candidate.json (mỗi giá trị có source/confidence/evidence), config.snippet.json (dán vào config/config.json để dùng
adapter), adapter_candidate.py (khi engine là API Python/HTTP), needs_user.json (chỉ những gì không thể tự biết), analysis.json.
Sau đó kiểm chứng bằng chạy thật:  python scripts/tts_tune.py --root . --profile onboarding/<engine>/profile.candidate.json
"""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contentfactory.tts import analyzer                       # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reference")
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine")
    a = ap.parse_args()
    src = analyzer.fetch_reference(a.reference, Path(tempfile.mkdtemp(prefix="cf-tts-ref-")))
    res = analyzer.analyze(src, a.engine)
    paths = analyzer.write_onboarding(res, Path(a.out))
    caps = res["capabilities"]
    print(f"engine={res['engine']}  adapter={res['adapter']['kind']} (ready={res['adapter']['ready']})  files={len(res['files'])}")
    print("max_chars={max_chars} languages={languages} sample_rate={sample_rate} speed={speed} cloning={voice_cloning}".format(**caps))
    print("cần người dùng:", [f"{n['key']} ({n['reason']})" for n in res["needs_user"]] or "không")
    print("đã ghi:", *(f"\n  {p}" for p in paths.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
