# MODULE_CONTRACTS

> Hợp đồng giữa orchestrator và các module. Neo vào HANDOFF (§3 artifact + manifest + state, §20) và `CURRENT_SYSTEM_AUDIT.md`.
> Ngôn ngữ đặc tả: chữ ký kiểu Python (`typing.Protocol`) để đọc dễ; **hợp đồng thật là artifact trên đĩa + manifest JSON**, không phải lời gọi hàm. Ngôn ngữ cài đặt orchestrator chưa chốt (xem `DECISIONS.md` D-02).

## 0. Quy ước chung

### 0.1 Workspace và artifact

```text
workspace/job_<id>/
  source/    story/    tts/    audio/    render/    temp/
  manifest.json
```

- Mọi module **chỉ đọc/ghi trong workspace của job** (và đọc pool/channel asset được cấu hình). Không module nào đọc `output/`.
- Module nhận/trả `ArtifactRef`; artifact ghi **atomic** (`*.part` → rename).

```python
class ArtifactRef(TypedDict):
    path: str          # tương đối so với workspace job
    kind: str          # "story_text" | "audio_master" | "video_mp4" | "image" | "json" ...
    sha256: str
    bytes: int
    meta: dict         # duration_sec, width, height, ...
```

### 0.2 Lỗi

Dùng một phân lớp thống nhất, ánh xạ từ `ContentFlow.VideoError` (RESOURCE/POLICY/TRANSIENT/CANCELLED) và `yt_uploader.error_class`:

```python
class ErrorClass(Enum):
    TRANSIENT   # tự retry với backoff (mạng, ffmpeg crash, 5xx, 429)
    RESOURCE    # môi trường thiếu (ffmpeg, GPU, đầy đĩa) -> doctor/chờ, retry sau khi sửa
    POLICY      # input/config sai -> KHÔNG retry, cần sửa
    AUTH        # credential hết hạn/thu hồi -> cần người
    AMBIGUOUS   # không rõ đã hoàn tất chưa (vd upload) -> cần kiểm tra, không retry mù
    CANCELLED

class StageError(Exception):
    error_class: ErrorClass; code: str; message: str; detail: dict
```

### 0.3 Idempotency và cache

Mỗi lời gọi stage có `stage_key = sha256(canonical_json(inputs_sha256 + params + profile_version))`. Cùng `stage_key` + artifact đã tồn tại và hợp lệ ⇒ **bỏ qua, trả artifact cũ**. Dùng làm `idempotency_key` cho ContentFlow worker và yt_uploader. Đổi watermark/video/thumbnail **không** đổi `stage_key` của TTS (HANDOFF §6.7, §10).

### 0.4 Cancel, deadline, progress

```python
class StageContext:
    job_id: str
    workspace: Path
    cancel: CancelToken          # kiểm tra định kỳ
    deadline_s: float | None
    on_progress: Callable[[float, str], None]   # 0..1, thông điệp
    on_warning: Callable[[str], None]
    secrets: SecretProvider      # .env / secret store, KHÔNG nằm trong profile (HANDOFF §8)
```

### 0.5 Health

Mỗi adapter có `health() -> HealthReport{ok, details, fix_hint}` để `doctor.ps1` gọi (HANDOFF §19).

---

## 1. SourceProcessor

**Trách nhiệm:** biến *input người dùng* (URL YouTube, file transcript, text…) thành `source_bundle` chuẩn hóa cho Story. **Hiện không có trong 3 project (A2) ⇒ module mới.**

```python
class SourceInput(TypedDict):
    kind: Literal["youtube_url", "transcript_file", "text", "local_folder"]
    value: str
    options: dict        # ngôn ngữ phụ đề, ...

class SourceBundle(TypedDict):
    source_id: str
    title: str
    language: str
    transcript: ArtifactRef        # source/transcript.txt, đã làm sạch
    metadata: ArtifactRef          # source/metadata.json (url, kênh, độ dài, ngày tải…)
    analysis: ArtifactRef | None   # source/analysis.json (nhân vật, chuỗi sự kiện, giọng điệu)

class SourceProcessor(Protocol):
    def process(self, src: SourceInput, ctx: StageContext) -> SourceBundle: ...
    def health(self) -> HealthReport: ...
```

- **Vào:** `SourceInput`. **Ra:** `source/transcript.txt`, `source/metadata.json`, tùy chọn `source/analysis.json`.
- **Lỗi:** URL không có phụ đề/transcript → `POLICY` (code `NO_TRANSCRIPT`); tải lỗi mạng → `TRANSIENT`.
- **Cache key:** URL/nội dung + options + version processor.
- **Triển khai:** mới (công cụ tải phụ đề chọn ở Phase 3). Phần "analysis" có thể là một lời gọi LLM; nếu StoryAdapter cần format riêng, `analysis.json` là điểm chuyển đổi.

