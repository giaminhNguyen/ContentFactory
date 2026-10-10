"""Prosody Engine (D-100): Story -> Normalizer -> Segmenter -> Prosody Analyzer -> Speech Plan -> TTS -> stitcher -> QC.

Độc lập với adapter TTS: engine chịu trách nhiệm giọng/phát âm/ngữ điệu nội bộ; ContentFactory chịu trách nhiệm tách câu, nhóm tổng hợp,
khoảng nghỉ và QC. Mặc định tất định, KHÔNG dùng LLM."""
from .plan import SEMANTIC_LABELS, boundary_key, build_speech_plan, check_integrity, legacy_plan, plan_key, segments_of, sentence_texts
from .profiles import PROFILES, RULES_VERSION, describe, resolve_prosody
from .qc import analyze_audio, analyze_plan
from .semantic import LLMSemanticLabeler, semantic_for
from .sample import PREVIEW_TEXT
from .segmenter import SCENE_MARK, mark_scenes, paragraphs, split_long, split_sentences, strip_marks

__all__ = ["PREVIEW_TEXT", "SEMANTIC_LABELS", "boundary_key", "build_speech_plan", "check_integrity", "legacy_plan", "plan_key", "segments_of", "sentence_texts", "LLMSemanticLabeler", "semantic_for", "PROFILES", "RULES_VERSION", "describe",
           "resolve_prosody", "analyze_audio", "analyze_plan", "SCENE_MARK", "mark_scenes", "paragraphs", "split_long", "split_sentences", "strip_marks"]
