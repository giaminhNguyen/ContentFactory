# ContentFactory - Technical Handoff

> Handoff kiến trúc đã chốt từ quá trình trao đổi.
> **Cập nhật (tích hợp Subtitle_supperVip):** Source/Subtitle nay là một `SourceAdapter` có nhiều provider, `Subtitle_supperVip` là provider chính, ContentFactory vẫn là orchestrator duy nhất giữ state. Xem **§2A Current Integrations** và **§4A Source / Subtitle**. Các điểm đã lệch khỏi bản thiết kế đầu tiên được chỉnh trực tiếp trong tài liệu này; lý do và bằng chứng nằm ở `docs/` (`CURRENT_SYSTEM_AUDIT.md`, `DECISIONS.md`).
> **Cập nhật (Phase 7):** UX mặc định là Auto Mode: `cf go <URL> --channel K` (preset kênh nhớ TTS profile/pool/render/watermark/publishing; ưu tiên `params > preset > mặc định`; mọi lựa chọn tự động được ghi vào `params.auto`). Xem D-82…D-87, `README.md` (hướng dẫn người dùng).
> **Cập nhật (Phase 9):** có giao diện web cục bộ `cf ui` (D-89): dán link → chọn kênh → RUN → Mở output; nhận dạng đầu vào, chế độ một phần, giải thích trạng thái giữ/lỗi, kênh/TTS/pool/cài đặt/Doctor; `cf samples` tạo dữ liệu mẫu để thử (D-90). Xem `docs/UI_GUIDE.md`.
> Mục tiêu: một pipeline duy nhất biến một nguồn truyện/video đầu vào thành **1 video YouTube hoàn chỉnh** và **nhiều video TikTok theo part**, trong khi hệ thống dễ thay module, dễ debug, dễ setup máy mới và không bắt người dùng phải hiểu chi tiết kỹ thuật.

---

## 1. Mục tiêu sản phẩm

Người dùng chỉ nên quan tâm 2 thứ:

1. **Input**: nguồn để tạo truyện + một số lựa chọn cấp cao nếu cần.
2. **Output**:
   - 1 video YouTube hoàn chỉnh.
   - N video TikTok theo từng part.

Các file trung gian, cache, chunk audio, source sync, render temp, database... là tài sản nội bộ của hệ thống và không được làm rối output người dùng.

---

## 2. Các project/module hiện có

### Source / Subtitle
Repo / implementation:
- `giaminhNguyen/Subtitle_supperVip` — **provider chính** (lấy phụ đề/caption YouTube bằng `youtube-transcript-api`);
- `yt-dlp` — provider dự phòng và nguồn metadata bổ sung (mô tả, kênh);
- provider cho file phụ đề local và văn bản thuần;
- provider khác có thể thêm sau (cùng một `SourceAdapter`).

Nhiệm vụ:
- nhận YouTube URL (hoặc file phụ đề, hoặc văn bản);
- thu thập phụ đề thô (ưu tiên phụ đề có sẵn hơn auto-caption) và metadata;
- trả `SourceResult` chuẩn hóa; việc xử lý transcript thuộc **Transcript Processor** của ContentFactory (xem §4A).

`Subtitle_supperVip` là một ứng dụng quản lý subtitle theo kênh (FastAPI + SQLite + worker + React). ContentFactory **chỉ dùng phần acquisition** của nó; API, hàng đợi, worker, DB và UI của nó không nằm trong pipeline.

### Story
Repo:
- `giaminhNguyen/oh-story-claudecode`
- dùng `story-branch` (và `story-long-write` trong cùng bộ skill)

Nhiệm vụ:
- nhận clean transcript làm "tác phẩm gốc";
- `story-branch` rút canon và dựng một nhánh truyện độc lập (nó **chỉ chuẩn bị tư liệu**, không viết văn);
- `story-long-write` viết truyện dài theo từng chương, kèm continuity (blueprint, đại cương, tracking nằm trong workspace nội bộ);
- `StoryAdapter` điều khiển cả chuỗi (oh-story chạy trong Claude Code CLI, không phải thư viện) và không sửa oh-story;
- chương/section chỉ là nội bộ: **Story Assembler** (của ContentFactory) dựng output publish cuối cùng thành một truyện liền mạch, không có header kiểu `Chapter 1`, `Chapter 2`, `Section 1`...

### Media / Render
Repo:
- `giaminhNguyen/ContentFlow`

Nhiệm vụ:
- thumbnail (dùng `channel.name` + `project.title`, §4B);
- render video;
- lấy video nguồn random từ source folder được truyền vào;
- source có thể được sync/normalize trước và tái sử dụng;
- render profile riêng cho YouTube và TikTok.

### Upload
Repo:
- `giaminhNguyen/yt_uploader`

Nhiệm vụ:
- upload video YouTube cùng metadata/thumbnail tương ứng; metadata (title, description) do Metadata Builder của ContentFactory dựng (§4B), uploader **không tự nghĩ title**.

### TTS
Module mới cần xây.

Nhiệm vụ:
- biến full story thành master audio;
- support nhiều TTS engine khác nhau: local, API, custom service;
- có adapter chung;
- mỗi engine có rule/profile riêng để tối ưu cách chia câu, đoạn, pause và parameter.

### 2A. Current Integrations

| Vai trò | Implementation hiện tại | Cách tích hợp | Trạng thái |
|---|---|---|---|
| Source / Subtitle | `Subtitle_supperVip` (provider chính), `yt-dlp` (fallback) | `SourceAdapter` (ProviderChain) → bridge subprocess gọi lại code acquisition của Subtitle_supperVip | đã tích hợp |
| Transcript | Transcript Processor của ContentFactory | trong ContentFactory (parser có timestamp → structured → clean) | đã tích hợp |
| Story | `oh-story-claudecode` | `StoryBranchAdapter` (Claude Code CLI headless) + Story Assembler | đã tích hợp, chưa kiểm chứng với LLM thật |
| Render | `ContentFlow` | `media_worker` subprocess (JSON-lines) | chưa tích hợp |
| Upload | `yt_uploader` | daemon HTTP headless | chưa tích hợp |
| TTS | modular TTS framework | Phase 3 | chưa bắt đầu |
| Publishing metadata | Metadata Builder + Sequence Manager + Channel Config (§4B) | trong ContentFactory | thiết kế; thumbnail ở Phase 5, phần còn lại ở Phase 6 |

Nguyên tắc chung: ContentFactory là **orchestrator cấp cao duy nhất**. DB của ContentFactory giữ state pipeline, state từng stage, tham chiếu artifact, trạng thái retry/resume. Mọi DB/queue/worker nội bộ của module bên ngoài (nếu có) chỉ là chi tiết cài đặt của provider và không bao giờ quyết định một job đang ở stage nào.

---

## 3. Kiến trúc tổng thể

Không gộp logic tất cả thành monolith.

