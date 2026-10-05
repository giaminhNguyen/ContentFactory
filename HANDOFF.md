# ContentFactory - Technical Handoff

> Handoff kiến trúc đã chốt từ quá trình trao đổi.
> **Cập nhật (tích hợp Subtitle_supperVip):** Source/Subtitle nay là một `SourceAdapter` có nhiều provider, `Subtitle_supperVip` là provider chính, ContentFactory vẫn là orchestrator duy nhất giữ state. Xem **§2A Current Integrations** và **§4A Source / Subtitle**. Các điểm đã lệch khỏi bản thiết kế đầu tiên được chỉnh trực tiếp trong tài liệu này; lý do và bằng chứng nằm ở `docs/` (`CURRENT_SYSTEM_AUDIT.md`, `DECISIONS.md`).
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
- thumbnail;
- render video;
- lấy video nguồn random từ source folder được truyền vào;
- source có thể được sync/normalize trước và tái sử dụng;
- render profile riêng cho YouTube và TikTok.

### Upload
Repo:
- `giaminhNguyen/yt_uploader`

Nhiệm vụ:
- upload video YouTube cùng metadata/thumbnail tương ứng.

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
9. Mô tả/tiêu đề của video nguồn **không** được dùng làm mô tả video của sản phẩm; tiêu đề/mô tả riêng là việc của một bước sinh nội dung riêng (chưa làm).
10. Subtitle_supperVip cần Python env riêng có `youtube-transcript-api` (không cài vào tiến trình orchestrator); `doctor` phải kiểm tra.

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

Watermark là channel asset riêng, không thuộc story.

```text
channels/
  channel_a/
    channel.yaml
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

---

## 16. Workspace và Output phải tách hoàn toàn

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

Scripts:

```text
setup.ps1
update.ps1
doctor.ps1
start.ps1
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
- fallback ASR khi video không có phụ đề nào;
- bước sinh tiêu đề/mô tả riêng (không dùng của nguồn);
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

