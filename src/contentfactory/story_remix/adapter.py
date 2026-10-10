"""StoryAdapter của chế độ Story Remix: lập kế hoạch (plan.py) → viết chương có bộ nhớ (writer.py) → trả các section cho Story Assembler/Validator HIỆN CÓ (không đổi).

`finalize` được stage `story` gọi SAU khi story.txt đã được ghép + kiểm: chạy QA cuối và (Phase 6) publish Kho nhân vật khi đạt.
Universe và LLM do orchestrator tiêm; module này không import chúng.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..contracts import ErrorClass, StageContext, StageError
from ..fsutil import atomic_write_json
from . import writer as W
from .core import Ledger
from .plan import chapters_for, plan_story


class StoryRemixAdapter:
    def __init__(self, llm, universe_factory, publisher=None) -> None:
        self.llm = llm
        self.universe_factory = universe_factory
        self.publisher = publisher                      # Phase 6: callable(ctx, cast, final_qa, story_text, mode_cfg) -> dict

    def generate(self, bundle, profile: dict, out_dir: Path, ctx: StageContext):
        mode = (ctx.params or {}).get("story_mode") or {}
        if mode.get("mode") != "story_remix":
            raise StageError(ErrorClass.POLICY, "NOT_REMIX_JOB", "Job không ở chế độ Story Remix.", resource="input")
        st, cu = mode["story"], mode["character_universe"]
        rdir = out_dir / "remix"
        source = Path(bundle["transcript"]).read_text(encoding="utf-8", errors="replace")
        ledger = Ledger(rdir / "cost_report.json", st.get("budget_usd"))
        lang = {"vi": "tiếng Việt", "en": "tiếng Anh", "zh": "tiếng Trung", "ja": "tiếng Nhật", "ko": "tiếng Hàn"}.get(bundle["language"], bundle["language"])
        ctx.log("story_remix_plan_start", chapters=chapters_for(profile))
        plan = plan_story(self.llm, self.universe_factory(), source, bundle["title"], lang, mode, profile, rdir, ctx.job_id, ctx, ledger)
        target = int(profile.get("chapter_chars", 3000))
        left = len(plan["outline"]["chapters"]) - len(list((rdir / "chapters").glob("ch_*.md")))
        ledger.preflight(left, "viết chương")                       # ngân sách không đủ cho phần việc còn lại ⇒ dừng SỚM, giữ kế hoạch + chương đã xong
        try:
            meta = ctx.read_json("metadata")
        except (KeyError, OSError, ValueError):
            meta = {}
        marks = W.source_brand_marks(bundle.get("title") or "", str((meta.get("metadata") or {}).get("channel") or ""))
        res = W.write_chapters(self.llm, ledger, rdir, plan["bible"], plan["outline"], plan["cast"], self.universe_factory().profiles(plan["cast"]), plan["dna"], lang, target,
                               st["audio_readability"], st["quality_repair_max_passes"], ctx, marks)
        ledger.save()
        cost = ledger.report()
        atomic_write_json(rdir / "writer_report.json", {"chapters": res["chapters"], "ran": res["ran"], "skipped": res["skipped"]})
        stats = {"mode": "story_remix", "cost_usd": cost["known_cost_usd"], "cost_unknown_calls": cost["calls_with_unknown_cost"], "llm_calls": cost["calls"],
                 "chapters": len(res["sections"]), "chapters_written": len(res["ran"]), "chapters_reused": len(res["skipped"]), "premise": plan["premise"]["id"],
                 "cast_reused": sum(1 for m in plan["cast"]["members"] if m["origin"] == "reused"), "cast_new": sum(1 for m in plan["cast"]["members"] if m["origin"] == "created"),
                 "originality": plan["originality"]["decision"], "plan_steps_ran": plan["steps"]["ran"], "plan_steps_skipped": plan["steps"]["skipped"]}
        return {"sections": res["sections"], "stats": stats}

    def outline_of(self, bundle, profile: dict, out_dir: Path) -> str | None:
        """Dàn ý đã thiết kế (story bible + outline) — dùng để đặt tên truyện."""
        parts = [(out_dir / "remix" / f).read_text(encoding="utf-8") for f in ("story_bible.json", "outline.json") if (out_dir / "remix" / f).is_file()]
        return "\n\n".join(parts) or None

    def title(self, story_text: str, bundle, ctx: StageContext) -> str | None:
        """Tên truyện mới đã được đặt lúc lập kế hoạch (story bible) — không tốn thêm lượt gọi."""
        try:
            return json.loads((ctx.stage_dir / "remix" / "story_bible.json").read_text(encoding="utf-8")).get("title") or None
        except (OSError, ValueError):
            return None

    def finalize(self, story_text: str, ctx: StageContext) -> dict:
        """QA cuối + (nếu đạt và bật ‘tự cập nhật kho sau QA’) publish Kho nhân vật. Idempotent: chạy lại cùng kết quả không làm gì thêm."""
        mode = (ctx.params or {}).get("story_mode") or {}
        rdir = ctx.stage_dir / "remix"
        try:
            cast = json.loads((rdir / "character_cast.json").read_text(encoding="utf-8"))
            mem = json.loads((rdir / "story_memory.json").read_text(encoding="utf-8"))
            chapters = json.loads((rdir / "writer_report.json").read_text(encoding="utf-8"))["chapters"]
        except (OSError, ValueError):
            return {}
        try:
            meta = ctx.read_json("metadata")
        except (KeyError, OSError, ValueError):
            meta = {}
        marks = W.source_brand_marks(str(meta.get("title") or ""), str((meta.get("metadata") or {}).get("channel") or ""))
        qa = W.final_qa(chapters, cast["members"], story_text, mem, marks)
        out: dict = {"final_qa_accepted": qa["accepted"]}
        if self.publisher is not None and qa["accepted"] and mode["character_universe"]["auto_update_after_qa"]:
            try:                                                        # lỗi publish KHÔNG làm hỏng truyện đã đạt QA: ghi rõ, có thể thử lại thủ công (idempotent)
                res = self.publisher(ctx, cast, qa, story_text, mode)
                qa["universe_publish"] = res["universe_publish"]
            except Exception as e:                                      # noqa: BLE001
                ctx.log("universe_publish_failed", "warning", error=repr(e))
                qa["universe_publish"] = {"status": "failed", "error": getattr(e, "message", None) or repr(e)}
            out["universe_publish"] = qa["universe_publish"]["status"]
        elif qa["accepted"]:
            qa["universe_publish"] = {"status": "skipped", "reason": "tắt ‘tự cập nhật kho sau QA’" if self.publisher is not None else "chưa cấu hình publisher"}
        else:
            qa["universe_publish"] = {"status": "not_accepted", "reason": "truyện chưa đạt QA cuối: kho nhân vật không bị thay đổi"}
        atomic_write_json(rdir / "final_qa.json", qa)
        return out

    def health(self) -> dict:
        return {"ok": True, "adapter": "story_remix"}
