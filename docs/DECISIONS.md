# DECISIONS

> Nhật ký quyết định kiến trúc. `HANDOFF.md` là nguồn sự thật cho các quyết định **đã chốt** (§20); file này ghi thêm các quyết định phát sinh từ Phase 0 và các câu hỏi **còn mở**.
> Trạng thái: ✅ Đã quyết (gồm các mặc định suy ra từ code/môi trường, xem lại được) · 🔴 Mở (cần thông tin không suy ra được).

## 1. Quyết định

### D-01 ✅ Ba project đặt ở `modules/`, giữ nguyên repo git độc lập
- **Bối cảnh:** HANDOFF §19 yêu cầu pin version theo commit; ba repo có `.git` riêng, remote `git@home.com:…`.
- **Quyết định:** chuyển vào `modules/ContentFlow`, `modules/oh-story-claudecode`, `modules/yt_uploader`; repo gốc ContentFactory **không** chứa code của chúng (`.gitignore` bỏ `modules/*/`), pin bằng `modules.lock` (SHA). `setup.ps1` (Phase 1) clone theo SHA.
- **Hệ quả:** repo gốc nhẹ, không lẫn lịch sử. Máy mới cần truy cập được `git@home.com` (R10); nếu không, chuyển sang submodule/mirror sau.
- **Đã làm ở Phase 0:** di chuyển + xác minh SHA không đổi, working tree sạch.

### D-02 ✅ Orchestrator viết bằng Python (mặc định, HANDOFF không nêu)
- **Lý do:** ContentFlow là Python và có thư viện `media_core`; ffmpeg/ffprobe bọc dễ; HTTP client tới yt_uploader đơn giản; SQLite có trong stdlib. HANDOFF không nêu ngôn ngữ.
- **Hệ quả:** hợp đồng ở `MODULE_CONTRACTS.md` viết dạng Python Protocol nhưng ràng buộc thật là artifact + manifest, nên đổi ngôn ngữ vẫn được.
- **Ghi chú:** chọn Python **không** có nghĩa import code ContentFlow; vẫn dùng subprocess (D-05).

### D-03 ✅ StoryAdapter: S2 (`DirectLLMStoryAdapter`) là hướng chính; oh-story chỉ là tài liệu phương pháp
- **Bối cảnh:** `story-branch` chỉ chuẩn bị tư liệu và không sinh văn bản; `story-long-write` không headless, **chỉ tiếng Trung**, từng chương một file, không có assembler (A1–A7). Ngôn ngữ đích là tiếng Việt (D-04) nên S1 không dùng trực tiếp được (bộ cấm từ, detector, đếm chữ đều hardcode tiếng Trung).
- **Quyết định:** contract `StoryAdapter` giữ nguyên (HANDOFF §3–§5). Cài đặt thật là S2: orchestrator gọi LLM theo section, giữ state continuity và blueprint riêng trong `workspace/job_x/story/`, rồi Assembler + validator bất biến sinh `story.txt`. Phương pháp (blueprint → section → kiểm continuity → bỏ heading) tham khảo `skills/story-long-write` và `story-long-analyze`, **không** phụ thuộc runtime của oh-story. `FixtureStoryAdapter` dùng để dựng pipeline ở Phase 1.
- **S1 (`OhStoryCliAdapter`) không xây.** Chỉ xem xét lại nếu sau này cần một kênh truyện tiếng Trung. Repo `oh-story-claudecode`/`story-branch` vẫn là nguồn tham chiếu phương pháp và có thể là đầu vào "viết nhánh tiếp của truyện đã có" ở phiên bản sau (giữ tinh thần HANDOFF §2).
- **Hệ quả:** phải tự xây continuity/blueprint (R1 chuyển từ "chưa biết hướng" thành "khối lượng công việc ước lượng được"); spike Phase 2a thu hẹp còn kiểm chứng S2.