## 2. StoryAdapter

**Trách nhiệm:** từ `SourceBundle` + `StoryProfile` ⇒ **một** `story.txt` liền mạch, **không** header `Chapter/Section/Part`, không marker kỹ thuật (HANDOFF §5). Gồm cả Story Assembler (A5): đây là một phần bắt buộc của adapter hoặc stage ngay sau nó.

```python
class StoryProfile(TypedDict):
    id: str; version: str
    language: str                  # PHẢI khai báo; xem DECISIONS D-04
    target_chars: int | None
    genre: str | None
    params: dict                   # tùy engine

class StoryResult(TypedDict):
    story: ArtifactRef             # story/story.txt (kind=story_text)
    sections: ArtifactRef | None   # story/sections/ (nội bộ, để debug; KHÔNG publish)
    continuity: ArtifactRef | None # story/continuity.json (báo cáo kiểm)
    stats: dict                    # chars, section_count, repeated_ngram_rate

class StoryAdapter(Protocol):
    def generate(self, bundle: SourceBundle, profile: StoryProfile, ctx: StageContext) -> StoryResult: ...
    def resume(self, ctx: StageContext) -> StoryResult: ...   # tiếp tục từ state trong workspace/story/
    def health(self) -> HealthReport: ...
```

- **Bất biến đầu ra (validator tất định chạy sau mọi implementation):** `story.txt` không chứa dòng khớp `^(第.+章|Chapter\s*\d+|Section\s*\d+|Part\s*\d+|Chương\s*\d+)`, không còn marker kỹ thuật (`<!--`, `[[`, `TODO`…), không rỗng, ngôn ngữ đúng `profile.language`, tỉ lệ lặp n-gram dưới ngưỡng.
- **Lỗi:** engine cần xác nhận người (gate tương tác không auto-trả lời được) → `POLICY`/`AMBIGUOUS` code `NEEDS_HUMAN`; hết quota LLM → `TRANSIENT`/`RESOURCE`.
- **Resume:** continuity nằm trong `workspace/story/` nên retry tiếp được ở section dang dở (HANDOFF §14: trong một branch, generation phải tuần tự theo state).
- **Triển khai ứng viên** (chưa chốt, xem DECISIONS D-03):
  - `OhStoryCliAdapter`: một workspace/job, `/story-setup` deploy, điều khiển `story-long-write` qua `claude -p`, tự trả lời gate, poll `追踪/`, rồi Assembler gộp `正文/*.md` bỏ dòng tiêu đề đầu. Tiếng Trung only (A4).
  - `DirectLLMStoryAdapter`: orchestrator tự gọi LLM theo section với state continuity riêng (có thể tham khảo `skills/story-long-write` làm tài liệu nguồn nhưng không phụ thuộc runtime của nó).
  - `FixtureStoryAdapter`: đọc `story.txt` có sẵn — dùng cho Phase 1 để dựng pipeline end-to-end không cần Story thật.

## 3. TTSAdapter

**Trách nhiệm:** biến text thành audio chunk với một engine cụ thể (HANDOFF §6). **Module mới hoàn toàn (A18).**

```python
class TTSProfile(TypedDict):
    engine: str; profile_version: str
    voice: str; language: str
    segment: {"preferred_chars": int, "max_chars": int, "min_chars": int}   # HANDOFF §6.3
    break_priority: list[str]
    pause_ms: {"paragraph": int, "sentence": int, "dialogue": int}
    settings: dict                  # tham số riêng engine (speed, model, sample_rate...)
    provenance: dict                # confidence/source cho từng rule (HANDOFF §7)

class Segment(TypedDict):
    index: int; text: str; pause_after_ms: int

class ChunkResult(TypedDict):
    index: int; audio: ArtifactRef | None; duration_sec: float; error: StageError | None

class TTSCapabilities(TypedDict):
    max_chars: int | None; languages: list[str]; speed: bool; ssml: bool
    streaming: bool; output_formats: list[str]; sample_rate: int; max_concurrency: int

class TTSAdapter(Protocol):
    engine_id: str
    def capabilities(self) -> TTSCapabilities: ...
    def synthesize(self, segment: Segment, profile: TTSProfile, ctx: StageContext) -> ChunkResult: ...
    def health(self) -> HealthReport: ...
```

