# CURRENT_SYSTEM_AUDIT

> Phase 0. Audit 3 project hiện có so với `HANDOFF.md`. Không sửa source cũ.
> Ngày audit: 2026-10-05. §6–§7 (Subtitle_supperVip) được thêm khi tích hợp Source/Subtitle.

## 0. Phương pháp và độ tin cậy

- Source 3 project nằm ở `modules/` (mỗi project vẫn là git repo độc lập, đã chuyển nguyên vẹn: cùng HEAD, working tree sạch).
- Mỗi project được một agent đọc code thực tế (entrypoint, core module, config, test, doc). Người audit chính **tự kiểm lại** các điểm quyết định (xem cột "Xác minh" ở §5).
- **Không có gì được chạy end-to-end.** Chỉ có: `go test ./...` của yt_uploader pass (do agent báo). Test của ContentFlow **chưa chạy** (Python hệ thống thiếu Pillow/pytest). Không có kiểm chứng với Google/YouTube thật.
- Quy ước: **[CODE]** = đọc trực tiếp từ code/doc của repo; **[VERIFIED]** = người audit chính đã tự grep/đọc lại; **[UNVERIFIED]** = agent suy luận hoặc kiến thức ngoài repo.

## 1. Snapshot (pin)

| Module | Path | Branch | HEAD | Remote |
|---|---|---|---|---|
| ContentFlow | `modules/ContentFlow` | main | `f3126f0457df457162f7f48d0d17131eef2f18c9` | `git@home.com:giaminhNguyen/ContentFlow.git` |
| oh-story-claudecode | `modules/oh-story-claudecode` | main (= `origin/feature/story-branch`) | `a8dc6749b830f53055958cf328558c53c7404c3c` | `git@home.com:giaminhNguyen/oh-story-claudecode.git` |
| yt_uploader | `modules/yt_uploader` | main | `cfcc45de0aa83529888034ec7222343721bc1572` | `git@home.com:giaminhNguyen/yt_uploader.git` |

Cùng nội dung ghi trong `modules.lock`.

## 2. oh-story-claudecode và `story-branch`

### 2.1 Nó là gì

- Bộ skill viết web novel (Markdown), fork từ `zenstory-ai/oh-story-claudecode`. `AGENTS.md`: sản phẩm là "workflow contract viết bằng Markdown, **không phải runtime**". [CODE]
- 14 skill (AGENTS.md/docs vẫn ghi 13): `story` (router), `story-setup`, `story-import`, `story-long-analyze/scan/write`, `story-short-analyze/scan/write`, `story-review`, `story-deslop`, `story-cover`, `story-branch`, `browser-cdp`. [CODE]
- Chỉ chạy **bên trong một agent CLI** (Claude Code, Codex, OpenCode, …) qua slash command/ngôn ngữ tự nhiên. Không có CLI/API riêng. [CODE]
- Script phụ: Python (`skills/story-long-write/scripts/storyctl.py`, `tracking_commit.py`, `build_writer_prompt.py`, …) và Node 18+ (`check-ai-patterns.js`, `check-degeneration.js`, …). Thiếu Node thì không commit được chương. [CODE]
- `/story-setup` deploy 7 sub-agent + hook vào `.claude/` của **workspace đang dùng**. [CODE]

### 2.2 `story-branch`

- Vừa là **skill** (`skills/story-branch/SKILL.md` + 4 file reference) vừa là **tên git branch** (`origin/feature/story-branch`, hiện trùng `main` ở `a8dc674`). [VERIFIED]
- Mục đích: "từ tác phẩm có sẵn, sinh **nhánh truyện** độc lập" (vd: góc nhìn phản diện, nếu nhân vật không chết). Dòng 8 SKILL.md: **"你只备料，不写正文"** — chỉ chuẩn bị tư liệu, không viết nội dung. [VERIFIED]
- 4 lệnh con: `analyze` (rút `正典.md`), `explore` (5–8 hướng nhánh → `分支提案.md`), `create` (chốt `分支简报`), `handoff` (gieo `设定/分支设定.md` + `.story/work/分支交接.md`, rồi bảo tác giả chạy `/story-long-write 开书`). [CODE]
- Không spawn agent; mọi bước cần tác giả xác nhận. Không có test cho hành vi story-branch. [CODE]

### 2.3 Input thực tế

