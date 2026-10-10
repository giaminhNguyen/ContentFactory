"""TTS Manager: điều phối trên adapter (adapter chỉ biết "một segment -> một file audio").

  story.txt -> Normalizer -> (Rule | AI) Planner -> Validator tất định -> segments.json
            -> từng segment: cache (job-local, rồi cache chung) -> adapter.synthesize (retry RIÊNG segment đó) -> QA chunk
            -> AudioProcessor.assemble -> master.wav ; tts_manifest.json ghi lại mọi quyết định.

Cache key của chunk = sha256(text segment + engine + version + model + voice + language + settings liên quan + profile_version).
Đổi voice/model/settings/engine => key đổi => synth lại; đổi ngắt đoạn/pause/retry/qa => KHÔNG đổi key (audio chunk không đổi).
Resume sau crash: chunk có sidecar (key + sha256) khớp thì dùng lại, và kế hoạch segments.json được dùng lại nếu cùng `plan_key`
(planner AI không tất định, nên không lập lại kế hoạch khác đi giữa chừng).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from ..contracts import AudioProcessor, ErrorClass, Segment, StageContext, StageError, StageResult, TTSAdapter
from ..fsutil import atomic_write, atomic_write_json, sha256_file
from . import qa as chunk_qa
from . import prosody as PRO
from .normalize import normalize_text
from .planner import RuleSegmentPlanner, SegmentPlanner, drop_unspeakable_paragraphs, has_speech, validate_plan
from .schema import cache_identity, normalize_capabilities, resolve, segment_key, stable_hash

_TERMINAL = re.compile(r"[.!?…][\"”’)\]]*\s*$")
MAX_INNER_RETRY_AFTER_S = 30.0       # Retry-After lớn hơn: không ngủ trong stage, trả lỗi cho chính sách retry/hold của job


def _link_or_copy(src: Path, dst: Path) -> None:
    def w(tmp: Path) -> None:
        try:
            os.link(src, tmp)                               # chunk bất biến (ghi atomic, không sửa tại chỗ) nên hardlink an toàn
        except OSError:
            shutil.copyfile(src, tmp)
    atomic_write(dst, w)


class TTSManager:
    def __init__(self, tts: TTSAdapter, audio: AudioProcessor, planner: SegmentPlanner | None = None) -> None:
        self.tts, self.audio = tts, audio
        self.planner: SegmentPlanner = planner or RuleSegmentPlanner()
        self.rule = RuleSegmentPlanner()

    # -- kế hoạch -------------------------------------------------------------------------------------
    def plan(self, ctx: StageContext, text: str, flat: dict) -> tuple[list[Segment], dict]:
        plan_key = stable_hash({"text": text, "planner": self.planner.name, "joiner": flat["joiner"], "seg": flat["segment"],
                                "pause": flat["pause_ms"], "ai": flat["planner"]})
        f = ctx.stage_dir / "segments.json"
        if f.is_file():
            try:
                old = json.loads(f.read_text(encoding="utf-8"))
                if old.get("plan_key") == plan_key and not validate_plan(old["segments"], text, flat)["errors"]:
                    return old["segments"], {**old["report"], "reused_plan": True}
            except (OSError, ValueError, KeyError):
                pass
        report: dict = {"requested": self.planner.name, "used": self.planner.name, "attempts": [], "reused_plan": False}
        segments: list[Segment] | None = None
        feedback: list[str] | None = None
        tries = 1 + (int(flat["planner"]["ai_retries"]) if self.planner.name != "rule" else 0)
        for n in range(1, tries + 1):
            ctx.cancel.check()
            try:
                cand = self.planner.plan(text, flat, feedback)
            except StageError as e:
                if e.error_class == ErrorClass.CANCELLED:
                    raise
                report["attempts"].append({"try": n, "planner": self.planner.name, "error": f"{e.code}: {e.message}"[:300]})
                feedback = [f"planner lỗi {e.code}"]
                continue
            except Exception as e:                           # LLM hỏng/JSON hỏng: không để chặn job, thử lại rồi dự phòng
                report["attempts"].append({"try": n, "planner": self.planner.name, "error": repr(e)[:300]})
                feedback = [str(e)[:200]]
                continue
            v = validate_plan(cand, text, flat)
            report["attempts"].append({"try": n, "planner": self.planner.name, "errors": v["errors"][:10],
                                       "warnings": len(v["warnings"])})
            if not v["errors"]:
                segments, report["warnings"] = cand, v["warnings"][:50]
                break
            feedback = [f"{x['code']}: {x['message']}" for x in v["errors"][:5]]
        if segments is None and self.planner.name != "rule":
            ctx.log("tts_plan_fallback", "warning", planner=self.planner.name)
            cand = self.rule.plan(text, flat)
            v = validate_plan(cand, text, flat)
            report["used"] = "rule"
            report["attempts"].append({"try": 1, "planner": "rule", "errors": v["errors"][:10], "warnings": len(v["warnings"])})
            if not v["errors"]:
                segments, report["warnings"] = cand, v["warnings"][:50]
        if segments is None:
            raise StageError(ErrorClass.POLICY, "PLAN_INVALID", "kế hoạch ngắt đoạn không qua validator",
                             {"attempts": report["attempts"]})
        atomic_write_json(f, {"plan_key": plan_key, "report": report, "segments": segments})
        return segments, report

    # -- speech plan (Prosody Engine) ---------------------------------------------------------------
    def speech_plan(self, ctx: StageContext, text: str, flat: dict, caps: dict, prosody: dict) -> tuple[dict, dict]:
        """Speech plan tất định, KHÔNG LLM (trừ khi prosody.semantic_llm bật rõ ràng và có bộ gán nhãn). Plan được snapshot vào `speech_plan.json`:
        retry/resume/rerender dùng lại đúng plan đó nếu khóa nội dung (văn bản, profile, override, luật, giới hạn engine, nhãn ngữ nghĩa) không đổi."""
        mx = int(flat["segment"]["max_chars"])
        report: dict = {"requested": "prosody", "used": "prosody", "attempts": [], "reused_plan": False, "llm_calls": 0}
        semantic, warnings = None, []
        if prosody["semantic_llm"]:
            labeler = getattr(self.planner, "semantic_labeler", None)
            if labeler is None:
                warnings.append("prosody.semantic_llm bật nhưng adapter planner không có bộ gán nhãn (semantic_labeler): bỏ qua, dùng luật tất định")
            else:
                semantic, srep = PRO.semantic_for(labeler, PRO.sentence_texts(text, mx), PRO.plan_key(text, prosody, flat["segment"], flat["joiner"], caps),
                                                  ctx.stage_dir / "semantic.json")
                report["llm_calls"] = srep["calls"]
                report["semantic_cached"] = srep["cached"]
        key = PRO.plan_key(text, prosody, flat["segment"], flat["joiner"], caps, semantic)
        f = ctx.stage_dir / "speech_plan.json"
        plain = PRO.strip_marks(text)
        if f.is_file():
            try:
                old = json.loads(f.read_text(encoding="utf-8"))
                if old.get("plan_key") == key and not validate_plan(PRO.segments_of(old), plain, flat)["errors"]:
                    return old, {**report, "reused_plan": True}
            except (OSError, ValueError, KeyError, TypeError):
                pass
        plan = PRO.build_speech_plan(text, flat, caps, prosody, semantic)
        plan["plan_key"] = key
        plan["warnings"] = [*plan["warnings"], *warnings]
        v = validate_plan(PRO.segments_of(plan), plain, flat)
        if v["errors"]:
            raise StageError(ErrorClass.POLICY, "SPEECH_PLAN_INVALID", "speech plan không qua validator: " + "; ".join(f"{x['code']} {x['message']}" for x in v["errors"][:3]),
                             {"errors": v["errors"][:10]})
        report["warnings"] = v["warnings"][:50]
        atomic_write_json(f, plan)
        return plan, report

    # -- một segment ----------------------------------------------------------------------------------
    def _synth(self, ctx: StageContext, seg: Segment, flat: dict, out: Path, key: str) -> tuple[dict, dict]:
        """Retry CHỈ segment này (TRANSIENT hoặc chunk không đạt QA). Trả (kết quả adapter, thông tin lần thử)."""
        rp = flat["retry"]
        attempts: list[str] = []
        last: StageError | None = None
        for n in range(1, int(rp["max_attempts"]) + 1):
            ctx.cancel.check()
            try:
                res = self.tts.synthesize({**seg, "key": key}, flat, out, ctx) or {}
            except StageError as e:
                if e.error_class != ErrorClass.TRANSIENT:
                    e.detail = {**e.detail, "segment": seg["index"]}
                    raise
                last = e
                attempts.append(f"{e.code}")
            else:
                issues = self._qa(out, seg["text"], flat) if out.is_file() else ["NO_OUTPUT"]
                if not issues:
                    return res, {"attempts": n, "errors": attempts}
                if out.is_file():
                    out.unlink()                            # chunk hỏng không được ở lại để resume nhầm dùng lại
                last = StageError(ErrorClass.TRANSIENT, "CHUNK_QA_FAILED", f"segment {seg['index']}: {','.join(issues)}",
                                  {"segment": seg["index"], "issues": issues})
                attempts.append("QA:" + ",".join(issues))
            if n < int(rp["max_attempts"]):
                wait = max(float(rp["backoff_s"][min(n - 1, len(rp["backoff_s"]) - 1)]) if rp["backoff_s"] else 0.0,
                           last.retry_after_s or 0.0)
                if (last.retry_after_s or 0) > MAX_INNER_RETRY_AFTER_S:
                    break
                ctx.log("tts_segment_retry", "warning", index=seg["index"], try_no=n, error=attempts[-1], wait_s=wait)
                ctx.cancel.wait(wait)
        assert last is not None
        last.detail = {**last.detail, "segment": seg["index"], "attempts": len(attempts), "errors": attempts}
        raise last

    def _qa(self, out: Path, text: str, flat: dict) -> list[str]:
        issues = chunk_qa.chunk_issues(out, text, flat["qa"])
        if not issues and not self.audio.qa(out)["ok"]:
            issues = ["AUDIO_QA_FAILED"]
        return issues

    # -- văn bản ngắn (dùng chung với Watermark; không cần story/job) --------------------------------------
    def describe_text(self, text: str, raw_profile: dict | None, language: str | None = None) -> dict:
        """Danh tính ngữ nghĩa của một văn bản ngắn nếu được đọc bằng engine + profile này — KHÔNG gọi engine. `fingerprint` phụ thuộc văn bản đã chuẩn hóa +
        engine/phiên bản/model/voice/ngôn ngữ/settings liên quan/phiên bản profile (đúng khóa cache của chunk) + cách ngắt/ghép; đổi bất kỳ thứ gì làm đổi âm thanh
        thì khác, còn giữ nguyên thì cùng fingerprint (không cần gọi provider lại)."""
        caps = normalize_capabilities(self.tts.capabilities())
        flat = resolve(raw_profile, caps, language, getattr(self.tts, "engine_id", None))
        norm, _ = normalize_text(text, flat["normalize"])
        if not norm.strip():
            raise StageError(ErrorClass.POLICY, "EMPTY_TEXT", "văn bản rỗng sau chuẩn hóa")
        ident = cache_identity(flat, caps)
        return {"text": norm, "engine": flat["engine"], "engine_version": caps["engine_version"], "model": flat["model"], "voice": flat["voice"],
                "language": flat["language"], "profile_version": flat["profile_version"], "settings": ident["settings"],
                "fingerprint": stable_hash({"text": norm, "identity": ident, "segment": flat["segment"], "pause_ms": flat["pause_ms"], "joiner": flat["joiner"]})}

    def synthesize_text(self, text: str, raw_profile: dict | None, work_dir: Path, *, language: str | None = None, cache_dir: Path | None = None,
                        cancel=None, log=None) -> dict:
        """Đọc một văn bản NGẮN thành audio bằng CHÍNH đường TTS của stage (profile → resolve, planner/segmenter nếu quá giới hạn engine, cache chunk dùng chung,
        retry riêng chunk, QA chunk/master, assemble). Không có story/job: workspace là `work_dir` tạm. Trả {path (master.wav), duration_sec, segments,
        synthesized, cache_hits, **describe_text()}."""
        from ..contracts import CancelToken
        info = self.describe_text(text, raw_profile, language)
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        ctx = StageContext(job_id="short-text", stage="tts", attempt=1, stage_key="", workspace=work_dir, stage_dir=work_dir,
                           params={"language": language} if language else {}, inputs={},
                           config={"tts_cache_dir": str(cache_dir)} if cache_dir else {}, cancel=cancel or CancelToken(), log=log or (lambda *a, **k: None))
        res = self.run(ctx, text, raw_profile)
        return {**info, "path": work_dir / "audio" / "master.wav", "duration_sec": res.data["duration_sec"], "segments": res.data["segments"],
                "synthesized": res.data["synthesized"], "cache_hits": res.data["cache_hits"]}

    # -- toàn stage -----------------------------------------------------------------------------------
    def run(self, ctx: StageContext, text: str, raw_profile: dict | None) -> StageResult:
        caps = normalize_capabilities(self.tts.capabilities())
        flat = resolve(raw_profile, caps, ctx.params.get("language"), getattr(self.tts, "engine_id", None))
        prosody = PRO.resolve_prosody(ctx.params["prosody"]) if ctx.params.get("prosody") else None      # None = job cũ: đường planner cũ, không đổi hành vi
        text, nrep = normalize_text(PRO.mark_scenes(text) if prosody else text, flat["normalize"])
        if not (PRO.strip_marks(text) if prosody else text):
            raise StageError(ErrorClass.POLICY, "EMPTY_STORY", "story.txt không có nội dung sau chuẩn hóa")
        text, n_dropped = drop_unspeakable_paragraphs(text, keep=(PRO.SCENE_MARK,))        # đoạn chỉ dấu câu/ký hiệu (`…`, `— —`, `“”`, emoji) không có gì để đọc
        if n_dropped:
            ctx.log("tts_unspeakable_dropped", "warning", paragraphs=n_dropped)
        if not has_speech(text):                                                           # hard stop TRƯỚC khi lập plan/gọi TTS
            raise StageError(ErrorClass.POLICY, "NO_SPEECH_CONTENT", "story.txt không có chữ/số nào để đọc (chỉ dấu câu/ký hiệu) — không tạo plan rỗng",
                             {"dropped_paragraphs": n_dropped})
        if prosody:
            splan, plan_report = self.speech_plan(ctx, text, flat, caps, prosody)
            segments = PRO.segments_of(splan)
        else:
            segments, plan_report = self.plan(ctx, text, flat)
            splan = PRO.legacy_plan(segments, flat, plan_report)
        use_ctx = bool(caps.get("supports_context"))
        if prosody and use_ctx:
            for seg, g in zip(segments, splan["groups"]):
                first = next(s for s in splan["segments"] if s["id"] == g["segments"][0])
                last = next(s for s in splan["segments"] if s["id"] == g["segments"][-1])
                seg["context"] = {"previous": first.get("previous_context", ""), "next": last.get("next_context", "")}
        ctx.log("speech_plan", mode=splan["mode"], profile=splan.get("profile"), groups=len(segments), warnings=len(splan["qc"]["warnings"]),
                llm_calls=plan_report.get("llm_calls", 0), reused=plan_report.get("reused_plan"))
        ident = cache_identity(flat, caps)
        cache_dir = Path(ctx.config["tts_cache_dir"]) if ctx.config.get("tts_cache_dir") else None
        chunk_dir = ctx.stage_dir / "chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunks: list[Path] = []
        rows: list[dict] = []
        n_local = n_cache = n_synth = n_retry = 0
        for seg in segments:
            ctx.cancel.check()
            idx, key = seg["index"], segment_key(self._key_text(seg), ident)
            out, side = chunk_dir / f"{idx:06d}.wav", chunk_dir / f"{idx:06d}.json"
            row = {"index": idx, "chars": len(seg["text"]), "key": key[:16], "pause_after_ms": seg["pause_after_ms"]}
            hit = self._local_hit(out, side, key)
            if hit:
                n_local += 1
                row |= {"source": "reused", "duration_sec": hit["duration_sec"], "attempts": 0}
            elif cache_dir and (c := self._cache_hit(cache_dir, key)):
                _link_or_copy(c[0], out)
                atomic_write_json(side, c[1])
                n_cache += 1
                row |= {"source": "cache", "duration_sec": c[1]["duration_sec"], "attempts": 0}
            else:
                if side.exists():
                    side.unlink()
                _, info = self._synth(ctx, seg, flat, out, key)
                st = chunk_qa.wav_stats(out)
                meta = {"key": key, "sha256": sha256_file(out), "duration_sec": round(st["duration_sec"], 3) if st else 0.0}
                atomic_write_json(side, meta)
                if cache_dir:
                    self._cache_put(cache_dir, key, out, meta)
                n_synth += 1
                n_retry += info["attempts"] - 1
                row |= {"source": "synth", "duration_sec": meta["duration_sec"], "attempts": info["attempts"]}
                if info["errors"]:
                    row["retry_errors"] = info["errors"]
            rows.append(row)
            chunks.append(out)
            ctx.log("tts_chunk_done", index=idx, total=len(segments), source=row["source"])
            ctx.progress(len(chunks), len(segments), "segments")
        out_dir = ctx.workspace / "audio" / ctx.extra.get("rerun_dir", "")                 # chạy lại thủ công: thư mục riêng, không ghi đè master hiện hành trước khi commit
        master = out_dir / "master.wav"
        joined: dict = {}

        def build(tmp: Path) -> None:
            joined.update(self.audio.assemble(chunks, [s["pause_after_ms"] for s in segments], tmp, ctx) or {})
        atomic_write(master, build)
        timeline = self._timeline(segments, rows, joined, flat)
        audio_qc = PRO.analyze_audio([s["pause_after_ms"] for s in segments], joined, None, joined.get("duration_sec"))
        splan["qc"]["audio"] = audio_qc
        sp_file = atomic_write_json(ctx.stage_dir / "speech_plan.json", splan)                  # bản cuối kèm QC sau ghép (nội dung plan không đổi)
        tl_file = atomic_write_json(out_dir / "timeline.json", timeline)
        qa = self.audio.qa(master)
        if not qa["ok"]:
            raise StageError(ErrorClass.TRANSIENT, "MASTER_QA_FAILED", ",".join(qa["issues"]))
        manifest = {"schema": 1, "engine": flat["engine"], "engine_version": caps["engine_version"],
                    "language": flat["language"], "voice": flat["voice"], "model": flat["model"],
                    "profile_version": flat["profile_version"], "profile_hash": stable_hash(flat)[:16],
                    "identity_hash": stable_hash(ident)[:16], "normalize": nrep, "planner": plan_report,
                    "prosody": {"mode": splan["mode"], "profile": splan.get("profile"), "rules_version": splan.get("rules_version"),
                                "plan_key": splan.get("plan_key"), "strategy": splan.get("strategy"), "qc": splan["qc"], "llm_calls": plan_report.get("llm_calls", 0)},
                    "segments": rows,
                    "totals": {"segments": len(rows), "chars": sum(r["chars"] for r in rows), "reused": n_local,
                               "cache_hits": n_cache, "synthesized": n_synth, "retries": n_retry,
                               "duration_sec": qa["duration_sec"]}}
        mf = atomic_write_json(ctx.stage_dir / "tts_manifest.json", manifest)
        return StageResult(
            [ctx.draft(master, "audio_master", duration_sec=qa["duration_sec"]), ctx.draft(mf, "tts_manifest"),
             ctx.draft(tl_file, "audio_timeline", segments=len(timeline["segments"])),
             ctx.draft(sp_file, "speech_plan", mode=splan["mode"], groups=len(segments))],
            {"segments": len(rows), "reused_chunks": n_local + n_cache, "cache_hits": n_cache, "synthesized": n_synth,
             "segment_retries": n_retry, "duration_sec": qa["duration_sec"], "engine": flat["engine"],
             "planner": plan_report["used"], "prosody": splan["mode"], "prosody_warnings": len(splan["qc"]["warnings"]) + len(audio_qc["warnings"]),
             "llm_calls": plan_report.get("llm_calls", 0)})

    @staticmethod
    def _key_text(seg: dict) -> str:
        """Văn bản dùng cho cache key: thêm ngữ cảnh/break CHỈ khi chúng thật sự đi vào lời gọi engine (engine hỗ trợ), vì khi đó chúng đổi âm thanh."""
        extra = {k: seg[k] for k in ("breaks", "context") if seg.get(k)}
        return seg["text"] + ("\x1f" + json.dumps(extra, sort_keys=True, ensure_ascii=False) if extra else "")

    # -- timeline ranh giới (cho cắt part TikTok ở Phase 4) -------------------------------------------
    @staticmethod
    def _timeline(segments: list[Segment], rows: list[dict], joined: dict, flat: dict) -> dict:
        """Mỗi segment: thời điểm trong narration, điểm cắt (giữa khoảng nghỉ) và LOẠI ranh giới sau nó:
        paragraph (pause cấp đoạn) > sentence (segment kết thúc bằng dấu câu) > cut (cắt giữa câu, tránh dùng làm điểm chia part)."""
        pm = flat["pause_ms"]
        tl = joined.get("timeline")
        if not tl:                                          # processor không cung cấp: tự tính từ độ dài chunk + pause
            tl, t = [], 0.0
            for r, s in zip(rows, segments):
                end = t + float(r["duration_sec"])
                gap = s["pause_after_ms"] / 1000
                tl.append({"index": s["index"], "start_sec": round(t, 4), "end_sec": round(end, 4), "gap_after_sec": gap,
                           "cut_sec": round(end + gap / 2, 4)})
                t = end + gap
        out = []
        for i, (e, s) in enumerate(zip(tl, segments)):
            p = s["pause_after_ms"]
            if i == len(segments) - 1:
                kind = "end"
            elif pm["paragraph"] > 0 and p >= pm["paragraph"]:
                kind = "paragraph"
            else:
                kind = "sentence" if _TERMINAL.search(s["text"]) else "cut"
            out.append({**e, "index": s["index"], "kind": kind, "chars": len(s["text"])})
        return {"schema": 1, "duration_sec": joined.get("duration_sec"), "segments": out}

    # -- cache ----------------------------------------------------------------------------------------
    @staticmethod
    def _valid_meta(audio: Path, meta_file: Path, key: str) -> dict | None:
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta["key"] == key and audio.is_file() and sha256_file(audio) == meta["sha256"]:
                return meta
        except (OSError, ValueError, KeyError):
            pass
        return None

    def _local_hit(self, out: Path, side: Path, key: str) -> dict | None:
        return self._valid_meta(out, side, key)

    def _cache_hit(self, cache_dir: Path, key: str) -> tuple[Path, dict] | None:
        d = cache_dir / key[:2]
        meta = self._valid_meta(d / f"{key}.wav", d / f"{key}.json", key)
        if meta:
            try:
                os.utime(d / f"{key}.wav")                        # đánh dấu vừa dùng: Auto Cleanup xóa file dùng lâu nhất trước (LRU theo mtime)
            except OSError:
                pass
        return (d / f"{key}.wav", meta) if meta else None

    @staticmethod
    def _cache_put(cache_dir: Path, key: str, src: Path, meta: dict) -> None:
        d = cache_dir / key[:2]
        try:
            _link_or_copy(src, d / f"{key}.wav")
            atomic_write_json(d / f"{key}.json", meta)
        except OSError:
            pass                                            # cache chỉ là tối ưu: không bao giờ làm hỏng job (vd hai job ghi cùng khóa cùng lúc)
