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
    RESOURCE    # tài nguyên tạm thời thiếu (mạng, quota, token, đĩa, GPU/runtime) -> job bị GIỮ (PAUSED_*), resume khi Resource Monitor báo sẵn sàng (§11)
    POLICY      # input/config sai -> KHÔNG retry, cần sửa
    AUTH        # credential hết hạn/thiếu/thu hồi -> PAUSED_CREDENTIAL; chỉ resume khi credential hợp lệ trở lại (§11)
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

**Mã lỗi provider supervip** (map từ exception của module qua bridge): `SubtitleUnavailable → NO_SUBTITLES` (POLICY), `LanguageUnavailable → LANGUAGE_UNAVAILABLE` (POLICY), `BlockedByYouTube → YOUTUBE_BLOCKED` (RESOURCE, `resource=network` ⇒ hold `PAUSED_NETWORK`, §11.4), thiếu thư viện → `SUPERVIP_DEPENDENCY_MISSING` (RESOURCE), lỗi khác/crash/timeout → `SUPERVIP_ERROR`/`SUPERVIP_BRIDGE_FAILED`/`SUPERVIP_TIMEOUT` (TRANSIENT), không chạy được Python → `SUPERVIP_UNAVAILABLE` (RESOURCE).

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

## 3. TTSAdapter, TTS Manager, profile, Analyzer (đã triển khai ở Phase 3)

**Trách nhiệm:** biến text thành audio chunk với một engine cụ thể (HANDOFF §6) mà **không khóa hệ thống vào engine nào**. Quyết định: `DECISIONS.md` D-57…D-63. Mã: `tts/` (schema, normalize, planner, qa, manager, analyzer, autotune, stage) và `adapters/command_tts.py`.

### 3.1 Hợp đồng adapter (tối thiểu)

```python
class TTSAdapter(Protocol):
    engine_id: str
    def capabilities(self) -> dict: ...                  # TTSCapabilities (3.2); trường thiếu được điền mặc định
    def synthesize(self, segment: Segment, profile: dict, out_path: Path, ctx: StageContext) -> ChunkResult: ...
    def health(self) -> dict: ...
# Segment = {index, text, pause_after_ms, key}   ChunkResult = {index, duration_sec}
```
- **Một segment → một file WAV** tại `out_path`, ghi atomic (`.part` → rename). `profile` là profile **flat** hiệu lực (3.3).
- Lỗi: `TRANSIENT` (429/5xx/timeout/file hỏng: Manager thử lại riêng segment), `AUTH` (thiếu/hết hạn credential: job giữ `PAUSED_CREDENTIAL`), `RESOURCE` (+`resource`: GPU/OOM/thiếu module/…), `POLICY` (input/config sai). Adapter không retry, không cache, không cắt đoạn, không ghép.
- **Thêm adapter mới = config, không sửa core:** `adapters.tts = "package.module:Class"`, `adapter_config.tts = {...}` ⇒ `Class(config)`. Engine CLI không cần viết code: `contentfactory.adapters.command_tts:CommandTTS` + spec (Analyzer sinh sẵn).

### 3.2 Capability schema (`schema.CAPABILITY_FIELDS`)
`max_chars` (int|null), `languages`, `voices`, `speed`, `ssml`, `streaming`, `batch`, `voice_cloning`, `requires_reference_audio`, `output_formats` (mặc định `["wav"]`), `sample_rate`, `max_concurrency`, `engine_version` (vào cache key), `cache_settings` (setting nào ảnh hưởng âm thanh; null = tất cả), `device`; thêm ở D-100: `supports_ssml_break`, `supports_exact_break_ms`, `supports_context` (mặc định False: ContentFactory gom nhóm và chèn khoảng lặng ngoài; True thì segment có thêm `breaks`/`context`). Sai kiểu ⇒ `POLICY BAD_CAPABILITY`.

### 3.2b Prosody Engine và Speech Plan (D-100)
`params.prosody` (xem `tts/prosody/profiles.py`) → `tts/prosody` → `speech_plan.json` (artifact kind `speech_plan`, stage `tts`): `{schema, mode: prosody|legacy, rules_version, profile, pauses{loại: ms}, strategy: external_pauses|native_breaks, segments[{id, key, text, paragraph_id, dialogue, boundary_before/after, pause_after_ms, source, locked, manual_override, realized: external|engine|native, synthesis_group, micro[], previous_context, next_context}], groups[{id, text, segments[], boundary_after, pause_after_ms, chars, breaks?}], qc{…}, plan_key, warnings[]}`. Manager chuyển `groups` thành `Segment` (index, text, pause_after_ms) cho adapter/AudioProcessor — hợp đồng adapter và `assemble` không đổi. Job không có `params.prosody` chạy planner cũ và vẫn nhận speech plan mode `legacy`.

### 3.3 Profile
- **Flat (hiệu lực):** `engine, language, voice, model, settings{}, profile_version, segment{preferred_chars, max_chars, min_chars}, break_priority[], pause_ms{paragraph, sentence, dialogue}, joiner, normalize{strip_markdown, collapse_punct, ellipsis, ensure_terminal_punct, replacements[]}, qa{silence_ratio_max, duration_chars_per_sec, min_duration_sec}, retry{max_attempts, backoff_s[]}, planner{ai_retries}`. `resolve()` = mặc định ⊕ giá trị job (dạng trần, dạng annotated, hoặc dạng Phase 1 `{max_chars, pause_ms}`) kẹp theo capability.
- **Annotated (lưu trữ):** `{schema: 1, engine, status: candidate|ready, <trường>: Fact, needs_user[{key, reason, detail, ref}], capabilities, meta}` với `Fact = {value, source, confidence, evidence[{ref, quote}], note?, alternatives?[]}`; `source` ∈ `official_docs|source_code|official_example|runtime_test|ai_inference|user|default`, `confidence` ∈ `high|medium|low`. Kiểm bằng `validate_annotated`.

### 3.4 TTS Manager (`tts/manager.py`)
```text
story.txt -> normalize_text -> Planner(rule|ai) -> validate_plan -> segments.json
          -> mỗi segment: cache job-local -> cache chung -> adapter.synthesize (retry riêng) -> QA chunk -> sidecar + cache
          -> AudioProcessor.assemble -> audio/master.wav ; tts/tts_manifest.json
```
- Artifact stage `tts`: `audio_master` (+ `duration_sec`) và `tts_manifest` (JSON; schema 1; xem D-59). Adapter orchestrator-inject: `tts`, `audio`, `planner` (`rule` mặc định; `module:Class` cho planner khác, giao diện `plan(text, profile, feedback) -> list[Segment]`, thuộc tính `name`).
- Validator (`validate_plan`) mã lỗi: `EMPTY_PLAN, BAD_INDEX, EMPTY_SEGMENT, NO_SPEECH, TOO_LONG, BAD_PAUSE, TEXT_MISMATCH`; cảnh báo: `SHORT, CUT_MID_SENTENCE`. QA chunk: `UNDECODABLE, TOO_SHORT, SILENT, DURATION_TOO_SHORT_FOR_TEXT, DURATION_TOO_LONG_FOR_TEXT` (+ `AudioProcessor.qa`).
- **Cache key chunk:** D-59. Checkpoint tiến độ: `ctx.progress(done, total, "segments")`.