- Adapter chỉ làm **một segment → một file audio**. Việc lập kế hoạch cắt, validator, retry từng chunk, cache, QA, ghép thuộc về *TTS Manager* (module điều phối nằm trên adapter, không phải adapter):

```text
story.txt -> Preprocess -> SegmentPlanner(AI, theo rule) -> RuleValidator(tất định)
          -> segments.json -> TTS Manager (retry từng chunk, cache) -> chunks/000001.wav
          -> Audio QA -> master_audio (qua AudioProcessor.assemble)
```

- **Cache key chunk:** `text + engine + model + voice + relevant settings + profile_version` (HANDOFF §6.7).
- **Lỗi:** quá `max_chars` → `POLICY` (lỗi planner/validator, không phải engine); 429/5xx → `TRANSIENT`; thiếu API key → `AUTH`.
- Skill `gen-audio-queue` (VieNeu-TTS) có sẵn trong môi trường người dùng có thể là tham chiếu cho một adapter đầu tiên — **chưa audit**, không thuộc 3 project.

## 4. AudioProcessor

**Trách nhiệm:** mọi thao tác audio tất định bằng ffmpeg: ghép chunk → master, tạo bản YouTube (watermark), bản TikTok (tăng tốc, cắt part). **Mới (A11, A12); ContentFlow không có các thao tác này.**

```python
class AudioQAReport(TypedDict):
    ok: bool; duration_sec: float; silent_ratio: float; issues: list[str]

class AudioProcessor(Protocol):
    def qa(self, audio: ArtifactRef) -> AudioQAReport: ...                       # HANDOFF §6.8
    def assemble(self, chunks: list[ChunkResult], pauses_ms: list[int], ctx) -> ArtifactRef: ...   # -> audio/master.wav
    def build_youtube_audio(self, master: ArtifactRef, watermark: ArtifactRef | None, ctx) -> ArtifactRef: ...
    def build_tiktok_parts(self, master: ArtifactRef, speed: float, target_part_sec: int,
                           ctx) -> list[ArtifactRef]: ...                         # audio/tiktok/part_01.wav ...
    def health(self) -> HealthReport: ...                                        # ffmpeg/ffprobe
```

- **Quy tắc:** `speed`, `target_part_sec` (mặc định 2.0 và 600) **đến từ config**, không hardcode (HANDOFF §11). Cắt part tại điểm im lặng gần ranh giới mục tiêu; part cuối có thể ngắn hơn; không tạo part < `min_last_part_sec` (cấu hình) mà gộp vào part trước.
- **Watermark** nằm ở `channels/<channel>/watermark.wav`; đổi watermark chỉ làm lại `build_youtube_audio` trở xuống (HANDOFF §10).
- **Lỗi:** thiếu ffmpeg → `RESOURCE`; file hỏng → `POLICY`; đầy đĩa → `RESOURCE`.
- **Triển khai:** wrapper ffmpeg/ffprobe của orchestrator (ffmpeg đã là điều kiện của ContentFlow nên không thêm phụ thuộc mới).

## 5. RenderAdapter

**Trách nhiệm:** nhận audio + source pool + profile ⇒ video MP4 / thumbnail; đồng bộ source pool. Bọc ContentFlow qua **worker subprocess** (không import trực tiếp).

```python
class RenderProfile(TypedDict):
    id: str                          # "youtube" | "tiktok" | ...
    aspect_ratio: str; resolution: str
    frame_path: str                  # PNG overlay QUYẾT ĐỊNH kích thước output của ContentFlow
    config_overrides: dict           # video_generator.viewport / frame_layouts / encoding ... (params.config)
    source_pool: str                 # tên pool trong config/pools.yaml
    selection_mode: str; fps: int
    encoder: str | None

class RenderRequest(TypedDict):
    audio: ArtifactRef
    profile: RenderProfile
    output_path: str                 # tuyệt đối, trong workspace/render/
    thumbnail: "ThumbnailRequest | None"

class ThumbnailRequest(TypedDict):
    image: ArtifactRef | None        # ảnh nhân vật
    channel_name: str; title: str
    highlight: Literal["auto","manual","none"]; highlight_text: str | None
    output_path: str                 # .jpg/.png

class RenderResult(TypedDict):
    video: ArtifactRef | None; thumbnail: ArtifactRef | None
    warnings: list[str]; clips_used: list[str]   # nếu worker báo; KHÔNG đảm bảo tái tạo (A20)

class SyncRequest(TypedDict):
    raw_dir: str; size: str; fps: int; remove_audio: bool; quality: str

class RenderAdapter(Protocol):
    def sync_source(self, req: SyncRequest, ctx: StageContext) -> ArtifactRef: ...   # trỏ tới <raw>/_synced + source_profile.json
    def render_video(self, req: RenderRequest, ctx: StageContext) -> RenderResult: ...
    def render_thumbnail(self, req: ThumbnailRequest, ctx: StageContext) -> ArtifactRef: ...
    def status(self, idempotency_key: str, output_dir: str) -> JobStatus: ...         # hỏi lại sau crash
    def health(self) -> HealthReport: ...
```