### D-04 ✅ Ngôn ngữ đích mặc định: tiếng Việt (`vi`), cấu hình theo `StoryProfile.language` và `TTSProfile.language`
- **Bằng chứng (suy ra từ code/môi trường):** ContentFlow README yêu cầu font hỗ trợ tiếng Việt, `renderer.py` kiểm glyph tiếng Việt (`VIETNAMESE_PROBE`), 40 file Python có chuỗi tiếng Việt, test dùng đường dẫn tiếng Việt; môi trường có skill `gen-audio-queue` (VieNeu-TTS) và `viet-tieu-thuyet`.
- **Quyết định:** mặc định `vi` cho truyện, TTS, tiêu đề/mô tả YouTube. Ngôn ngữ **nguồn** có thể khác (transcript bất kỳ), Story chịu trách nhiệm chuyển thành truyện tiếng Việt. Trường `language` vẫn bắt buộc khai báo trong profile (không hardcode); validator kiểm đúng ngôn ngữ.
- **Xem lại nếu:** cần kênh ngôn ngữ khác; chỉ cần thêm profile, không đổi contract.

### D-05 ✅ ContentFlow dùng qua `media_worker` (subprocess + JSON-lines), không import
- **Lý do:** worker có protocol `idempotency_key`, `status`, cancel, deadline, atomic output, exit code định nghĩa; hai commit gần nhất của ContentFlow làm đúng việc này. Tránh phụ thuộc phiên bản Python/Pillow của orchestrator.
- **Hệ quả:** Source Sync (không có job type) dùng shim gọi `sync_videos` bằng Python của ContentFlow (D-09).

### D-06 ✅ TikTok: chỉ xuất file, không đăng tự động
- **Lý do:** yt_uploader chỉ YouTube; không project nào có TikTok (đã grep). HANDOFF §16 cũng chỉ yêu cầu `tiktok/part_NN.mp4` trong output.
- **Hệ quả:** `PUBLISHED` chỉ nghĩa là YouTube đã đăng; nhánh TikTok kết thúc ở `OUTPUT_READY`. Thêm `PublishAdapter` TikTok là việc tương lai.

### D-07 ✅ Tên `output/<project>/` = `<yyyymmdd>_<slug>`
- `slug` lấy từ tiêu đề truyện: bỏ dấu tiếng Việt, chữ thường, ASCII, `-` thay khoảng trắng, tối đa 60 ký tự (an toàn cho Windows và công cụ không xử lý Unicode). Trùng tên thì hậu tố `-2`, `-3`, không ghi đè. Tiêu đề gốc có dấu nằm trong `project.json`/`title.txt`.
- Mẫu tên đặt ở `config/config.json` → `output.name_template`, không hardcode.

### D-08 ✅ Chấp nhận nền video ngẫu nhiên không tái tạo y hệt
- **Bối cảnh:** `rng` có ở `video_utils.choose_next_clip` nhưng không lộ ra `render_video`/`media_core`/worker (đã kiểm). Không có đường adapter để seed.
- **Quyết định:** không sửa ContentFlow; manifest ghi `nondeterministic: ["render.background_selection"]`; video đã render là artifact được cache, không tái sinh để so sánh.
- **Xem lại nếu:** cần tái tạo bit-exact → patch upstream nhỏ (đưa seed lên `params`).

### D-09 ✅ Source Sync: shim trong orchestrator, kèm kiểm tra sau sync
- Gọi `source_sync.sync_videos`; sau đó ffprobe từng file trong `_synced/` và xóa file không đọc được (bù cho việc ghi không atomic, R8); fingerprint pool dựa trên `source_profile.json` + danh sách file để tạo `stage_key`. Chạy khi pool/profile đổi, hoặc theo lịch — **không** là daemon nền riêng ở giai đoạn đầu.

