# IMPLEMENTATION_PHASES

> Lộ trình triển khai sau Phase 0. Nguyên tắc: **walking skeleton trước** (pipeline chạy end-to-end với adapter giả), rồi thay từng adapter bằng bản thật, **rủi ro cao làm sớm** (Story, upload thật). Mã R#/A# tham chiếu `INTEGRATION_PLAN.md` và `CURRENT_SYSTEM_AUDIT.md`.

Mỗi phase có **đầu ra kiểm chứng được**; chưa đạt thì chưa sang phase sau. Không phase nào sửa `modules/*`.

## Phase 0 — Audit ✅ (đã xong)

Đầu ra: 5 tài liệu trong `docs/`, `modules.lock`, commit. Các quyết định mở (D-03 Story = S2 — sau đó bị D-23 thay ở Phase 2, D-04 ngôn ngữ = `vi`, D-07 tên output) đã được chốt bằng mặc định suy ra từ code/môi trường; xem `DECISIONS.md`.

## Phase 1 — Khung orchestrator + walking skeleton ✅ (đã xong)

**Đã làm:** `src/contentfactory/` (orchestrator, jobs, adapters, source, story, tts, audio, render, publish, output), SQLite job store, state machine + stage table, hàng đợi theo stage có giới hạn tài nguyên, checkpoint nguyên tử, retry theo lớp lỗi, resume sau kill (lease + heartbeat), dừng êm, structured logging (JSONL chung + riêng từng job), manifest dẫn xuất, workspace riêng từng job, `OutputPublisher` thật, fake adapter cho cả 7 contract, CLI `python -m contentfactory {submit,run,status,retry}`, `scripts/run_fake_job.py`, 31 test (stdlib `unittest`, ~13 s). Chi tiết quyết định: `DECISIONS.md` D-16…D-22; hợp đồng: `MODULE_CONTRACTS.md` §9–§10.
**Kiểm chứng:** job fake đi hết `NEW → PUBLISHED`; kill tiến trình thật giữa stage và giữa chunk TTS, chạy lại thì resume đúng stage (chunk xong không tổng hợp lại); stage lỗi không làm mất artifact stage trước, retry chỉ chạy lại stage lỗi; xóa `output/` không ảnh hưởng pipeline; test kiến trúc cấm module import nhau. Đã thử phá code có chủ đích (3 lỗi) và test bắt được cả 3.
**Hoãn sang phase sau:** `doctor`/`setup`/`update`/`start` (bản đầu cùng spike, đầy đủ ở Phase 7), `rerun --from`, cache-hit theo `stage_key`, `cancel` job. Danh sách giới hạn: `DECISIONS.md` §4.

## Phase 2 — Source + Story thật ✅ (đã xong; Story chưa kiểm chứng với LLM thật)

**Đã làm:**
- **Source** (`source/`; bản đầu dùng yt-dlp, nay là `SourceAdapter` nhiều provider, xem phần mở rộng bên dưới): URL YouTube → `yt-dlp` (ưu tiên phụ đề có sẵn, sau đó auto-caption) → phụ đề gốc nguyên byte → `transcript_structured.json` (cue/câu/đoạn với `start,end,text,gap_before`, khử auto-caption rolling) → dựng câu/đoạn bằng timestamp (ghép caption bị cắt giữa câu, phát hiện pause, phục hồi đoạn, dấu chấm + viết hoa) → `transcript.txt` sạch không timestamp. Idempotent theo từng bước, có cache liên job theo video id.
- **Story** (`adapters/story_branch.py`, `story/`): `StoryBranchAdapter` điều khiển story-branch → story-long-write qua Claude CLI headless (resume theo file, chặn chi phí, quyền thận trọng); **Story Assembler** (gỡ heading/marker, làm mượt chỗ nối, loại trùng lặp, lưới an toàn) + validator bất biến; blueprint/continuity/sections nằm ở workspace nội bộ.
- Artifact mới: `subtitle_raw`, `transcript_structured`, `story_report`. Cấu hình chọn adapter thật: `adapters.source = "provider_chain"`, `adapters.story = "story_branch"`.
- `scripts/run_real_job.py` (`--dry-run` kiểm tra điều kiện, không gọi LLM), 86 test (stdlib `unittest`, ~17 s).

**Kiểm chứng:** ba mẫu phụ đề (auto-caption rolling, manual bị cắt giữa câu, có timestamp gap); `story.txt` không heading (stage + validator); rerun không tải/làm lại artifact còn hợp lệ (cùng workspace và job mới cùng URL); resume story theo bước và theo lô chương; `ClaudeCliRunner` chạy với tiến trình giả lập CLI thật (kết quả, tác vụ nền, lỗi đăng nhập, huỷ giữa lượt); deploy oh-story thật vào thư mục tạm không sửa module; **YouTube thật**: phụ đề thủ công + auto-caption thật. Đã phá code có chủ đích 7 chỗ ở phần mới và test bắt cả 7.
**Chưa kiểm chứng:** lượt Claude thật (tiếng Việt qua oh-story, cổng xác nhận, chi phí) và phụ đề tiếng Việt thật. Xem `DECISIONS.md` D-23, §5.

