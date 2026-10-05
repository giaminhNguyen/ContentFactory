# MODULE_CONTRACTS

> Hợp đồng giữa orchestrator và các module. Neo vào HANDOFF (§3 artifact + manifest + state, §20) và `CURRENT_SYSTEM_AUDIT.md`.
> Ngôn ngữ đặc tả: chữ ký kiểu Python (`typing.Protocol`) để đọc dễ; **hợp đồng thật là artifact trên đĩa + manifest JSON**, không phải lời gọi hàm. Ngôn ngữ cài đặt orchestrator mặc định là Python (`DECISIONS.md` D-02).
> **Phase 1:** chữ ký chuẩn nằm ở `src/contentfactory/contracts.py` (được test). Các khối code bên dưới là đặc tả ý định từ Phase 0; chỗ nào khác với code thì **code đúng**, danh sách khác biệt ở §10.

## 0. Quy ước chung

### 0.1 Workspace và artifact

```text
workspace/job_<id>/
  source/  story/  tts/  audio/  render/{youtube,tiktok}/  output/  publish/  temp/
  manifest.json   job.log.jsonl
```

- Mọi module **chỉ đọc/ghi trong workspace của job** (và đọc pool/channel asset được cấu hình). Không module nào đọc `output/`.
- **Adapter làm việc với `Path`** trong workspace; chỉ orchestrator "niêm phong" thành `ArtifactRef` (sha256, bytes) khi checkpoint stage. Handler khai báo `ArtifactDraft(path, kind, meta)`.
- **Adapter PHẢI ghi output atomic** (ghi `*.part` rồi rename): path tồn tại ⇔ file hoàn chỉnh. Handler dựa vào đó để dùng lại output có sẵn khi resume (chunk TTS, video render, part TikTok).
- Stage chỉ nhận artifact thuộc `kind` mà stage khai báo trong `requires` và chỉ được sinh `kind` khai báo trong `produces` (orchestrator từ chối kind lạ hoặc thiếu).

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

Mỗi lời gọi stage có `stage_key = sha256(canonical_json(inputs_sha256 + params + profile_version))`. Cùng `stage_key` + artifact đã tồn tại và hợp lệ ⇒ **bỏ qua, trả artifact cũ** (**Phase 1 chỉ tính và ghi `stage_key`** vào `stage_runs`/manifest; bỏ qua theo cache liên job chưa làm, vì job tuyến tính và artifact bất biến sau checkpoint nên resume đã đủ). Dùng làm `idempotency_key` cho ContentFlow worker và yt_uploader. Đổi watermark/video/thumbnail **không** đổi `stage_key` của TTS (HANDOFF §6.7, §10).

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

## 1. Source: SourceAdapter, SourceProvider, Transcript Processor

**Trách nhiệm:** biến *input người dùng* (URL YouTube, file phụ đề, văn bản) thành bộ artifact chuẩn hóa cho Story. Chia hai phần, **mỗi phần có một chủ sở hữu**:

```text
input -> SourceAdapter (ProviderChain) -> SourceResult -> Transcript Processor -> artifact -> Story
          thu thập phụ đề thô               (acquisition)     xử lý transcript (ContentFactory)
```

```python
class SourceInput(TypedDict):          # kind, value bắt buộc; title, language tùy chọn
    kind: str                          # youtube_url | transcript_file | text
    value: str                         # URL, đường dẫn file, hoặc chính văn bản

class SourceResult(TypedDict, total=False):
    source_url: str
    source_type: str                   # youtube | local_subtitle | plain_text
    provider: str                      # supervip | ytdlp | local | text | ...
    video_id: str | None
    title: str | None
    description: str | None            # mô tả CỦA NGUỒN; không dùng làm mô tả video của ta (D-31)
    language: str | None               # ngôn ngữ của phụ đề/văn bản nguồn
    raw_subtitle_path: Path            # đúng như provider trả về, nguyên byte
    subtitle_format: str               # srt | vtt | json | txt
    subtitle_kind: str                 # manual | auto | translated | unknown
    has_timestamps: bool
    metadata: dict
    status: str                        # ok | error
    error: dict | None                 # StageError.to_dict()
    attempts: list[dict]               # nhật ký từng provider đã thử (chẩn đoán)
    origin: str                        # network | cache | job

class SourceProvider(Protocol):        # một cách thu thập cụ thể; raise StageError khi lỗi
    name: str
    def supports(self, kind: str) -> bool: ...
    def available(self) -> bool: ...
    def acquire(self, src, work_dir, ctx, prefs) -> SourceResult: ...
    def describe(self, src, ctx) -> dict: ...      # metadata bổ sung hoặc {}
    def health(self) -> dict: ...

class SourceAdapter(Protocol):         # điểm vào của stage; ProviderChain là cài đặt
    def acquire(self, src, out_dir, ctx) -> SourceResult: ...
    def health(self) -> dict: ...
```