- Không có schema file/CLI. Bước 1 hỏi tương tác tên tác phẩm nguồn, tìm `拆文库/{书名}/`. Nguồn = **một cuốn truyện đã hoàn chỉnh, chia chương, tiếng Trung**, tốt nhất đã qua `story-long-analyze`. [CODE]
- **Không có** xử lý transcript/subtitle/YouTube. [CODE]
- **Không có chế độ headless.** Có điểm dừng chờ tác giả, giới hạn tối đa 3 chương/lượt, dừng hỏi khi lệch số chữ hoặc có tình tiết mới. `/story-long-write` trần chỉ chẩn đoán. [CODE]

### 2.4 Output thực tế (do `story-long-write`, không phải story-branch)

```text
{书名}/设定/…            大纲/大纲.md, 卷纲_第一卷.md, 细纲_第001章.md
{书名}/正文/第001章_章名.md      <- MỖI CHƯƠNG MỘT FILE, có dòng tiêu đề chương
{书名}/追踪/_tracking-state.json + 上下文.md, 伏笔.md, 角色状态/, 逐章记录/, 时间线/
.story/作者记忆/
```

- Có **dòng tiêu đề chương** trong mỗi file; format "服从项目既有格式" (`long-format.md:9`). Cách bỏ tiêu đề không phải tính năng built-in. [VERIFIED]
- Đoạn văn cách nhau 1 `\n`, không dòng trống. [CODE]
- **Chỉ tiếng Trung**: bộ cấm từ, detector "AI-flavor", đếm chữ CJK, band số chữ đều hardcode cho tiếng Trung. Tiếng Việt không phải cấu hình được hỗ trợ. [CODE]
- **Không có assembler/export** gộp thành một file văn bản liền. [CODE; agent grep không thấy]
- `story-short-write` dùng cấu trúc khác (`###1.` tiểu mục, 8–15 mục). Chưa audit sâu. [UNVERIFIED]

### 2.5 Continuity

- `追踪/_tracking-state.json` là nguồn sự thật duy nhất, chỉ ghi qua `tracking_commit.py` (transaction). View dẫn xuất: `上下文.md` (≤12KB, đọc nguyên mỗi chương), `伏笔.md`, `角色状态`, `时间线`. `consistency-checker` chạy sau mỗi chương trong luồng tương tác. Không có check continuity độc lập trên văn bản bất kỳ. [CODE]
- Chạy song song nhiều truyện: về nguyên tắc được (mỗi sách một thư mục), nhưng mỗi workspace phải có `/story-setup` và cwd quyết định host tìm `.claude/`. [CODE]

### 2.6 Cách một orchestrator điều khiển nó

- Không có entrypoint headless chính thức, không schema vào/ra. [CODE]
- Tiền lệ duy nhất: `scripts/bench/run.py` — spawn `claude` CLI thật, deploy bằng `deploy.py`, gửi prompt `/story-long-write 日更3章`, tự trả lời câu hỏi bằng câu cố định, lặp đến khi đủ N chương, poll `追踪/`. Cần model + credential thật, không nằm trong CI. [CODE]

### 2.7 Kết luận Story

`story-branch` **không phải** bộ sinh truyện từ nguồn. Muốn dùng bộ này như Story module phải điều khiển `story-long-write` (qua `claude -p`, tự động trả lời gate), chấp nhận tiếng Trung, rồi tự viết Story Assembler. Phù hợp nhất cho "viết nhánh tiếp của truyện đã có", lệch với luồng "YouTube transcript → blueprint → truyện".

## 3. ContentFlow

### 3.1 Nó là gì

Một repo, 3 bề mặt: [CODE]

1. **GUI Tkinter** (`app.py`, `ui_video.py`, `ui_source_sync.py`), build exe bằng `build.bat`.
2. **Thư viện headless** `media_core/` (`render_video`, `render_thumbnail`, `prepare_assets`) với lỗi có kiểu.
3. **Worker CLI** `media_worker.py`: `python -m media_worker run --request req.json [--base-dir DIR]`, `status`, `health`. Event JSON-lines ra stdout; exit 0 ok / 3 failed / 4 cancelled / 2 usage. [VERIFIED có 3 subcommand `run|status|health` và `JOB_TYPES = ("render","thumbnail","prepare_assets")`]
4. `renderer.py` CLI chỉ cho thumbnail.

Dependency: `requirements.txt` chỉ có `Pillow>=10`; video đi qua `ffmpeg`/`ffprobe` subprocess (không moviepy/opencv). Encoder `h264_nvenc` (probe bằng encode thử) fallback `libx264`. Máy audit có ffmpeg 9.0.2 trên PATH với NVENC. [CODE]