### 3.5 Onboarding và Auto Tune
- `tts.analyzer.analyze(root, engine, ai_infer) -> {engine, capabilities, profile(annotated candidate), adapter{kind, ready, spec|code, config}, needs_user, candidates, files}`; `fetch_reference`, `write_onboarding(result, out_dir)`; CLI `scripts/tts_onboard.py`. Không chạy mã của repo. Chi tiết và giới hạn: D-61.
- `tts.autotune.AutoTuner(adapter, language, profile).run() -> TuneReport`, `apply_to_profile(profile, report)`; CLI `scripts/tts_tune.py`. Chỉ chạy khi người dùng yêu cầu. D-62.

## 4. AudioProcessor (đã triển khai ở Phase 4)

**Trách nhiệm:** mọi thao tác audio tất định, **độc lập engine TTS**: ghép chunk (chuẩn hóa kỹ thuật + dọn biên + Pause Engine) → Narration Master (loudness/compressor/limiter) → bản YouTube (watermark) và các part TikTok (tăng tốc giữ cao độ, split thông minh) + QA. Quyết định: `DECISIONS.md` D-64…D-69. Mã: `audio/` (`profile`, `pause`, `split`, `qa`, `wavio`, `ffmpeg`, `processor`, `stage`).

```python
class AudioProcessor(Protocol):
    def qa(self, audio: Path) -> AudioQAReport: ...                                   # nhanh: đọc được, dài > 0
    def qa_full(self, audio: Path, expect: dict, ctx) -> dict: ...                    # {ok, errors[{code,message}], warnings, kind, measures}
    def assemble(self, chunks, pauses_ms, out, ctx) -> dict: ...                      # {duration_sec, timeline[{index,start_sec,end_sec,gap_after_sec,cut_sec}]}
    def master(self, src, out, ctx) -> dict: ...                                       # narration thô -> Narration Master
    def build_youtube_audio(self, master, watermark | None, out, ctx) -> dict: ...
    def build_tiktok_parts(self, master, speed, target_part_sec, out_dir, ctx, timeline=None) -> dict:
        # {"parts": [{path, index, start_sec, end_sec, duration_sec, boundary, forced, mid_sentence}], "stretch": {...}, "split": {...}, "warnings": [...]}
    def health(self) -> dict: ...
```
- **Cài đặt:** `FfmpegAudio` (`adapters.audio = "ffmpeg"`; `tools.ffmpeg`/`tools.ffprobe` ở config máy, mặc định tìm trên PATH). `FakeAudio` (Phase 1) thực hiện cùng hợp đồng mà không xử lý thật.
- **Profile** (`audio/profile.py`; `params.audio` ghi đè): `format {sample_rate, channels, sample_fmt}` (mặc định 48000/1/s24le, nội bộ lossless), `join {edge{threshold_db, keep_ms, fade_ms, highpass_hz}, pause{scale, min_ms, max_ms, compensate_edge, tail_ms}}`, `master {highpass_hz, compressor{enabled=false, ...}, loudness{enabled, target_lufs=-16, true_peak_db=-1.5, lra=11}, limiter{enabled, ceiling_db=-1, attack_ms, release_ms}}`, `youtube.watermark {position start|end|both, gap_ms, offset_db, fade_ms, trim}`, `tiktok {stretch{engine rubberband|atempo, options}, split{min_ratio, max_ratio, min_last_ratio, fade_ms, bonus_sec{...}, silence{...}}, loudness, limiter}`, `qa {...ngưỡng}`; thêm `params.tiktok {speed=2.0, target_part_sec=600}` và `params.watermark`.
- **Stage `audio`:** `requires audio_master`, `optional audio_timeline` (`Stage.optional`: nạp vào `ctx.inputs` nếu có, không ảnh hưởng planner), `produces narration_master, audio_youtube, audio_tiktok (nhiều part, meta index/duration/boundary/forced/mid_sentence), audio_report`. Stage `tts` sinh thêm `audio_timeline` (§3.4). `audio_report` là JSON (profile, đo đạc, QA từng bản, ranh giới split, cảnh báo).
- **Quy tắc:** `speed`, `target_part_sec` đến từ profile/config; split ưu tiên ranh giới câu/đoạn, cho phép dao động (D-67). **Watermark** là asset của channel; đổi watermark chỉ chạy lại stage `audio` (và trong stage chỉ làm lại nhánh YouTube) (D-66).
- **Lỗi:** thiếu ffmpeg/ffprobe → `RESOURCE` (`FFMPEG_MISSING`, job giữ `PAUSED_RESOURCE`); đầy đĩa → `RESOURCE disk`; file hỏng/không decode được → `POLICY` (`AUDIO_DECODE_FAILED`, `AUDIO_UNREADABLE`); thiếu chunk/chunk rỗng → `POLICY` (`MISSING_CHUNKS`, `CHUNK_EMPTY`); audio im lặng/rỗng → `POLICY` (`AUDIO_SILENT`, `WATERMARK_SILENT`); QA không đạt → `POLICY AUDIO_QA_FAILED`; ffmpeg lỗi không rõ → `TRANSIENT`.

## 5. RenderAdapter (đã triển khai ở Phase 5)

**Trách nhiệm:** một audio + profile + source pool ⇒ một video MP4 / thumbnail; chuẩn bị source pool dùng chung. Bọc ContentFlow qua **worker subprocess** (không import, không sửa module). Quyết định: `DECISIONS.md` D-70…D-75. Mã: `render/` (`profile`, `frames`, `pools`, `sync_shim`, `contentflow`, `manager`, `stage`) và `orchestrator/pools.py`.