**Provider hiện có**

| Provider | Nguồn | Định dạng raw | Ghi chú |
|---|---|---|---|
| `SubtitleSupperVipProvider` (`supervip`) | YouTube URL | `json` (snippet `{text,start,duration}`) | provider **chính**; bridge subprocess gọi code của Subtitle_supperVip; cần Python env riêng; metadata đầy đủ khi có `YOUTUBE_API_KEY` |
| `YtDlpProvider` (`ytdlp`) | YouTube URL | `vtt`/`srt` | fallback; bổ sung title/mô tả khi provider chính thiếu |
| `LocalSubtitleProvider` (`local`) | `transcript_file` | `srt`/`vtt`/`json`/`txt` | copy nguyên byte |
| `PlainTextProvider` (`text`) | `text` | `txt` | không timestamp |

**ProviderChain (cài đặt `SourceAdapter`)**
- Thứ tự provider theo config (`source.providers`); provider `available() == False` bị bỏ qua và ghi vào `attempts`.
- Lỗi một provider ⇒ thử provider kế, **trừ** lỗi dứt khoát về video/đầu vào (`NOT_YOUTUBE_URL`, `BAD_VIDEO_ID`, `VIDEO_UNAVAILABLE`, `FILE_NOT_FOUND`, `UNSUPPORTED_FORMAT`, `EMPTY_SUBTITLE`). Hết provider: ném lỗi `TRANSIENT` nếu có provider nào lỗi TRANSIENT (còn hy vọng retry), nếu không thì lỗi của provider đầu; không provider nào khả dụng ⇒ `NO_SOURCE_PROVIDER` (RESOURCE). Chi tiết `attempts` nằm trong `StageError.detail`.
- Thiếu title: hỏi `describe()` của provider khác; vẫn thiếu thì dùng video id (`metadata.title_from = fallback`).
- **Cache & idempotency:** `cache_key = sha256(kind, định danh nguồn, ngôn ngữ ưu tiên)`. (1) job: `subtitle_raw.meta.json` + raw còn đúng sha256 ⇒ dùng lại; (2) cache chung `runtime/cache/source/<key>/` ⇒ copy vào job; (3) gọi provider. Khóa theo `cache_key` nên hai job cùng nguồn chạy song song chỉ tải một lần. `refresh_source: true` bỏ qua (1)(2).
- **Không chạm state pipeline:** chain chỉ đọc/ghi file trong workspace của job và cache; DB/queue của module bên ngoài không bao giờ được dùng.

**Mã lỗi provider supervip** (map từ exception của module qua bridge): `SubtitleUnavailable → NO_SUBTITLES` (POLICY), `LanguageUnavailable → LANGUAGE_UNAVAILABLE` (POLICY), `BlockedByYouTube → YOUTUBE_BLOCKED` (RESOURCE), thiếu thư viện → `SUPERVIP_DEPENDENCY_MISSING` (RESOURCE), lỗi khác/crash/timeout → `SUPERVIP_ERROR`/`SUPERVIP_BRIDGE_FAILED`/`SUPERVIP_TIMEOUT` (TRANSIENT), không chạy được Python → `SUPERVIP_UNAVAILABLE` (RESOURCE).

**Transcript Processor** (`source/transcript.py`, ContentFactory sở hữu): `process(raw, format, out_dir, ctx, provenance)` = `raw subtitle → timestamp-aware parser (srt/vtt/json/txt) → structured transcript → caption reconstruction → duplicate cleanup → punctuation/paragraph → clean transcript`.
- Timestamp **không bị xóa**: `transcript_structured.json` có `cues[{i,start,end,text,gap_before}]`, `sentences[{i,start,end,text,gap_before,cue_range,interpolated_time,punctuation_added,capitalized,internal_pauses,paragraph}]`, `paragraphs[{i,sentence_range,start,end,gap_before}]`, `provenance{raw_sha256,format,parser_version,config,config_hash,...}`, `stats`, `clean_sha256`. Văn bản thuần (`txt`) dùng cùng schema với `start/end = null`.
- `transcript_clean.txt`: không timestamp, đoạn cách nhau một dòng trống, sinh **sau** khi dựng lại.
- Idempotent: dùng lại structured khi `(raw_sha256, format, parser_version, config_hash)` không đổi; dùng lại clean khi sha256 khớp `clean_sha256`.

