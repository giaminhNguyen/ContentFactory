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
import shutil
import time
from pathlib import Path

from ..contracts import AudioProcessor, ErrorClass, Segment, StageContext, StageError, StageResult, TTSAdapter
from ..fsutil import atomic_write, atomic_write_json, sha256_file
from . import qa as chunk_qa
from .normalize import normalize_text
from .planner import RuleSegmentPlanner, SegmentPlanner, validate_plan
from .schema import cache_identity, normalize_capabilities, resolve, segment_key, stable_hash

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

    # -- toàn stage -----------------------------------------------------------------------------------
    def run(self, ctx: StageContext, text: str, raw_profile: dict | None) -> StageResult:
        caps = normalize_capabilities(self.tts.capabilities())
        flat = resolve(raw_profile, caps, ctx.params.get("language"), getattr(self.tts, "engine_id", None))
        text, nrep = normalize_text(text, flat["normalize"])
        if not text:
            raise StageError(ErrorClass.POLICY, "EMPTY_STORY", "story.txt không có nội dung sau chuẩn hóa")
        segments, plan_report = self.plan(ctx, text, flat)
        ident = cache_identity(flat, caps)
        cache_dir = Path(ctx.config["tts_cache_dir"]) if ctx.config.get("tts_cache_dir") else None
        chunk_dir = ctx.stage_dir / "chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunks: list[Path] = []
        rows: list[dict] = []
        n_local = n_cache = n_synth = n_retry = 0
        for seg in segments:
            ctx.cancel.check()
            idx, key = seg["index"], segment_key(seg["text"], ident)
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
        master = ctx.workspace / "audio" / "master.wav"
        atomic_write(master, lambda tmp: self.audio.assemble(chunks, [s["pause_after_ms"] for s in segments], tmp, ctx))
        qa = self.audio.qa(master)
        if not qa["ok"]:
            raise StageError(ErrorClass.TRANSIENT, "MASTER_QA_FAILED", ",".join(qa["issues"]))
        manifest = {"schema": 1, "engine": flat["engine"], "engine_version": caps["engine_version"],
                    "language": flat["language"], "voice": flat["voice"], "model": flat["model"],
                    "profile_version": flat["profile_version"], "profile_hash": stable_hash(flat)[:16],
                    "identity_hash": stable_hash(ident)[:16], "normalize": nrep, "planner": plan_report,
                    "segments": rows,
                    "totals": {"segments": len(rows), "chars": sum(r["chars"] for r in rows), "reused": n_local,
                               "cache_hits": n_cache, "synthesized": n_synth, "retries": n_retry,
                               "duration_sec": qa["duration_sec"]}}
        mf = atomic_write_json(ctx.stage_dir / "tts_manifest.json", manifest)
        return StageResult(
            [ctx.draft(master, "audio_master", duration_sec=qa["duration_sec"]), ctx.draft(mf, "tts_manifest")],
            {"segments": len(rows), "reused_chunks": n_local + n_cache, "cache_hits": n_cache, "synthesized": n_synth,
             "segment_retries": n_retry, "duration_sec": qa["duration_sec"], "engine": flat["engine"],
             "planner": plan_report["used"]})

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
        return (d / f"{key}.wav", meta) if meta else None

    @staticmethod
    def _cache_put(cache_dir: Path, key: str, src: Path, meta: dict) -> None:
        d = cache_dir / key[:2]
        _link_or_copy(src, d / f"{key}.wav")
        atomic_write_json(d / f"{key}.json", meta)