### D-10 ✅ AudioProcessor thuộc orchestrator (ffmpeg), không đưa vào ContentFlow
- ContentFlow không có `atempo`, split, watermark (đã kiểm). ffmpeg đã là điều kiện bắt buộc.

### D-11 ✅ Walking skeleton trước, spike rủi ro song song
- Xem `IMPLEMENTATION_PHASES.md`: Phase 1 dựng pipeline với adapter giả; Phase 2 spike Story/ContentFlow/yt_uploader.

### D-12 ✅ yt_uploader chạy như daemon headless, adapter là HTTP client loopback
- Có sẵn idempotency, resume bền, `error_class`; chạy `serve --headless`. Không dùng chế độ `upload` một phát vì mất queue/resume/idempotency.

### D-13 ✅ Một phân lớp lỗi thống nhất cho mọi adapter
- `TRANSIENT / RESOURCE / POLICY / AUTH / AMBIGUOUS / CANCELLED`, ánh xạ từ `VideoError` (ContentFlow) và `error_class` (yt_uploader). Quyết định retry của orchestrator dựa trên lớp này, không dựa trên message.

### D-14 ✅ Không sửa `HANDOFF.md` trong Phase 0
- HANDOFF là source of truth do người dùng chốt. Các điểm lệch với code được ghi ở audit §5 và đề xuất chỉnh ở §3 dưới đây để người dùng duyệt.

### D-15 ✅ Đồng thời render mặc định = 1 job NVENC, theo tài nguyên máy
- **Bằng chứng:** máy chính là GTX 1650 4 GB (`nvidia-smi`), NVENC có, VRAM nhỏ. HANDOFF §14 muốn async nhưng là chồng **stage khác nhau** (story/TTS/render/upload), không nhân đôi stage nặng cùng loại.
- **Quyết định:** giới hạn đồng thời theo loại tài nguyên trong `config/config.json` → `limits`: render GPU = 1, TTS theo `max_concurrency` của engine, upload theo `--concurrency` của yt_uploader (mặc định 2). `doctor` đọc GPU thật để đề xuất giá trị; nâng lên sau khi đo (R12).

### D-16 ✅ Trong một job các stage chạy tuyến tính; song song nằm giữa các job
- **Bối cảnh:** HANDOFF §3 vẽ nhánh YouTube và TikTok song song. Hai nhánh cùng dùng GPU, mà D-15 giới hạn 1 render GPU; state của một job chỉ là một giá trị.
- **Quyết định:** job đi `render_youtube → render_tiktok` tuần tự. Pipeline vẫn bất đồng bộ theo HANDOFF §14: job N đang render trong khi job N+1 chạy TTS và job N-1 đang upload (mỗi stage có hàng đợi riêng, giới hạn theo tài nguyên).
- **Xem lại nếu:** có nhiều GPU hoặc muốn nhánh TikTok chạy sớm. Cần tách state theo nhánh (đổi `jobs`, không đổi contract).

### D-17 ✅ Phase 1 chỉ dùng stdlib: Python ≥ 3.10, `sqlite3`, `unittest`, config JSON
- **Bằng chứng:** máy có Python 3.10 (và 3.13), không có `pytest`, không có `tomllib` (3.11+). `setup` máy mới không nên phụ thuộc gói ngoài khi chưa cần.
- **Quyết định:** config ở `config/config.json` (đã sửa các chỗ nhắc `.yaml` trong D-07, D-15). Các ví dụ YAML của HANDOFF (`channel.yaml`, TTS profile) sẽ được đọc khi tới Phase 4/5; chọn PyYAML hay JSON lúc đó. Test chạy bằng `python -m unittest discover -s tests -t .` (pytest cũng chạy được nếu có).