Dùng một hệ thống tổng với các module độc lập:

```text
INPUT
  |
  v
Source Adapter  (providers: Subtitle_supperVip | yt-dlp | local file | plain text)
  |
  v
Transcript Processor  (raw subtitle -> structured -> clean)
  |
  v
Story Adapter  (oh-story: story-branch + story-long-write)
  |
  v
Story Assembler
  |
  v
Full Story
  |
  v
TTS Module
  |
  v
Master Audio
  |
  +-----------------------------+
  |                             |
  v                             v
YouTube Builder             TikTok Builder
  |                             |
  v                             v
YouTube Render              TikTok Split/Render
  |                             |
  v                             v
1 YouTube MP4               N TikTok MP4
  |                             |
  +--------------+--------------+
                 |
                 v
           Output Publisher
```

Nguyên tắc cốt lõi:

> Module không gọi chặt trực tiếp lẫn nhau. Module giao tiếp bằng job state + artifact + manifest.

> Pipeline **không bắt buộc chạy từ đầu đến cuối**: mỗi stage là một đơn vị độc lập với input/output artifact rõ ràng (§15A).

---

## 4. Ví dụ một phiên chạy

Input ban đầu có thể là một YouTube URL.

```text
YouTube URL
    |
    v
Source Adapter -> Subtitle_supperVip provider (fallback: yt-dlp)
    |
    v
raw subtitle (giữ nguyên) + metadata
    |
    v
Transcript Processor: timestamp-aware parse -> structured transcript (còn timestamp)
                      -> caption reconstruction -> duplicate cleanup -> punctuation/paragraph
    |
    v
clean transcript (không timestamp)
    |
    v
Story Adapter: story-branch (canon, nhánh) -> story-long-write (chương nội bộ + continuity)
    |
    v
Story Assembler
    |
    v
FULL STORY
(no chapter headers)
    |
    v
TTS Module
    |
    v
MASTER AUDIO
    |
    +------------------------+
    |                        |
    v                        v
YouTube audio            TikTok audio path
    |                        |
watermark + master          speed x2
    |                        |
    v                        v
YouTube render            split by target duration
    |                        |
    v                        v
video.mp4              part_01 audio, part_02...
                             |
                             v
                        TikTok render
                             |
                      part_01.mp4...
```

---

## 4A. Source / Subtitle

```text
YouTube URL
    |
    v
SourceAdapter  (ProviderChain, ContentFactory sở hữu)
    |-- SubtitleSupperVipProvider --> bridge --> code acquisition của Subtitle_supperVip (chính)
    |-- YtDlpProvider             (dự phòng + metadata bổ sung)
    |-- LocalSubtitleProvider     (srt / vtt / json / txt trên đĩa)
    |-- PlainTextProvider         (văn bản thuần, không timestamp)
    |
    v
SourceResult
    |
    v
Transcript Processor  (ContentFactory sở hữu)
    |
    v
Story pipeline
```

`SourceResult` tối thiểu: `source_url`, `source_type` (`youtube` / `local_subtitle` / `plain_text`), `provider`, `video_id`, `title`, `description` (của nguồn, nếu có), `language`, `raw_subtitle_path`, `subtitle_format` (`srt` / `vtt` / `json` / `txt`), `metadata`, `status`, `error`; thêm `subtitle_kind` (`manual` / `auto` / `translated`), `has_timestamps`, `attempts` (nhật ký provider đã thử).

**Quy tắc đã chốt**

1. `Subtitle_supperVip` là **implementation của SourceAdapter**, không phải orchestrator. ContentFactory giữ toàn bộ state pipeline; DB/queue/worker của Subtitle_supperVip không được dùng và không bao giờ quyết định stage của một job.
2. `SourceAdapter` không hardcode một provider: thêm provider mới = thêm một lớp tuân thủ `SourceProvider`, không đổi pipeline.
3. Chọn phụ đề: **có sẵn (thủ công) trước, auto-caption sau**, cuối cùng thủ công ở ngôn ngữ bất kỳ; mặc định không dùng bản dịch máy của YouTube. Provider lỗi thì chuyển provider kế (trừ lỗi dứt khoát về video: URL sai, video không tồn tại).
4. **Timestamp không được xóa ngay sau khi tải.** Phụ đề thô được giữ nguyên; structured transcript giữ `start`/`end`/`gap` của cue, câu, đoạn; chỉ clean transcript (sinh sau bước dựng lại) là không có timestamp.
5. **Không coi mỗi dòng caption là một câu.** Timestamp/gap được dùng để nối caption bị cắt giữa câu, phát hiện pause, xác định ranh giới câu và đoạn, hỗ trợ khôi phục dấu câu.
6. Transcript Processor: `raw subtitle -> timestamp-aware parser -> structured transcript -> caption reconstruction -> duplicate cleanup -> punctuation/paragraph reconstruction -> clean transcript`.
7. Artifact của stage (workspace của job):

```text
workspace/<job>/source/
  source.json                   SourceResult + tóm tắt transcript
  subtitle_raw.<srt|vtt|json|txt>
  transcript_structured.json
  transcript_clean.txt
```

8. **Cache / rerun:** phụ đề đã có và còn hợp lệ (sha256 + khóa theo nguồn và ngôn ngữ ưu tiên) thì không tải lại, kể cả khi là job mới cho cùng video; transcript không dựng lại nếu raw, định dạng, phiên bản processor và cấu hình không đổi; Story không chạy lại nếu transcript đầu vào không đổi và artifact Story còn hợp lệ. Đổi đầu vào thượng nguồn thì vô hiệu hóa phần phía sau.
9. Mô tả/tiêu đề của video nguồn **không** được dùng làm mô tả video của sản phẩm. Tiêu đề của sản phẩm là `project.title` (§4B); mô tả lấy từ template trong Channel Config (§4B).
10. Subtitle_supperVip cần Python env riêng có `youtube-transcript-api` (không cài vào tiến trình orchestrator); `doctor` phải kiểm tra.

---

## 4B. Project metadata, Channel Config và Publishing metadata

> **Trạng thái:** **đã triển khai ở Phase 6** (`docs/DECISIONS.md` D-78…D-80, `docs/MODULE_CONTRACTS.md` §12): `project.title`, Metadata Builder, Channel Config dạng `channels/<id>/channel.json` (JSON thay cho YAML, D-79), Sequence Manager. Blockquote "chưa triển khai" bên dưới là bản gốc.

> **Trạng thái:** thiết kế đã chốt, **chưa triển khai** phần metadata. Thumbnail đã tích hợp ở Phase 5 (renderer thật, nhưng tiêu đề vẫn là placeholder `meta["title"]`); `project.title`, Metadata Builder, Sequence Manager và publish package làm ở **Phase 6 (Publishing)**. Xem `docs/DECISIONS.md` D-43…D-49, `docs/MODULE_CONTRACTS.md` §12.

### Canonical project title

