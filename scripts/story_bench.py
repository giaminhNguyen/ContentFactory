"""Benchmark Story (Dopamine rollout, Phase 0) — KHÔNG thuộc production, không đổi hành vi Story.

    python scripts/story_bench.py metrics FILE                      # số liệu thô của một văn bản
    python scripts/story_bench.py compare BENCHMARK-001 [--candidate ID]   # nguồn / baseline đã duyệt / candidate
    python scripts/story_bench.py run BENCHMARK-001 --candidate ID --yes   # sinh candidate THẬT (tốn tiền: gọi Claude CLI)

Layout: benchmarks/<ID>/{benchmark.json, source.txt, approved/story.txt, candidates/<ID>/story.txt}.
`run` từ chối ghi đè candidate đã có và không bao giờ đụng `approved/`. Số liệu chỉ là bằng chứng, không phải điểm chất lượng.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
BENCH = REPO / "benchmarks"

SENT = re.compile(r"(?<=[.!?…])[\"'”’»)\]]*\s+")
QUOTE = re.compile(r"[\"“”]")
NAMES = re.compile(r"(?<![.!?…\n\"“]\s)(?<!^)\b[A-ZÀ-Ỹ][a-zà-ỹ]+(?:\s[A-ZÀ-Ỹ][a-zà-ỹ]+)+")   # cụm viết hoa giữa câu ~ tên riêng (xấp xỉ)


def metrics(text: str) -> dict:
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    sents = [s for p in paras for s in SENT.split(p) if s.strip()]
    lens = [len(s.split()) for s in sents]
    dlg = [p for p in paras if QUOTE.match(p) or p.startswith(("-", "—"))]      # nguồn là ASR không dấu ngoặc => gần 0, không so sánh được
    # câu liên tiếp không có hỏi / cảm thán / ngoặc thoại = proxy thô cho "vùng ít thay đổi" (không thay thế việc nghe), tính bằng từ
    run = best = seen = 0
    first_hook = None
    for s, n in zip(sents, lens):
        if "?" in s or "!" in s or QUOTE.search(s):
            first_hook = seen if first_hook is None else first_hook
            run = 0
        else:
            run += n
            best = max(best, run)
        seen += n
    nw = max(1, len(text.split()))
    return {"chars": len(text), "words": len(text.split()), "paragraphs": len(paras), "sentences": len(sents),
            "sent_words_mean": round(statistics.mean(lens), 1) if lens else 0,
            "sent_words_p90": sorted(lens)[int(len(lens) * 0.9)] if lens else 0,
            "dialogue_para_ratio(unreliable_on_source)": round(len(dlg) / len(paras), 3) if paras else 0,
            "question_per_1k_words": round(1000 * text.count("?") / nw, 2),
            "exclaim_per_1k_words": round(1000 * text.count("!") / nw, 2),
            "longest_quiet_span_words": best, "hook_latency_words(first ?/!/quote)": first_hook,
            "distinct_name_like_phrases": len(set(NAMES.findall(text)))}


def _load(bid: str) -> tuple[Path, dict]:
    d = BENCH / bid
    if not (d / "benchmark.json").is_file():
        sys.exit(f"không thấy {d / 'benchmark.json'}")
    return d, json.loads((d / "benchmark.json").read_text(encoding="utf-8"))


def compare(bid: str, candidate: str | None) -> int:
    d, meta = _load(bid)
    cols = {"source": d / "source.txt", "approved": d / "approved" / "story.txt"}
    if candidate:
        cols["candidate"] = d / "candidates" / candidate / "story.txt"
    ms = {k: metrics(p.read_text(encoding="utf-8")) for k, p in cols.items()}
    print(f"{bid}: {meta.get('observed_source_genre', '')}\n(generation nondeterministic; metrics are evidence, not truth)\n")
    print(f"{'metric':32}" + "".join(f"{k:>14}" for k in ms))
    for key in ms["source"]:
        print(f"{key:32}" + "".join(f"{str(m[key]):>14}" for m in ms.values()))
    if candidate:
        out = d / "candidates" / candidate / "compare.json"
        out.write_text(json.dumps(ms, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n-> {out}")
    return 0


def run(bid: str, candidate: str, yes: bool) -> int:
    from contentfactory.adapters.story_branch import StoryBranchAdapter
    from contentfactory.contracts import CancelToken, StageContext
    from contentfactory.orchestrator.config import load_config
    from contentfactory.story.assembler import assemble
    from contentfactory.story.validate import validate_story_text

    d, meta = _load(bid)
    cdir = d / "candidates" / candidate
    if cdir.exists():
        sys.exit(f"candidate {candidate!r} đã tồn tại ({cdir}); dùng id mới, không ghi đè")
    if not yes:
        sys.exit("run gọi Claude CLI qua nhiều lượt (tốn token/tiền). Thêm --yes để chạy.")
    cfg = load_config(REPO)
    cdir.mkdir(parents=True)
    log = lambda event, level="info", **f: print(f"[{event}] {f}", flush=True)
    ctx = StageContext(job_id=f"bench-{bid}", stage="story", attempt=1, stage_key=candidate, workspace=cdir, stage_dir=cdir,
                       params={}, inputs={}, config=cfg.data, cancel=CancelToken(), log=log)
    bundle = {"title": meta["title"], "language": meta["language"], "source_language": meta["source_language"],
              "transcript": str(d / "source.txt")}
    adapter = StoryBranchAdapter(cfg.data.get("story_branch", {}), REPO / "modules" / "oh-story-claudecode")
    h = adapter.health()
    if not h["ok"]:
        sys.exit(f"adapter chưa sẵn sàng: {h['problems']}")
    t0 = time.time()
    res = adapter.generate(bundle, {}, cdir, ctx)
    text, rep = assemble([Path(p).read_text(encoding="utf-8") for p in res["sections"]])
    issues = validate_story_text(text)
    (cdir / "story.txt").write_text(text, encoding="utf-8")
    (cdir / "run.json").write_text(json.dumps({"stats": res.get("stats"), "issues": issues, "seconds": round(time.time() - t0),
                                               "assembly": {k: v for k, v in rep.items() if not isinstance(v, list)},
                                               "config": cfg.data.get("story_branch")}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"candidate -> {cdir / 'story.txt'} issues={issues}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("metrics").add_argument("file")
    c = sub.add_parser("compare")
    c.add_argument("benchmark")
    c.add_argument("--candidate")
    r = sub.add_parser("run")
    r.add_argument("benchmark")
    r.add_argument("--candidate", required=True)
    r.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    if a.cmd == "metrics":
        print(json.dumps(metrics(Path(a.file).read_text(encoding="utf-8")), ensure_ascii=False, indent=2))
        return 0
    return compare(a.benchmark, a.candidate) if a.cmd == "compare" else run(a.benchmark, a.candidate, a.yes)


if __name__ == "__main__":
    sys.exit(main())