### D-18 ✅ Phát hiện tiến trình chết bằng lease + heartbeat; resume tự động
- **Quyết định:** claim job ghi `lease_owner`/`lease_until` (mặc định 30 s, heartbeat 10 s). Orchestrator mới (hoặc cùng orchestrator) thấy lease hết hạn thì đánh dấu stage_run `interrupted` và xếp job về `queue_state` **của đúng stage đó**. Không kiểm PID (không tin cậy trên Windows khi thiếu thư viện, và không dùng được khi máy khởi động lại).
- **Hệ quả:** sau crash/kill, resume trễ tối đa `lease_s`. Dừng **có chủ đích** (Ctrl-C/`stop`) thì handler hợp tác nhận `CancelToken`, trả `CANCELLED` và job được **trả về hàng ngay**, không chờ lease và không tốn retry.
- **Chống vòng lặp chết:** lần bị ngắt không tính vào ngân sách retry, nhưng sau `max_interruptions` (mặc định 5) lần liên tiếp thì job vào `FAILED` (`INTERRUPTED_REPEATEDLY`).
- **Đã kiểm chứng:** test kill tiến trình thật (TerminateProcess) giữa stage và giữa chunk TTS.

### D-19 ✅ Chính sách retry theo lớp lỗi
- Chỉ `TRANSIENT` tự retry: backoff 2/10/60 s, tối đa 3 lần (`retry.*` trong config). `RESOURCE`, `POLICY`, `AUTH`, `AMBIGUOUS` → `FAILED` ngay, chờ người sửa rồi `retry`. Exception không lường trước → `POLICY/UNEXPECTED` (retry mù một lỗi lập trình là vô nghĩa).
- `retry` thủ công chỉ đưa **đúng stage lỗi** về hàng đợi, reset ngân sách retry; artifact các stage trước giữ nguyên (có test so sánh sha256 + mtime). Số `attempt` của stage luôn tăng dần để log/manifest truy vết được.

### D-20 ✅ Cấu trúc `src/contentfactory/…` và luật import do test cưỡng chế
- **Quyết định:** đặt `orchestrator/ jobs/ adapters/ source/ story/ tts/ audio/ render/ publish/ output/` dưới namespace `contentfactory` (tên `jobs`, `audio`, `render`… quá chung chung để làm package top-level). Các package module chỉ được import `contracts` và `fsutil`; **chỉ `orchestrator` biết module cụ thể** và tiêm adapter vào handler (vd stage `tts` nhận cả `TTSAdapter` và `AudioProcessor` mà không import package `audio`).
- **Cưỡng chế:** `tests/test_architecture.py` quét AST; module gọi chéo nhau thì test đỏ.

### D-21 ✅ Tên state Phase 1 và ánh xạ sang HANDOFF §15
- Dùng đúng danh sách Phase 1, thêm 3 running state để mỗi stage đều có `queue → running → done`: `AUDIO_PROCESSING`, `OUTPUT_PUBLISHING`, `UPLOADING`.
- Ánh xạ: `TTS_PLANNING`+`TTS_RENDERING` → `TTS_RUNNING`; `MASTER_AUDIO_READY` → `AUDIO_READY`. `FAILED` là một state kèm `failed_stage` + `last_error` thay cho "failure state theo từng stage" của HANDOFF §15 (retry đúng chỗ vẫn đạt được nhờ `failed_stage`). Bảng đầy đủ ở `MODULE_CONTRACTS.md` §9.

### D-22 ✅ Checkpoint là một transaction SQLite; manifest là bản dẫn xuất
- Artifact + stage_run + chuyển state commit cùng một transaction (`JobStore.succeed`); chỉ owner của lease mới commit được (kết quả của tiến trình đã mất lease bị bỏ). Manifest ghi lại từ DB sau mỗi stage và dựng lại khi khởi động nên crash giữa commit và ghi file không để lại manifest sai.
- Đầu vào mỗi stage được kiểm lại **theo kích thước** (rẻ với file GB); sha256 tính một lần lúc niêm phong.

## 2. Câu hỏi còn mở

