"""Stage table + state machine. Nguồn sự thật duy nhất về thứ tự pipeline.

Mỗi stage = (queue_state -> running_state -> done_state). done_state của stage này là
queue_state của stage kế tiếp, nên "X_READY" nghĩa là "xong bước trước, đang xếp hàng cho bước sau".
Job đi tuyến tính trong một job; song song là giữa các job (render GPU = 1, DECISIONS D-15/D-16).
"""
from __future__ import annotations

from dataclasses import dataclass

NEW = "NEW"
SOURCE_PROCESSING, SOURCE_READY = "SOURCE_PROCESSING", "SOURCE_READY"
STORY_RUNNING, STORY_READY = "STORY_RUNNING", "STORY_READY"
TTS_RUNNING, AUDIO_READY = "TTS_RUNNING", "AUDIO_READY"
AUDIO_PROCESSING = "AUDIO_PROCESSING"                       # thêm so với danh sách tối thiểu
YOUTUBE_RENDER_READY, YOUTUBE_RENDERING = "YOUTUBE_RENDER_READY", "YOUTUBE_RENDERING"
TIKTOK_RENDER_READY, TIKTOK_RENDERING = "TIKTOK_RENDER_READY", "TIKTOK_RENDERING"
OUTPUT_READY, OUTPUT_PUBLISHING = "OUTPUT_READY", "OUTPUT_PUBLISHING"   # OUTPUT_PUBLISHING: thêm
UPLOAD_READY, UPLOADING = "UPLOAD_READY", "UPLOADING"                   # UPLOADING: thêm (HANDOFF §15)
PUBLISHED = "PUBLISHED"
FAILED = "FAILED"


@dataclass(frozen=True)
class Stage:
    name: str
    queue_state: str
    running_state: str
    done_state: str
    requires: tuple[str, ...]      # kind artifact đầu vào (từ stage trước)
    produces: tuple[str, ...]      # kind artifact bắt buộc phải có khi xong
    workdir: str                   # thư mục con trong workspace của job
    adapters: tuple[str, ...]      # adapter được tiêm vào handler
    resource: str = ""             # tài nguyên dùng chung giới hạn đồng thời; mặc định = name


STAGES: tuple[Stage, ...] = (
    Stage("source", NEW, SOURCE_PROCESSING, SOURCE_READY,
          (), ("transcript", "metadata"), "source", ("source",)),
    Stage("story", SOURCE_READY, STORY_RUNNING, STORY_READY,
          ("transcript", "metadata"), ("story_text",), "story", ("story",)),
    Stage("tts", STORY_READY, TTS_RUNNING, AUDIO_READY,
          ("story_text",), ("audio_master",), "tts", ("tts", "audio")),
    Stage("audio", AUDIO_READY, AUDIO_PROCESSING, YOUTUBE_RENDER_READY,
          ("audio_master",), ("audio_youtube", "audio_tiktok"), "audio", ("audio",)),
    Stage("render_youtube", YOUTUBE_RENDER_READY, YOUTUBE_RENDERING, TIKTOK_RENDER_READY,
          ("audio_youtube", "metadata"), ("video_youtube", "thumbnail"), "render/youtube", ("render",), "gpu"),
    Stage("render_tiktok", TIKTOK_RENDER_READY, TIKTOK_RENDERING, OUTPUT_READY,
          ("audio_tiktok",), ("video_tiktok",), "render/tiktok", ("render",), "gpu"),
    Stage("output", OUTPUT_READY, OUTPUT_PUBLISHING, UPLOAD_READY,
          ("story_text", "metadata", "video_youtube", "thumbnail", "video_tiktok"),
          ("output_package",), "output", ("output",)),
    Stage("publish", UPLOAD_READY, UPLOADING, PUBLISHED,
          ("video_youtube", "thumbnail", "metadata", "story_text"),
          ("publish_result",), "publish", ("publish",)),
)

BY_NAME = {s.name: s for s in STAGES}
BY_QUEUE = {s.queue_state: s for s in STAGES}
BY_RUNNING = {s.running_state: s for s in STAGES}
TERMINAL = frozenset({PUBLISHED, FAILED})
ALL_STATES = tuple([NEW] + [x for s in STAGES for x in (s.running_state, s.done_state)] + [FAILED])


def resource_of(stage: Stage) -> str:
    return stage.resource or stage.name


def allowed(src: str, dst: str) -> bool:
    """Chuyển trạng thái hợp lệ: queue->running, running->done, running->queue (retry/ngắt),
    running->FAILED, FAILED->queue_state của stage lỗi (retry thủ công)."""
    if src in BY_QUEUE and dst == BY_QUEUE[src].running_state:
        return True
    if src in BY_RUNNING:
        s = BY_RUNNING[src]
        return dst in (s.done_state, s.queue_state, FAILED)
    if src == FAILED:
        return dst in BY_QUEUE
    return False


def _check_chain() -> None:
    assert STAGES[0].queue_state == NEW and STAGES[-1].done_state == PUBLISHED
    for a, b in zip(STAGES, STAGES[1:]):
        assert a.done_state == b.queue_state, (a.name, b.name)


_check_chain()