### 3.2 ContentFlow nhận audio/video nguồn thế nào

- **Audio là timeline chính** (bắt buộc, có stream + duration): mp3, wav, m4a, aac, flac, ogg, opus. Không có chế độ "video là chính, tách audio". [CODE]
- **Video nguồn = một thư mục**, quét **không đệ quy**: mp4, mov, mkv, webm, avi, m4v, mpg, mpeg, wmv. Audio của clip nguồn luôn bị bỏ. [CODE]
- Frame PNG tùy chọn (overlay). Thư mục `input/` chỉ là chỗ mặc định chọn ảnh nhân vật cho thumbnail, không phải hàng đợi nạp. [CODE]
- Worker request: `protocol=1, job_id, type, idempotency_key, output_dir (tuyệt đối), params{}, inputs[{type: audio|video_dir|frame|image, path, sha256?}], deadline_s?, attempt?`. [CODE]

### 3.3 Source Sync

(`source_sync.py::sync_videos(SyncOptions, …)`, UI ở `ui_source_sync.py`) [CODE]

- Re-encode hàng loạt mọi video trong thư mục về **H.264, yuv420p, CFR**; kích thước original/preset/tùy chỉnh bằng scale-fit + pad đen (không crop); FPS original/24/25/30/60/tùy chỉnh; mặc định bỏ audio; chất lượng fast/balanced/high.
- Output mặc định `<source>/_synced/<stem>.mp4`, không ghi đè nguồn.
- **Skip**: bỏ qua nếu file đích tồn tại và size > 0 (không hash).
- Sau chạy ghi `source_profile.json` (size, mtime_ns, duration, w/h/fps, codec, pix_fmt, has_audio, `standard.uniform`). Lúc render, file khớp size+mtime thì bỏ qua ffprobe; file mới/đổi/xóa sinh warning.
- Chạy độc lập được (không import GUI). **Worker không có job type sync.**
- Điểm yếu: ghi thẳng vào đích (không `.part`), crash có thể để lại file cụt mà lần sau bị "skip" vì size > 0; profile ghi không atomic; cancel chỉ kiểm giữa các file; tuần tự, đơn luồng.

### 3.4 Render video

(`video_renderer.render_video`, `render_timeline`) [CODE]

1. Validate audio, `probe_audio`, nạp `source_profile.json`, `scan_videos`, `build_playlist`.
2. Chế độ nguồn: `auto` (nguồn đồng nhất → fast path concat `-c copy`, ngược lại normalize từng clip bằng `cover_filter`: scale, crop, setsar, fps, yuv420p), `normal`, `fast`. Fast path lỗi trong `auto` thì tự retry bằng normalize.
3. `filter_complex`: timeline lên canvas đen tại viewport, overlay frame PNG; map audio, `-t <duration audio>`, H.264 + AAC 192k/48k, `+faststart`.

- **Chọn nền**: ngẫu nhiên, **không seed qua API**. `video_utils.choose_next_clip(..., rng=...)` có tham số `rng`, nhưng `build_playlist` dùng `random.Random()` và `rng` không được đưa lên `video_renderer`/`media_core`/worker. [VERIFIED]
  Cơ chế: `loopCount` chọn N clip khác nhau (0 = tất cả), rồi bốc clip cho đến khi tổng ≥ độ dài audio. Mỗi clip phát từ đầu, không offset ngẫu nhiên. Tối đa 10000 clip.
- **Kích thước/tỉ lệ do frame PNG quyết định.** Frame mặc định `assets/video/frame.png` là 1080×1920 (9:16), viewport 900×1200 tại (90,300). **Không có profile "youtube"/"tiktok"**. `fit_mode` chỉ có `cover`. [CODE]
- **Không có**: đổi tốc độ audio (`atempo` — [VERIFIED] grep rỗng), cắt audio/video thành part, watermark audio, phụ đề, chuẩn hóa/fade audio, đặt tên theo part. [CODE]
- Output qua `media_core`: đường dẫn tường minh, ghi atomic qua `.part` + `os.replace`. Đường GUI cũ: `video_YYYYMMDD_HHMMSS.mp4`.
- Progress `-progress pipe:1` (0..1, đơn điệu), cancel bằng `threading.Event` + `kill_tree`. **Timeout 3600 s cho mỗi lệnh ffmpeg** (rủi ro với 40–60 phút trên CPU, [UNVERIFIED]).
- Lỗi phân lớp: RESOURCE (FFMPEG_MISSING, DISK_FULL, GPU_UNAVAILABLE), POLICY (MISSING_INPUT, INVALID_CONFIG), TRANSIENT (FFMPEG_FAILED, TIMEOUT), CANCELLED.
- Worker ghi `.worker_state.json` (+ lock 30 s) và scratch `.work/` **bên trong `output_dir`**.