- **Ánh xạ sang ContentFlow `media_worker`:**
  - `render_video` → `python -m media_worker run --request req.json`, `type=render`, `inputs=[{audio, path, sha256}, {video_dir}, {frame}]`, `params={video_dir, fps, selection_mode, source_processing, encoder, output_name, config=<profile.config_overrides>}`; `idempotency_key = stage_key`; `output_dir` tuyệt đối riêng mỗi lần gọi (worker ghi `.worker_state.json` và `.work/` vào đó).
  - `render_thumbnail` → `type=thumbnail`.
  - `sync_source` → worker **không có** job type sync (A10): dùng một shim nhỏ **nằm trong orchestrator** gọi `source_sync.sync_videos(SyncOptions)`; không sửa ContentFlow.
  - Đọc stdout JSON-lines để lấy progress/warning; exit 0/3/4/2 ánh xạ OK/FAILED/CANCELLED/USAGE. `media_worker status` dùng để reconcile sau crash.
  - Video pool truyền vào là `<raw>/_synced` (render quét không đệ quy).
- **Profile** (A8): "youtube" = frame 16:9 1920×1080 + layout; "tiktok" = frame 9:16 1080×1920 (frame mặc định của ContentFlow). **Cần tạo frame 16:9 và layout** — việc thuộc config/asset của orchestrator.
- **TikTok:** gọi `render_video` **một lần mỗi part** với audio `part_NN`.
- **Lỗi** ánh xạ từ `VideoError.code`: `FFMPEG_MISSING/DISK_FULL/GPU_UNAVAILABLE`→RESOURCE, `MISSING_INPUT/INVALID_CONFIG`→POLICY, `FFMPEG_FAILED/TIMEOUT`→TRANSIENT, cancel→CANCELLED. Timeout 3600 s/lệnh ffmpeg là trần cứng (rủi ro R7).
- **Cache key:** `audio.sha256 + profile + pool_fingerprint(source_profile.json) + ContentFlow commit`. **Nền ngẫu nhiên không seed** ⇒ cùng key có thể ra video khác; coi video là artifact đã cache, không tái sinh để "so sánh".

## 6. PublishAdapter

**Trách nhiệm:** đưa video lên một nền tảng. Hiện chỉ **YouTube** (A14, A15).

```python
class PublishRequest(TypedDict):
    platform: Literal["youtube"]          # "tiktok" chưa có implementation
    video: ArtifactRef; thumbnail: ArtifactRef | None
    title: str; description: str; tags: list[str]
    category: str | None; privacy: Literal["private","unlisted","public"]
    schedule: str | None                  # RFC3339
    made_for_kids: bool                   # BẮT BUỘC (yt_uploader không default)
    playlists: list[str]
    account_id: str | None                # = channel trong channel.yaml
    idempotency_key: str                  # = stage_key

class PublishResult(TypedDict):
    state: Literal["completed","failed","paused","cancelled","pending"]
    remote_id: str | None; remote_url: str | None
    error: StageError | None; post_step_warnings: list[str]

class PublishAdapter(Protocol):
    platform: str
    def publish(self, req: PublishRequest, ctx: StageContext) -> PublishResult: ...   # submit + poll tới trạng thái cuối
    def find(self, idempotency_key: str) -> PublishResult | None: ...                 # tra cứu sau crash
    def health(self) -> HealthReport: ...
```

