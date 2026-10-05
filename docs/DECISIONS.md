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
- Mẫu tên đặt ở `config/output.yaml`, không hardcode.

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
- **Quyết định:** giới hạn đồng thời theo loại tài nguyên trong `config/resources.yaml`: render GPU = 1, TTS theo `max_concurrency` của engine, upload theo `--concurrency` của yt_uploader (mặc định 2). `doctor` đọc GPU thật để đề xuất giá trị; nâng lên sau khi đo (R12).

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