### 3.5 Thumbnail

(`renderer.ThumbnailRenderer`, bọc bởi `media_core.render_thumbnail`) [CODE]

- Chỉ Pillow, headless hoàn toàn. Canvas 1648×928. PNG hoặc JPG (q95, 4:4:4) theo đuôi file; ghi atomic.
- Input: ảnh nhân vật, tên kênh, tiêu đề, highlight (auto|manual|none), zoom/offset.
- **Cần `assets/template.png`** (hoặc `background.png` + `foreground.png`) và font `assets/fonts/*.ttf`; cả hai **bị gitignore, repo không có**. Checkout hiện tại: thumbnail và `prepare_assets` sẽ lỗi `MISSING_INPUT` cho đến khi bổ sung. Fallback font là font Windows (`C:\Windows\Fonts`).
- Một template cố định, layout tọa độ cố định, không tạo thumbnail từ khung hình video.

### 3.6 Config

`config.json` keys: `canvas, render, template, fonts, photo, channel, title, highlight, export, video_generator`. Mặc định ở `core_common.DEFAULT_CONFIG`, deep-merge. Headless: `media_core.common.load_media_config(base_dir, overrides)` đưa path về tuyệt đối theo `base_dir`. Còn sót: `video_settings` vẫn resolve `output.directory` tương đối theo `app_dir()` (media_core né bằng cách override); `audio_codec`/`audio_bitrate` được đọc nhưng args ffmpeg thực tế hardcode `AUDIO_ARGS` ([UNVERIFIED] chưa lần hết đường). `config.json` mang một khóa `frame_layouts` tuyệt đối cũ (`C:\Users\ming\Pictures\Thumbnail\Frame.png`), lệch theo máy. README ContentFlow lỗi thời một phần (nhắc `thumbnail_tool`). [CODE]

### 3.7 Test

21 file, ~4.500 dòng, pytest + marker `real_ffmpeg`; chủ yếu fake ffmpeg; test real-ffmpeg tự skip nếu thiếu ffmpeg. Bao phủ protocol worker (replay, cancel, deadline, hard-kill, sha256), media_core, Source Sync, encoder/fast path, progress. **Chưa chạy trong audit** (thiếu Pillow/pytest). [CODE]

## 4. yt_uploader

### 4.1 Nó là gì

Một binary Go `cmd/yt-uploader` (cobra) vừa là CLI vừa là **daemon localhost**: REST API + Web UI nhúng + worker pool. Go (`go.mod` `go 1.26.5`), **không CGO**, SQLite thuần Go (`modernc.org/sqlite`), không cần ffmpeg/Node để build. UI nhúng sẵn. Chạy: `yt-uploader serve --headless` (cổng mặc định 8973, chỉ bind loopback). [CODE]

Lệnh: `serve, login (--print-url/--device), logout, accounts, account use, channels, playlists, upload <file> (một phát, bỏ qua queue), enqueue, jobs, status, cancel, retry, version`; phần lớn có `--json`. Hai commit cuối (`af85857`, `cfcc45d`) được thêm **riêng cho orchestrator**: `--headless`, `idempotency_key`, resume bền, `error_class`. [CODE]

### 4.2 Input

- `POST /api/v1/jobs` (JSON, `Authorization: Bearer <nội dung <datadir>/api_token>`) hoặc CLI `enqueue`/`upload`. Không có folder-watch/CSV/batch/preset (preset chỉ nằm trong schema). Byte video **không** đi qua API: `file_path` là đường dẫn trên máy chạy `serve`. [CODE]
- Trường: `file_path` (bắt buộc), `made_for_kids` (bắt buộc, `*bool`), `title` (≤100), `description` (≤5000 byte), `tags` (≤500 ký tự gộp), `category`, `privacy` (mặc định `private`), `playlists[]`, `thumbnail_path` (jpeg/png/gif/bmp, **tối đa 2 MiB**), `schedule` (RFC3339 tương lai, ép private), `idempotency_key` (≤255), `account_id`, `channel_id` (chỉ thông tin). Field lạ → 400. **Không có** `defaultLanguage`, `notifySubscribers`. [CODE]
- Trả 201 + `JobView`; 200 + job cũ nếu `idempotency_key` đã có. Kết quả: `state, video_id, video_url, last_error{code,message}, error_class, progress_percent`. `GET /api/v1/health` không cần auth, trả `features:[idempotency_key, resume_probe]`. [CODE]