Mỗi project có **đúng một** field tiêu đề chính: `project.title` (ví dụ `Tôi Trùng Sinh Quyết Tâm Làm Hại Nữ Chính`). Project hiện tương ứng 1-1 với job (`project.id` = id job).

- Mọi nơi khác cần tiêu đề (thumbnail, YouTube title, tên thư mục output, README, `project.json`) đều **derive thuần túy** từ `project.title` bằng template/slug. Không lưu thêm bản title độc lập có thể lệch khỏi giá trị gốc.
- Downstream phải dùng `project.title` làm nguồn chính.
- `project.title` kèm provenance `title_source` ∈ `user` (người dùng nhập, khuyến nghị) | `story` (dành cho bước sinh tiêu đề của Story sau này, nếu có; vẫn ghi vào đúng một field) | `source_default` (tạm dùng tiêu đề video nguồn khi người dùng chưa nhập; Publishing phải cảnh báo).
- Đổi `project.title` chỉ làm các artifact phụ thuộc nó chạy lại (thumbnail, output package, publish payload); **không** làm TTS/Audio chạy lại.

### Thumbnail

- Dùng `channel.name` (lấy từ Channel Config) và `project.title`.
- Tiêu đề thumbnail **chính là** `project.title`; **không** dùng AI để tạo một thumbnail title khác.
- Title dài: renderer xử lý bằng layout / wrapping / font sizing, **không** âm thầm đổi hay cắt canonical title; nếu không vừa ở cỡ chữ tối thiểu thì báo lỗi rõ ràng.
- Thực hiện ở Phase 5 (Render).

### YouTube title

Template cố định:

```text
[Full Audio {sequence}] | {project_title}
```

Ví dụ: `[Full Audio 27] | Tôi Trùng Sinh Quyết Tâm Làm Hại Nữ Chính`, với `project_title` = `project.title` và `sequence` = số thứ tự Full Audio của channel. **Uploader không tự nghĩ title**: nó nhận title đã dựng. Title dựng ra dài hơn giới hạn của YouTube (100 ký tự) thì báo lỗi, không cắt âm thầm.

### Description

Lấy từ **template chung trong Channel Config**, hỗ trợ biến `{channel_name}`, `{project_title}`, `{sequence}`. Render template là code thường, không dùng AI; biến lạ là lỗi; `{{`/`}}` để viết dấu ngoặc nhọn; vượt giới hạn mô tả (5000 byte) thì báo lỗi, không cắt âm thầm.

### Channel Config

Channel Config (`channels/<id>/channel.yaml`, cùng chỗ với watermark ở §10) chứa tối thiểu:

```yaml
channel:
  id: "UCxxxxxxxxxxxxxxxxxxxxxx"     # channel id (YouTube)
  name: "Tên kênh"                   # -> thumbnail và {channel_name}
description_template: |
  {project_title}
  Full Audio {sequence} của kênh {channel_name}.
thumbnail:
  defaults: {}                       # mặc định liên quan thumbnail của kênh (template, font, ...)
publishing:
  defaults: {}                       # mặc định đăng (privacy, category, made_for_kids, tags, ...)
sequence:
  last_used: 26                      # tùy chọn: nối tiếp số Full Audio đã có của kênh
```

Các field publishing khác được bổ sung ở Phase 6. Channel Config được đọc và **snapshot theo job** lúc bắt đầu (§15C); secrets không nằm trong Channel Config.

### Sequence / Full Audio STT

- Mỗi channel có **sequence riêng**.
- Mô hình **reserve**: một project được cấp (reserve) sequence **một lần** và sequence đó được lưu cố định với project. Retry upload hoặc rerender **không bao giờ** đổi sequence; không tính lại sequence mỗi lần upload.
- Reserve lười, ở lần cần đầu tiên (Metadata Builder), chứ không phải lúc tạo job, và idempotent: gọi lại trả về đúng số đã cấp.
- Chi tiết Sequence Manager thực hiện ở Phase 6.

### Trách nhiệm theo phase

| Phase | Trách nhiệm |
|---|---|
| 3 TTS, 4 Audio | **Không** phụ thuộc publishing metadata (`project.title`, `channel.name`, sequence, description); chỉ dùng identifier thật sự cần (`project.id`/job id, `language`). Audio dùng watermark của channel vì đó là channel asset (§10), không phải publishing metadata |
| 5 Render | dùng `channel.name` + `project.title` để render thumbnail |
| 6 Publishing | Metadata Builder; YouTube title template; description template; Sequence Manager (reserve/lưu); publish package; tích hợp `yt_uploader` |

---

## 5. Logic tạo truyện

`story-branch` và `story-long-write` (oh-story) dùng chapter/section nội bộ, nhưng đó không phải output cuối. `story-branch` chỉ chuẩn bị tư liệu nhánh; phần viết do `story-long-write`; `StoryAdapter` điều khiển cả hai và trả về danh sách section cho Story Assembler.

Ví dụ nội bộ:

```text
Section 01
Section 02
Section 03
...
```

Sau đó Story Assembler phải:

- bỏ chapter/section heading;
- bỏ marker kỹ thuật;
- xử lý transition giữa các đoạn;
- kiểm tra lặp;
- kiểm tra continuity;
- tạo một văn bản đọc liên tục từ đầu đến cuối.

Output publish:

```text
story.txt
```

Không có:

```text
Chapter 1
Chapter 2
Part 1
Section 3
```

Audio full dự kiến thường khoảng 40-60 phút ở tốc độ YouTube.

---

## 6. TTS module - kiến trúc đã chốt

> **Trạng thái:** framework **đã triển khai ở Phase 3** (Manager, Planner + Validator, retry theo segment, cache, manifest; xem `docs/MODULE_CONTRACTS.md` §3, `docs/DECISIONS.md` D-57…D-63). **Chưa có engine TTS thật**, chưa kiểm chứng với LLM thật. Phần mô tả dưới đây là thiết kế gốc; nơi khác biệt, tài liệu `docs/` là chuẩn.

### 6.1 Core flow

```text
story.txt
   |
   v
Text Preprocessor
   |
   v
TTS Profile / Rule
   |
   v
AI Segment Planner
   |
   v
Rule Validator
   |
   v
segments.json
   |
   v
TTS Manager
   |
   v
Selected Adapter
   |
   v
Audio Chunks
   |
   v
Audio QA
   |
   v
Audio Assembler
   |
   v
master_audio
```

### 6.2 Adapter

Mỗi TTS engine implement một contract chung.

Ví dụ adapter:

```text
tts/adapters/
  base
  edge_tts
  elevenlabs
  xtts
  fish_speech
  custom_api
  ...
```

Pipeline ngoài chỉ biết:

```text
tts.generate(story, profile)
```

Nó không cần biết engine là local hay API.

### 6.3 Rule/Profile theo engine

Không dùng một cách chia text cho mọi TTS.

