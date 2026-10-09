"""Benchmark Story Remix đa thể loại (Phase 7) — KHÔNG thuộc production.

    python scripts/remix_bench.py run --llm fake                      # kiểm tra đường ống trên mọi nguồn trong benchmarks/REMIX/* bằng LLM giả (miễn phí)
    python scripts/remix_bench.py run --llm claude --confirm-spend    # chạy THẬT (tốn tiền: gọi Claude CLI cho từng nguồn); cần --confirm-spend
    python scripts/remix_bench.py run --baseline DIR                  # kèm so sánh với baseline Story cũ nếu DIR/<id>.json có {cost_usd, seconds}

Layout: benchmarks/REMIX/<id>/source.txt  →  benchmarks/REMIX-RESULTS/<run>/{<id>/..., report.json, report.md}.
Báo cáo CHỈ ghi số đo thật; thiếu dữ liệu (chi phí không báo, không có baseline) ghi `unknown`. Chạy `--llm fake` chứng minh đường ống/cổng/cơ chế thưởng theo thể loại,
KHÔNG chứng minh chất lượng văn bản thật; so sánh chi phí/chất lượng cũ-mới cần chạy thật cùng nguồn và nghe/đọc kết quả.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from contentfactory.orchestrator.remix_universe import UniverseBridge          # noqa: E402
from contentfactory.story import mode as SM                                      # noqa: E402
from contentfactory.story_remix import similarity as SIM                         # noqa: E402
from contentfactory.story_remix import writer as W                               # noqa: E402
from contentfactory.story_remix.core import Ledger                               # noqa: E402
from contentfactory.story_remix.plan import plan_story                           # noqa: E402
from contentfactory.universe import Universe, UniverseDB                         # noqa: E402

SRC_DIR = REPO / "benchmarks" / "REMIX"
OUT_DIR = REPO / "benchmarks" / "REMIX-RESULTS"
PROFILE = {"target_chars": 12000, "chapter_chars": 3000}


def sources(only: list[str] | None = None) -> dict[str, Path]:
    out = {p.parent.name: p for p in sorted(SRC_DIR.glob("*/source.txt"))}
    return {k: v for k, v in out.items() if not only or k in only}


def run_one(bid: str, path: Path, llm, out_dir: Path, mode_cfg: dict, write: bool, baseline: dict | None, profile: dict | None = None) -> dict:
    profile = profile or PROFILE
    text = path.read_text(encoding="utf-8")
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        uni = Universe(UniverseDB(Path(tmp) / "u.db"))                      # kho riêng cho mỗi benchmark: không lẫn nhân vật giữa thể loại
        bridge = UniverseBridge(uni)
        d = out_dir / bid
        ledger = Ledger(d / "cost_report.json", mode_cfg["story"].get("budget_usd"))
        try:
            return _run_inner(bid, text, uni, bridge, llm, d, ledger, mode_cfg, write, baseline, profile, t0)
        finally:
            uni.db.close()                                                         # Windows: phải đóng SQLite trước khi xoá thư mục tạm


def _run_inner(bid, text, uni, bridge, llm, d, ledger, mode_cfg, write, baseline, profile, t0) -> dict:
    if True:
        plan = plan_story(llm, bridge, text, bid, "vi", mode_cfg, profile, d, f"bench-{bid}", None, ledger)
        chapters = None
        if write:
            res = W.write_chapters(llm, ledger, d, plan["bible"], plan["outline"], plan["cast"], bridge.profiles(plan["cast"]), plan["dna"], "vi", profile["chapter_chars"],
                                   mode_cfg["story"]["audio_readability"], mode_cfg["story"]["quality_repair_max_passes"])
            chapters = {"written": len(res["ran"]), "repairs": sum(c["repairs"] for c in res["chapters"]), "warned": sum(1 for c in res["chapters"] if c["issues"])}
        cost = ledger.report()
        uni.db.close()                                                         # Windows: phải đóng SQLite trước khi xoá thư mục tạm
    names = SIM.proper_names(text)
    cov = plan["quality"]["metrics"].get("reward_types_covered", "unknown") if plan["quality"].get("metrics") else "unknown"
    og = plan["originality"]
    seconds = round(time.time() - t0, 2)
    row = {"id": bid, "source_chars": len(text), "dna": {"genre": plan["dna"]["genre"], "reward_types": plan["dna"]["reward_types"], "cadence": plan["dna"]["payoff_cadence"]},
           "selected_premise": plan["premise"]["id"], "premise_payoff_types": sorted({x["type"] for x in plan["premise"]["payoff_plan"]}), "reward_types_covered_in_outline": cov,
           "dopamine": plan["quality"]["decision"], "originality": {"decision": og["decision"], "level": og["level"], "plan_4gram_containment": og["metrics"]["plan_4gram_containment"],
                                                                       "source_names_reused": og["metrics"]["source_names_reused"]},
           "source_names_detected": len(names), "cast": {"reused": sum(1 for m in plan["cast"]["members"] if m["origin"] == "reused"), "new": sum(1 for m in plan["cast"]["members"] if m["origin"] == "created")},
           "cost": {"known_usd": cost["known_cost_usd"] if cost["calls"] - cost["calls_with_unknown_cost"] else "unknown", "llm_calls": cost["calls"], "calls_with_unknown_cost": cost["calls_with_unknown_cost"]},
           "seconds": seconds, "chapters": chapters}
    if baseline is None:
        row["baseline"] = "unknown (không có dữ liệu Story cũ cho nguồn này)"
    else:
        row["baseline"] = {"cost_usd": baseline.get("cost_usd", "unknown"), "seconds": baseline.get("seconds", "unknown")}
    return row


def run(llm, out_dir: Path, only: list[str] | None = None, write: bool = False, baseline_dir: Path | None = None, llm_name: str = "fake", extra: dict | None = None,
        profile: dict | None = None, budget: float | None = None) -> dict:
    mode_cfg = SM.parse({"mode": "story_remix", "story": {"budget_usd": budget}})
    rows = []
    for bid, p in {**({} if extra and not only else sources(only)), **(extra or {})}.items():
        base = None
        if baseline_dir and (baseline_dir / f"{bid}.json").is_file():
            base = json.loads((baseline_dir / f"{bid}.json").read_text(encoding="utf-8"))
        rows.append(run_one(bid, p, llm, out_dir, mode_cfg, write, base, profile))
    genres = {r["dna"]["genre"] for r in rows}
    summary = {"benchmarks": len(rows), "distinct_genres": len(genres), "llm": llm_name, "all_gates_pass": all(r["dopamine"] == "pass" and r["originality"]["decision"] in ("pass", "pass_with_note") for r in rows),
               "note": ("LLM GIẢ: số liệu chỉ chứng minh đường ống/cổng/cơ chế thưởng theo thể loại, không phải chất lượng văn bản hay chi phí thật." if llm_name == "fake"
                        else "Chạy thật: chi phí chỉ gồm lượt nhà cung cấp báo; quality cần người nghe/đọc xác nhận."),
               "heterogeneous_requirement": "Cần ≥ 3 thể loại khác nhau (nên ≥ 5): " + ("ĐẠT" if len(genres) >= 3 else "CHƯA ĐẠT")}
    rep = {"summary": summary, "results": rows}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    md = ["# Story Remix benchmark", "", f"LLM: **{llm_name}** — {summary['note']}", "", f"{summary['heterogeneous_requirement']} · cổng đạt hết: {summary['all_gates_pass']}", "",
          "| id | thể loại | cơ chế thưởng giữ lại | originality | nhịp thưởng | chi phí | baseline |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['id']} | {r['dna']['genre']} | {r['reward_types_covered_in_outline']} | {r['originality']['decision']} | {r['dopamine']} | {r['cost']['known_usd']} | {r['baseline'] if isinstance(r['baseline'], str) else r['baseline']} |")
    (out_dir / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--llm", choices=["fake", "claude"], default="fake")
    r.add_argument("--confirm-spend", action="store_true", help="bắt buộc khi --llm claude (chạy thật tốn tiền)")
    r.add_argument("--only", nargs="*")
    r.add_argument("--write", action="store_true", help="viết cả chương (tốn nhiều hơn)")
    r.add_argument("--baseline", type=Path)
    r.add_argument("--source", type=Path, action="append", help="thêm một nguồn tuỳ ý (id = tên thư mục cha), vd benchmarks/BENCHMARK-001/source.txt")
    r.add_argument("--chapters", type=int, help="số chương mục tiêu (mặc định 4)")
    r.add_argument("--budget", type=float, help="ngân sách USD tối đa (dừng an toàn khi chi phí đã biết vượt)")
    r.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    if a.llm == "claude" and not a.confirm_spend:
        print("Chạy thật tốn tiền (gọi Claude cho từng nguồn). Thêm --confirm-spend để xác nhận.", file=sys.stderr)
        return 2
    if a.llm == "claude":
        from contentfactory.adapters.claude_llm import ClaudeCliLLM
        llm = ClaudeCliLLM({})
    else:
        from contentfactory.adapters.fake_remix import FakeRemixLLM
        llm = FakeRemixLLM(cost=0.0, report_cost=False)
    out = a.out or OUT_DIR / time.strftime("%Y%m%d-%H%M%S")
    extra = {p.parent.name: p for p in (a.source or [])}
    profile = {**PROFILE, "target_chars": (a.chapters or 4) * PROFILE["chapter_chars"], "chapters": a.chapters or 4}
    rep = run(llm, out, a.only, a.write, a.baseline, a.llm, extra, profile, a.budget)
    print(json.dumps(rep["summary"], ensure_ascii=False, indent=1))
    print("báo cáo:", out / "report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