### 4.3 Cách upload

- YouTube Data API v3 **resumable upload** qua `net/http` thuần, không automation trình duyệt. Chunk mặc định 8 MiB. Sau mỗi chunk lỗi gửi probe `bytes */total` trước khi tiếp tục (tránh trùng). Backoff mũ + jitter, 8 lần, tôn trọng `Retry-After`. [CODE]
- Session URI lưu DB **trước khi gửi byte đầu**; sau crash, `retry` probe session rồi resume hoặc hoàn tất bằng `video_id`. Job `completed` không retry được ⇒ không đăng 2 lần. `AMBIGUOUS_UPLOAD` cần người xem rồi `retry?force=true`. [CODE]
- Sau upload: `thumbnails.set`, `playlistItems.insert`; lỗi các bước này vẫn `completed` kèm `last_error`, không upload lại. **Không poll trạng thái xử lý của YouTube.** [CODE]
- OAuth: installed-app, loopback, scope `youtube` đầy đủ; **client ID/secret do người dùng tự tạo** (env `YT_UPLOADER_CLIENT_ID/SECRET` hoặc `<datadir>\oauth_client.json`). Token `<datadir>\secrets\<accountId>.tok`, Windows mã hóa DPAPI (chỉ giải mã được bởi đúng user Windows). Nhiều account được; **không chọn kênh thật** (video lên kênh của token; `channel_id` không dùng). [CODE]
- Quota: chỉ phân loại lỗi, không theo dõi/ngân sách. Kiến thức ngoài repo: `videos.insert` ~1600 đơn vị trên hạn mức mặc định 10.000/ngày ⇒ ~6 upload/ngày; project API chưa audit có thể bị ép private. [UNVERIFIED — cần kiểm tra với Google]

### 4.4 State, config, test

- SQLite `<datadir>\yt-uploader.db` (WAL; migration 0001–0004; bảng `accounts, channels, upload_jobs, upload_presets (không dùng), app_settings`). Trạng thái job: `queued, preparing, uploading, processing, completed, paused, retry_wait, cancelled, failed`. Restart: job đang chạy → `paused`, **không tự retry**; job `failed` cần `retry` thủ công. [CODE]
- Data dir `%APPDATA%\yt-uploader` hoặc `<exe>\data` với `--portable`. Không có file config; env: `YT_UPLOADER_PORT`, `YT_UPLOADER_CLIENT_ID`, `YT_UPLOADER_CLIENT_SECRET`. Windows-first. [CODE]
- `go test ./...` pass (kể cả `archtest`), toàn bộ bằng fake server. **Chưa từng chạy với Google thật** (IMPLEMENTATION_STATUS.md thừa nhận). `IMPLEMENTATION_STATUS.md` **lỗi thời** (ghi "Last updated: Phase 5", nói thiếu resume/idempotency/headless trong khi code đã có); CHANGELOG.md và docs/API.md đúng hơn. [CODE]
- **TikTok: không hỗ trợ.** [VERIFIED] grep `tiktok` trên cả ContentFlow và yt_uploader (.go/.py/.md) không ra kết quả.

## 5. HANDOFF so với code thực tế

Mức độ: ❌ sai/không có trong code · ⚠️ đúng một phần · ✅ khớp. Cột Xác minh: V = người audit chính đã tự kiểm.