Mỗi engine có profile riêng, ví dụ:

```yaml
engine: example_tts

segment:
  preferred_chars: 350
  max_chars: 600
  min_chars: 80

break_priority:
  - paragraph
  - sentence
  - semicolon
  - comma

pause:
  paragraph_ms: 600
  sentence_ms: 250
  dialogue_ms: 350
```

Rule dùng để hướng dẫn AI Segment Planner.

### 6.4 AI Segment Planner

AI được phép quyết định các điểm ngắt tự nhiên dựa trên rule của engine:

- không cắt giữa câu;
- không cắt tên riêng;
- giữ context hội thoại;
- ưu tiên semantic boundary;
- tạo pause hợp lý;
- giữ segment trong range phù hợp với engine.

AI không có quyền phá giới hạn hard-limit của engine.

Sau AI phải có validator deterministic.

### 6.5 Validator

Kiểm tra:

- max chars/tokens;
- min length;
- quote chưa đóng;
- segment rỗng;
- segment quá ngắn;
- split bất hợp lý;
- giới hạn API/model;
- ký tự không hỗ trợ nếu engine có hạn chế.

### 6.6 Audio chunk + retry

Không render một truyện 40-60 phút bằng một request duy nhất.

Tạo chunk:

```text
tts/chunks/
  000001.wav
  000002.wav
  000003.wav
  ...
```

Nếu chunk 003 lỗi thì chỉ retry chunk 003.

### 6.7 Cache

Cache key phải gồm tối thiểu:

```text
text
+ engine
+ model
+ voice
+ relevant engine settings
+ rule/profile version
```

Đổi watermark/video/thumbnail không được làm TTS chạy lại.

### 6.8 Audio QA

Tối thiểu kiểm tra:

- file tồn tại;
- decode được;
- duration > 0;
- không silent bất thường;
- duration/text ratio không bất thường quá mức;
- không corrupt.

Chunk lỗi quay lại retry queue.

---

## 7. TTS Auto-Profile - hướng mới đã chốt

> **Trạng thái:** **đã triển khai khung ở Phase 3**: TTS Analyzer tĩnh (repo/docs/source → capabilities, profile candidate có `source`/`confidence`/evidence, ứng viên adapter, `needs_user`) và Auto Tune (benchmark nội bộ). Mới thử với repo mẫu và engine giả/CLI mẫu; phân tích bằng AI thật và engine thật là Phase 8 (`docs/DECISIONS.md` D-61, D-62).

Người dùng không muốn tự nghiên cứu thông số từng TTS.

Input ưu tiên cho việc thêm TTS mới sẽ chỉ là một **TTS source reference**, ví dụ:

- GitHub repository;
- official documentation;
- package/library source;
- API documentation;
- source code dự án local;
- ví dụ code nhà sản xuất cung cấp.

Sau đó AI/TTS Analyzer tự làm:

```text
TTS Source Reference
      |
      v
Read source + docs + examples
      |
      v
Detect engine capabilities
      |
      +-- input limits
      +-- supported languages
      +-- voice selection
      +-- speed support
      +-- emotion/style support
      +-- SSML support
      +-- streaming/batch support
      +-- output format/sample rate
      +-- local/API requirements
      +-- recommended usage from vendor
      |
      v
Generate Adapter Draft
      |
      v
Generate Candidate Profile
      |
      v
Generate Segmentation Rule
      |
      v
Validation / optional benchmark
      |
      v
Ready TTS Profile
```

### Mục tiêu UX

Thay vì bắt người dùng nhập:

```text
max chars?
recommended chunk length?
pause bao nhiêu?
SSML hay không?
parameter nào?
```

người dùng chỉ cần đưa:

```text
repo/docs/source của TTS
```

AI sẽ tự nghiên cứu phần còn lại.

### Những gì có thể tự suy ra tốt

AI có thể tự tìm từ source/docs/vendor examples:

- cách gọi engine;
- dependency;
- API/local mode;
- model/voice parameter;
- hard limits;
- output format;
- supported language;
- sample rate;
- SSML hoặc pause mechanisms;
- vendor recommendations;
- concurrency/streaming capabilities;
- retry/error behavior;
- authentication requirements;
- reference-audio requirement;
- các default hợp lý.

### Những gì không nên giả định là tuyệt đối đúng

Các yếu tố mang tính cảm nhận:

- chunk dài bao nhiêu nghe tự nhiên nhất;
- pause nào hay nhất cho thể loại truyện cụ thể;
- voice nào người dùng thích nhất;
- emotion/style nào phù hợp nhất;
- một model có lỗi phát âm đặc thù với dataset/voice cụ thể hay không.

Vì vậy profile AI sinh ra là **auto-generated candidate profile**, không phải chân lý cố định.

Nên có:

```text
confidence: high / medium / low
source: vendor_docs / source_code / inferred
```

cho các rule quan trọng.

### Optional automatic benchmark

Để giảm tối đa việc người dùng phải suy nghĩ, hệ thống sau này nên có chế độ:

```text
Auto Tune TTS
```

Nó dùng một bộ sample text chuẩn có:

- câu ngắn;
- câu dài;
- hội thoại;
- dấu phẩy;
- dấu chấm;
- dấu hỏi;
- dấu cảm thán;
- dấu ba chấm;
- số;
- ngày tháng;
- tên riêng;
- nhiều paragraph.

Sau đó render tự động vài candidate segmentation/config và dùng rule/metrics để loại cấu hình lỗi.

Nếu có human review thì chỉ cần người dùng chọn phiên bản nghe hay hơn, không phải tự hiểu parameter.

---

## 8. Dữ liệu tối thiểu để thêm một TTS mới

Mục tiêu là yêu cầu người dùng ít nhất có thể.

### Bắt buộc

Chỉ cần một trong các dạng sau:

```text
GitHub repo
hoặc
official docs URL
hoặc
local source folder
hoặc
API documentation
```

### Khi API yêu cầu secret

Credential/API key không đưa vào profile source.

Dùng config/secret riêng:

```text
.env
secret store
machine config
```

### Optional

Nếu người dùng có thì có thể cung cấp thêm:

- voice/reference audio mong muốn;
- một audio mẫu họ thích;
- language mong muốn;
- mục tiêu: story / horror / drama / news...;
- lỗi họ từng gặp.

Nhưng các mục này **không phải điều kiện để bắt đầu tích hợp**.

---

## 9. Master Audio và distribution audio

> **Trạng thái:** **đã triển khai ở Phase 4** (`docs/MODULE_CONTRACTS.md` §4, `docs/DECISIONS.md` D-64…D-69). Chi tiết: `audio_master` = narration thô từ TTS (chuẩn hóa kỹ thuật, dọn biên, pause); **Narration Master** (`narration_master`, đã chỉnh loudness/limiter) do stage `audio` tạo; từ đó mới sinh YouTube/TikTok. Chưa kiểm chứng với giọng TTS thật.