**Artifact của stage `source`** (`workspace/<job>/source/`):

| File | Artifact kind | Nội dung |
|---|---|---|
| `source.json` | `metadata` | `SourceResult` (đường dẫn tương đối) + `transcript{structured, clean, stats}`; không có `origin` (đổi theo lần chạy) |
| `subtitle_raw.<srt|vtt|json|txt>` | `subtitle_raw` | phụ đề thô |
| `transcript_structured.json` | `transcript_structured` | như trên |
| `transcript_clean.txt` | `transcript` | transcript sạch |

Nội bộ, không đăng ký artifact: `subtitle_raw.meta.json` (dấu vân tay thu thập), `_acq/` (thư mục tạm của provider, xóa sau khi xong).

## 2. StoryAdapter

**Trách nhiệm:** từ transcript nguồn ⇒ **các section nội bộ theo thứ tự**; **stage** (không phải adapter) dựng một `story.txt` liền mạch bằng **Story Assembler** + validator bất biến (HANDOFF §5, D-24).

```python
class SourceBundle(TypedDict):
    title: str
    language: str            # ngôn ngữ ĐÍCH (mặc định "vi", D-04)
    source_language: str     # ngôn ngữ của transcript nguồn
    transcript: Path

class StoryResult(TypedDict):
    sections: list[Path]     # section/chương nội bộ theo thứ tự (có thể có heading, marker: Assembler gỡ)
    stats: dict

class StoryAdapter(Protocol):
    def generate(self, bundle: SourceBundle, profile: dict, out_dir: Path, ctx: StageContext) -> StoryResult: ...
    def health(self) -> dict: ...
```

- **`profile`:** `chapters` (hoặc `target_chars`/`chapter_chars`), `book_name`, `max_removed_ratio`. Blueprint/continuity/sections nằm trong `out_dir` (workspace nội bộ), không vào output.
- **Story Assembler** (`story/assembler.py`, tất định, `ASSEMBLER_VERSION`): gỡ heading/đường kẻ/marker/"còn tiếp"/tóm tắt chương trước; mỗi dòng là một đoạn → xuất đoạn cách nhau một dòng trống; trim phần đầu section chép lại đuôi section trước; nối câu bị cắt ở ranh giới section; loại câu lặp liền kề, đoạn trùng khít, đoạn gần trùng; lỗi `ASSEMBLER_REMOVED_TOO_MUCH` nếu loại > 35%. Ghi `assembly_report.json` (artifact `story_report`).
- **Bất biến đầu ra (validator, chạy sau Assembler):** `story.txt` không rỗng; không có dòng mở đầu bằng `第N章` / `Chapter N` / `Section N` / `Part N` / `Chương N` / `Phần N`; không marker kỹ thuật (`<!--`, `[[`, `{{`, `TODO`, `#`); không quá 30% đoạn trùng. Vi phạm → `STORY_INVALID` (POLICY) và **không** ghi `story.txt`. (Chưa kiểm ngôn ngữ đúng `profile.language`.)
- **Artifact của stage:** `story_text`, `story_report`.
- **Lỗi của adapter:** `STORY_STEP_INCOMPLETE` / `STORY_TURN_LIMIT` / `STORY_MISSING_CHAPTERS` (POLICY); `CLAUDE_NOT_LOGGED_IN` (AUTH); `CLAUDE_CLI_MISSING` / `OH_STORY_MISSING` / `OH_STORY_DEPLOY_FAILED` (RESOURCE); `AGENT_NO_RESULT` / `AGENT_TIMEOUT` (TRANSIENT).
- **Resume:** điều kiện xong của từng bước kiểm bằng file trên đĩa (`分支库/*/正典.md`, `分支提案.md`, `分支/*-B*.md`, `设定/分支设定.md`, `大纲/大纲.md` + `细纲_第*.md`, `追踪/_tracking-state.json`); chạy lại bỏ qua bước đã xong và tiếp tục từ chương chưa commit.
- **Triển khai:**
  - `StoryBranchAdapter` (**chính**, D-23): điều khiển oh-story qua `AgentRunner` (`ClaudeCliRunner` thật; `ScriptedOhStory` trong test). **Chưa chạy với LLM thật.**
  - `FakeStory`: sinh 3 section có heading/marker để kiểm Assembler (test pipeline).
  - Dự phòng chưa xây: S2 `DirectLLMStoryAdapter` (D-03).

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
- **Tên `<project>`** = `<yyyymmdd>_<slug ASCII không dấu>` (D-07), cấu hình ở `config/config.json` → `output.name_template`.