| # | HANDOFF nói (mục) | Thực tế | Mức | Xác minh |
|---|---|---|---|---|
| A1 | `story-branch` "nhận input từ câu chuyện nguồn, phân tích, xây blueprint, sinh truyện dài" (§2, §3) | Chỉ chuẩn bị tư liệu nhánh; **không viết nội dung**. Viết truyện là `story-long-write` | ❌ | V |
| A2 | Input có thể là YouTube URL → transcript → source analyzer → story (§4) | Không project nào xử lý transcript/YouTube. Story cần truyện hoàn chỉnh tiếng Trung. **SourceProcessor hoàn toàn mới** (sau đó được giải quyết: `SourceAdapter` + Subtitle_supperVip + Transcript Processor, xem §6–§7) | ❌ | |
| A3 | Story là một module chạy tự động trong pipeline (§3, §14) | Chỉ chạy trong agent CLI, nhiều cổng chờ người, không headless, không schema | ❌ | |
| A4 | Ngôn ngữ truyện (HANDOFF **không nêu**) | Story chỉ viết tiếng Trung; checker hardcode tiếng Trung | ⚠️ ẩn số | |
| A5 | "Story Assembler" bỏ header/marker, nối thành `story.txt` (§5) | Không tồn tại. Có tiêu đề chương trong từng file `正文/第NNN章_*.md` | ❌ (mới) | V |
| A6 | Continuity check (§4, §5) | Có, nhưng tích hợp trong luồng viết từng chương (`tracking_commit.py` + `consistency-checker`), không độc lập | ⚠️ | |
| A7 | Nhiều story song song bằng workspace riêng (§14) | Được, nhưng mỗi workspace phải `/story-setup` (deploy `.claude/`, hook), cwd quan trọng | ⚠️ | |
| A8 | "Render profile riêng cho YouTube và TikTok" (§2, §12) | Không có profile. Frame PNG quyết định size; chỉ có frame 9:16. Cần tự tạo frame 16:9 + layout + config override | ❌ | |
| A9 | "Lấy video nguồn random từ source folder" (§2) | Random, nhưng bốc **nhiều clip nối nhau** đến đủ độ dài audio; không seed qua API; không offset ngẫu nhiên | ⚠️ | V |
| A10 | Source Sync là background/shared, chỉ sync lại khi source/profile đổi (§13) | Là hàm/tab GUI chạy thủ công. Skip theo "file đích tồn tại" (không hash, không so profile/nguồn). Không atomic. Không có job type trong worker | ⚠️ | V (không có type sync) |
| A11 | TikTok: speed ×2 → split theo duration → render từng part (§11) | ContentFlow **không** có atempo, không split. Phải làm ở AudioProcessor + gọi worker một lần/part | ❌ (mới) | V |
| A12 | YouTube audio = watermark.wav + master (§10) | Không có trong ContentFlow. AudioProcessor mới | ❌ (mới) | |
| A13 | Thumbnail thuộc ContentFlow (§2) | Có, nhưng **thiếu `template.png` + font**, font fallback Windows, một template cố định, 1648×928 | ⚠️ | |
| A14 | Upload YouTube kèm metadata/thumbnail (§2) | ✅ khớp, API headless + idempotency. Nhưng chưa test với Google thật, OAuth client thủ công, không chọn kênh thật, thumbnail ≤2 MiB | ✅/⚠️ | |
| A15 | Trạng thái `UPLOADING/PUBLISHED` (§15), TikTok parts trong output (§16) | Chỉ YouTube upload được. TikTok chỉ **xuất file**, không đăng | ⚠️ làm rõ | V |
| A16 | Mỗi stage retry độc lập, idempotent (§20.13) | ContentFlow worker (`idempotency_key`, `status`) và yt_uploader (`idempotency_key`) hỗ trợ tốt; Story thì không | ⚠️ | |
| A17 | Module giao tiếp bằng artifact + manifest + state (§3, §20.2) | ✅ khớp với hướng thiết kế của worker JSON-lines và REST yt_uploader (hai repo vừa được bổ sung cho orchestrator) | ✅ | |
| A18 | TTS là module mới (§2) | ✅ Không project nào có TTS | ✅ | |
| A19 | Pin version từng repo theo commit (§19) | Mỗi repo là git độc lập, remote `git@home.com:…` (máy ngoài chưa chắc truy cập được) | ⚠️ | V |
| A20 | Reproducible manifest (§18) | Nền ngẫu nhiên không seed ⇒ video không tái tạo y hệt. Chấp nhận, ghi vào manifest | ⚠️ | V |
| A21 | `doctor` kiểm tra ffmpeg/NVENC/YouTube credentials (§19) | Có thể: `media_worker health`, `GET /api/v1/health`, ffmpeg probe. Story engine chưa có cách health-check | ⚠️ | |

Các "Điểm chưa chốt" của HANDOFF §21 vẫn đúng và không bị code mới giải quyết; riêng "interface giữa TTS output và ContentFlow" (audio file → `inputs[type=audio]`) và "TikTok renderer profile cụ thể" đã được làm rõ ở `MODULE_CONTRACTS.md`.

## 6. Subtitle_supperVip (audit bổ sung)

### 6.1 Snapshot