TTS chỉ sinh nội dung truyện một lần.

```text
FULL STORY
    |
    v
TTS
    |
    v
MASTER AUDIO
```

Từ master mới sinh các phiên bản platform.

---

## 10. YouTube Audio

> **Trạng thái:** **đã triển khai ở Phase 4** (D-66): đổi watermark chỉ chạy lại stage `audio`, trong stage chỉ làm lại bản YouTube; phần truyện ghép nguyên từng mẫu.

Watermark là channel asset riêng, không thuộc story.

```text
channels/
  channel_a/
    channel.yaml          # Channel Config (§4B)
    watermark.wav
```

YouTube audio:

```text
watermark.wav
     +
master_audio
     |
     v
youtube_audio
```

Nếu đổi watermark:

- không TTS lại truyện;
- chỉ build lại YouTube distribution audio/video nếu cần.

Watermark phải dễ thay theo channel.

---

## 11. TikTok Audio và Video

> **Trạng thái:** phần **audio** đã triển khai ở Phase 4 (D-67): tăng tốc giữ cao độ (rubberband), split thông minh ~`target_part_sec`, part cuối ngắn hơn được. Phần **video** TikTok **đã triển khai ở Phase 5** (từng part = một video 9:16, D-73).

TikTok cũng phải output thành **video**, không chỉ audio.

Flow:

```text
master_audio
     |
     v
speed x2
     |
     v
split theo target duration
     |
     +-- part_01 audio
     +-- part_02 audio
     +-- ...
            |
            v
       TikTok Renderer
            |
            +-- source video pool
            +-- vertical profile
            +-- fit duration
            |
            v
       part_01.mp4
       part_02.mp4
       ...
```

Mặc định đang thảo luận:

- tốc độ TikTok: x2;
- mỗi video TikTok target khoảng 10 phút sau khi tăng tốc;
- part cuối có thể ngắn hơn.

Các thông số này phải là config, không hardcode.

---

## 12. Render profile

> **Trạng thái:** **đã triển khai ở Phase 5** (`docs/MODULE_CONTRACTS.md` §5, `docs/DECISIONS.md` D-70…D-75): profile YouTube/TikTok ở `config.render.profiles` + `params.render`, chạy thật với ContentFlow. `audio_speed`/`target_part_duration_sec` ở §12 thực tế là `params.tiktok.speed/target_part_sec` (Phase 4).

Ví dụ YouTube:

```yaml
youtube:
  aspect_ratio: "16:9"
  resolution: "1920x1080"
  audio_source: youtube_audio
  source_pool: gameplay
```

TikTok:

```yaml
tiktok:
  aspect_ratio: "9:16"
  resolution: "1080x1920"
  audio_speed: 2.0
  target_part_duration_sec: 600
  source_pool: gameplay_vertical
```

---

## 13. Source Sync

> **Trạng thái:** **đã triển khai ở Phase 5** (D-72): Source Sync nền, dùng chung, chỉ làm lại khi nguồn/tùy chọn đổi; CLI `contentfactory pools`.

Source Sync là background/shared preprocessing task.

Không đặt source normalization vào từng story job.

```text
Raw Source Pool
      |
      v
Background Source Sync
      |
      v
Synced Source Pool
      |
      +---------------------> render jobs reuse
```

Chỉ sync lại khi source/profile thay đổi.

---

## 14. Pipeline concurrency

> **Trạng thái:** phần render **đã triển khai ở Phase 5** (D-74): lane `gpu` riêng, Story/TTS/Audio không bị render chặn (có test). Upload (Phase 6) và vận hành bất đồng bộ đầy đủ (Phase 7) chưa làm.

Không chạy tuyến tính kiểu:

```text
Story 1 -> TTS 1 -> Render 1 -> Upload 1 -> Story 2
```

Mà chạy pipeline bất đồng bộ:

```text
Story N+3   -> generating
TTS N+2     -> generating audio
Render N+1  -> rendering
Upload N    -> uploading
```

Cùng một branch story có dependency continuity nên generation trong branch phải tuân theo state cần thiết.

Các branch/story độc lập có thể chạy song song bằng workspace riêng.

---

## 15. Queue và state

Khuyến nghị local-first:

```text
SQLite + filesystem
```

Không cần Redis/Kafka ở giai đoạn này.

Ví dụ state:

```text
NEW
SOURCE_READY
STORY_RUNNING
STORY_READY
TTS_PLANNING
TTS_RENDERING
MASTER_AUDIO_READY
YOUTUBE_RENDER_READY
YOUTUBE_RENDERING
TIKTOK_RENDER_READY
TIKTOK_RENDERING
OUTPUT_READY
UPLOADING
PUBLISHED
```

Failure state tách riêng theo stage để retry đúng chỗ.

> **Triển khai thực tế** (`docs/DECISIONS.md` D-21): `NEW -> SOURCE_PROCESSING -> SOURCE_READY -> STORY_RUNNING -> STORY_READY -> TTS_RUNNING -> AUDIO_READY -> AUDIO_PROCESSING -> YOUTUBE_RENDER_READY -> YOUTUBE_RENDERING -> TIKTOK_RENDER_READY -> TIKTOK_RENDERING -> OUTPUT_READY -> OUTPUT_PUBLISHING -> UPLOAD_READY -> UPLOADING -> PUBLISHED`, cộng `FAILED` kèm `failed_stage` để retry đúng stage. Danh sách ở trên là bản thiết kế ban đầu.

> Job bị **giữ** vì tài nguyên tạm thời (hold, §15B) **không đổi state**: vị trí trong pipeline được giữ nguyên, hold chỉ ngăn runner nhận job.

---

## 15A. Stage-based artifact pipeline

> **Trạng thái:** **đã triển khai ở Phase 2.9** (xem `docs/IMPLEMENTATION_PHASES.md`, `docs/MODULE_CONTRACTS.md` §11, `docs/DECISIONS.md` D-36…D-42 và D-50…D-56). Phần chưa làm ghi ở D-56.

ContentFactory **không phải pipeline bắt buộc chạy từ đầu đến cuối**. Pipeline là một chuỗi *stage* nối với nhau bằng **artifact**. Mỗi stage phải:

- có input artifact rõ ràng (`requires`) và output artifact rõ ràng (`produces`), khai báo theo **kind** chứ không theo tên stage;
- chạy độc lập, retry độc lập, resume độc lập;
- bị **skip** nếu output hợp lệ đã tồn tại.

Pipeline không được buộc người dùng chạy lại stage thượng nguồn nếu artifact cần thiết đã có và hợp lệ.

### Job: `start_stage` và `target_stage`

| Trường | Ý nghĩa | Mặc định |
|---|---|---|
| `start_stage` | stage đầu tiên được phép chạy; các stage trước nó không chạy | stage đầu của pipeline |
| `target_stage` | stage cuối cùng cần hoàn tất; runner không nhận stage sau nó | `publish` (toàn bộ) |
| `inputs` | artifact đưa từ ngoài vào (`kind -> đường dẫn`) hoặc tham chiếu job khác (`from_job`) | rỗng |

