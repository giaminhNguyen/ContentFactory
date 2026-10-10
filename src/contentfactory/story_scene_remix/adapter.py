"""StoryAdapter của chế độ Remix bám sự việc (`story_scene_remix`).

Source → Bản đồ cảnh toàn cục → Kế hoạch remix toàn cục (MỘT lần) → Viết lại có mục tiêu (chỉ cảnh bị ảnh hưởng) → QA liên tục → một section cho Story Assembler HIỆN CÓ.
Không premise, không Kho nhân vật, không story bible, không outline generator của Story Remix. Mọi bước đắt đều có checkpoint (fingerprint) ⇒ resume không gọi lại LLM.
Hợp đồng đầu ra có trong prompt (constraint-first) và được kiểm bằng code ngay sau khi sinh (logic.py); LLM chỉ dùng cho lỗi về nghĩa.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..contracts import ErrorClass, StageError
from ..fsutil import atomic_write_json, atomic_write_text
from ..story import mode as SM
from ..story.naming import brand_marks
from ..story_remix.core import Invalid, Ledger, Steps, ask_json, fail, fingerprint
from . import logic as L

VERSION = 2
HARD = {"EMPTY", "TOO_SHORT", "OLD_REMAINS", "SOURCE_BRAND_TRACE", "FORMAT"}          # sau 1 lượt thử lại vẫn lỗi ⇒ Hard Stop; còn lại chỉ cảnh báo
SYS_MAP = "Phân tích diễn biến truyện để CHỈNH SỬA, không viết truyện mới. Chỉ trả JSON."
SYS_PLAN = "Biên tập viên remix bám sự việc: chọn mức can thiệp THẤP NHẤT đủ dùng. Chỉ trả JSON."
SYS_QA = "Độc giả nghe truyện audio kiêm biên tập viên liên tục. Chỉ báo lỗi logic thật, không bắt sửa vì sở thích. Chỉ trả JSON."
SYS_WRITE = "Biên tập viên sửa truyện audio theo cảnh: logic, nhân vật và văn phong tự nhiên quan trọng hơn thay chữ máy móc. Chỉ xuất văn xuôi."

MAP_PROMPT = """Với MỖI cảnh dưới đây trả: event (diễn biến chính), cause (nguyên nhân/động cơ), effect (kết quả), emotional_role (vai trò cảm xúc),
characters (tên nhân vật xuất hiện), objects (đồ vật/bằng chứng/bí mật có thể xuất hiện lại), beat (một trong: hook, twist, climax, payoff, setup, none).
Không bỏ hook/twist/cao trào. Chỉ phân tích, KHÔNG viết lại. Trả đủ đúng các id {ids}.
JSON: {{"scenes":[{{"id","event","cause","effect","emotional_role","characters":[],"objects":[],"beat"}}]}}
NGUỒN (JSON):
{data}"""

PLAN_SOURCE_CAP = 150_000                                                                  # ký tự nguồn gửi kèm kế hoạch (một lần); dài hơn thì rút đều mỗi cảnh

PLAN_PROMPT = """Lập MỘT kế hoạch thay đổi thống nhất cho TOÀN BỘ truyện trước khi viết lại. Mục tiêu: truyện mới cuốn như nguồn (giữ hook, nhịp cảm xúc, twist, cao trào, kết thúc) nhưng thay các chi tiết tương đương.
CẤP 1 (mặc định): thay vật phẩm/hành động/lý do/hoàn cảnh bằng yếu tố tương đương (vd trộm bút → trộm hộp cơm). Mỗi thay đổi phải còn khớp hành động, động cơ, bằng chứng, hậu quả.
CẤP 2 (hạn chế): chỉ khi thay chi tiết khiến cảnh không còn hợp lý; ghi why_level2 (vì sao cấp 1 không đủ).
KHÔNG dùng cấp 3. Nếu chỉ thay được bằng cách đổi cả tuyến sự kiện, trả {{"changes":[],"needs_level3":"lý do"}}.
QUY TẮC: thay đổi NHỎ NHẤT đủ dùng; không đổi chi tiết đang hoạt động tốt; không đổi ngôi kể/tính cách nhân vật; không làm yếu hook/cao trào/kết; không thêm tình tiết/cảnh phụ mới.
`old` phải TRÍCH NGUYÊN VĂN (đúng từng chữ, đúng dấu) một cụm ngắn có trong `source_scenes` bên dưới — KHÔNG lấy từ phần tóm tắt event; liệt kê mọi scene_ids có cụm đó và cảnh phụ thuộc (đồ vật/chi tiết xuất hiện lại, hậu quả). Truyện {n} cảnh; không để thay đổi lan quá 65% số cảnh.
JSON: {{"changes":[{{"id","old","new","level":1,"scene_ids":["s001"],"why","why_level2"}}],"global_rules":["quy tắc nhất quán chung"]}}
BẢN ĐỒ TRUYỆN (JSON):
{data}"""

WRITE_PROMPT = """CHỈ viết lại CẢNH HIỆN TẠI ({sid}/{n}) theo kế hoạch đã chốt.
HỢP ĐỒNG ĐẦU RA (bắt buộc, sẽ được kiểm bằng code):
- Chỉ văn xuôi của cảnh (lời kể + thoại). KHÔNG tiêu đề, KHÔNG "Chương/Cảnh/Phần", KHÔNG markdown (#, **), KHÔNG ghi chú/giải thích/tóm tắt, KHÔNG marker hay placeholder.
- Độ dài {lo}–{hi} ký tự (nguồn {chars}). Không rút gọn, không độn chữ, không thêm tình tiết/nhân vật ngoài kế hoạch.
- Ngôi kể: {pov}. Giữ đúng tên và cách xưng hô nhân vật: {cast}.
- Không chứa tên kênh/watermark/thương hiệu nguồn.
- {readability}
- Mức can thiệp {level}: {level_rule}
THAY ĐỔI ÁP DỤNG CHO CẢNH NÀY (đã chốt, không tự đổi):
{relevant}
Mọi cụm cũ phải biến mất; câu thoại, vật chứng, động cơ, cử chỉ, hậu quả liên quan phải khớp chi tiết mới. Viết thành đoạn văn liền mạch, không vá từng câu.
TOÀN BỘ thay đổi của truyện (để nhất quán): {all_changes}
{rules}{ctx_prev}{ctx_next}
CẢNH NGUỒN:
{source}"""

REPAIR_PROMPT = """Sửa CẢNH {sid} có lỗi liên tục dưới đây. Không thêm tình tiết, không đổi kế hoạch đã chốt, không rút gọn đáng kể. Chỉ xuất văn xuôi của cảnh đã sửa (không tiêu đề/ghi chú/markdown/marker).
Giữ ngôi kể {pov}, tên/xưng hô: {cast}. Thay đổi đã chốt: {all_changes}
LỖI CẦN SỬA:
{issues}
{ctx_prev}{ctx_next}
CẢNH HIỆN TẠI:
{text}"""

QA_PROMPT = """Kiểm tra các mối nối và những thay đổi đã chốt của truyện remix. Chỉ báo lỗi THẬT: mâu thuẫn logic/nhân quả, nhân vật/xưng hô/ngôi kể sai, thời gian-địa điểm, chi tiết đã đổi mà phần sau chưa cập nhật,
bằng chứng/hậu quả không khớp, chuyển cảnh gượng, lặp ý, hook bị kéo dài không cần, kết thúc không nhất quán. Không bắt sửa vì sở thích.
Không lỗi: {{"issues":[]}}; có lỗi: {{"issues":[{{"scene_id":"s003","problem":"..."}}]}} (chỉ id cảnh có thật; mỗi cảnh một mục).
DỮ LIỆU (JSON):
{data}"""

LEVEL_RULE = {1: "viết lại tối thiểu, chỉ những câu liên quan chi tiết đổi.", 2: "được chỉnh cả cảnh để logic đúng, nhưng không tạo tuyến truyện mới."}
MAX_QA_REPAIRS = 4                                                                        # tối đa số cảnh sửa mỗi lượt


def _short_changes(changes: list[dict]) -> str:
    return "; ".join(f"{c['id']}: \"{c['old']}\"→\"{c['new']}\"" for c in changes)


class StorySceneRemixAdapter:
    def __init__(self, llm, price: dict | None = None) -> None:
        self.llm = llm
        self.price = price                                  # {"in","out"} USD/triệu token (Cài đặt máy) hoặc None ⇒ chỉ chặn theo chi phí thật

    # ------------------------------------------------------------------ gọi LLM trả văn xuôi
    def _prose(self, ledger: Ledger, step: str, prompt: str, system: str, source: str, relevant: list[dict], marks: list[str], level: int, ctx, retries: int = 1) -> tuple[str, list[dict], list[str]]:
        """LLM → văn xuôi: làm sạch bằng code, kiểm hợp đồng; lỗi cứng ⇒ thử lại có lý do (tối đa `retries`), vẫn lỗi ⇒ SCENE_REWRITE_INVALID (không chuyển dữ liệu sai đi tiếp)."""
        err = ""
        for attempt in range(1, retries + 2):
            ctx.cancel.check()
            ledger.guard()
            res = self.llm.complete(prompt + (f"\n\nLẦN TRƯỚC BỊ TỪ CHỐI:\n{err}\nViết lại đúng hợp đồng, sửa đúng các lỗi trên." if err else ""), system=system, step=step, ctx=ctx)
            ledger.record(step, res, attempt)
            raw = str(res.get("text") or "").strip()
            if raw.startswith("{"):                                         # LLM bọc JSON {"text": ...}: lấy ra bằng code
                try:
                    j = json.loads(raw)
                    raw = str(j.get("text") or "") if isinstance(j, dict) else raw
                except ValueError:
                    pass
            text, removed = L.clean_prose(raw)
            issues = L.check_scene(source, text, relevant, marks, level)
            hard = [i for i in issues if i["code"] in HARD]
            if not hard:
                return text, [i for i in issues if i["code"] not in HARD], removed
            err = "\n".join(f"- {i['message']}" for i in hard)
        raise fail("SCENE_REWRITE_INVALID", f"Bước {step}: LLM không tạo được cảnh đúng hợp đồng sau {retries + 1} lần.", {"issues": hard, "step": step,
                   "hint": "Các cảnh đã xong được giữ; chạy lại để thử tiếp cảnh này, hoặc chọn nguồn/mô hình khác."})

    # ------------------------------------------------------------------ generate
    def generate(self, bundle, profile: dict, out_dir: Path, ctx):
        mode = (ctx.params or {}).get("story_mode") or {}
        if mode.get("mode") != SM.SCENE:
            raise StageError(ErrorClass.POLICY, "NOT_SCENE_REMIX_JOB", "Job không ở chế độ Remix bám sự việc.", resource="input")
        SM.check_scene_rights(mode)                                       # Hard Stop: kiểm lại cả lúc chạy (job cũ/params sửa tay), không tin riêng bước tạo job
        st = mode["story"]
        source = Path(bundle["transcript"]).read_text(encoding="utf-8", errors="replace")
        scenes = L.split_scenes(source)
        if not scenes:
            raise StageError(ErrorClass.POLICY, "EMPTY_SOURCE", "Không có nội dung nguồn để remix.", resource="input")
        n = len(scenes)
        rdir = out_dir / "scene_remix"
        (rdir / "scenes").mkdir(parents=True, exist_ok=True)
        ledger, steps = Ledger(rdir / "cost_report.json", st.get("budget_usd")), Steps(rdir)
        try:
            meta = ctx.read_json("metadata")
        except (KeyError, OSError, ValueError):
            meta = {}
        marks = self._marks(str(meta.get("title") or bundle.get("title") or ""), str((meta.get("metadata") or {}).get("channel") or ""))
        lang = {"vi": "tiếng Việt", "en": "English", "zh": "tiếng Trung"}.get(bundle.get("language"), bundle.get("language") or "tiếng Việt")
        src_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
        self._budget_preflight(st, len(source), ledger, ctx)
        ctx.log("scene_remix_start", scenes=n)

        # ---- 1. Bản đồ cảnh (theo nhóm, checkpoint từng nhóm)
        per_scene: list[dict] = []
        for g in range(0, n, L.GROUP):
            ids = [L.sid(i) for i in range(g, min(g + L.GROUP, n))]
            group = [{"id": i, "text": scenes[int(i[1:]) - 1]} for i in ids]
            step = f"source_map_{g // L.GROUP + 1:03d}"
            check = lambda d, ids=ids: L.check_map_group(d, ids)                                                        # noqa: E731
            m = steps.run(step, fingerprint(v=VERSION, group=group), f"{step}.json", lambda group=group, ids=ids, step=step, check=check: ask_json(
                self.llm, ledger, step, SYS_MAP, MAP_PROMPT.format(ids=", ".join(ids), data=json.dumps(group, ensure_ascii=False)), check, ctx=ctx), check)
            per_scene.extend(m["scenes"])
        gmap = {**L.global_map(scenes, per_scene), "source_sha256": src_sha, "scenes": per_scene}
        atomic_write_json(rdir / "scene_map.json", gmap)

        # ---- 2. Kế hoạch remix toàn cục (một lần, đóng băng)
        compact = [{"id": s["id"], "event": s["event"][:160], "beat": s["beat"], "characters": s["characters"][:5], "objects": s["objects"][:5]} for s in per_scene]
        check_plan = lambda d: L.check_plan(d, scenes)                                                                  # noqa: E731
        plan_fp = fingerprint(v=VERSION, map=compact)
        per = max(800, PLAN_SOURCE_CAP // n)
        src_scenes = [{"id": L.sid(i), "text": t if len(t) <= per else t[:per // 2] + " […] " + t[-per // 2:]} for i, t in enumerate(scenes)]
        plan_data = json.dumps({"scenes": compact, "pov": gmap["pov"], "recurring_objects": gmap["recurring_objects"], "hook_scene": gmap["hook_scene"], "ending_scenes": gmap["ending_scenes"],
                                "source_scenes": src_scenes}, ensure_ascii=False)

        def make_plan():
            try:
                return ask_json(self.llm, ledger, "scene_change_plan", SYS_PLAN, PLAN_PROMPT.format(n=n, data=plan_data), check_plan, ctx=ctx)
            except StageError as e:
                if e.code == "REMIX_LLM_INVALID" and (e.detail or {}).get("kind") == "content":
                    # lỗi NỘI DUNG đã hết lượt sửa: không để orchestrator tự chạy lại cả bước (nhân lượt gọi); dừng, người dùng quyết định chạy tiếp
                    raise fail("REMIX_PLAN_INVALID", e.message, {**(e.detail or {}), "hint": "Kế hoạch remix không đạt kiểm tra sau số lượt cho phép; bản đồ cảnh đã được giữ. Chạy lại để thử tiếp hoặc đổi mô hình."}) from None
                raise
        plan = steps.run("remix_plan", plan_fp, "remix_plan.json", make_plan, check_plan)
        changes = plan["changes"]
        affected = L.affected_scenes(scenes, changes)
        atomic_write_json(rdir / "affected_scenes.json", {"ids": [L.sid(i) for i in affected], "levels": {L.sid(i): max(c["level"] for c in L.relevant_changes(L.sid(i), scenes[i], changes) or [{"level": 1}]) for i in affected}})

        # ---- 3. Viết lại có mục tiêu (chỉ cảnh bị ảnh hưởng)
        cast = ", ".join(list(gmap["characters"])[:12]) or "(giữ nguyên như nguồn)"
        rules = ("QUY TẮC NHẤT QUÁN CHUNG: " + " | ".join(plan["global_rules"]) + "\n") if plan.get("global_rules") else ""
        readability = "Câu ngắn, rõ người nói, hạn chế đại từ mơ hồ (độ dễ nghe CAO)." if st.get("audio_readability") == "high" else "Văn nói tự nhiên, dễ nghe qua audio."
        changed = [L.clean_prose(t)[0] or t for t in scenes]                       # cảnh không đổi: chỉ gỡ heading/marker như Assembler sẽ làm
        recs: dict[int, dict] = {}
        todo = [i for i in affected if not self._load(rdir, i, self._fp(scenes, i, changes, st))]
        ledger.preflight(len(todo) + 1 + int(st.get("quality_repair_max_passes", 1)), "viết lại + QA")
        for i in affected:
            s = L.sid(i)
            rel = L.relevant_changes(s, scenes[i], changes)
            level = max(c["level"] for c in rel) if rel else 1
            fp = self._fp(scenes, i, changes, st)
            rec = self._load(rdir, i, fp)
            if rec is None:
                prompt = WRITE_PROMPT.format(
                    sid=s, n=n, lo=int(len(scenes[i]) * L.LEN_LO[level]), hi=int(len(scenes[i]) * L.LEN_HI[level]), chars=len(scenes[i]), pov=gmap["pov"], cast=cast, readability=readability,
                    level=level, level_rule=LEVEL_RULE[level], relevant="\n".join(f"- {c['id']} [cấp {c['level']}]: \"{c['old']}\" → \"{c['new']}\"" + (f" ({c['why']})" if c["why"] else "") for c in rel) or "- (cảnh phụ thuộc: chỉnh cho khớp các thay đổi liền kề)",
                    all_changes=_short_changes(changes), rules=rules, ctx_prev=self._ctx("CUỐI CẢNH TRƯỚC (đã chốt, chỉ để nối mạch, KHÔNG viết lại)", changed[i - 1][-550:] if i else ""),
                    ctx_next=self._ctx("ĐẦU CẢNH SAU (nguồn, chỉ để nối mạch, KHÔNG viết lại)", scenes[i + 1][:550] if i + 1 < n else ""), source=scenes[i])
                text, warns, removed = self._prose(ledger, f"scene_rewrite_{s}", prompt, SYS_WRITE, scenes[i], rel, marks, level, ctx)
                rec = {"fp": fp, "text": text, "level": level, "repairs": 0, "warnings": [w["message"] for w in warns], "cleaned_lines": removed[:10], "source_chars": len(scenes[i])}
                self._save(rdir, i, rec)
            recs[i] = rec
            changed[i] = rec["text"]

        for i in range(n):                                                                  # cảnh KHÔNG bị ảnh hưởng nhưng đã được sửa mối nối ở lần chạy trước: khôi phục bản sửa (resume không được làm mất)
            if i not in recs:
                rec = self._load_any(rdir, i)
                if rec and rec.get("repairs") and rec.get("fp") == self._fp(scenes, i, changes, st) and isinstance(rec.get("text"), str) and rec["text"].strip():
                    recs[i], changed[i] = rec, rec["text"]

        # ---- 4. Continuity QA (tất định trước, LLM chỉ cho lỗi về nghĩa) + sửa có mục tiêu
        qa_state = self._qa_state(rdir, plan_fp)
        passes_max = int(st.get("quality_repair_max_passes", 1))
        profile_ratio = float((profile or {}).get("max_removed_ratio", 0.35))
        issues: list[dict] = []
        qa_log: list[dict] = []
        while True:
            det = L.det_qa(changed, changes, marks, profile_ratio)
            can_repair = qa_state["passes_used"] < passes_max
            ai: list[dict] = []
            if not (det and can_repair):                                       # còn lỗi tất định và còn lượt sửa: sửa trước, khỏi tốn một lượt QA
                ai = self._ai_qa(ledger, rdir, scenes, changed, per_scene, changes, affected, gmap, n, ctx)
            issues = det + ai
            qa_log.append({"pass": qa_state["passes_used"], "issues": issues})
            if not issues or not can_repair:
                break
            ledger.preflight(min(len(L.merge_issues(issues)), MAX_QA_REPAIRS) + 1, "sửa mối nối + QA lại")
            qa_state["passes_used"] += 1
            atomic_write_json(rdir / "qa_state.json", qa_state)
            for s_id, group in list(L.merge_issues(issues).items())[:MAX_QA_REPAIRS]:
                i = int(s_id[1:]) - 1
                rel = L.relevant_changes(s_id, scenes[i], changes)
                level = max([c["level"] for c in rel] or [1])
                prompt = REPAIR_PROMPT.format(sid=s_id, pov=gmap["pov"], cast=cast, all_changes=_short_changes(changes), issues="\n".join(f"- {x['problem']}" for x in group),
                                              ctx_prev=self._ctx("CUỐI CẢNH TRƯỚC", changed[i - 1][-800:] if i else ""), ctx_next=self._ctx("ĐẦU CẢNH SAU", changed[i + 1][:800] if i + 1 < n else ""), text=changed[i])
                try:
                    text, warns, _ = self._prose(ledger, f"scene_repair_{s_id}", prompt, SYS_WRITE, changed[i], rel, marks, level, ctx, retries=0)
                except StageError as e:
                    if e.code != "SCENE_REWRITE_INVALID":
                        raise
                    continue                                                    # bản sửa hỏng hợp đồng: giữ cảnh cũ, lỗi vẫn được báo ở QA kế tiếp (không đưa dữ liệu sai đi tiếp)
                changed[i] = text
                rec = recs.get(i) or self._load_any(rdir, i) or {"fp": self._fp(scenes, i, changes, st), "level": level, "repairs": 0, "warnings": [], "source_chars": len(scenes[i])}
                rec.update(text=text, repairs=rec.get("repairs", 0) + 1)
                recs[i] = rec
                self._save(rdir, i, rec)
        qa = {"issues": issues, "repair_passes": qa_state["passes_used"], "passes_max": passes_max, "history": qa_log}
        atomic_write_json(rdir / "continuity_qa.json", qa)

        # ---- 5. Ghi truyện (một section; Story Assembler HIỆN CÓ là bộ định dạng duy nhất) + báo cáo
        story_path = rdir / "rewritten_story.md"
        atomic_write_text(story_path, "\n\n".join(t.strip() for t in changed if t.strip()) + "\n")
        ledger.save()
        cost = ledger.report()
        lv = [r.get("level", 1) for r in recs.values()]
        report = {"source_sha256": src_sha, "scenes_total": n, "scenes_revised": len(affected), "changes": len(changes), "level_1": sum(c["level"] == 1 for c in changes),
                  "level_2": sum(c["level"] == 2 for c in changes), "level_3": 0, "scenes_level_2": lv.count(2), "plan_warnings": plan.get("warnings", []), "qa": qa,
                  "scene_warnings": {L.sid(i): r["warnings"] for i, r in recs.items() if r.get("warnings")}, "cost": cost, "steps_ran": steps.ran, "steps_skipped": steps.skipped}
        atomic_write_json(rdir / "scene_remix_report.json", report)
        if issues and not st.get("review_accepted"):
            raise StageError(ErrorClass.POLICY, "SCENE_CONTINUITY_REVIEW", "Còn lỗi liên tục chưa sửa được trong giới hạn lượt sửa; dừng trước khi tạo story.txt.",
                             {"issues": issues[:8], "path": str(rdir / "continuity_qa.json"),
                              "hint": "Xem continuity_qa.json: nếu chấp nhận được, bật “Đã xem báo cáo — tiếp tục”; hoặc tăng số lượt sửa rồi chạy lại (các cảnh đã xong được giữ)."})
        return {"sections": [story_path], "stats": {"mode": SM.SCENE, "scenes": n, "scenes_revised": len(affected), "changes": len(changes), "level_2_changes": report["level_2"],
                                                     "llm_calls": cost["calls"], "cost_usd": cost["known_cost_usd"], "cost_unknown_calls": cost["calls_with_unknown_cost"],
                                                     "qa_unresolved": len(issues), "steps_ran": len(steps.ran), "steps_skipped": len(steps.skipped)}}

    # ------------------------------------------------------------------ phần phụ
    @staticmethod
    def _marks(title: str, channel: str) -> list[str]:
        """Dấu hiệu kênh gốc: tên kênh + chữ trong 【】/[] + đuôi sau '|' của tên video. KHÔNG dùng phần đầu tên video (dễ trùng chữ trong truyện ⇒ báo nhầm)."""
        out: list[str] = []
        for m in [channel.strip(), *brand_marks(title)]:
            m = re.sub(r"\s*-\s*Videos$", "", m).strip()
            if len(m) >= 4 and m.lower() not in {x.lower() for x in out}:
                out.append(m)
        return out

    @staticmethod
    def _ctx(label: str, text: str) -> str:
        return f"[{label}]\n{text}\n" if text else ""

    @staticmethod
    def _fp(scenes: list[str], i: int, changes: list[dict], st: dict) -> str:
        """Khoá cảnh i: nguồn cảnh + lân cận NGUỒN + thay đổi liên quan + độ dễ nghe. Dùng nguồn (không phải bản đã viết lại) của lân cận để một lần sửa không kéo đổ cache các cảnh khác."""
        s = L.sid(i)
        rel = L.relevant_changes(s, scenes[i], changes)
        return fingerprint(v=VERSION, src=scenes[i], prev=scenes[i - 1][-550:] if i else "", nxt=scenes[i + 1][:550] if i + 1 < len(scenes) else "", rel=rel, readability=st.get("audio_readability"))

    @staticmethod
    def _load_any(rdir: Path, i: int) -> dict | None:
        try:
            return json.loads((rdir / "scenes" / f"{L.sid(i)}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _load(self, rdir: Path, i: int, fp: str) -> dict | None:
        rec = self._load_any(rdir, i)
        return rec if rec and rec.get("fp") == fp and isinstance(rec.get("text"), str) and rec["text"].strip() else None

    @staticmethod
    def _save(rdir: Path, i: int, rec: dict) -> None:
        atomic_write_json(rdir / "scenes" / f"{L.sid(i)}.json", rec)
        atomic_write_text(rdir / "scenes" / f"{L.sid(i)}.txt", rec["text"])           # bản đọc được cho người xem

    @staticmethod
    def _qa_state(rdir: Path, plan_fp: str) -> dict:
        """Số lượt sửa đã dùng, bền qua resume (đổi kế hoạch ⇒ về 0): giới hạn lượt sửa là của job, không phải của mỗi lần chạy."""
        try:
            d = json.loads((rdir / "qa_state.json").read_text(encoding="utf-8"))
            if d.get("plan_fp") == plan_fp:
                return d
        except (OSError, ValueError):
            pass
        return {"plan_fp": plan_fp, "passes_used": 0}

    def _ai_qa(self, ledger, rdir: Path, scenes, changed, per_scene, changes, affected, gmap, n, ctx) -> list[dict]:
        """MỘT lượt QA ngữ nghĩa trên mối nối quanh cảnh đã đổi + mở đầu/kết thúc; cache theo băm văn bản hiện tại (resume không gọi lại)."""
        touched = set(affected)
        joins = [{"scene_id": L.sid(i), "before": changed[i - 1][-480:], "after": changed[i][:480]} for i in range(1, n) if i in touched or i - 1 in touched][:40]
        data = {"changes": changes, "pov": gmap["pov"], "characters": list(gmap["characters"])[:12],
                "source_events": [{"id": s["id"], "event": s["event"][:120]} for s in per_scene], "joins": joins,
                "opening_source": scenes[0][:650], "opening_remix": changed[0][:650], "ending_remix": changed[-1][-650:]}
        key = hashlib.sha256(json.dumps([changed, changes], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
        cache = rdir / "qa_ai_cache.json"
        try:
            c = json.loads(cache.read_text(encoding="utf-8"))
            if c.get("key") == key:
                return c["issues"]
        except (OSError, ValueError, KeyError):
            pass
        res = ask_json(self.llm, ledger, "scene_continuity_qa", SYS_QA, QA_PROMPT.format(data=json.dumps(data, ensure_ascii=False)), lambda d: L.check_qa(d, n), ctx=ctx)
        atomic_write_json(cache, {"key": key, "issues": res["issues"]})
        return res["issues"]

    def _budget_preflight(self, st: dict, chars: int, ledger: Ledger, ctx) -> None:
        """Trước khi tốn đồng nào: nếu có ngân sách + giá cấu hình mà ước tính TỐI THIỂU đã vượt ⇒ dừng; ước tính tối đa vượt ⇒ cảnh báo (guard/preflight vẫn chặn khi chạy)."""
        est = L.estimate(chars, int(st.get("quality_repair_max_passes", 1)), self.price)
        atomic_write_json(ledger.path.parent / "estimate.json", est)
        b = st.get("budget_usd")
        if b is not None and est["usd"]:
            if est["usd"]["min"] > b:
                raise fail("BUDGET_EXCEEDED", f"Ngân sách ${b:.2f} thấp hơn cả ước tính TỐI THIỂU (${est['usd']['min']:.2f}) cho truyện này; chưa gọi AI.",
                           {"hint": "Nâng ngân sách của job hoặc chọn nguồn ngắn hơn.", "estimate_usd": est["usd"], "budget_usd": b})
            if est["usd"]["max"] > b:
                ctx.log("scene_remix_budget_tight", "warning", estimate_max_usd=est["usd"]["max"], budget_usd=b)

    # ------------------------------------------------------------------ giao diện StoryAdapter
    def outline_of(self, bundle, profile: dict, out_dir: Path) -> str | None:
        """Tóm tắt NGẮN để đặt tên truyện (một lượt titler, ngân sách ký tự do code tính): thay đổi đã chốt + mở đầu + kết thúc đã remix."""
        r = out_dir / "scene_remix"
        try:
            plan = json.loads((r / "remix_plan.json").read_text(encoding="utf-8"))
            txt = (r / "rewritten_story.md").read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        return ("Thay đổi so với nguồn: " + _short_changes(plan["changes"]) + "\n\nMỞ ĐẦU:\n" + txt[:1500] + "\n\nKẾT:\n" + txt[-800:])

    def title(self, story_text: str, bundle, ctx) -> str | None:
        return None                                                                  # không có titler (cấu hình fake) ⇒ giữ tên nguồn như Story hiện có

    def finalize(self, story_text: str, ctx) -> dict:
        return {}

    def health(self) -> dict:
        return {"ok": True, "adapter": "story_scene_remix"}