| | |
|---|---|
| Path | `modules/Subtitle_supperVip` (copy từ `Desktop/GIT/Subtitle_supperVip`; bản gốc không bị sửa) |
| Branch / HEAD | `Update` / `3d9281f010424dc4807e76f8bd443ddb50fb3694` ("feat: initialize YouTube subtitle manager") |
| Remote | `git@home.com:giaminhNguyen/Subtitle_supperVip.git` — **clone từ máy này bị từ chối (SSH publickey)**, nên dùng bản local |
| Cây làm việc | 3 file đã stage chưa commit: `README.md` (sửa), `dev.ps1`, `start.ps1` (thêm). Code backend khớp HEAD |
| Quy mô | 36 file, ~116 KB; backend ~25 KB Python |
| Test của nó | 12 test pytest, **12 pass** (Python 3.13, thư viện `youtube-transcript-api` 1.2.4) |

### 6.2 Là gì

Ứng dụng web **quản lý subtitle theo kênh**: dán URL kênh → quét toàn bộ uploads → tải caption theo hàng đợi → lưu file. FastAPI + SQLAlchemy/SQLite + worker riêng + React/Vite UI. README yêu cầu Python 3.12+ (máy audit có 3.10 và 3.13, **không có 3.12**; backend chạy được trên 3.13, `dev.ps1` dùng `py -3.12` nên sẽ lỗi trên máy này).

### 6.3 Kết quả audit theo từng mục (đọc từ code)

| Mục | Thực tế trong code |
|---|---|
| **Entrypoint** | `app.main:app` (uvicorn), `python -m app.worker` (vòng lặp poll), frontend Vite. Không có CLI. **Không có entrypoint cho một video lẻ.** |
| **Kiến trúc** | REST FastAPI (`main.py`) → SQLite qua SQLAlchemy (`models.py`: Channel, ChannelSettings, Video, Subtitle, Job, JobLog, SyncRun); services `youtube.py` (Data API), `subtitles.py` (transcript), `jobs.py` (enqueue/scan); `worker.py` |
| **Nhận URL/ID** | Chỉ URL **kênh** (`parse_channel_url`: `/channel/ID`, `/@handle`, `/user/`, `/c/` qua search). Video vào hệ thống **chỉ** qua scan uploads của một kênh. `POST /api/videos/{id}/download` nhận UUID nội bộ. Không có parse URL video / video id |
| **Resolve video** | kênh → `channels.list` → uploads playlist → `playlistItems.list` (50/trang) + `videos.list` (`snippet,contentDetails,liveStreamingDetails`): title, publishedAt, duration (ISO8601→giây), thumbnail, loại (short ≤ 60 s / live / video). **Cần `YOUTUBE_API_KEY`**, tốn quota. Dữ liệu lưu **không có description**. `_video_details(ids)` là phương thức private |
| **Lấy subtitle** | `youtube-transcript-api` ≥ 1.0: `YouTubeTranscriptApi().list(id)` → chọn track → `fetch()` → snippet `{text,start,duration}`. **Không giữ file timedtext gốc**, chỉ snippet đã parse. Không Selenium |
| **Chọn ngôn ngữ** | `choose_transcript`: lọc theo `manual|auto|any`; duyệt ngôn ngữ ưu tiên (so mã trước dấu `-`); `original` = track đầu tiên; dịch của YouTube nếu `allow_translation` (mặc định **true**). Không có "manual trước auto" xuyên ngôn ngữ: với `any`, auto-vi có thể thắng manual-en |
| **Metadata** | Chỉ qua Data API (cần key). `metadata.json` ghi video_id, title, url, published_at, thông tin track, danh sách file |
| **Retry** | Chỉ ở worker: `max_job_attempts`=5, backoff `min(300, 2^lần × 5)` s. `Blocked` (IpBlocked/RequestBlocked/429) → video `blocked`, job `failed` ngay (không retry). NoTranscript/TranscriptsDisabled → `no_subtitle` (job `completed`). LanguageUnavailable → `language_unavailable`. Lỗi khác → retry. **Không retry trong thư viện** |
| **Rate limit** | `REQUESTS_PER_MINUTE` chỉ được **khai báo** trong `config.py` và `.env.example`, **không nơi nào dùng** (README mô tả như có hiệu lực) |
| **Queue / worker** | Bảng `jobs` SQLite; `process_one()` poll mỗi `WORKER_POLL_SECONDS`; pause/resume/cancel/retry qua API; `recover_interrupted()` đưa job `processing` về `queued` khi worker khởi động. Claim bằng SELECT rồi UPDATE (không atomic, không lease/heartbeat): **chỉ an toàn với một worker** (README thừa nhận) |
| **SQLite / state** | `data/app.db`, alembic `0001`. Trạng thái Video (9) và Job (6) là state machine **của riêng nó** |
| **Trùng lặp** | Video unique `(channel_id, youtube_video_id)` (cùng video ở hai kênh có thể trùng); `enqueue` chặn job active trùng `(kind, channel_id, video_id)`; Subtitle unique `(video_id, language_code, format)` |
| **Lưu trữ** | `data/<kênh>/<YYYY-MM>-<tên>-<id>/{<lang>.srt|txt|json|vtt|csv, metadata.json}`, tên file được làm sạch |
| **Chẩn đoán** | `/health` chỉ trả `{"ok": true}` (không kiểm DB/key/thư viện); dashboard đếm; bảng `job_logs`. Không có doctor |
| **Setup / portable** | `dev.ps1` (venv + pip + alembic + npm + 3 cửa sổ), `start.ps1` + `docker-compose.yml` (api, worker, web). `.env` (`YOUTUBE_API_KEY` bắt buộc để resolve/scan). Không portable thực sự |
| **Xử lý lỗi** | 3 exception tự định nghĩa; phân loại lỗi thư viện bằng **tên lớp / chuỗi message** (`"ipblocked" in name`, `"429" in message`…): mong manh theo phiên bản thư viện |