- **Ánh xạ sang yt_uploader:** daemon `yt-uploader serve --headless` chạy nền; adapter là HTTP client tới `127.0.0.1:8973`, Bearer = nội dung `<datadir>/api_token`. `POST /api/v1/jobs` kèm `idempotency_key`, rồi poll `GET /api/v1/jobs/{id}` (hoặc `?idempotency_key=`). Health: `GET /api/v1/health` (`features` phải có `idempotency_key`, `resume_probe`).
- **Điều kiện:** `video`/`thumbnail` phải nằm trên đĩa của máy chạy daemon; thumbnail ≤ 2 MiB (adapter phải tự kiểm/nén JPG trước khi gửi, R5).
- **Ánh xạ lỗi:** `auth_revoked`→AUTH, `quotaExceeded`→RESOURCE (không retry trong ngày), `rate_limited`/5xx→TRANSIENT, `AMBIGUOUS_UPLOAD`→AMBIGUOUS (người xác nhận rồi `retry?force=true`). Job `failed` **không tự retry** ⇒ retry là việc của orchestrator (có giới hạn).
- **Bất biến:** một `idempotency_key` ⇒ tối đa một video trên YouTube. Job `completed` không retry được.
- `TikTokPublishAdapter`: **không tồn tại**; TikTok chỉ được xuất file bởi OutputPublisher (D-06).

## 7. OutputPublisher

**Trách nhiệm:** khi job xong, **sao chép** artifact cuối từ workspace sang gói output của người dùng (HANDOFF §16–17). Không liên quan tới việc đăng nền tảng (đó là PublishAdapter).

```python
class OutputPackage(TypedDict):
    project_dir: str                     # output/<project>/
    files: list[str]
    manifest_public: str                 # project.json

class OutputPublisher(Protocol):
    def publish(self, job: JobRecord, manifest: Manifest, cfg: OutputConfig) -> OutputPackage: ...
```

Layout (khớp HANDOFF §16):

```text
output/<project>/
  README.txt   project.json   story.txt
  youtube/{video.mp4, thumbnail.jpg, title.txt, description.txt}
  tiktok/{part_01.mp4, part_02.mp4, ...}
```

- **Quy tắc:**
  1. Chỉ **copy**, không move/symlink; sau publish pipeline **không đọc lại `output/`** (HANDOFF §17). Rebuild dùng workspace/cache.
  2. Dựng trong thư mục tạm cùng volume (`output/.tmp-<id>/`) rồi `rename` ⇒ người dùng không bao giờ thấy gói nửa vời. Tên đụng nhau ⇒ hậu tố `-2`, không ghi đè.
  3. `project.json` công khai chỉ gồm thứ người dùng cần (file, tiêu đề, độ dài); **manifest nội bộ** (version/commit/profile hash) ở lại `workspace/job_x/manifest.json`.
  4. `story.txt` copy từ artifact đã qua validator bất biến (§2).
  5. Không chứa cache, chunk audio, sync, temp.
  6. Verify sha256 sau copy; lỗi → `RESOURCE`/`TRANSIENT`, giữ nguyên workspace.
- **Quy tắc đặt tên `<project>`** chưa chốt (HANDOFF §21), xem DECISIONS D-07.

---

## 8. Manifest nội bộ (đối chiếu HANDOFF §18)

```json
{
  "schema": 1, "job_id": "story-001", "state": "OUTPUT_READY",
  "versions": {
    "orchestrator": "<sha>",
    "modules": {"ContentFlow": "<sha>", "oh-story-claudecode": "<sha>", "yt_uploader": "<sha>"}
  },
  "profiles": {"story": "...@v", "tts": "...@v", "render_youtube": "...@v", "render_tiktok": "...@v"},
  "channel": "channel_a", "source_pool": "gameplay",
  "stages": {
    "source": {"stage_key": "...", "artifacts": [ArtifactRef], "started": "...", "ended": "...", "attempts": 1},
    "story": {}, "tts": {}, "audio": {}, "render_youtube": {}, "render_tiktok": {}, "publish_youtube": {}
  },
  "nondeterministic": ["render.background_selection"]
}
```

## 9. Trạng thái job (từ HANDOFF §15) và ai chịu trách nhiệm

| Trạng thái | Do | Ghi chú |
|---|---|---|
| NEW → SOURCE_READY | SourceProcessor | |
| STORY_RUNNING → STORY_READY | StoryAdapter (+ validator bất biến) | |
| TTS_PLANNING → TTS_RENDERING → MASTER_AUDIO_READY | TTS Manager + TTSAdapter + AudioProcessor | |
| YOUTUBE_RENDER_READY → YOUTUBE_RENDERING / TIKTOK_RENDER_READY → TIKTOK_RENDERING | AudioProcessor + RenderAdapter | nhánh YouTube và TikTok độc lập |
| OUTPUT_READY | OutputPublisher | người dùng đã có gói output |
| UPLOADING → PUBLISHED | PublishAdapter (**YouTube**) | TikTok dừng ở OUTPUT_READY |
| `*_FAILED:<stage>` | orchestrator | giữ `StageError.error_class` để quyết định retry |
