# ContentFactory - Technical Handoff

> Handoff kiến trúc đã chốt từ quá trình trao đổi.
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

### Story
Repo:
- `giaminhNguyen/oh-story-claudecode`
- dùng `story-branch`

Nhiệm vụ:
- nhận dữ liệu đầu vào từ một câu chuyện nguồn;
- phân tích và xây blueprint;
- quản lý continuity;
- sinh truyện dài;
- có thể dùng section/chapter nội bộ để kiểm soát quá trình sinh;
- output publish cuối cùng phải là một truyện liền mạch, không có header kiểu `Chapter 1`, `Chapter 2`, `Section 1`...

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

---

## 3. Kiến trúc tổng thể

Không gộp logic tất cả thành monolith.

Dùng một hệ thống tổng với các module độc lập:

```text
INPUT
  |
  v
Source Processor
  |
  v
Story Branch
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
Download subtitle / transcript / metadata
    |
    v
Clean + normalize source
    |
    v
Source Analyzer
    |
    v
Story input / blueprint data
    |
    v
story-branch
    |
    v
Generate story internally by sections
    |
    v
Continuity check
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

## 5. Logic tạo truyện

`story-branch` có thể dùng chapter/section nội bộ, nhưng đó không phải output cuối.

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

- story engine;
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

---

## 21. Điểm chưa chốt / cần thiết kế tiếp

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