```python
class RenderAdapter(Protocol):
    requires_pool: bool                                           # True với ContentFlow; FakeRender: False
    def render_video(self, req: dict, ctx) -> dict: ...           # req: audio, audio_sha256, profile (flat), output, pool, key, part?, on_progress?
    def render_thumbnail(self, req: dict, ctx) -> Path: ...       # req: title, channel_name, output, image?, highlight, highlight_text, config_overrides, key
    def prepare_pool(self, pool: dict, ctx=None) -> dict: ...     # Source Sync DÙNG CHUNG: idempotent, trả ngay nếu nguồn không đổi
    def pool_status(self, pool: dict) -> dict: ...                # {name, ready, syncing, reason, fingerprint, raw_files, todo, dir}
    def version(self) -> str: ...                                  # git HEAD ContentFlow + phiên bản worker
    def health(self) -> dict: ...
```
- **Ánh xạ sang `media_worker`:** `render_video` → `type=render`, `inputs=[audio(+sha256), frame, video_dir]`, `params={fps, selection_mode, source_processing, encoder, output_name, config={video_generator: {viewport, video.fps}, …profile.config_overrides}}`, `output_dir` = thư mục của file đích, `idempotency_key` = khóa nội dung do Render Manager đặt; `render_thumbnail` → `type=thumbnail`; `prepare_pool` → shim `sync_shim.py` gọi `source_sync.sync_videos`; `status` → `media_worker status`. Events JSON-lines: `started/progress/artifact/completed/failed`; exit 0/3/4/2.
- **Profile** (`render/profile.py`, D-71): `id, aspect_ratio, resolution, fps, source_pool, selection_mode (shuffle|random|sequential), source_processing (auto|normal|fast), encoder, deadline_s, frame_path?, viewport?, config_overrides, retry{max_attempts, backoff_s}, thumbnail{enabled, highlight, highlight_text, image, config_overrides}` (chỉ YouTube). `config.render = {profiles, pools{<tên>: {raw_dir, sync{size, fps, quality, remove_audio, encoder}}}, pools_dir, pool_sync_background, pool_sync_interval_s}`; `tools.contentflow = {root, python, base_dir, sync_wait_s, verify_output}`.
- **Stage `render_youtube`** (`requires audio_youtube, metadata`; `produces video_youtube, thumbnail, youtube_render_report`) và **`render_tiktok`** (`requires audio_tiktok`; `produces video_tiktok (một per part, meta.index), tiktok_render_report`): `Render Manager` (D-73): khóa nội dung + sidecar, retry riêng từng output, part lỗi TRANSIENT không chặn part khác (`RENDER_PARTS_FAILED`), checkpoint `outputs`/`parts`. Cả hai dùng lane tài nguyên `gpu` (D-74).
- **Lỗi** (D-70): `FFMPEG_MISSING/GPU_UNAVAILABLE`→RESOURCE `runtime`, `DISK_FULL`→RESOURCE `disk`, `MISSING_INPUT`→POLICY `resource=input` (giữ job `PAUSED_MISSING_INPUT`), `INVALID_CONFIG`→POLICY, `FFMPEG_FAILED/TIMEOUT/INTERNAL_ERROR`→TRANSIENT, `RENDER_WORKER_DIED`/`RENDER_OUTPUT_INVALID`→TRANSIENT, hủy→CANCELLED; pool: `POOL_NOT_CONFIGURED`/`MISSING_INPUT`→POLICY `resource=input`, `POOL_SYNC_FAILED`→POLICY, `POOL_SYNC_TIMEOUT`→TRANSIENT.
- **Template (Phase 10, D-92…D-96):** request có thêm `template` (snapshot đã chốt của job: `{id, version, checksum, fingerprint, template, assets, summary}`); khi có, adapter KHÔNG gửi `frame`/`viewport`/`config` mà gửi `params.template` cho `media_worker` (ContentFlow compile bố cục từ template). `req.profile` đã được `profile.apply_template` đặt canvas→`resolution`, fps (nếu template đặt), xóa layout cũ. Không có `template` ⇒ đường tương thích cũ. Adapter có `supports_templates` + thuộc tính `templates` (§13).
- **Cache key:** audio sha256 + profile (gồm `template {id, version, fingerprint}`) + dấu vân tay pool + phiên bản ContentFlow (+ part). Nền ngẫu nhiên không seed (D-08) ⇒ cùng key có thể ra video khác nếu render lại; coi video là artifact đã cache.
- **CLI:** `contentfactory pools [--sync]`, `retry-part <job> <n>`; `status` hiển thị trạng thái từng part.

## 6. PublishAdapter (đã triển khai ở Phase 6)

**Trách nhiệm:** đưa video lên một nền tảng. Hiện chỉ **YouTube** (A14, A15). Quyết định: `DECISIONS.md` D-76. Mã: `publish/yt_uploader.py`, `publish/stage.py`.

```python
class PublishRequest(TypedDict):
    platform: str; video: Path; thumbnail: Path | None            # đường dẫn artifact trong WORKSPACE (không phải output/)
    title: str                  # = publish_metadata.youtube_title (Metadata Builder); uploader không tự nghĩ title
    description: str            # = publish_metadata.description
    tags: list[str]; privacy: str; category: str | None; playlists: list[str]
    made_for_kids: bool         # BẮT BUỘC
    account_id: str | None
    idempotency_key: str        # = stage_key

class PublishAdapter(Protocol):
    platform: str
    def publish(self, req, ctx) -> PublishResult: ...     # tìm-hoặc-tạo job theo key, chờ tới trạng thái cuối; lỗi => StageError có kiểu
    def find(self, idempotency_key: str) -> PublishResult | None: ...
    def health(self) -> dict: ...                          # {ok, features, token}
```
`PublishResult = {state: completed|failed|pending, remote_id, remote_url, warnings, job_id}`.
- **`YtUploaderPublish`:** HTTP client của `yt-uploader serve --headless` (`tools.yt_uploader = {url, data_dir|token, poll_s, max_wait_s, ...}`; Bearer = `api_token`). Luồng: `GET /jobs?idempotency_key=` → (không có: `POST /jobs`) | (completed: trả kết quả cũ) | (lỗi tạm thời lần trước: `POST /jobs/{id}/retry`, daemon probe + resume) → poll `GET /jobs/{id}`. Health yêu cầu features `idempotency_key`, `resume_probe`. Thumbnail > 2 MiB nén ra file tạm bằng ffmpeg.
- **Lỗi:** `quota_exceeded`→RESOURCE `quota` + `resume_after` (reset 00:00 Pacific); `auth_*`/token sai/token thiếu→AUTH `credential`; `network_error`→TRANSIENT `network`; `rate_limited`→TRANSIENT `provider`; `invalid_metadata`/`youtube_rejected`→POLICY; `invalid_file`→POLICY `input`; `database_error`/daemon không chạy→RESOURCE `runtime`; `AMBIGUOUS_UPLOAD`→AMBIGUOUS (người xác nhận rồi `retry?force=true`).
- **Bất biến:** một `idempotency_key` ⇒ tối đa một video trên YouTube; job `completed` không retry được; lỗi vĩnh viễn không bị retry mù.
- **Stage `publish`:** `requires video_youtube, thumbnail, publish_metadata`; adapters `publish`, `sequence`; mặc định đăng: params > Channel Config `publishing` > `config.publishing.defaults`; `made_for_kids` bắt buộc (`MISSING_MADE_FOR_KIDS` = FAILED); thành công ⇒ `sequence.mark_published`, artifact `publish_result {remote_id, remote_url, warnings, job_id, title, sequence}`.
- `TikTokPublishAdapter`: **không tồn tại**; TikTok chỉ được xuất file bởi OutputPublisher (D-06).

## 7. OutputPublisher (đã triển khai ở Phase 6)