Job **đạt đích** khi job ở `done_state` của `target_stage`.

Use case tối thiểu:

| Use case | `start_stage` | `target_stage` | `inputs` |
|---|---|---|---|
| Chỉ tải subtitle | `source` | `source` | — |
| Chỉ tạo story | `source` | `story` | — (hoặc `start_stage=story` + `transcript`) |
| Chạy đến TTS rồi dừng | mặc định | `tts` | — |
| TTS từ `story.txt` có sẵn | `tts` | `tts` hoặc xa hơn | `story_text = story.txt` |
| Render video từ audio có sẵn | `audio` (hoặc `render_youtube` nếu đã có audio YouTube/TikTok) | `render_tiktok` hoặc xa hơn | `audio_master` (hoặc `audio_youtube` + `audio_tiktok`) + `metadata` (ít nhất tiêu đề) |
| Full pipeline | mặc định | mặc định | — |

### Artifact hợp lệ và skip

Một artifact **hợp lệ** khi: (a) file tồn tại và khớp sha256/kích thước đã ghi; (b) qua **validator của kind** (`story_text`: validator bất biến — không heading, không marker; `audio_*`: kiểm audio; `video_*`: file đọc được); (c) `stage_key` hiện tại khớp, gồm sha256 input, tham số liên quan, phần config snapshot liên quan và phiên bản processor.

- Stage có **mọi** output hợp lệ ⇒ **skip** (ghi nhận `skipped_valid`).
- Có output không hợp lệ ⇒ chạy; stage phía sau trong `[start_stage, target_stage]` có `stage_key` đổi theo nên tự chạy lại; stage ngoài khoảng đó không bị đụng tới.
- Stage **trước** `start_stage` không bao giờ chạy: input của `start_stage` phải có sẵn (import hoặc đã có trong job). Thiếu ⇒ từ chối ngay lúc tạo job; nếu file bị mất lúc đang chạy ⇒ `PAUSED_MISSING_INPUT`.

**Import artifact:** file ngoài được đăng ký như artifact của stage giả `import`, kèm sha256 và provenance (đường dẫn gốc), và **phải qua validator của kind** — một `story.txt` có sẵn vẫn phải qua validator bất biến. **Reuse từ job khác** (`from_job`): copy vào workspace của job mới (workspace vẫn cô lập), ghi provenance.

---

## 15B. Resource pause / resume

> **Trạng thái:** **đã triển khai ở Phase 2.9** (xem `docs/IMPLEMENTATION_PHASES.md`, `docs/MODULE_CONTRACTS.md` §11, `docs/DECISIONS.md` D-36…D-42 và D-50…D-56). Phần chưa làm ghi ở D-56.

Phân biệt **lỗi tài nguyên tạm thời** với **lỗi vĩnh viễn**. Lỗi tài nguyên tạm thời **không phải job failure**: không tiêu ngân sách retry, không ghi lỗi vĩnh viễn.

Job bị **giữ (hold)** kèm lý do; state (vị trí pipeline) không đổi, hold chỉ ngăn runner nhận job.

| Lý do | Nguyên nhân | Điều kiện hồi phục | Auto Resume |
|---|---|---|---|
| `PAUSED_NETWORK` | mất mạng, provider không với tới | probe mạng + health provider OK | có |
| `PAUSED_TOKEN` | hết token/usage của LLM | tới thời điểm reset do provider báo | có (theo thời gian) |
| `PAUSED_QUOTA` | hết quota API (vd YouTube) | `resume_after` từ provider / `Retry-After` / thời điểm reset | có (theo thời gian) |
| `PAUSED_DISK` | đầy đĩa | dung lượng trống ≥ ngưỡng của stage | có |
| `PAUSED_RESOURCE` | GPU / runtime cục bộ / ffmpeg / engine không sẵn sàng | probe runtime OK | có |
| `PAUSED_CREDENTIAL` | credential thiếu / hết hạn / bị thu hồi | credential trở lại hợp lệ (token tự làm mới, hoặc người dùng đăng nhập lại) | chỉ khi probe thấy sẵn sàng; **không** retry theo timer |
| `PAUSED_MISSING_INPUT` | input artifact biến mất hoặc chưa được cung cấp | artifact xuất hiện và hợp lệ | chỉ khi monitor thấy input hợp lệ |
| `FAILED_PERMANENT` | lỗi cấu hình/dữ liệu/logic, `AMBIGUOUS` (cần người xác nhận), hết ngân sách retry mà nguyên nhân không phải tài nguyên | người dùng sửa rồi `retry` | **không** |

`FAILED_PERMANENT` chính là trạng thái `FAILED` (kèm `failed_stage`) đã có trong code; tên cũ được giữ để không phá tương thích.

**Ánh xạ lỗi:** `TRANSIENT` → retry có backoff; hết ngân sách mà nguyên nhân là tài nguyên (mạng, provider) → hold; `RESOURCE` → hold ngay, lý do theo trường `resource` của lỗi; `AUTH` → `PAUSED_CREDENTIAL`; `POLICY` → `FAILED_PERMANENT` (riêng thiếu input → `PAUSED_MISSING_INPUT`); `AMBIGUOUS` → `FAILED_PERMANENT`; `CANCELLED` → trả về hàng, không tính lỗi.

### Checkpoint

Checkpoint phải đủ chi tiết để resume **đúng vị trí**, không làm lại artifact/stage đã hoàn thành:

| Stage | Điểm resume |
|---|---|
| Source | bước chưa xong (tải phụ đề → parse → dựng câu) |
| Story | section/chương chưa hoàn thành (theo tracking của oh-story) |
| TTS | segment/chunk chưa hoàn thành |
| Render YouTube | video/thumbnail chưa xong |
| Render TikTok | part lỗi hoặc chưa render (các part xong giữ nguyên) |

Tiến độ được ghi vào DB (checkpoint + progress) để hiển thị và để đo "có tiến triển hay không".

### Resource Monitor

Code **deterministic**, không dùng AI. Kiểm tra khi phù hợp:

| Resource | Cách kiểm tra |
|---|---|
| network | DNS/TCP tới các host cấu hình |
| API / provider health | `health()` của adapter, `GET /health` |
| quota / token | chỉ khi xác định được (thời điểm reset từ lỗi provider, `Retry-After`); nếu không xác định được thì chỉ dựa vào thời gian, **không đoán** |
| disk | dung lượng trống so với ngưỡng theo stage |
| GPU / runtime cục bộ | `nvidia-smi`, thử encoder ffmpeg, `media_worker health`, `health()` của engine TTS |
| credential | `health()` của adapter, file token, hạn dùng |