---

## 8. Manifest nội bộ (đối chiếu HANDOFF §18) — đã triển khai ở Phase 1

`workspace/job_<id>/manifest.json` là **dẫn xuất từ DB** (DB là nguồn sự thật), ghi atomic sau mỗi lần stage kết thúc và dựng lại khi orchestrator khởi động:

```json
{
  "schema": 1, "job_id": "000001", "state": "PUBLISHED", "failed_stage": null, "last_error": null,
  "params": {"input": {}, "language": "vi", "tiktok": {"speed": 2.0, "target_part_sec": 600}},
  "modules": {"ContentFlow": "<sha>", "oh-story-claudecode": "<sha>", "yt_uploader": "<sha>"},
  "created": "...", "updated": "...",
  "stages": {
    "source": {"status": "succeeded", "attempts": 1, "stage_key": "...", "started": "...", "ended": "...",
               "data": {}, "artifacts": [{"path": "source/transcript_clean.txt", "kind": "transcript", "sha256": "...", "bytes": 1, "meta": {}}]},
    "story": {}, "tts": {}, "audio": {}, "render_youtube": {}, "render_tiktok": {}, "output": {}, "publish": {}
  },
  "nondeterministic": ["render.background_selection"]
}
```

`modules` đọc từ `modules.lock`. Chưa có: hash của profile (chưa có profile thật), SHA của orchestrator.

## 9. Trạng thái job và ai chịu trách nhiệm — đã triển khai ở Phase 1

Mỗi stage = `queue_state → running_state → done_state`; `done_state` là `queue_state` của stage kế ("X_READY" = xong bước trước, đang xếp hàng cho bước sau). Bảng stage nằm ở `src/contentfactory/jobs/pipeline.py` (nguồn sự thật, có test).

| Stage | queue → running → done | Module/adapter | Đầu vào → đầu ra (artifact kind) |
|---|---|---|---|
| source | NEW → SOURCE_PROCESSING → SOURCE_READY | SourceAdapter + Transcript Processor | — → subtitle_raw, transcript_structured, transcript, metadata |
| story | SOURCE_READY → STORY_RUNNING → STORY_READY | StoryAdapter + Story Assembler + validator bất biến | transcript, metadata → story_text, story_report |
| tts | STORY_READY → TTS_RUNNING → AUDIO_READY | TTSAdapter + AudioProcessor | story_text → audio_master |
| audio | AUDIO_READY → AUDIO_PROCESSING* → YOUTUBE_RENDER_READY | AudioProcessor | audio_master → audio_youtube, audio_tiktok |
| render_youtube | YOUTUBE_RENDER_READY → YOUTUBE_RENDERING → TIKTOK_RENDER_READY | RenderAdapter (GPU) | audio_youtube, metadata → video_youtube, thumbnail |
| render_tiktok | TIKTOK_RENDER_READY → TIKTOK_RENDERING → OUTPUT_READY | RenderAdapter (GPU) | audio_tiktok → video_tiktok |
| output | OUTPUT_READY → OUTPUT_PUBLISHING* → UPLOAD_READY | OutputPublisher | story_text, metadata, video_*, thumbnail → output_package |
| publish | UPLOAD_READY → UPLOADING* → PUBLISHED | PublishAdapter (**YouTube**) | video_youtube, thumbnail, metadata, story_text → publish_result |

`*` = state thêm so với danh sách tối thiểu của Phase 1 (cần để mỗi stage có một running state). Terminal: `PUBLISHED`, `FAILED`.

**FAILED** là một state kèm `failed_stage` + `last_error` (lớp lỗi theo `ErrorClass`), không phải một state riêng cho mỗi stage: retry thủ công đưa job về `queue_state` của đúng stage đó. TikTok không có bước đăng (D-06): nhánh TikTok kết thúc bằng file trong output.

Chuyển trạng thái hợp lệ (`pipeline.allowed`, kiểm tra ở mọi lần ghi DB): `queue→running`, `running→done`, `running→queue` (retry có backoff / bị ngắt / dừng có chủ đích), `running→FAILED`, `FAILED→queue_state`.

## 10. Khác biệt Phase 1 so với đặc tả Phase 0 ở trên

