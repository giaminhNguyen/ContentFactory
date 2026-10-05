# DECISIONS

> Nhật ký quyết định kiến trúc. `HANDOFF.md` là nguồn sự thật cho các quyết định **đã chốt** (§20); file này ghi thêm các quyết định phát sinh từ Phase 0 và các câu hỏi **còn mở**.
> Trạng thái: ✅ Đã quyết · 🟡 Đề xuất (làm theo trừ khi bị đổi) · 🔴 Mở (cần người dùng).

## 1. Quyết định

### D-01 ✅ Ba project đặt ở `modules/`, giữ nguyên repo git độc lập
- **Bối cảnh:** HANDOFF §19 yêu cầu pin version theo commit; ba repo có `.git` riêng, remote `git@home.com:…`.
- **Quyết định:** chuyển vào `modules/ContentFlow`, `modules/oh-story-claudecode`, `modules/yt_uploader`; repo gốc ContentFactory **không** chứa code của chúng (`.gitignore` bỏ `modules/*/`), pin bằng `modules.lock` (SHA). `setup.ps1` (Phase 1) clone theo SHA.
- **Hệ quả:** repo gốc nhẹ, không lẫn lịch sử. Máy mới cần truy cập được `git@home.com` (R10); nếu không, chuyển sang submodule/mirror sau.
- **Đã làm ở Phase 0:** di chuyển + xác minh SHA không đổi, working tree sạch.

### D-02 🟡 Orchestrator viết bằng Python
- **Lý do:** ContentFlow là Python và có thư viện `media_core`; ffmpeg/ffprobe bọc dễ; HTTP client tới yt_uploader đơn giản; SQLite có trong stdlib. HANDOFF không nêu ngôn ngữ.
- **Hệ quả:** hợp đồng ở `MODULE_CONTRACTS.md` viết dạng Python Protocol nhưng ràng buộc thật là artifact + manifest, nên đổi ngôn ngữ vẫn được.
- **Ghi chú:** chọn Python **không** có nghĩa import code ContentFlow; vẫn dùng subprocess (D-05).

### D-03 🔴 Chiến lược StoryAdapter: S1 (điều khiển oh-story qua `claude -p`) / S2 (direct LLM) / S3 (hybrid)
- **Bối cảnh:** `story-branch` chỉ chuẩn bị tư liệu và không sinh văn bản; `story-long-write` không headless, tiếng Trung, từng chương một file; không có assembler (A1–A7).
- **Đề xuất làm trước khi quyết:** đóng băng contract `StoryAdapter`, dựng bằng `FixtureStoryAdapter` (Phase 1), spike S1 vs S2 (Phase 2a) và quyết bằng số liệu.
- **Cần người dùng trả lời:** có chấp nhận hướng S2 (không dùng runtime oh-story, chỉ tham khảo phương pháp) không? Có kỳ vọng dùng `story-branch` theo nghĩa "viết nhánh tiếp của một truyện đã có" không, hay chỉ muốn "dùng repo này" như nguồn kỹ thuật?

### D-04 🔴 Ngôn ngữ của truyện/audio
- **Bối cảnh:** HANDOFF không nêu. Story chỉ tiếng Trung; ContentFlow kiểm glyph tiếng Việt; môi trường có skill VieNeu-TTS (chưa audit, không thuộc 3 project).
- **Cần trả lời:** ngôn ngữ đích (Việt? Trung? khác?), và ngôn ngữ của nguồn đầu vào. Quyết định này loại hoặc giữ S1 (R2).

### D-05 ✅ ContentFlow dùng qua `media_worker` (subprocess + JSON-lines), không import
- **Lý do:** worker có protocol `idempotency_key`, `status`, cancel, deadline, atomic output, exit code định nghĩa; hai commit gần nhất của ContentFlow làm đúng việc này. Tránh phụ thuộc phiên bản Python/Pillow của orchestrator.
- **Hệ quả:** Source Sync (không có job type) dùng shim gọi `sync_videos` bằng Python của ContentFlow (D-09).

### D-06 ✅ TikTok: chỉ xuất file, không đăng tự động
- **Lý do:** yt_uploader chỉ YouTube; không project nào có TikTok (đã grep). HANDOFF §16 cũng chỉ yêu cầu `tiktok/part_NN.mp4` trong output.
- **Hệ quả:** `PUBLISHED` chỉ nghĩa là YouTube đã đăng; nhánh TikTok kết thúc ở `OUTPUT_READY`. Thêm `PublishAdapter` TikTok là việc tương lai.

### D-07 🔴 Quy tắc đặt tên `output/<project>/` (HANDOFF §21)
- **Đề xuất:** `<yyyymmdd>_<slug-tiêu-đề>`; trùng thì hậu tố `-2`. Chờ xác nhận.

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

## 2. Câu hỏi mở tổng hợp (cần người dùng)

1. **D-03** — Story: S1 / S2 / S3? Mục đích dùng `story-branch` là gì?
2. **D-04** — Ngôn ngữ đích và ngôn ngữ nguồn.
3. **D-07** — Quy tắc đặt tên output.
4. Có sẵn Google Cloud OAuth client + kênh thử để làm spike Phase 2c không? (hạn mức/chính sách private của project API là điều chưa biết.)
5. Có sẵn `template.png`, font và frame 16:9 cho ContentFlow không, hay cần thiết kế mới?
6. Máy chạy chính có GPU NVENC không (ảnh hưởng R7, R12)?

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
| §21 | Bổ sung: ngôn ngữ đích; chiến lược Story (D-03/D-04) | |