Monitor chạy khi stage báo lỗi tài nguyên, định kỳ **chỉ cho resource đang có job bị giữ** (cooldown tăng dần, có sàn tối thiểu), và ngay trước `Resume Now`. Kết quả ghi vào DB để CLI/UI đọc. Monitor cập nhật trạng thái dù Auto Resume bật hay tắt; **chỉ Auto Resume mới được phép đưa job trở lại hàng đợi**.

### Auto Resume

- Cấu hình global `auto_resume_default = true`; mỗi job có `auto_resume = true | false` (không đặt = thừa kế). Giá trị hiệu lực được **chốt vào config snapshot lúc tạo job** (§15C), nên đổi default không âm thầm đổi job đang có.
- **ON:** job đang hold vì điều kiện tự hồi phục sẽ được đưa lại hàng đợi khi resource hợp lệ, resume từ checkpoint, không chạy lại artifact/stage đã xong.
- **OFF:** monitor vẫn cập nhật trạng thái resource (job hiển thị "resource đã sẵn sàng") nhưng job không tự chạy; chỉ tiếp tục khi người dùng gọi **Resume / Resume Now**.
- Chỉ áp dụng cho điều kiện có khả năng tự hồi phục. **Không tự resume vô hạn:** nếu `max_auto_resumes_without_progress` (mặc định 5) lần liên tiếp không có checkpoint tiến thêm thì job giữ nguyên ở trạng thái paused kèm cờ `needs_user` (không chuyển FAILED, không lặp thêm). `FAILED_PERMANENT` và lỗi cấu hình không bao giờ tự resume.
- **Resume Now:** probe resource trước; nếu vẫn hỏng thì giữ hold (cập nhật lý do), không đốt retry.

### Retry policy

- Lỗi tạm thời: exponential backoff **có jitter** (±20%), trần 5 phút, sàn 1 giây, ngân sách theo lớp lỗi.
- **Ưu tiên `Retry-After`** của provider (giây hoặc HTTP-date): `delay = max(backoff, Retry-After)`. Nếu `Retry-After` quá lớn (mặc định > 10 phút) thì không chờ trong hàng đợi mà chuyển thành hold (`PAUSED_QUOTA`/`PAUSED_NETWORK`) với `resume_after`.
- **Không polling/retry quá dày:** probe có cooldown riêng (ví dụ network 30 s tăng gấp đôi tới 5 phút, disk 60 s); nhịp scheduler nội bộ không phải tần suất gọi dịch vụ ngoài.

---

## 15C. Config snapshot theo job

> **Trạng thái:** **đã triển khai ở Phase 2.9** (xem `docs/IMPLEMENTATION_PHASES.md`, `docs/MODULE_CONTRACTS.md` §11, `docs/DECISIONS.md` D-36…D-42 và D-50…D-56). Phần chưa làm ghi ở D-56.

- Lúc **bắt đầu** job, snapshot **cấu hình ngữ nghĩa** hiệu lực vào DB (JSON + hash) và manifest: adapters/providers, ngôn ngữ, cấu hình dựng câu, `story_branch`, `tiktok`, mẫu tên output, retry policy, `auto_resume` (đã resolve từ default), `start_stage`/`target_stage`.
- **Không** snapshot: đường dẫn máy, giới hạn đồng thời theo tài nguyên, lease/heartbeat (cấu hình của máy chạy), và **secrets** (không bao giờ ghi vào snapshot, manifest hay log).
- Đổi global config **không** làm đổi job đang chạy. Đổi config của một job phải là hành động **explicit** (`config set <job> key=value`), ghi `config_revision` vào manifest; chỉ stage có `stage_key` bị ảnh hưởng mới chạy lại.
- Adapter của job được dựng từ snapshot của job, không từ global tại thời điểm chạy.

---

## 16. Workspace và Output phải tách hoàn toàn

> **Trạng thái:** **đã triển khai ở Phase 6** (D-77): chỉ copy có kiểm sha256, không ghi đè, phiên bản mới `-vN` bên cạnh, `project.json` trỏ artifact nguồn.

### Workspace

Hệ thống sở hữu.

```text
workspace/job_000123/
  source/
  story/
  tts/
  audio/
  render/
  temp/
  manifest.json
```

Người dùng không cần đụng vào.

### Output

Người dùng sở hữu.

```text
output/project-name/
  README.txt
  project.json
  story.txt

  youtube/
    video.mp4
    thumbnail.jpg
    title.txt
    description.txt

  tiktok/
    part_01.mp4
    part_02.mp4
    part_03.mp4
    ...
```

Đây là output cuối cùng người dùng quan tâm.

---

## 17. Output Publisher

> **Trạng thái:** **đã triển khai ở Phase 6** (D-77, D-76): upload YouTube qua daemon `yt_uploader` đọc artifact trong workspace; retry upload không render lại.

Khi job hoàn tất, Output Publisher copy/export artifact final từ workspace sang output package.

Sau khi publish:

> Pipeline không phụ thuộc vào file trong `output/` để tiếp tục vận hành.

Người dùng có thể:

- copy;
- move;
- backup;
- xóa output cũ;

mà không làm hỏng database/cache/workspace logic.

Nếu cần rebuild, hệ thống dùng source/workspace/cache chứ không xem output final là internal dependency.

---

## 18. Project manifest

Mỗi job có manifest để truy vết.

Ví dụ:

```json
{
  "job_id": "story-001",
  "project": {"title": "Tôi Trùng Sinh Quyết Tâm Làm Hại Nữ Chính", "title_source": "user", "sequence": 27},
  "story_profile": "...",
  "tts_profile": "...",
  "channel": "channel_a",
  "source_pool": "gameplay",
  "youtube": {
    "video": "youtube/video.mp4"
  },
  "tiktok": [
    "tiktok/part_01.mp4",
    "tiktok/part_02.mp4"
  ]
}
```

Internal manifest nên chứa thêm version/commit/profile hash để reproducible.

---

## 19. Setup máy mới

Mục tiêu:

```text
git clone ContentFactory
setup
start
```

Nên có repo orchestrator riêng:

```text
ContentFactory/
  orchestrator/
  config/
  scripts/
  runtime/
  workspace/
  output/
```

> **Trạng thái:** **đã triển khai ở Phase 7** (`docs/DECISIONS.md` D-82…D-87). Khác thiết kế ban đầu: `doctor` là lệnh CLI (`cf doctor [--json]`) chứ không phải `doctor.ps1`; thêm `cf.cmd`/`cf.ps1` (launcher), `cf go`, `cf demo`; `setup`/`update`/`start` có `.ps1` mỏng gọi lõi Python (`orchestrator/setup_env.py`). Cấu hình máy ở `config/config.local.json` + `config/secrets.local.env` (không commit); `channels/`, `tts_profiles/`, `tools/`, `.venv/` cũng không commit.

Scripts:

