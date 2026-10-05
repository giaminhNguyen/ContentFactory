# IMPLEMENTATION_PHASES

> Lộ trình triển khai sau Phase 0. Nguyên tắc: **walking skeleton trước** (pipeline chạy end-to-end với adapter giả), rồi thay từng adapter bằng bản thật, **rủi ro cao làm sớm** (Story, upload thật). Mã R#/A# tham chiếu `INTEGRATION_PLAN.md` và `CURRENT_SYSTEM_AUDIT.md`.

Mỗi phase có **đầu ra kiểm chứng được**; chưa đạt thì chưa sang phase sau. Không phase nào sửa `modules/*`.

## Phase 0 — Audit ✅ (đã xong)

Đầu ra: 5 tài liệu trong `docs/`, `modules.lock`, commit. Các quyết định mở (D-03 Story = S2, D-04 ngôn ngữ = `vi`, D-07 tên output) đã được chốt bằng mặc định suy ra từ code/môi trường; xem `DECISIONS.md`.

## Phase 1 — Khung orchestrator + walking skeleton

**Làm:** repo layout (`orchestrator/ config/ scripts/ runtime/ workspace/ output/ modules/`), SQLite schema job + stage + attempt, state machine theo HANDOFF §15, workspace + manifest + `stage_key` + cache, `ErrorClass`, runner có cancel/retry theo `error_class`, `ArtifactRef` ghi atomic, `doctor` bản đầu (ffmpeg, NVENC, `media_worker health`, thư mục, DB), `modules.lock` + `setup.ps1` (clone theo SHA).
Adapter giả: `FixtureStoryAdapter`, `FakeTTS` (tạo sóng sin/âm im theo độ dài text), `FakeRender`, `FakePublish`, `OutputPublisher` thật.
**Xong khi:** một lệnh chạy job giả từ NEW tới OUTPUT_READY, sinh đúng cây `output/<project>/…`; kill giữa chừng rồi chạy lại chỉ làm lại stage dở; xóa `output/` không phá workspace/DB (HANDOFF §17).

## Phase 2 — Spike rủi ro (song song được, không phụ thuộc nhau)

| Spike | Mục tiêu | Tiêu chí kết luận |
|---|---|---|
| 2a Story (S2) | Prototype `DirectLLMStoryAdapter` tiếng Việt trên một nguồn nhỏ: blueprint → vài section → continuity → assemble | Ra `story.txt` tiếng Việt hợp lệ (qua validator bất biến), chi phí/thời gian/token ước tính, ổn định qua ≥3 lần chạy; nếu không đạt thì ghi lại và điều chỉnh thiết kế section/continuity (không quay lại S1) |
| 2b ContentFlow thật | Cài Pillow/pytest, chạy test ContentFlow; gọi `media_worker` render thật 1–2 phút audio với frame 9:16 mặc định; thử NVENC | Có video; test pass/fail ghi lại (R14); đo thời gian |
| 2c yt_uploader thật | Build, tạo OAuth client, `login`, upload video test `private` lên kênh thử, đặt thumbnail, `idempotency_key` lặp lại | Có `video_id`; hành vi quota/private thật được ghi (R4) |

**Xong khi:** có số liệu chi phí/ổn định cho S2; hai adapter Render/Publish biết chắc chạy được hay không trên máy thật. Spike 2c bỏ qua (kèm cảnh báo `doctor`) nếu chưa có OAuth client + kênh thử; các spike còn lại không bị chặn.

## Phase 3 — SourceProcessor + Story thật

**Làm:** SourceProcessor (URL → transcript/metadata/tùy chọn analysis), `DirectLLMStoryAdapter` (S2) hoàn chỉnh, Story Assembler, validator bất biến (§2 MODULE_CONTRACTS), báo cáo continuity.
**Xong khi:** một URL thật ra `story.txt` không header, đúng ngôn ngữ `vi`, qua validator; resume story giữa chừng hoạt động.

## Phase 4 — TTS

**Làm:** `TTSAdapter` cho **một** engine đầu tiên, TTSProfile/TTSRule schema, Text Preprocessor, SegmentPlanner (AI) + RuleValidator tất định, TTS Manager (chunk, retry từng chunk, cache), Audio QA, `AudioProcessor.assemble`.
**Xong khi:** `story.txt` 40–60 phút → `master.wav`; hỏng một chunk chỉ retry chunk đó; chạy lại không gọi lại TTS (cache hit); đổi video/watermark không làm TTS chạy lại.
**Không làm ở phase này:** TTS Auto-Profile/Auto Tune (HANDOFF §7) — để phase 8.

## Phase 5 — AudioProcessor + Render thật

**Làm:** `build_youtube_audio` (watermark), `build_tiktok_parts` (speed ×2, cắt part theo config), `ContentFlowRenderAdapter` (worker subprocess), Source Sync shim + kiểm tra sau sync (R8), profile YouTube 16:9 (tạo frame + layout, R6) và TikTok 9:16, thumbnail (cung cấp asset, nén ≤2 MiB, R5), reconcile sau crash.
**Xong khi:** từ `master.wav` ra `youtube/video.mp4` 1920×1080 + thumbnail hợp lệ và N file `tiktok/part_NN.mp4` 1080×1920; đo thời gian render 40–60 phút (R7); đổi watermark chỉ build lại nhánh YouTube.

## Phase 6 — Publish YouTube + Output đầy đủ

**Làm:** `YtUploaderPublishAdapter` (HTTP, token, idempotency, poll), policy retry theo `error_class`, xử lý `AMBIGUOUS_UPLOAD`, `OutputPublisher` hoàn chỉnh (README.txt, project.json, tên đụng nhau, verify sha256), `start.ps1` quản daemon.
**Xong khi:** một job thật chạy NEW → PUBLISHED (video `private`/`unlisted` ở kênh thử) và OUTPUT_READY có gói đầy đủ; kill orchestrator lúc upload rồi chạy lại **không** tạo video thứ hai.

## Phase 7 — Pipeline bất đồng bộ và vận hành

**Làm:** worker pool theo stage (HANDOFF §14), giới hạn đồng thời theo tài nguyên (GPU/đĩa, R12), nhiều job song song với workspace riêng, `update.ps1`, `doctor` đầy đủ (HANDOFF §19), CLI/UI tối thiểu (form Input/Channel/TTS/Pool + RUN).
**Xong khi:** ≥3 job chạy chồng stage mà không tranh chấp; máy mới `git clone → setup → start` chạy được.

## Phase 8 — TTS Auto-Profile (HANDOFF §7–8)

**Làm:** TTS Source Analyzer (repo/docs → adapter draft + candidate profile có `confidence/source`), Auto Tune benchmark tùy chọn, thêm engine thứ hai để kiểm chứng tính tổng quát của contract.
**Xong khi:** thêm một TTS mới chỉ bằng repo/docs reference, không nhập tham số tay.

## Phụ thuộc

```text
P1 ─┬─ P2a ─ P3 ─┐
    ├─ P2b ──────┼─ P5 ─┐
    └─ P2c ──────┘      ├─ P6 ─ P7 ─ P8
         P4 ────────────┘
```
(P4 phụ thuộc P1, và cần `story.txt` – có thể dùng fixture – nên song song được với P3.)

## Chưa nằm trong phạm vi

Đăng TikTok tự động (D-06), tách nhiều kênh YouTube trong một login, giao diện đồ họa hoàn chỉnh, hỗ trợ ngoài Windows, Redis/Kafka (HANDOFF §15).