| Mục | Đặc tả Phase 0 | Code Phase 1 | Lý do |
|---|---|---|---|
| Kiểu dữ liệu adapter | `ArtifactRef` vào/ra | `Path` vào/ra; handler trả `ArtifactDraft`; orchestrator niêm phong | Adapter không phải hash/ghi DB; sha256 tính đúng một lần tại checkpoint |
| `TTSAdapter.synthesize` | trả `ChunkResult{audio: ArtifactRef}` | nhận `out_path`, trả `{index, duration_sec}` | TTS Manager (handler) kiểm soát tên chunk và resume |
| `AudioProcessor` | theo `ArtifactRef` | theo `Path`; `qa()` trả `AudioQAReport` | như trên |
| `StoryAdapter.generate` | `(SourceBundle, profile, ctx)` | `(SourceBundle{title,language,transcript:Path}, profile, out_dir, ctx)` | adapter biết chỗ ghi |
| `OutputPublisher.publish` | `(job, manifest, cfg)` | `(OutputRequest, ctx)` với đường dẫn artifact | Module không đọc DB/manifest, chỉ nhận artifact |
| `RenderAdapter` | có `sync_source`, `status` | chỉ `render_video`, `render_thumbnail`, `health` | Source Sync và reconcile thuộc Phase 4 |
| `PublishAdapter` | có `find()` | chỉ `publish`, `health` | tra cứu sau crash thuộc Phase 5; Phase 1 dựa vào `idempotency_key = stage_key` |
| `TTSAdapter` | có `capabilities()` đầy đủ | `capabilities()` trả dict tự do | schema chính thức thuộc Phase 3 |
| `StageContext` | có `secrets`, `deadline_s`, `on_progress` | có `params`, `inputs`, `config`, `cancel`, `log`, `stage_key`, `attempt` | thêm khi có nhu cầu thật |
| Tên stage | `*_PLANNING/*_RENDERING`, `MASTER_AUDIO_READY` | theo danh sách Phase 1 (§9) | yêu cầu Phase 1; tương ứng `TTS_RUNNING`, `AUDIO_READY` |

### Khác biệt thêm ở Phase 2

| Mục | Trước | Sau | Lý do |
|---|---|---|---|
| `SourceResult` | `{title, language, transcript, metadata}` | thêm `subtitle_raw`, `structured`, `stats` | giữ raw + structured + clean (yêu cầu Phase 2) |
| `StoryResult` | `{story: Path, stats}` | `{sections: [Path], stats}` | Assembler chạy ở stage cho mọi adapter; adapter không cần tự gộp |
| `SourceBundle` | `{title, language, transcript}` | thêm `source_language` | Story cần biết ngôn ngữ nguồn khác ngôn ngữ đích |
| Artifact | source: `transcript`, `metadata`; story: `story_text` | thêm `subtitle_raw`, `transcript_structured`, `story_report` | D-27 |
| `ctx.config` | `output_dir` | không đổi; thêm `source` (cấu hình dựng câu); thư mục cache nằm trong constructor của `ProviderChain` (do registry truyền `runtime/cache/source`) | module không cần biết đường dẫn runtime |

### Khác biệt thêm khi tích hợp Subtitle_supperVip

| Mục | Trước | Sau | Lý do |
|---|---|---|---|
| Interface Source | `SourceProcessor.process(src, out_dir, ctx) -> {title, language, subtitle_raw, structured, transcript, metadata, stats}` (một khối) | `SourceAdapter.acquire -> SourceResult` (chỉ thu thập) + `TranscriptProcessor` (xử lý) | thay provider không đụng xử lý transcript; ContentFactory sở hữu transcript |
| `SourceResult` | đường dẫn artifact đã xử lý | thông tin thu thập: `source_url, source_type, provider, video_id, title, description, language, raw_subtitle_path, subtitle_format, subtitle_kind, has_timestamps, metadata, status, error, attempts, origin` | yêu cầu tích hợp |
| Tên file | `raw/subtitle.*`, `structured.json`, `transcript.txt`, `metadata.json` | `subtitle_raw.*`, `transcript_structured.json`, `transcript_clean.txt`, `source.json` | bố cục `source/` đã chốt trong HANDOFF §4A |
| Mô tả video | `meta.description` có thể dùng làm mô tả đăng | output/publish luôn dùng 300 ký tự đầu của `story.txt` | không đăng lại mô tả của nguồn (D-31) |
| Cấu hình | `youtube.preferred_langs`, `youtube.reconstruct` | `source.providers`, `source.languages`, `source.allow_translation`, `source.reconstruct`, `supervip.*`, `youtube.yt_dlp_*` (chỉ fallback) | nhiều provider |
| Adapter name | `youtube` | `provider_chain` | |