```text
setup.ps1     # máy mới: clone module (theo modules.lock), venv, ffmpeg, build yt-uploader, credential, doctor (idempotent, -DryRun/-Yes/-Force)
update.ps1    # git pull, module về đúng SHA, cài lại dependency khi đổi, migration DB, doctor
start.ps1     # dịch vụ nền: tự bật uploader, Source Sync, auto resume, cleanup
cf.cmd / cf.ps1   # cf go <url> --channel K [--open], cf status/open/resume/retry/doctor/channels/channel-init/demo
```

`doctor` kiểm tra tối thiểu:

- story engine (claude CLI đã đăng nhập, Node, oh-story);
- source providers (Subtitle_supperVip: Python env có `youtube-transcript-api`; `yt-dlp`);
- TTS engines configured;
- FFmpeg;
- ContentFlow renderer;
- GPU/NVENC nếu dùng;
- source pools;
- YouTube credentials;
- storage;
- database.

Thực tế (D-85): doctor còn kiểm Python, git, Node, quyền ghi, ffmpeg filter (`loudnorm/alimiter/rubberband`), thumbnail assets, từng pool, từng kênh (preset hợp lệ), và **cảnh báo khi TTS là bản giả**; mỗi mục lỗi kèm cách sửa.

Version từng repo/module phải pin theo commit/version để máy mới reproducible.

---

## 20. Nguyên tắc kỹ thuật đã chốt

1. Một sản phẩm tổng, nhiều module độc lập.
2. Module giao tiếp bằng artifact + manifest + state, không coupling chặt.
3. Story có thể chia section nội bộ nhưng output cuối là văn bản liền mạch.
4. TTS engine được abstract bằng Adapter.
5. TTS segmentation được điều khiển bằng engine-specific Rule/Profile.
6. AI dùng rule để lập segmentation plan, deterministic validator giữ hard constraints.
7. TTS chỉ sinh narration một lần thành Master Audio.
8. Watermark YouTube là channel asset riêng, dễ thay.
9. TikTok dùng master audio -> speed -> split -> render video.
10. YouTube và TikTok đều có render profile riêng.
11. Source Sync chạy background và được tái sử dụng.
12. Pipeline chạy bất đồng bộ, không chờ toàn bộ stage trước kết thúc.
13. Mỗi stage retry độc lập.
14. Workspace và Output tách hoàn toàn.
15. Output final phải dễ tìm, dễ nhận biết và người dùng có thể di chuyển mà không ảnh hưởng hệ thống.
16. TTS mới ưu tiên onboarding bằng repo/docs/source; AI tự phân tích và tạo adapter/profile/rule candidate.
17. Không bắt người dùng nhập những thông số kỹ thuật mà hệ thống có thể tự khám phá.
18. Thu thập phụ đề đi qua `SourceAdapter` nhiều provider; `Subtitle_supperVip` là provider chính, không phải orchestrator.
19. Một nguồn state duy nhất: DB của ContentFactory. DB/queue của module bên ngoài chỉ là chi tiết cài đặt của provider.
20. Timestamp của phụ đề được giữ cho tới sau bước dựng lại transcript; không coi mỗi dòng caption là một câu.
21. Mỗi tầng cache (phụ đề, transcript, story) có dấu vân tay riêng; đầu vào thượng nguồn đổi thì vô hiệu hóa phần phía sau.
22. Pipeline là chuỗi stage nối bằng artifact, không bắt buộc chạy từ đầu đến cuối: job có `start_stage`/`target_stage`; stage skip nếu output hợp lệ (§15A).
23. Lỗi tài nguyên tạm thời (mạng, token, quota, đĩa, GPU/runtime, credential, thiếu input) làm job **paused**, không phải failed; chỉ lỗi vĩnh viễn mới `FAILED_PERMANENT` (§15B).
24. Resource Monitor là code deterministic, không dùng AI; Auto Resume bật mặc định, override theo job, chỉ cho điều kiện tự hồi phục và không bao giờ vô hạn.
25. Retry/backoff có jitter, ưu tiên `Retry-After`, không polling dày.
26. Mỗi job snapshot config ngữ nghĩa lúc bắt đầu; đổi config của job đang chạy chỉ qua hành động explicit (§15C).
27. Mỗi project có đúng một `project.title`; mọi tiêu đề khác (thumbnail, YouTube, thư mục, README) chỉ là derive từ nó, không có bản độc lập (§4B).
28. Thumbnail dùng `channel.name` + `project.title`, không có AI sinh thumbnail title riêng; title dài xử lý bằng layout/wrap/font sizing, không đổi hay cắt title.
29. YouTube title và description dựng bằng template (title: `[Full Audio {sequence}] | {project_title}`; description: template trong Channel Config); uploader không tự nghĩ title.
30. Sequence/Full Audio STT là theo channel, reserve một lần cho project và lưu cố định; retry upload/rerender không đổi sequence.
31. TTS và Audio không phụ thuộc publishing metadata ngoài identifier thật sự cần.

---

## 21. Điểm chưa chốt / cần thiết kế tiếp

Đã chốt từ tích hợp Source/Subtitle: kiến trúc Source stage (§4A).

Các phần cần triển khai chi tiết ở bước sau:

- schema chính thức cho `TTSAdapter`;
- schema chính thức cho `TTSProfile` / `TTSRule`;
- TTS Source Analyzer;
- auto-generated profile confidence model;
- optional Auto Tune benchmark;
- exact job/queue database schema;
- interface giữa Story output và TTS input;
- interface giữa TTS output và ContentFlow;
- TikTok renderer profile cụ thể;
- naming/version policy cho output package;
- uploader policy và retry/rate-limit;
- triển khai job control layer: `start_stage`/`target_stage`, import artifact, hold/auto-resume, Resource Monitor, config snapshot (đã thiết kế ở §15A–§15C);
- cách xác định token/usage còn lại của Claude mà không tốn lượt LLM (hiện chỉ biết thời điểm reset khi provider báo);
- fallback ASR khi video không có phụ đề nào;
- cách `project.title` được sinh tự động nếu người dùng không nhập (nếu làm, kết quả vẫn ghi vào đúng một field `project.title`);
- Sequence Manager chi tiết, schema Channel Config đầy đủ, Metadata Builder (Phase 6);
- UI/CLI cuối cùng.

---

## 22. Hướng UX cuối cùng

Mục tiêu người dùng không cần quản trị pipeline thủ công.

Ví dụ:

```text
Input source: <YouTube URL>
Channel: Story Channel A
TTS: Auto / selected profile
Video source: Gameplay Pool A

[ RUN ]
```

Hệ thống tự:

```text
source
-> story
-> master audio
-> YouTube version
-> TikTok parts
-> render
-> output package
```

Kết quả người dùng mở:

```text
output/<project>/youtube/video.mp4
output/<project>/tiktok/part_01.mp4
output/<project>/tiktok/part_02.mp4
...
```

Đó là boundary sản phẩm cuối cùng.