**Trách nhiệm:** khi job xong, **sao chép** artifact cuối từ workspace sang gói output của người dùng (HANDOFF §16–17). `workspace/` = hệ thống sở hữu, `output/` = người dùng sở hữu. Không liên quan tới việc đăng nền tảng (đó là PublishAdapter). Quyết định: `DECISIONS.md` D-77, D-78. Mã: `output/publisher.py`, `output/stage.py`, `output/metadata.py`.

```python
class OutputRequest(TypedDict):
    job_id: str; project: dict; youtube_title: str; description: str; output_root: Path
    story: dict; youtube_video: dict; youtube_thumbnail: dict          # {path (workspace), source (đường dẫn workspace tương đối), sha256}
    tiktok_parts: list[dict]                                           # + {index, duration_sec?}
    warnings: list[str]

class OutputPackage(TypedDict):
    project_dir: str; version: int; reused: bool; supersedes: str | None; files: list[str]

class OutputPublisher(Protocol):
    def publish(self, req: OutputRequest, ctx) -> OutputPackage: ...
```

Layout (khớp HANDOFF §16):

```text
output/<yyyymmdd>_<slug>/
  README.txt   project.json   story.txt
  youtube/{video.mp4, thumbnail.jpg, title.txt, description.txt}
  tiktok/{part_01.mp4, part_02.mp4, ...}        # đệm số 0; >=100 part: part_001
```

- **Quy tắc:**
  1. Chỉ **copy**, có kiểm sha256 bản copy so với artifact đã niêm phong; pipeline **không bao giờ đọc lại `output/`** (di chuyển/đổi tên/xóa gói không làm hỏng pipeline, kể cả upload và retry).
  2. Dựng trong `output/.tmp-<job>/` rồi `rename` ⇒ không bao giờ thấy gói nửa vời; lỗi ⇒ dọn tạm, không để lại gì.
  3. **Không sửa âm thầm:** nội dung y hệt gói đã có ⇒ không đụng tới (`reused`); nội dung khác ⇒ gói mới `<tên>-v2`/`-v3` (`version`, `supersedes`), gói cũ giữ nguyên. Tên đụng thư mục của job khác ⇒ hậu tố `-2`.
  4. `project.json` ghi cho từng file: đường dẫn, sha256, kích thước, artifact nguồn (đường dẫn workspace + sha256); manifest nội bộ (version/commit/profile hash) ở lại `workspace/job_x/manifest.json`.
  5. `story.txt` từ artifact đã qua validator bất biến (§2); không chứa cache, chunk, sync, temp.
- **Stage `output`:** `requires metadata`; tùy chọn `story_text`, `tiktok_render_report` (độ dài part) và các nhánh đóng gói `video_youtube + thumbnail` / `video_tiktok` (D-98: nhánh nào nằm trong kế hoạch thì phải có và được đóng gói, nhánh bị bỏ thì không); adapters `output`, `sequence`; `produces output_package, publish_metadata`. Metadata Builder + reserve sequence chạy ở đầu stage này (D-78, D-80).
- **Tên `<project>`** = `<yyyymmdd>_<slug ASCII không dấu của project.title>` (D-07), cấu hình `output.name_template`.

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
| tts | STORY_READY → TTS_RUNNING → AUDIO_READY | TTS Manager + TTSAdapter + AudioProcessor + SegmentPlanner | story_text → audio_master, tts_manifest, audio_timeline |
| audio | AUDIO_READY → AUDIO_PROCESSING* → YOUTUBE_RENDER_READY | AudioProcessor | audio_master (+ audio_timeline tùy chọn) → narration_master, audio_youtube, audio_tiktok, audio_report |
| render_youtube | YOUTUBE_RENDER_READY → YOUTUBE_RENDERING → TIKTOK_RENDER_READY | Render Manager + RenderAdapter (lane gpu) | audio_youtube, metadata → video_youtube, thumbnail, youtube_render_report |
| render_tiktok | TIKTOK_RENDER_READY → TIKTOK_RENDERING → OUTPUT_READY | Render Manager + RenderAdapter (lane gpu) | audio_tiktok → video_tiktok (từng part), tiktok_render_report |
| output | OUTPUT_READY → OUTPUT_PUBLISHING* → UPLOAD_READY | Metadata Builder + OutputPublisher + SequenceManager | metadata (+ story_text tùy chọn; + nhánh video_youtube/thumbnail, video_tiktok theo kế hoạch) → output_package, publish_metadata |
| publish | UPLOAD_READY → UPLOADING* → PUBLISHED | PublishAdapter (**YouTube**, yt_uploader) + SequenceManager | video_youtube, thumbnail, publish_metadata → publish_result |

`*` = state thêm so với danh sách tối thiểu của Phase 1 (cần để mỗi stage có một running state). Terminal: `PUBLISHED`, `FAILED`.

**FAILED** là một state kèm `failed_stage` + `last_error` (lớp lỗi theo `ErrorClass`), không phải một state riêng cho mỗi stage: retry thủ công đưa job về `queue_state` của đúng stage đó. TikTok không có bước đăng (D-06): nhánh TikTok kết thúc bằng file trong output.

Chuyển trạng thái hợp lệ (`pipeline.allowed`, kiểm tra ở mọi lần ghi DB): `queue→running`, `running→done`, `running→queue` (retry có backoff / bị ngắt / dừng có chủ đích), `running→FAILED`, `FAILED→queue_state`.

## 10. Khác biệt Phase 1 so với đặc tả Phase 0 ở trên

| Mục | Đặc tả Phase 0 | Code Phase 1 | Lý do |
|---|---|---|---|
| Kiểu dữ liệu adapter | `ArtifactRef` vào/ra | `Path` vào/ra; handler trả `ArtifactDraft`; orchestrator niêm phong | Adapter không phải hash/ghi DB; sha256 tính đúng một lần tại checkpoint |
| `TTSAdapter.synthesize` | trả `ChunkResult{audio: ArtifactRef}` | nhận `out_path`, trả `{index, duration_sec}` | TTS Manager (handler) kiểm soát tên chunk và resume |
| `AudioProcessor` | theo `ArtifactRef` | theo `Path`; `qa()` trả `AudioQAReport` | như trên. Phase 4 mở rộng: `qa_full`, `master`, `build_tiktok_parts` trả dict (part + stretch + split + cảnh báo), `assemble` trả timeline (§4) |
| `StoryAdapter.generate` | `(SourceBundle, profile, ctx)` | `(SourceBundle{title,language,transcript:Path}, profile, out_dir, ctx)` | adapter biết chỗ ghi |
| `OutputPublisher.publish` | `(job, manifest, cfg)` | `(OutputRequest, ctx)` với đường dẫn artifact | Module không đọc DB/manifest, chỉ nhận artifact |
| `RenderAdapter` | có `sync_source`, `status` | chỉ `render_video`, `render_thumbnail`, `health` | Source Sync và reconcile thuộc Phase 5 |
| `PublishAdapter` | có `find()` | chỉ `publish`, `health` | tra cứu sau crash thuộc Phase 6; Phase 1 dựa vào `idempotency_key = stage_key` |
| `TTSAdapter` | có `capabilities()` đầy đủ | `capabilities()` trả dict tự do | **Phase 3 đã chốt schema** (§3.2): `normalize_capabilities` điền trường thiếu, adapter Phase 1 vẫn dùng được |
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