### Phase 2.5 — tích hợp Subtitle_supperVip + thiết kế job control ✅ (không phải Phase 3)

**Đã làm:** audit `Subtitle_supperVip` (`CURRENT_SYSTEM_AUDIT.md` §6–§7); `SourceAdapter` + `SourceProvider` + `SourceResult` mới; `ProviderChain` (fallback, cache chung, khóa chống tải trùng, dấu vân tay trong workspace); providers `supervip` (bridge tới code của module), `ytdlp` (fallback), `local`, `text`; `TranscriptProcessor` tách riêng và nhận cả `srt/vtt/json/txt`; bố cục `source/{source.json, subtitle_raw.*, transcript_structured.json, transcript_clean.txt}`; vô hiệu hóa cache của Story theo dấu vân tay đầu vào; gỡ `YouTubeSourceProcessor` cũ; cập nhật `HANDOFF.md` (§2, §2A, §3, §4, §4A, §5, §15, §19, §20, §21).
**Sửa lỗi phát hiện trên đường:** `Orchestrator.run()` không dùng lại được sau lần dừng đầu tiên (token huỷ không reset); race khi hai job cùng nguồn chạy song song cùng tải.
**Kiểm chứng:** 116 test (stdlib), gồm chạy **code thật của Subtitle_supperVip** với stub thư viện mạng; cây thư mục của module không bị đụng; **YouTube thật**: supervip và yt-dlp cho transcript sạch giống hệt nhau (similarity 1.0), fallback và rerun (0,0 s) đúng. **Chưa kiểm chứng:** phụ đề tiếng Việt thật.
**Thiết kế metadata/publishing (chỉ tài liệu, chưa code):** `project.title` canonical, thumbnail = `channel.name` + `project.title`, YouTube title/description bằng template, Channel Config, Sequence/Full Audio STT reserve một lần (HANDOFF §4B, MODULE_CONTRACTS §12, DECISIONS D-43…D-49).
**Thiết kế job control (chỉ tài liệu, chưa code):** stage-based artifact pipeline (`start_stage`/`target_stage`, import, skip theo artifact hợp lệ), hold/`PAUSED_*` thay cho fail với lỗi tài nguyên tạm thời, Resource Monitor deterministic, Auto Resume (global default `true`, override theo job), retry/backoff có jitter và `Retry-After`, config snapshot theo job. Xem HANDOFF §15A–§15C, MODULE_CONTRACTS §11, DECISIONS D-36…D-42.
Không bắt đầu TTS (Phase 3).

### Phase 2.9 — căn chỉnh lõi với kiến trúc pipeline cuối ✅ (không phải Phase 3)

**Đã làm:** hợp đồng stage mở rộng + `start_stage`/`target_stage` (planner, `MODES`, import artifact, `from_job`, skip khi hợp lệ, `stage_key` theo khai báo); hold `PAUSED_*` tách khỏi `FAILED_PERMANENT`; checkpoint chi tiết; Auto Resume (global + theo job, có trần không-tiến-triển); khung Resource Monitor; retry có jitter/`Retry-After`; config snapshot theo job; migration DB có phiên bản và sao lưu. Chi tiết: `DECISIONS.md` D-50…D-56, `MODULE_CONTRACTS.md` §11.
**Tương thích:** SCHEMA Phase 1 không đổi; cả 116 test Phase 0–2.5 vẫn pass nguyên vẹn; job cũ không snapshot/target chạy như trước.
**Kiểm chứng:** +31 test (`test_stage_control`, `test_pause_resume`): Source→Story, Story artifact→Audio, Audio artifact→Video, skip artifact hợp lệ, lỗi tài nguyên → `PAUSED` (không `FAILED`), Auto Resume ON/OFF, trần không tiến triển, đĩa đầy → resume, restart giữ checkpoint/start/target, stage đã xong không chạy lại, snapshot cô lập, migration v0→v1, bảng policy; kiểm đột biến cho cổng OFF và trần auto-resume.
Không bắt đầu TTS (Phase 3).

## Việc chờ (chưa xếp phase)

| Việc | Mục tiêu | Ghi chú |
|---|---|---|
| ~~Job control layer~~ | ✅ **đã làm ở Phase 2.9** | xem trên; còn lại: `pause`/`cancel`/`rerun --from`, cache liên job, probe GPU/quota thật |
| Kiểm chứng Story thật | chạy `scripts/run_real_job.py URL --chapters 3 --max-budget-usd 2` trên một video tiếng Việt | quyết định giữ S1 hay chuyển S2 (D-23) |
| Spike ContentFlow thật | cài Pillow/pytest, chạy test ContentFlow, render thật vài phút audio với NVENC | R6, R7, R14 |
| Spike yt_uploader thật | build, tạo OAuth client, upload `private` lên kênh thử, đặt thumbnail, thử `idempotency_key` | R4; bỏ qua nếu chưa có OAuth client + kênh thử |
| `doctor` bản đầu | gom `health()` của adapter, ffmpeg/NVENC, `media_worker health`, yt-dlp, claude CLI | HANDOFF §19 |

