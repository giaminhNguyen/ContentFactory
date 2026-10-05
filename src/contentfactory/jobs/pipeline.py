"""Stage table + state machine. Nguồn sự thật duy nhất về thứ tự pipeline.

Mỗi stage = (queue_state -> running_state -> done_state). done_state của stage này là
queue_state của stage kế tiếp, nên "X_READY" nghĩa là "xong bước trước, đang xếp hàng cho bước sau".
Thứ tự stage là tuyến tính (D-16) nhưng một job KHÔNG bắt buộc chạy hết chuỗi: job có `start_stage` và
`target_stage` (D-36); stage có output hợp lệ thì bị skip.

Contract của một stage (HANDOFF §15A, MODULE_CONTRACTS §11):
  required_inputs  = requires        produced_outputs = produces
  can_run / validate_inputs / validate_outputs / skip: xem orchestrator/stages.py (cần validator theo kind)
  retry  : RetryPolicy theo lớp lỗi (jobs/policy.py)
  resume : handler idempotent + checkpoint (`checkpoint` mô tả điểm resume, tiến độ ghi bằng ctx.progress)
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
FAILED_PERMANENT = FAILED       # D-37: cùng một trạng thái; tên FAILED giữ để tương thích

# Lý do GIỮ job (hold) do tài nguyên tạm thời. Hold không đổi `state` (vị trí pipeline), chỉ chặn runner nhận job.
PAUSED_NETWORK = "PAUSED_NETWORK"
PAUSED_TOKEN = "PAUSED_TOKEN"
PAUSED_QUOTA = "PAUSED_QUOTA"
PAUSED_DISK = "PAUSED_DISK"
PAUSED_RESOURCE = "PAUSED_RESOURCE"
PAUSED_CREDENTIAL = "PAUSED_CREDENTIAL"
PAUSED_MISSING_INPUT = "PAUSED_MISSING_INPUT"
HOLD_REASONS = (PAUSED_NETWORK, PAUSED_TOKEN, PAUSED_QUOTA, PAUSED_DISK, PAUSED_RESOURCE, PAUSED_CREDENTIAL,
                PAUSED_MISSING_INPUT)
TIME_BASED_HOLDS = frozenset({PAUSED_TOKEN, PAUSED_QUOTA})   # hồi phục theo thời điểm reset, không có thứ gì để "đo"


@dataclass(frozen=True)
class Stage:
    name: str
    queue_state: str
    running_state: str
    done_state: str
    requires: tuple[str, ...]      # kind artifact đầu vào (required_inputs)
    produces: tuple[str, ...]      # kind artifact bắt buộc phải có khi xong (produced_outputs)
    workdir: str                   # thư mục con trong workspace của job
    adapters: tuple[str, ...]      # adapter được tiêm vào handler
    resource: str = ""             # tài nguyên dùng chung giới hạn đồng thời; mặc định = name
    checkpoint: str = ""           # điểm resume của stage (mô tả; tiến độ ghi bằng ctx.progress)
    # Chỉ các khóa này của job.params ảnh hưởng stage_key (D-48). None = toàn bộ (kiểu cũ). Có AST test canh khai báo đủ.
    params_deps: tuple[str, ...] | None = None
    # Các khóa của config snapshot (ngữ nghĩa) ảnh hưởng stage_key; thêm adapters[<tên adapter của stage>] tự động.
    config_deps: tuple[str, ...] = ()
    # Stage tạo sản phẩm cuối mà người dùng cần (video, output package, publish): luôn chạy khi nằm trong [start, target],
    # dù không stage nào khác tiêu thụ output của nó.
    deliverable: bool = False

    @property
    def required_inputs(self) -> tuple[str, ...]:
        return self.requires

    @property
    def produced_outputs(self) -> tuple[str, ...]:
        return self.produces


STAGES: tuple[Stage, ...] = (
    Stage("source", NEW, SOURCE_PROCESSING, SOURCE_READY,
          (), ("subtitle_raw", "transcript_structured", "transcript", "metadata"), "source", ("source",),
          checkpoint="steps (tải phụ đề -> parse -> dựng câu)",
          params_deps=("input", "source_languages", "title", "language", "fake"),
          config_deps=("source", "supervip", "youtube")),
    Stage("story", SOURCE_READY, STORY_RUNNING, STORY_READY,
          ("transcript", "metadata"), ("story_text", "story_report"), "story", ("story",),
          checkpoint="sections/chương chưa commit",
          params_deps=("story_profile", "language", "fake"), config_deps=("story_branch",)),
    Stage("tts", STORY_READY, TTS_RUNNING, AUDIO_READY,
          ("story_text",), ("audio_master",), "tts", ("tts", "audio"),
          checkpoint="segments/chunks chưa hoàn thành", params_deps=("tts", "fake")),
    Stage("audio", AUDIO_READY, AUDIO_PROCESSING, YOUTUBE_RENDER_READY,
          ("audio_master",), ("audio_youtube", "audio_tiktok"), "audio", ("audio",),
          checkpoint="bản phân phối (YouTube, từng part TikTok)", params_deps=("watermark", "tiktok", "fake")),
    Stage("render_youtube", YOUTUBE_RENDER_READY, YOUTUBE_RENDERING, TIKTOK_RENDER_READY,
          ("audio_youtube", "metadata"), ("video_youtube", "thumbnail"), "render/youtube", ("render",), "gpu",
          checkpoint="video, thumbnail", params_deps=("render", "channel", "fake"), deliverable=True),
    Stage("render_tiktok", TIKTOK_RENDER_READY, TIKTOK_RENDERING, OUTPUT_READY,
          ("audio_tiktok",), ("video_tiktok",), "render/tiktok", ("render",), "gpu",
          checkpoint="từng part TikTok", params_deps=("render", "fake"), deliverable=True),
    Stage("output", OUTPUT_READY, OUTPUT_PUBLISHING, UPLOAD_READY,
          ("story_text", "metadata", "video_youtube", "thumbnail", "video_tiktok"),
          ("output_package",), "output", ("output",),
          checkpoint="gói output (dựng rồi mới rename)", params_deps=("language", "fake"), config_deps=("output",),
          deliverable=True),
    Stage("publish", UPLOAD_READY, UPLOADING, PUBLISHED,
          ("video_youtube", "thumbnail", "metadata", "story_text"),
          ("publish_result",), "publish", ("publish",),
          checkpoint="upload (retry không render lại)",
          params_deps=("made_for_kids", "tags", "privacy", "account_id", "fake"), deliverable=True),
)

BY_NAME = {s.name: s for s in STAGES}
BY_QUEUE = {s.queue_state: s for s in STAGES}
BY_RUNNING = {s.running_state: s for s in STAGES}
INDEX = {s.name: i for i, s in enumerate(STAGES)}
TERMINAL = frozenset({PUBLISHED, FAILED})
ALL_STATES = tuple([NEW] + [x for s in STAGES for x in (s.running_state, s.done_state)] + [FAILED])
CONSUMED_KINDS = frozenset(k for s in STAGES for k in s.requires)    # kind mà stage khác cần
INDEXED_KINDS = frozenset({"audio_tiktok", "video_tiktok"})          # nhiều phần, thứ tự trong meta["index"]

# Use case có tên (HANDOFF §15A): (start_stage, target_stage); None = mặc định (tự tính / publish).
MODES: dict[str, tuple[str | None, str | None]] = {
    "FULL": (None, None),
    "SUBTITLE_ONLY": (None, "source"),
    "STORY_ONLY": (None, "story"),
    "THROUGH_TTS": (None, "tts"),
    "TTS_ONLY": ("tts", "tts"),
    "VIDEO_ONLY": (None, "render_tiktok"),
}


def resource_of(stage: Stage) -> str:
    return stage.resource or stage.name


def position(state: str) -> int | None:
    """Chỉ số stage mà `state` thuộc về (queue hoặc running); PUBLISHED = len(STAGES); FAILED = None."""
    if state in BY_QUEUE:
        return INDEX[BY_QUEUE[state].name]
    if state in BY_RUNNING:
        return INDEX[BY_RUNNING[state].name]
    if state == PUBLISHED:
        return len(STAGES)
    return None


def is_complete(state: str, target_idx: int | None) -> bool:
    """Job đạt đích: ở done_state của target_stage (hoặc PUBLISHED khi chạy đủ)."""
    if state == PUBLISHED:
        return True
    pos = position(state)
    return target_idx is not None and pos is not None and pos > target_idx


def allowed(src: str, dst: str) -> bool:
    """Chuyển trạng thái hợp lệ: queue->running, running->done, running->queue (retry/ngắt/hold),
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
    for i, s in enumerate(STAGES):                    # mọi kind được yêu cầu phải do stage ĐỨNG TRƯỚC sản sinh
        assert set(s.requires) <= {k for t in STAGES[:i] for k in t.produces}, s.name


_check_chain()
