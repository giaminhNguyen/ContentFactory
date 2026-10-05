"""Hợp đồng giữa orchestrator và các module (xem docs/MODULE_CONTRACTS.md).

Đây là file DUY NHẤT mà các package module (source, story, tts, audio, render,
publish, output, adapters) được phép import từ contentfactory (cùng fsutil).
Module không import nhau; orchestrator nối chúng bằng cách tiêm adapter vào stage handler.

Quy ước:
- Adapter nhận/trả `Path` nằm trong workspace của job. Artifact (sha256, size) chỉ được
  "niêm phong" bởi orchestrator tại checkpoint của stage.
- Adapter PHẢI ghi output atomic (ghi tạm rồi rename; xem fsutil): path tồn tại <=> file hoàn chỉnh.
  Handler dựa vào đó để dùng lại output có sẵn khi resume.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol, TypedDict


class ErrorClass(str, Enum):
    TRANSIENT = "TRANSIENT"    # tự retry có backoff
    RESOURCE = "RESOURCE"      # tài nguyên tạm thời thiếu (mạng, quota, token, đĩa, GPU/runtime): job bị GIỮ (PAUSED_*)
    POLICY = "POLICY"          # input/config sai: không retry mù
    AUTH = "AUTH"              # credential thiếu/hết hạn/thu hồi: PAUSED_CREDENTIAL
    AMBIGUOUS = "AMBIGUOUS"    # không rõ đã hoàn tất chưa (vd upload): cần kiểm tra
    CANCELLED = "CANCELLED"    # dừng có chủ đích (shutdown), không tính là lỗi


class StageError(Exception):
    """Lỗi của một stage. `resource` cho orchestrator biết đây là lỗi TÀI NGUYÊN tạm thời (và loại nào) để GIỮ job
    thay vì cho thất bại: network | provider | quota | token | disk | runtime | credential | input.
    `retry_after_s`: Retry-After của provider (giây). `resume_after`: thời điểm reset đã biết (epoch)."""

    def __init__(self, error_class: ErrorClass, code: str, message: str = "", detail: dict | None = None, *,
                 resource: str | None = None, retry_after_s: float | None = None, resume_after: float | None = None):
        super().__init__(f"{error_class.value}:{code}: {message}")
        self.error_class, self.code, self.message, self.detail = error_class, code, message, detail or {}
        self.resource, self.retry_after_s, self.resume_after = resource, retry_after_s, resume_after

    def to_dict(self) -> dict:
        return {"error_class": self.error_class.value, "code": self.code, "message": self.message,
                "detail": self.detail, "resource": self.resource, "retry_after_s": self.retry_after_s,
                "resume_after": self.resume_after}


class CancelToken:
    def __init__(self) -> None:
        self._e = threading.Event()

    def set(self) -> None:
        self._e.set()

    def is_set(self) -> bool:
        return self._e.is_set()

    def wait(self, timeout: float) -> bool:
        """Ngủ tối đa `timeout` giây; raise CANCELLED nếu bị huỷ trong lúc chờ."""
        if self._e.wait(timeout):
            raise StageError(ErrorClass.CANCELLED, "CANCELLED", "stage cancelled")
        return False

    def check(self) -> None:
        if self._e.is_set():
            raise StageError(ErrorClass.CANCELLED, "CANCELLED", "stage cancelled")


class ArtifactRef(TypedDict):
    path: str      # tương đối so với workspace của job, dấu '/'
    kind: str
    sha256: str
    bytes: int
    meta: dict


@dataclass
class ArtifactDraft:
    """Artifact do handler khai báo; orchestrator hash + ghi DB khi checkpoint."""
    path: str
    kind: str
    meta: dict = field(default_factory=dict)


@dataclass
class StageResult:
    artifacts: list[ArtifactDraft]
    data: dict = field(default_factory=dict)   # thống kê ngắn, vào manifest


@dataclass
class StageContext:
    job_id: str
    stage: str
    attempt: int
    stage_key: str
    workspace: Path
    stage_dir: Path
    params: dict
    inputs: dict[str, list[ArtifactRef]]       # chỉ gồm kind mà stage khai báo `requires`
    config: dict
    cancel: CancelToken
    log: Callable[..., None]                   # log(event, level="info", **fields)
    # progress(done, total=None, detail="", **extra): ghi checkpoint (tiến độ) của stage vào DB để resume/hiển thị
    progress: Callable[..., None] = field(default=lambda *a, **k: None)

    def path(self, ref: ArtifactRef) -> Path:
        return self.workspace / ref["path"]

    def one(self, kind: str) -> Path:
        return self.path(self.inputs[kind][0])

    def read_json(self, kind: str) -> dict:
        return json.loads(self.one(kind).read_text(encoding="utf-8"))

    def draft(self, path: Path, kind: str, **meta: Any) -> ArtifactDraft:
        return ArtifactDraft(Path(path).resolve().relative_to(self.workspace.resolve()).as_posix(), kind, meta)


# ---- Resource Monitor -----------------------------------------------------------------------
class ResourceStatus(TypedDict):
    resource: str                  # network | disk | runtime | credential | quota | token | ...
    ok: bool
    detail: str
    checked_at: float
    next_check_at: float
    retry_after: float | None


class ResourceProbe(Protocol):
    """Kiểm tra DETERMINISTIC (không LLM, có timeout) một loại tài nguyên."""
    resource: str

    def check(self) -> tuple[bool, str]: ...      # (ok, chi tiết)


# ---- Source ---------------------------------------------------------------------------------
class _SourceInputRequired(TypedDict):
    kind: str          # youtube_url | transcript_file | text
    value: str         # URL, đường dẫn file phụ đề, hoặc chính văn bản


class SourceInput(_SourceInputRequired, total=False):
    title: str         # gợi ý tiêu đề (file local / văn bản)
    language: str      # ngôn ngữ của nguồn nếu biết


class SourceResult(TypedDict, total=False):
    """Kết quả THU THẬP phụ đề (chưa xử lý transcript). Provider nào cũng trả cùng dạng này."""
    source_url: str
    source_type: str            # youtube | local_subtitle | plain_text
    provider: str               # supervip | ytdlp | local | text | ...
    video_id: str | None
    title: str | None
    description: str | None     # mô tả CỦA NGUỒN; không dùng làm mô tả video của ta
    language: str | None        # ngôn ngữ của phụ đề/văn bản nguồn
    raw_subtitle_path: Path     # phụ đề gốc đúng như provider trả về (nguyên byte)
    subtitle_format: str        # srt | vtt | json (snippet start/duration/text) | txt
    subtitle_kind: str          # manual | auto | translated | unknown
    has_timestamps: bool
    metadata: dict              # title, channel, duration, upload_date, ... (tùy provider)
    status: str                 # ok | error
    error: dict | None          # StageError.to_dict() nếu status == error
    attempts: list[dict]        # nhật ký các provider đã thử (chẩn đoán)
    origin: str                 # network | cache | job


class SourceProvider(Protocol):
    """Một cách thu thập phụ đề cụ thể. Raise StageError khi thất bại."""
    name: str

    def supports(self, kind: str) -> bool: ...
    def available(self) -> bool: ...
    def acquire(self, src: SourceInput, work_dir: Path, ctx: StageContext, prefs: dict) -> SourceResult: ...
    def describe(self, src: SourceInput, ctx: StageContext) -> dict: ...   # metadata bổ sung (title, description...) hoặc {}
    def health(self) -> dict: ...


class SourceAdapter(Protocol):
    """Điểm vào của stage Source: chọn provider, fallback, cache; KHÔNG xử lý transcript."""
    def acquire(self, src: SourceInput, out_dir: Path, ctx: StageContext) -> SourceResult: ...
    def health(self) -> dict: ...


# ---- Story ----------------------------------------------------------------------------------
class SourceBundle(TypedDict):
    title: str
    language: str            # ngôn ngữ ĐÍCH của truyện
    source_language: str     # ngôn ngữ của transcript nguồn
    transcript: Path


class StoryResult(TypedDict):
    sections: list[Path]   # các section/chương nội bộ theo thứ tự; Story Assembler (stage) dựng story.txt từ đây
    stats: dict


class StoryAdapter(Protocol):
    def generate(self, bundle: SourceBundle, profile: dict, out_dir: Path, ctx: StageContext) -> StoryResult: ...
    def health(self) -> dict: ...


# ---- TTS ------------------------------------------------------------------------------------
class Segment(TypedDict):
    index: int
    text: str
    pause_after_ms: int


class ChunkResult(TypedDict):
    index: int
    duration_sec: float


class TTSAdapter(Protocol):
    engine_id: str

    def capabilities(self) -> dict: ...
    def synthesize(self, segment: Segment, profile: dict, out_path: Path, ctx: StageContext) -> ChunkResult: ...
    def health(self) -> dict: ...


# ---- Audio ----------------------------------------------------------------------------------
class AudioQAReport(TypedDict):
    ok: bool
    duration_sec: float
    issues: list[str]


class AudioProcessor(Protocol):
    def qa(self, audio: Path) -> AudioQAReport: ...
    def assemble(self, chunks: list[Path], pauses_ms: list[int], out: Path, ctx: StageContext) -> dict: ...
    def build_youtube_audio(self, master: Path, watermark: Path | None, out: Path, ctx: StageContext) -> dict: ...
    def build_tiktok_parts(self, master: Path, speed: float, target_part_sec: float,
                           out_dir: Path, ctx: StageContext) -> list[Path]: ...
    def health(self) -> dict: ...


# ---- Render ---------------------------------------------------------------------------------
class RenderRequest(TypedDict):
    audio: Path
    profile: dict      # id, aspect_ratio, resolution, source_pool, ...
    output: Path


class ThumbnailRequest(TypedDict):
    title: str
    channel_name: str
    output: Path


class RenderAdapter(Protocol):
    def render_video(self, req: RenderRequest, ctx: StageContext) -> dict: ...
    def render_thumbnail(self, req: ThumbnailRequest, ctx: StageContext) -> Path: ...
    def health(self) -> dict: ...


# ---- Publish (nền tảng, hiện chỉ YouTube) -----------------------------------------------------
class PublishRequest(TypedDict):
    platform: str
    video: Path
    thumbnail: Path | None
    title: str
    description: str
    tags: list[str]
    privacy: str
    made_for_kids: bool        # BẮT BUỘC, không default (yt_uploader cũng vậy)
    account_id: str | None
    idempotency_key: str


class PublishResult(TypedDict, total=False):
    state: str                 # completed | failed | pending
    remote_id: str | None
    remote_url: str | None
    error: dict | None         # StageError.to_dict()
    warnings: list[str]


class PublishAdapter(Protocol):
    platform: str

    def publish(self, req: PublishRequest, ctx: StageContext) -> PublishResult: ...
    def health(self) -> dict: ...


# ---- Output (gói cho người dùng) --------------------------------------------------------------
class OutputRequest(TypedDict):
    job_id: str
    title: str
    description: str
    language: str
    output_root: Path
    story: Path
    youtube_video: Path
    youtube_thumbnail: Path
    tiktok_parts: list[Path]


class OutputPackage(TypedDict):
    project_dir: str
    files: list[str]


class OutputPublisher(Protocol):
    def publish(self, req: OutputRequest, ctx: StageContext) -> OutputPackage: ...