## Phase 3 — TTS

**Làm:** `TTSAdapter` cho **một** engine đầu tiên, TTSProfile/TTSRule schema, Text Preprocessor, SegmentPlanner (AI) + RuleValidator tất định, TTS Manager (chunk, retry từng chunk, cache), Audio QA, `AudioProcessor.assemble`.
**Xong khi:** `story.txt` 40–60 phút → `master.wav`; hỏng một chunk chỉ retry chunk đó; chạy lại không gọi lại TTS (cache hit); đổi video/watermark/title không làm TTS chạy lại.
**Ràng buộc:** không phụ thuộc publishing metadata ngoài identifier (`project.id`, `language`) (D-48).
**Không làm ở phase này:** TTS Auto-Profile/Auto Tune (HANDOFF §7) — để Phase 8.

## Phase 4 — Audio

**Làm:** `AudioProcessor.build_youtube_audio` (watermark là channel asset), `build_tiktok_parts` (tăng tốc, cắt part theo config), Audio QA cho các bản phân phối.
**Xong khi:** từ `master.wav` ra audio YouTube (có watermark) và N part TikTok đúng thời lượng cấu hình; đổi watermark chỉ build lại nhánh YouTube; không phụ thuộc publishing metadata (D-48).

## Phase 5 — Render

**Làm:** `ContentFlowRenderAdapter` (worker subprocess), Source Sync shim + kiểm tra sau sync (R8), profile YouTube 16:9 (tạo frame + layout, R6) và TikTok 9:16, **thumbnail dùng `channel.name` + `project.title`** (D-44: wrapping/font sizing, không đổi/cắt title; asset, nén ≤2 MiB, R5), reconcile sau crash.
**Xong khi:** từ audio ra `youtube/video.mp4` 1920×1080 + thumbnail hợp lệ và N file `tiktok/part_NN.mp4` 1080×1920; đo thời gian render 40–60 phút (R7); thumbnail với title dài vẫn đọc được mà `project.title` giữ nguyên.

## Phase 6 — Publishing

**Làm:** Channel Config (schema đầy đủ, đọc + snapshot theo job), **Metadata Builder** (YouTube title `[Full Audio {sequence}] | {project_title}`, description template; strict, báo lỗi khi vượt giới hạn), **Sequence Manager** (reserve một lần, lưu cố định), publish package (`OutputPublisher` hoàn chỉnh: `title.txt`/`description.txt` từ Metadata Builder, `project.json` có `project.title` và sequence), `YtUploaderPublishAdapter` (HTTP, token, idempotency, poll), policy retry theo `error_class`, xử lý `AMBIGUOUS_UPLOAD`, `start.ps1` quản daemon.
**Xong khi:** một job thật chạy NEW → PUBLISHED (video `private`/`unlisted` ở kênh thử) với đúng title/description từ template; retry upload và rerender **không** đổi sequence; kill orchestrator lúc upload rồi chạy lại **không** tạo video thứ hai.

## Phase 7 — Pipeline bất đồng bộ và vận hành

**Làm:** worker pool theo stage (HANDOFF §14), giới hạn đồng thời theo tài nguyên (GPU/đĩa, R12), nhiều job song song với workspace riêng, `update.ps1`, `doctor` đầy đủ (HANDOFF §19), CLI/UI tối thiểu (form Input/Channel/TTS/Pool + RUN).
**Xong khi:** ≥3 job chạy chồng stage mà không tranh chấp; máy mới `git clone → setup → start` chạy được.

## Phase 8 — TTS Auto-Profile (HANDOFF §7–8)

**Làm:** TTS Source Analyzer (repo/docs → adapter draft + candidate profile có `confidence/source`), Auto Tune benchmark tùy chọn, thêm engine thứ hai để kiểm chứng tính tổng quát của contract.
**Xong khi:** thêm một TTS mới chỉ bằng repo/docs reference, không nhập tham số tay.

## Phụ thuộc

```text
P0 audit -> P1 core -> P2 Source + Story -> P3 TTS -> P4 Audio -> P5 Render -> P6 Publishing -> P7 Async + vận hành -> P8 TTS Auto-Profile
                  \-> (Việc chờ: kiểm chứng Story thật, spike ContentFlow/yt_uploader, doctor, job control layer) chạy song song khi cần
```
Từ P3 trở đi là đánh số hiện hành (D-49); nội dung chi tiết là đề xuất. P3 cần `story.txt`, có thể dùng `FakeStory` cho tới khi Story thật được kiểm chứng. Thumbnail (P5) cần `project.title` và `channel.name`; Metadata Builder và Sequence Manager (P6) cần Channel Config.

## Chưa nằm trong phạm vi

Đăng TikTok tự động (D-06), tách nhiều kênh YouTube trong một login, giao diện đồ họa hoàn chỉnh, hỗ trợ ngoài Windows, Redis/Kafka (HANDOFF §15).