## 11. Job control (đã triển khai ở Phase 2.9)

> Nguồn: `HANDOFF.md` §15A–§15C; quyết định `DECISIONS.md` D-36…D-42. Mục này là **hợp đồng đích**; phần "Hiện trạng" cho biết code đang ở đâu.

### 11.1 Hiện trạng so với thiết kế

| Năng lực | Mã | Ghi chú |
|---|---|---|
| Stage có `requires`/`produces`, `params_deps`/`config_deps`, `deliverable`, `checkpoint` | ✅ `jobs/pipeline.py` | `Stage.required_inputs/produced_outputs` là bí danh |
| `start_stage`/`target_stage`, `MODES`, planner | ✅ `jobs/plan.py`, `Orchestrator.submit/plan/update_target` (`set_target` = bản không giữ job đã xong) | lỗi spec bị từ chối lúc submit |
| Sửa job: đổi đích + xóa job | ✅ `plan.progress_floor`, `JobStore.update_target/mark_deleted`, `Orchestrator.update_target/delete_job`, `service_jobedit.py` | `PIPELINE_TARGET_INVALID`, `PIPELINE_TARGET_BEFORE_PROGRESS`, `JOB_UPDATE_CONFLICT`, `JOB_CANCELLED`, `JOB_NOT_FOUND` (D-108) |
| Tạm dừng an toàn / Hủy / cập nhật pipeline-config có revision, `clone_job` | ✅ `jobs/db.py` (v4), `contracts.JobCancelToken`, `orchestrator/revisions.py`, `Orchestrator.pause_job/cancel_job/request_update/apply_pending/clone_job` | D-99; điều khiển người dùng tách khỏi hold tài nguyên |
| Pipeline spec v2 (`requested_stages`, đóng kín phụ thuộc, output theo nhánh) | ✅ `jobs/plan.py` (`plan_spec`), `jobs/db.py` (v3 `pipeline_spec`), D-98 | `submit(pipeline=...)`, `cf submit --stages`; `mode/start/target` cũ giữ nguyên |
| Import artifact (`inputs`) và `from_job` | ✅ `Orchestrator._prepare_imports/_register_imports` | validator theo kind, copy vào `import/` |
| Skip khi hợp lệ, `stage_key` theo khai báo | ✅ `orchestrator/stages.py` (`StageContract`) | cache liên job: chưa |
| Hold / `PAUSED_*` / Auto Resume / `resume [--now]` | ✅ `jobs/db.py`, `jobs/policy.py`, `Orchestrator._monitor_tick/resume` | `FAILED` ≡ `FAILED_PERMANENT` |
| Resource Monitor | ✅ khung + probe network/disk/time-based (`orchestrator/monitor.py`) | probe GPU/quota thật: chưa |
| Retry có jitter, `Retry-After` | ✅ `jobs/policy.py` | |
| Config snapshot theo job, `set_job_config` | ✅ `orchestrator/snapshot.py` | |
| Checkpoint chi tiết (`ctx.progress`) | ✅ Source, TTS, Audio, Render | Story/Publish: chưa |
| `pause` chủ động, `cancel`, `rerun --from` | ❌ | D-56 |

### 11.2 JobSpec

```python
class ImportSpec(TypedDict, total=False):
    path: str                       # file ngoài đưa vào, vd story.txt
    from_job: str                   # hoặc tham chiếu artifact của job khác
    stage: str

class JobSpec(TypedDict, total=False):
    params: dict                    # như hiện nay (input, language, story_profile, ...)
    start_stage: str | None         # None = stage đầu
    target_stage: str | None        # None = publish
    inputs: dict[str, ImportSpec]   # kind -> nguồn
    auto_resume: bool | None        # None = thừa kế auto_resume_default tại thời điểm tạo job
```

Thay cho cặp `start_stage/target_stage`, job có thể mang **pipeline spec** `{"version": 2, "requested_stages": [...]}` (không dùng chung với `mode/start/target`); dependency tự suy ra (D-98).

Ràng buộc kiểm lúc tạo job: `start_stage` ≤ `target_stage`; mọi kind trong `requires` của `start_stage` phải được thỏa bởi `inputs` hoặc artifact hợp lệ đã có; mỗi import phải qua validator của kind. Vi phạm ⇒ từ chối (POLICY), không tạo job.

### 11.3 Hợp đồng stage mở rộng

```python
class Stage:                         # mở rộng bảng ở §9
    requires: tuple[str, ...]        # kind đầu vào
    produces: tuple[str, ...]        # kind đầu ra
    validators: dict[str, Validator] # kind -> kiểm tra tính hợp lệ của artifact
    checkpoint: CheckpointSchema     # điểm resume của stage (mô tả bên dưới)
```

| Kind | Validator |
|---|---|
| `story_text` | `story.validate.validate_story_text` (không heading/marker/rỗng/lặp) |
| `audio_master`, `narration_master`, `audio_youtube`, `audio_tiktok` | validator WAV của `orchestrator/validation.py` (đọc header cả WAVE_FORMAT_EXTENSIBLE, frames > 0); QA đầy đủ bằng `AudioProcessor.qa_full` trong stage |
| `audio_timeline`, `audio_report`, `youtube_render_report`, `tiktok_render_report`, `publish_metadata`, `output_package`, `publish_result` | JSON hợp lệ |
| `tts_manifest` | JSON hợp lệ (schema §3.4) |
| `video_youtube`, `video_tiktok`, `thumbnail` | tồn tại, đọc được, kích thước hợp lệ |
| `transcript`, `transcript_structured`, `subtitle_raw`, `metadata` | khớp sha256 + provenance (`raw_sha256`, `parser_version`, `config_hash`) |

**Quy tắc skip:** stage được skip khi mọi kind trong `produces` có artifact mà (a) sha256/kích thước khớp, (b) qua validator, (c) `stage_key` lưu khớp `stage_key` hiện tại. Ngược lại chạy; stage sau trong khoảng `[start_stage, target_stage]` tự chạy lại vì `stage_key` đổi.

**Điểm resume (`checkpoint`):** Source = bước (tải/parse/dựng câu); Story = chương/section chưa commit; TTS = chunk/segment chưa xong; Render TikTok = part chưa xong/lỗi. Handler báo tiến độ (`ctx.progress(done, total, detail)`), orchestrator ghi vào DB.

### 11.4 `StageError` mở rộng và ánh xạ kết quả

```python
class StageError(Exception):
    error_class: ErrorClass
    code: str
    resource: str | None            # network | token | quota | disk | runtime | credential | input
    retry_after_s: float | None     # từ Retry-After của provider
    resume_after: float | None      # epoch: thời điểm reset đã biết (quota/token)
```