Không còn câu hỏi nào chặn Phase 1. D-03, D-04, D-07 đã được chốt bằng mặc định suy ra từ code/môi trường (xem trên). Còn lại là **điều kiện đầu vào runtime**, không suy ra được từ code và chỉ cần tới Phase 2c/5; `doctor` sẽ báo thiếu thay vì chặn:

| Điều kiện | Cần ở | Mặc định nếu chưa có |
|---|---|---|
| Google Cloud OAuth client + một kênh thử (R4) | Phase 2c, 6 | Spike 2c bỏ qua; `PublishAdapter` chạy bằng `FakePublish`; `doctor` báo "YouTube chưa cấu hình" |
| `assets/template.png` + font tiếng Việt cho thumbnail (R5) | Phase 5 | Thumbnail bị bỏ qua có cảnh báo, video vẫn render; không tự tạo template |
| Frame 16:9 1920×1080 + layout (R6) | Phase 5 | Orchestrator sinh một frame viền đen tối thiểu để chạy được; thiết kế đẹp là việc sau |
| Engine TTS đầu tiên | Phase 4 | Bắt đầu với engine local đã có trong môi trường (VieNeu-TTS, sẽ audit ở Phase 4) |

## 3. Đề xuất chỉnh HANDOFF (chưa áp dụng, chờ duyệt)

| Mục HANDOFF | Chỉnh đề xuất | Mã |
|---|---|---|
| §2 Story | Ghi rõ: `story-branch` = chuẩn bị nhánh từ truyện có sẵn; sinh truyện là `story-long-write`; không headless; tiếng Trung | A1–A4 |
| §3/§4 | Thêm SourceProcessor là module **mới**, không có trong ba project | A2 |
| §5 | Ghi Story Assembler là module mới; chương hiện là file riêng có dòng tiêu đề | A5 |
| §2 Media/§12 | Ghi: ContentFlow chưa có profile 16:9/9:16; profile do orchestrator cấp qua frame PNG + config | A8 |
| §11 | Speed ×2/split/watermark thuộc AudioProcessor của orchestrator | A11–A12 |
| §13 Source Sync | Ghi: hiện là hàm/tab chạy thủ công; nền "background" là việc của orchestrator | A10 |
| §15/§16 | Ghi: `UPLOADING/PUBLISHED` chỉ áp dụng YouTube; TikTok = xuất file | A15 |
| §18 | Ghi: nền video ngẫu nhiên không tái tạo y hệt | A20 |
| §21 | Bổ sung: ngôn ngữ đích `vi` (D-04); Story = S2 direct LLM (D-03); tên output (D-07); đồng thời render (D-15) | |

## 4. Giới hạn đã biết sau Phase 1

- Dừng có chủ đích dựa vào handler **hợp tác** (kiểm `ctx.cancel`); handler không hợp tác sẽ chặn shutdown cho tới khi xong hoặc bị kill (rồi quay về cơ chế lease).
- Chưa có: `cancel` job, `rerun --from <stage>` (cần cho "đổi watermark chỉ build lại nhánh YouTube", Phase 5/7), cache-hit liên job theo `stage_key` (đã tính và lưu, chưa dùng), CLI ưu tiên job.
- Chưa có `doctor.ps1`, `setup.ps1`, `update.ps1`, `start.ps1` (HANDOFF §19) — chuyển sang Phase 2 (doctor/setup bản đầu) và Phase 7.
- Thay gói output của chính job khi retry là `rmtree` rồi `rename` (cửa sổ ngắn không có gói); gói vẫn không bao giờ ở trạng thái nửa vời.
- Chỉ kiểm thử trên Windows (kill bằng TerminateProcess). Nhiều orchestrator trên cùng DB được kiểm bằng 2 luồng trong một tiến trình và bằng kill/resume, chưa kiểm bằng 2 tiến trình chạy đồng thời.
- Fake adapter không đổi tốc độ audio thật (chỉ chia part theo `target×speed`), video/thumbnail chỉ là byte giả.