### 6.4 README so với code

- `REQUESTS_PER_MINUTE` "tốc độ request tối đa khuyến nghị": **không được thực thi** (xem trên).
- "Lấy metadata": không có mô tả video.
- Phần còn lại (kênh, scan, queue, trạng thái, test) khớp code.

### 6.5 Kiểm chứng thật

Chạy bằng venv Python 3.13, ở thư mục tạm (không sinh `data/`, không đọc `.env`) trên một video YouTube công khai:
- `available_transcripts`: liệt kê đủ track thủ công + auto (1,1 s).
- `fetch_selected(manual, [vi,en])` → en thủ công, 61 snippet; `(auto, [...,original])` → en auto, 52 snippet; `(manual, [vi])` → `LanguageUnavailable` đúng như thiết kế.
- Snippet **không có** thẻ HTML, không có dòng lặp kiểu rolling, không chồng thời gian; 29/61 snippet chứa `\n` bên trong (caption hai dòng); còn ký hiệu `♪` và `[♪♪♪]`.
- `serialize(..., "srt")` đúng định dạng.

### 6.6 Phần dùng được và phần không

- **Dùng được (hàm thuần, không dính DB):** `services/subtitles.py` (`fetch_selected`, `choose_transcript`, `available_transcripts`, `serialize`, 3 exception), `services/youtube.py::YouTubeDataClient._video_details` (metadata).
- **Không dùng:** channel/scan/sync, `jobs.py`, `worker.py`, `models.py`/DB/alembic, FastAPI, React UI, Docker, `dev.ps1`/`start.ps1`. Lý do: thiết kế theo kênh, tạo state machine thứ hai, một worker + SQLite không atomic, cần API key/quota.

## 7. Source Phase 2 so với Subtitle_supperVip

| Chức năng | Source Phase 2 | Subtitle_supperVip | Quyết định (D-29…D-35) |
|---|---|---|---|
| Nhận URL / video id | `parse_video_id` | không có (chỉ URL kênh) | **giữ** của ContentFactory (định danh, cache, bridge) |
| Lấy phụ đề | yt-dlp (subprocess) | `youtube-transcript-api` | supervip = **provider chính**; yt-dlp = **fallback** (`YtDlpProvider`) |
| Chọn track | `choose_subtitle` | `choose_transcript` | supervip: 3 pass (manual → auto → manual bất kỳ); yt-dlp giữ `choose_subtitle` |
| Metadata | yt-dlp info (title, mô tả, kênh, độ dài) | Data API (cần key, không mô tả) | supervip khi có key; yt-dlp **bổ sung** khi thiếu title |
| Raw | file VTT/SRT thật | snippet JSON đã parse | giữ nguyên "raw" theo provider (`subtitle_format` nói rõ) |
| Retry | orchestrator | worker riêng (5 lần) | **chỉ orchestrator**; worker của module không dùng |
| Queue / state | SQLite của ContentFactory | SQLite riêng | **chỉ ContentFactory** |
| Cache / idempotency | sidecar + cache theo video id | thư mục `data/` + DB | cache của ContentFactory (`ProviderChain`) |
| Parse / dựng câu / clean | có | chỉ `serialize` | **giữ** (Transcript Processor) |
| `YouTubeSourceProcessor` (nguyên khối) | có | — | **đã gỡ** sau khi tích hợp mới được kiểm chứng; logic tách thành provider + processor + chain |