| Điều kiện | Kết quả |
|---|---|
| `TRANSIENT`, còn ngân sách | queue lại sau `max(backoff có jitter, retry_after_s)` |
| `TRANSIENT`, hết ngân sách, `resource` ∈ {network, provider} | hold `PAUSED_NETWORK` |
| `TRANSIENT`, hết ngân sách, không phải tài nguyên | `FAILED_PERMANENT` |
| `RESOURCE` | hold ngay: `network→PAUSED_NETWORK`, `token→PAUSED_TOKEN`, `quota→PAUSED_QUOTA`, `disk→PAUSED_DISK`, `runtime→PAUSED_RESOURCE` |
| `AUTH` | hold `PAUSED_CREDENTIAL` |
| `POLICY` + `resource="input"` | hold `PAUSED_MISSING_INPUT` |
| `POLICY` còn lại, `AMBIGUOUS` | `FAILED_PERMANENT` |
| `CANCELLED` | trả về hàng, không tính lỗi |
| `retry_after_s` > ngưỡng (mặc định 10 phút) | không chờ trong hàng: hold (`PAUSED_QUOTA`/`PAUSED_NETWORK`) với `resume_after` |

Hold không tăng `retry_used` và không đổi `state`.

### 11.5 Trường hold của job (DB)

`hold_reason` (null | một trong bảng ở HANDOFF §15B), `hold_detail`, `hold_since`, `resume_after`, `auto_resumes_without_progress`, `needs_user` (bool), `checkpoint` (JSON theo stage), `progress`. `FAILED_PERMANENT` ≡ `state == FAILED` hiện tại. Runner **không nhận** job có `hold_reason`. Chuyển đổi: `hold(job, reason, detail, resume_after)`, `release(job)` (Auto Resume hoặc `resume`), `resume_now(job)` (probe trước).

### 11.6 Resource Monitor

```python
class ResourceStatus(TypedDict):
    resource: str                   # network | provider:<tên> | quota:<tên> | token:<tên> | disk | gpu | credential:<tên>
    ok: bool
    detail: str
    checked_at: float
    next_check_at: float
    retry_after: float | None

class ResourceProbe(Protocol):
    resource: str
    def check(self) -> ResourceStatus: ...       # deterministic, không LLM, có timeout

class ResourceMonitor(Protocol):
    def status(self, resource: str) -> ResourceStatus | None: ...
    def check_due(self) -> list[ResourceStatus]: ...            # chỉ resource đang có job bị giữ, theo cooldown
    def ready_for(self, job) -> bool: ...                       # hold_reason -> resource tương ứng đang ok?
```

- Probe có sẵn dự kiến: network (DNS/TCP), provider health (`adapter.health()`), disk (`shutil.disk_usage` so với ngưỡng stage), gpu/runtime (`nvidia-smi`, ffmpeg encoder, `media_worker health`, engine `health()`), credential (adapter `health()`/token file/hạn dùng), quota/token (**chỉ** thời điểm reset đã biết; không biết thì dựa vào thời gian).
- Cooldown: tăng dần có trần (network 30 s → 5 phút; disk 60 s; quota/token tới `resume_after`); không bao giờ dưới sàn. Kết quả ghi bảng `resource_status`.
- Monitor **không** tự đưa job vào hàng đợi: việc đó thuộc Auto Resume (job có `auto_resume` hiệu lực = true). `auto_resumes_without_progress` ≥ `max_auto_resumes_without_progress` ⇒ `needs_user = true`, dừng tự resume.

### 11.7 Config snapshot

```python
class JobConfigSnapshot(TypedDict):
    semantic: dict      # adapters/providers, ngôn ngữ, reconstruct, story_branch, tiktok, output template, retry, auto_resume, start/target
    hash: str
    revision: int       # tăng khi người dùng đổi config của job một cách explicit
    created_at: float
```

Loại khỏi snapshot: đường dẫn máy, giới hạn đồng thời, lease/heartbeat, **secrets**. `snapshot(job)` lúc tạo job; `set_config(job, patch) -> revision` là hành động explicit duy nhất làm đổi cấu hình job; stage có `stage_key` bị ảnh hưởng sẽ chạy lại.

### 11.8 Retry policy

```python
class RetryPolicy(TypedDict):
    base_s: float                    # 2
    factor: float                    # 2
    cap_s: float                     # 300
    floor_s: float                   # 1
    jitter: float                    # 0.2
    max_attempts: dict[str, int]     # theo ErrorClass, mặc định TRANSIENT=3
    retry_after_hold_threshold_s: float   # 600
```

`delay = clamp(base_s × factor^n, floor_s, cap_s) × (1 ± jitter)`, rồi `max(delay, retry_after_s)`.

### 11.9 CLI (đã có trừ `pause`)

`submit [--mode M] [--start S] [--target T] [--artifact kind=path]… [--metadata-title T] [--from-job ID] [--auto-resume on|off]`, `plan` (cùng tham số, không tạo job), `resume <job> [--now]`, `config <job> [--auto-resume on|off] [--target STAGE] [--patch JSON]`, `resources`, `status` (hiển thị start/target, hold, tiến độ). Chưa có: `pause`.

## 12. Project, Channel Config và Publishing metadata (đã triển khai ở Phase 6; vài chỗ lệch thiết kế ghi ở D-78, D-79)

> Nguồn: `HANDOFF.md` §4B; quyết định `DECISIONS.md` D-43…D-49. Thumbnail triển khai ở Phase 5; Metadata Builder, Sequence Manager, publish package ở Phase 6. Phase 3 (TTS) và Phase 4 (Audio) **không** phụ thuộc mục này ngoài identifier (Audio còn dùng watermark của channel, vốn là channel asset chứ không phải publishing metadata).

### 12.1 Schema

```python
class ProjectMeta(TypedDict):
    id: str                          # = id job (project 1-1 với job)
    title: str                       # CANONICAL: đúng một nguồn cho mọi tiêu đề
    title_source: str                # user | story | source_default
    channel_id: str                  # khóa tới Channel Config
    language: str
    sequence: int | None             # None tới khi Sequence Manager reserve; sau đó cố định

class ChannelConfig(TypedDict, total=False):
    id: str                          # channel id (YouTube)
    name: str                        # -> thumbnail, {channel_name}
    description_template: str        # biến: {channel_name} {project_title} {sequence}
    thumbnail: dict                  # defaults liên quan thumbnail
    publishing: dict                 # defaults đăng; Phase 6 bổ sung thêm field
    sequence: dict                   # tùy chọn {last_used: int} để nối tiếp số đã có
    watermark: str                   # channel asset (HANDOFF §10)

class PublishMetadata(TypedDict):
    youtube_title: str               # "[Full Audio {sequence}] | {project_title}"
    description: str                 # render từ description_template
    sequence: int
    project_title: str
    channel_name: str
```

`project.title` là field **duy nhất**; mọi giá trị khác là kết quả derive (template/slug), không lưu độc lập.

### 12.2 Metadata Builder (Phase 6)

```python
class MetadataBuilder(Protocol):
    def build(self, project: ProjectMeta, channel: ChannelConfig, sequence: int) -> PublishMetadata: ...
```

- Thuần deterministic, không AI. Template **strict**: biến lạ → lỗi; `{{`/`}}` là dấu ngoặc nhọn.
- Giới hạn của YouTube (title ≤ 100 ký tự, description ≤ 5000 byte, theo `yt_uploader`): vượt → `StageError(POLICY, TITLE_TOO_LONG | DESCRIPTION_TOO_LONG)`. **Không** cắt âm thầm và không đổi `project.title`.
- Vị trí trong pipeline: đầu stage `output` (hoặc stage riêng nếu Phase 6 thấy cần); kết quả là artifact `publish_metadata`, dùng cho `youtube/title.txt`, `youtube/description.txt` của gói output và cho payload của stage `publish`. Uploader nhận title/description đã dựng.

### 12.3 Sequence Manager (Phase 6)

```python
class SequenceManager(Protocol):
    def reserve(self, channel_id: str, project_id: str) -> int: ...   # idempotent: đã reserve thì trả đúng số cũ
    def get(self, project_id: str) -> int | None: ...
    def mark_published(self, project_id: str) -> None: ...
    def release(self, project_id: str) -> None: ...                    # hành động explicit; số đã release không được dùng lại
```

- Lưu trong DB của ContentFactory (dự kiến): `channel_sequences(channel_id, sequence, project_id UNIQUE, status reserved|published|released, reserved_at, published_at, PRIMARY KEY(channel_id, sequence))`.
- `reserve` chạy trong một transaction: nếu project đã có dòng → trả về; nếu chưa → `max(last_used trong Channel Config, max(sequence) của channel) + 1`.
- Sequence là **trạng thái của project**, không phải cấu hình: không nằm trong config snapshot, không đổi khi retry upload hoặc rerender. Số đã reserve không bị cấp lại (cho phép có khoảng trống).

### 12.4 Ai dùng field nào

| Module / stage | Dùng | Không dùng |
|---|---|---|
| source, story, tts | `project.id`, `language` (Story dùng tiêu đề của **tác phẩm nguồn**, không phải `project.title`) | `project.title`, channel, sequence |
| audio | `project.id`, `language`, **watermark của channel** (channel asset, HANDOFF §10) | `project.title`, `channel.name`, sequence, description (publishing metadata) |
| render_youtube (thumbnail) | `project.title`, `channel.name`, `thumbnail.defaults` | sequence |
| render_tiktok | profile, audio part | title, sequence |
| output | `project.title` (slug thư mục, README, `project.json`), `PublishMetadata` (title.txt, description.txt) | — |
| publish | `PublishMetadata`, `publishing.defaults`, `sequence` | tự sinh title/description |

**Khai báo phụ thuộc:** `stage_key` của một stage chỉ băm các tham số/field mà stage **khai báo** là phụ thuộc, nên đổi `project.title` chỉ làm render_youtube/output/publish chạy lại, **không** làm TTS/Audio chạy lại (D-48).

### 12.5 Hiện trạng sau Phase 6 (so với thiết kế đích)

| Nơi | Hiện trạng |
|---|---|
| `contracts.project_of` | `project.title` = `params.project.title` (`title_source` user/story) hoặc, nếu chưa đặt, tiêu đề video nguồn (`source_default`, có cảnh báo; `publishing.title_policy=require` thì chặn) |
| `render/manager.py` (thumbnail) | `project.title` + `channel.name` (D-44) |
| `output/metadata.py` | Metadata Builder strict; nằm ở package `output/` (luật cô lập module), chạy đầu stage `output`, artifact `publish_metadata` |
| `output/*`, `publish/stage.py` | `title.txt`/`description.txt` và payload upload cùng đọc `publish_metadata`; không còn "300 ký tự đầu của story" |
| Channel Config | `channels/<id>/channel.json` (JSON; YAML chỉ khi có PyYAML), snapshot theo job, `orchestrator/channels.py` |
| Sequence | `jobs/sequences.py` + bảng `channel_sequences` (DB v2); CLI `sequences`, `sequence-release` |
| `story/stage.py` | giữ nguyên (tiêu đề của tác phẩm nguồn) |
| `stage_key` | chỉ tham số/config khai báo (từ Phase 2.9); `project`/`channel_config` chỉ nằm ở render_youtube/output/publish |


## 13. Template/Asset (Phase 10; D-92…D-97)

**Trách nhiệm:** ContentFlow sở hữu template + asset; ContentFactory chọn ID, chốt version, snapshot. Giao diện giữa hai bên:

```python
class TemplateApi:            # = render.templates (ContentFlowRender: TemplateClient -> `python -m templating <cmd>`; FakeRender: FakeTemplateApi)
    list_templates(type=None, status=None, scope=None, include_archived=False) -> {"templates": [{id, name, type, scope, latest_published, latest, draft, versions[], canvas…}]}
    get_template(id, version="latest"|"latest_published"|N) -> {template, scope, checksum, versions, summary, assets, validation}
    resolve(id, policy="latest_published"|N, expect_type=None) -> snapshot       # {schema, id, version, type, name, scope, status, policy, checksum, fingerprint, template, assets{id:{sha256,type,scope,path}}, summary{canvas,fps,source_region}}
    resolve_many([{key, id, policy, expect_type}]) -> {key: snapshot}              # 1 tiến trình cho cả 3 kind; lỗi kèm detail.key
    create_draft / duplicate / new_draft / save_draft / publish / archive / delete_draft / validate / preview / test_render
    list_assets / get_asset / validate_asset / asset_path / import_asset / delete_asset / info / migrate_legacy
```

- **Lỗi** → `StageError`: vấn đề template/asset người dùng sửa được ⇒ `POLICY` (mã của ContentFlow: `TEMPLATE_NOT_FOUND`, `NO_PUBLISHED_VERSION`, `TEMPLATE_WRONG_TYPE`, `ASSET_IN_USE`…, `resource="input"`); không chạy được ContentFlow ⇒ `RESOURCE CONTENTFLOW_MISSING`; module chưa có `templating` ⇒ `RESOURCE TEMPLATES_UNAVAILABLE` (tạo job rơi về layout cũ + quyết định). `media_worker`: template/asset thiếu hoặc đổi ⇒ `MISSING_INPUT` (POLICY, giữ job); template hỏng/sai loại ⇒ `INVALID_CONFIG`.
- **Chọn lúc tạo job** (`orchestrator/templates.py`): ưu tiên `params.templates` (đã là snapshot: giữ; hoặc tham chiếu) > `channel.templates` > layout cũ (nếu có) > `config.templates.defaults`. Kết quả: `params.templates{thumbnail, youtube, tiktok}` + `params.auto` ("template.<kind> = id@vN vì …"). `fallback` chỉ khi kênh khai. Hành động explicit: `Orchestrator.retemplate(job, kind, id, policy)`.
- **`stage_key`:** `render_youtube` ← `templates.youtube`, `templates.thumbnail`; `render_tiktok` ← `templates.tiktok`.
- **CLI:** `cf templates list|show|use|validate|publish|archive|duplicate|preview|test-render|assets|migrate`, `cf retemplate <job> <kind> <id>` (chọn lại cho job CHƯA xong stage đó), `cf rerender <job>` (job mới dựng lại từ audio cũ bằng template hiện tại). **Doctor:** nhóm *Template* (ContentFlow có hệ thống template, template các kênh còn dùng được, còn layout cũ).
- **API giao diện:** `service_templates.TemplateService` + routes `/api/templates…`, `/api/assets…`, `/api/channels/<id>/templates`.

## 14. Khám phá nguồn YouTube và Channel Run (Agent Plan Phase 4; D-101)
- **Nhận dạng + liệt kê** (`source/discovery.py`, `YtDlp.list_flat`): `classify(url)` → `{provider:"youtube", kind: video|channel|playlist, id, canonical_url}` (không cần mạng; từ chối link không phải YouTube); `Discovery.inspect` thêm tiêu đề/kênh nguồn (một lần liệt kê 1 mục); `Discovery.discover(url, selection, filters, processed)` → ứng viên mới nhất trước kèm `selected`/`skip_reason`/`processed_job`, `truncated`, `requires_confirmation`. Chỉ metadata (`--flat-playlist -J`), không tải media, không tạo job. Chọn: `newest N` (mặc định 10, **sau** khi lọc) | `oldest N` | `range A..B` | `dates` | `manual`; lọc: đã xử lý (BẬT), livestream (BẬT, không quét tab Live), sắp công chiếu (BẬT), Shorts (TẮT). Trần: `batch.max_scan` 300 (quét), `confirm_above` 100 (cần `confirm_large`), `hard_max` 500.
- **Schema v5**: `jobs.batch_id`, `jobs.source_key` (`youtube:<video_id>`), `jobs.channel_id` (kênh xuất bản); chỉ mục duy nhất `(batch_id, source_key)`; bảng `batches` (nguồn, kênh xuất bản, `selection_spec`, `pipeline_spec`, `options`, `control_state`, `request_id` duy nhất) và `batch_items` (vị trí, video, `job_id`, `status` pending|created|error|cancelled). Job cũ giữ `batch_id=NULL` = Single Job; `channel_id` backfill từ params.
- **`BatchService`** (`orchestrator/batches.py`; `Orchestrator.batch_service()`): `create` (idempotent theo `request_id`; kiểm cấu hình con TRƯỚC khi ghi gì; ghi batch + mọi item trong một transaction rồi tạo job con tuần tự), `ensure_created` (hoàn tất item `pending` sau crash — chạy lúc runner khởi động), `detail/list` (trạng thái batch SUY RA: QUEUED|RUNNING|PAUSED|NEEDS_ATTENTION|COMPLETED|COMPLETED_WITH_ERRORS|CANCELLED), `pause/resume` (pause_origin BATCH; không đụng pause của USER hay hold tài nguyên), `retry_failed`, `cancel_queued`, `cancel`, `update_pipeline(target_stage, scope: unstarted|unfinished|selected|all_compatible)` (đúng `Orchestrator.update_target` của Sửa job cho từng job: progress floor kiểm riêng, báo từng kết quả; D-108), `rescan` (chỉ video đăng SAU video mới nhất đã có). Batch chỉ enqueue: concurrency do lane tài nguyên quyết định; một job con lỗi không dừng batch.
- **Nguồn/liên kết chuẩn của job**: `params.source = {provider, video_id, video_url, channel_id?, channel_url?, channel_title?}` do backend dựng từ id đã kiểm; API job trả `links {source_video_url, source_channel_url, published_video_url}` (chỉ https tới youtube.com/youtu.be) — frontend không tự đoán URL.
- **API**: `POST /api/sources/inspect`, `POST /api/sources/youtube/discover`, `GET|POST /api/batches`, `GET /api/batches/{id}?status&limit&offset`, `POST /api/batches/{id}/{pause|resume|retry-failed|cancel-queued|cancel|rescan|target}`; Sửa job: `PUT /api/jobs/{id}/target {target_stage}`, `DELETE /api/jobs/{id}`. CLI: `cf inspect <url>`, `cf batch create|discover|list|status|pause|resume|retry-failed|cancel-queued|cancel|rescan`.

## 15. Image Pool, preflight và lớp điều khiển Job (Agent Plan Phase 8–9; D-105, D-106)
- **Image Pool** (`media/image_pool.py`, package dùng chung như `contracts`/`fsutil`, chỉ phụ thuộc `contracts`): `scan(folder)` (kiểm JPEG/PNG/WebP theo NỘI DUNG, không symlink/file ẩn, ≤ 2 cấp con), `pick(state, rels, mode, rng, avoid)` (hàm thuần: `shuffle|random|sequential`), `ImagePools.assign(pool, job_dir, …)` (rút từ túi bền `image_pool_state` — schema v6, cập nhật nguyên tử — rồi sao chép vào `workspace/job_x/inputs/thumbnail/` + sha256), `resolve_source(job_dir, snap)` (kiểm sha; mất/đổi ⇒ `THUMBNAIL_SOURCE_MISSING|CHANGED`, không tự chọn ảnh khác). Cấu hình `image_pools.<tên>={folder, selection_mode}` (ngoài SEMANTIC_KEYS); kênh chọn bằng `thumbnail.image_pool`.
- **Hợp đồng với stage render:** `params.thumbnail_source = {pool, selection_mode, source_relpath, sha256, file, width, height, format, rerolls}` do Orchestrator chốt lúc tạo job (hoặc lúc mở rộng đích tới render); `render_youtube.params_deps` có `thumbnail_source.sha256`; `RenderManager.youtube` đọc ảnh qua `resolve_source` và khoá thumbnail gồm sha ảnh (video không dựng lại khi chỉ đổi ảnh). `Orchestrator.reroll_thumbnail(job_id)` đi qua `request_update(params_patch)` — cùng impact planner; job đã xong bị chặn + gợi ý Clone.
- **Preflight** (`orchestrator/preflight.py`): `run(orc, run_stages, channel, merged, templates)` → `{checks[{id,label,status ok|warn|fail,detail,hint,stages,blocking}], skipped[], ok, blocking[]}`; chỉ kiểm thứ các stage trong kế hoạch cần (suy từ `Stage.adapters` + nhánh render); chỉ `blocking` mới khoá RUN.
- **Timeline job** (`Service._timeline`): mỗi bước thêm `timeline`, `branch`, `why` (`state` cũ giữ nguyên). **Danh sách job** nhận `q, kind, channel, days`. **Hàng loạt** nhận `args` (`update_pipeline` = `{target_stage}`, `template`). **Dashboard** `GET /api/dashboard`.
- **Ranh giới import:** `media` được xếp cùng nhóm `contracts`/`fsutil` trong `tests/test_architecture.py` (module render được dùng nó, không import module nghiệp vụ khác).
