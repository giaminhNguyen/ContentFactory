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

## Phase 3 — TTS framework + auto onboarding ✅ (đã xong; chưa có engine TTS thật)

**Đã làm:** hợp đồng `TTSAdapter` + capability schema; profile schema có `source/confidence/evidence` cho từng giá trị; Text Normalizer; Rule Segment Planner + AI Segment Planner (chỉ gom câu) + validator tất định; TTS Manager (retry riêng từng segment, QA chunk, cache hai tầng theo nội dung, `tts_manifest`); `CommandTTS` (adapter CLI tổng quát) và cơ chế `module:Class` + `adapter_config` (thêm engine không sửa core); TTS Analyzer (repo/docs/source → capabilities + profile candidate + ứng viên adapter + `needs_user`); Auto Tune framework (benchmark nội bộ, thang độ dài, phát hiện lỗi/timeout/hỏng/im lặng/bất thường). Xem `DECISIONS.md` D-57…D-63, `MODULE_CONTRACTS.md` §3.
**Kiểm chứng:** +51 test (`tests/test_tts.py`; tổng 198): FakeTTS chạy cả pipeline, validator bắt kế hoạch xấu (mất/lặp/đổi chữ, cắt giữa từ, quá dài…), retry đúng segment lỗi, cache hit khi giống / miss khi đổi voice/model/settings/engine/phiên bản (và không miss khi chỉ đổi pause), cache hỏng không được tin, profile evidence/confidence, adapter ngoài repo và engine CLI onboard từ repo mẫu chạy trong pipeline không sửa core, Analyzer trên repo mẫu và nhiều dạng nguồn, Auto Tune với các kiểu hỏng; kiểm đột biến cho khóa cache, validator và retry theo segment.
**Chưa kiểm chứng / chưa làm:** engine TTS thật nào (chỉ fake + CLI mẫu), LLM thật cho AI Planner và Analyzer, adapter HTTP tổng quát, synth song song, định dạng ngoài WAV, đọc số/ngày, chất lượng nghe, repo TTS thật cho extractor (D-61, D-62, D-63). **Việc cần làm tiếp để dùng thật:** chọn engine đầu tiên (Phase 8 hoặc trước đó), chạy `tts_onboard.py` + `tts_tune.py` trên nó, rồi nghe thử.
**Ràng buộc giữ nguyên:** không phụ thuộc publishing metadata ngoài identifier (`project.id`, `language`) (D-48).
Không bắt đầu Audio (Phase 4).

## Phase 4 — Audio Quality Pipeline ✅ (đã xong; chưa kiểm chứng với giọng TTS thật)

**Đã làm:** chuẩn hóa kỹ thuật + dọn biên (cắt im lặng tới mép tín hiệu, fade vi mô, biên chuẩn) + Pause Engine theo profile + ghép lossless → narration thô và `audio_timeline`; Narration Master (highpass?/compressor?/loudnorm 2 lượt linear/limiter, mọi tham số bằng profile); bản YouTube (watermark khớp độ to + gap, phần truyện ghép nguyên từng mẫu); TikTok (rubberband ×speed giữ cao độ, split thông minh theo timeline hoặc khoảng lặng, cửa sổ dao động, part cuối ngắn hơn được, không part vụn); Audio QA (corrupt, empty, clipping, im lặng quá mức, thiếu chunk, sai định dạng, độ dài bất thường, loudness, true peak); `audio_report`. Xem `DECISIONS.md` D-64…D-69, `MODULE_CONTRACTS.md` §4.
**Kiểm chứng:** +68 test (`tests/test_audio.py`; tổng 266): nối 12 chunk không click (đo bước nhảy giữa các mẫu; đối chứng nối thô thì click), pause nghe được đúng giá trị profile, loudness đạt −16/−20 LUFS và true peak dưới trần, limiter chặn clipping, compressor giảm chênh lệch mức, định dạng nội bộ 48 kHz mono 24-bit, **×2 giữ cao độ** (Goertzel: 440 Hz ⇒ ~440 Hz; đối chứng resample thô ⇒ ~880 Hz), split chọn ranh giới và cắt trong khoảng lặng ở ranh giới đoạn, tránh cắt giữa câu khi có ranh giới gần, QA bắt audio lỗi giả lập (hỏng, cụt, rỗng, clipping, im lặng, sai định dạng, sai độ dài), đổi watermark không TTS lại (stage_key, from_job và cache trong stage), pipeline đầy đủ với ffmpeg thật, thiếu ffmpeg ⇒ job bị giữ; kiểm đột biến cho cao độ, split, dọn biên và QA clipping. Phần thuần chạy mọi nơi; phần ffmpeg bỏ qua nếu máy không có ffmpeg.
**Sửa trên đường:** `module wave` không đọc được WAV 24-bit/float của ffmpeg và nhiều engine TTS (validator và QA chunk TTS từng coi là hỏng) ⇒ `fsutil.wav_header`.
**Chưa kiểm chứng / chưa làm:** giọng TTS thật và nghe thử (ngưỡng và mặc định là khởi điểm), de-esser/EQ/denoise, ducking, crossfade, cache trung gian dùng chung giữa job, ranh giới `scene` (D-67, D-69). Mặc định `adapters.audio` vẫn là `fake`.
**Ràng buộc giữ nguyên:** không phụ thuộc publishing metadata (D-48).
Không bắt đầu Render (Phase 5).

## Phase 5 — Render (tích hợp ContentFlow) ✅ (đã xong; kiểm chứng với ContentFlow thật ở mức adapter)

**Đã làm:** `ContentFlowRender` bọc `media_worker` (subprocess JSON-lines, hủy, ánh xạ lỗi, replay theo `idempotency_key`, kiểm kết quả bằng ffprobe); profile YouTube 16:9 và TikTok 9:16 (frame trong suốt đúng kích thước do ta sinh); Source Sync **dùng chung và chạy nền** (dấu vân tay, chỉ tin file do mình ghi nhận, khóa liên tiến trình, thread nền, CLI `pools`); Render Manager (khóa nội dung + sidecar nên không render lại video hợp lệ, retry riêng từng output, part lỗi không chặn part khác, trạng thái từng part trong checkpoint/`status`/report, `retry-part`); lane render riêng (`gpu` = 1) nên Story/TTS/Audio tiếp tục. Xem `DECISIONS.md` D-70…D-75, `MODULE_CONTRACTS.md` §5.
**Kiểm chứng:** +49 test (`tests/test_render.py`; tổng 315, +3 test thật bỏ qua nếu không có `CF_TEST_CONTENTFLOW_PYTHON`): profile/frame/pool/lock/ánh xạ lỗi (thuần), adapter với `media_worker`+`source_sync` giả cùng giao thức (replay, hủy, lỗi có kiểu, worker chết, sync một lần + dùng lại + nguồn đổi + file cụt + 4 luồng đồng thời chỉ 1 sync + chờ hủy được), pipeline đầy đủ (pool đồng bộ đúng một lần qua nhiều job; **part 03 lỗi ⇒ chỉ part 03 được retry**, part khác render đúng một lần; retry thủ công chỉ render part 03; `retry-part`; DISK_FULL/thiếu template/thiếu pool ⇒ giữ job, không FAILED), **đồng thời** (job kẹt ở render YouTube: job B, C, job story-only mới vẫn chạy; mỗi lúc một job render; trạng thái part `done/rendering/pending` rõ ràng), sync nền không chặn Story/TTS; **ContentFlow thật** (Source Sync thật, render 16:9 và 9:16 đúng kích thước/độ dài, replay, output sai kích thước bị loại, thumbnail thiếu/có asset); kiểm đột biến (bỏ kiểm hợp lệ, dừng ở part lỗi đầu tiên, tin file đích bất kỳ, nới lane gpu). Test suite của ContentFlow trong venv tạm có Pillow: 312 pass, 12 skip.
**Sửa trên đường:** `progress()` bỏ rơi cập nhật trạng thái dồn dập (throttle 50 ms) ⇒ thêm `force` cho đổi trạng thái; adapter đóng pipe stdout của worker (ResourceWarning).
**Chưa kiểm chứng / chưa làm:** NVENC, render dài 10–60 phút ở quy mô thật (trần 3600 s mỗi lệnh ffmpeg của ContentFlow, R7), thumbnail với template/font thật của bạn (repo ContentFlow không có asset, R5), pipeline story→publish với ContentFlow thật, `scripts/setup` cho Pillow/venv, tiêu đề thumbnail theo `project.title` (Phase 6). Mặc định `adapters.render` vẫn là `fake`.
Không bắt đầu Publishing (Phase 6).

## Phase 6 — Output package + YouTube uploader ✅ (đã xong; chưa từng upload thật lên YouTube)

**Đã làm:** OutputPublisher thật (gói đúng layout, chỉ copy có kiểm sha256, **không ghi đè**: nội dung y hệt ⇒ không đụng; nội dung khác ⇒ phiên bản `-vN` bên cạnh, `project.json` có `version/supersedes` và trỏ từng file tới artifact nguồn kèm sha256, README đọc được ngay, part đánh số có thứ tự); Metadata Builder strict + `project.title` + Channel Config (`channels/<id>/channel.json`, snapshot theo job) + Sequence Manager (DB v2, số không cấp lại, idempotent, an toàn song song); `YtUploaderPublish` (HTTP client daemon `yt_uploader`, **một idempotency_key = tối đa một video**, retry = resume không upload lại, lỗi vĩnh viễn/AMBIGUOUS không retry mù, ánh xạ quota/auth/mạng, thumbnail > 2 MiB nén ra file tạm); stage `publish` đọc artifact workspace + `publish_metadata`. Xem `DECISIONS.md` D-76…D-81, `MODULE_CONTRACTS.md` §6, §7, §12.
**Kiểm chứng:** +57 test (`tests/test_publishing.py`; tổng 372 khi bật cả test thật, 368 chạy mặc định): Sequence (idempotent, `last_used`, không cấp lại sau release, 16 luồng, migration v1→v2), Metadata Builder (template strict, giới hạn 100 ký tự/5000 byte báo lỗi không cắt), Channel Config (nạp/lỗi/watermark mặc định/từ chối lúc submit), OutputPublisher (layout đúng từng file, `project.json` đối chiếu sha256 với workspace, thứ tự part kể cả >99, gói người dùng sửa/đổi tên/chuyển đi không bị đụng hay làm crash, phiên bản mới giữ nguyên bản cũ từng byte, copy sai bị chặn), adapter với daemon giả cùng API (không tạo video thứ hai, resume khi retry, ánh xạ 11 loại lỗi, paused, hủy rồi tìm lại job, daemon down/token sai/thiếu feature), pipeline đầy đủ (upload từ workspace, **upload lỗi không mất video và retry không render lại**, giữ job khi quota/auth/daemon down, **chuyển/xóa thư mục output giữa chừng không phá pipeline**, sequence một lần mỗi project và mỗi kênh, snapshot Channel Config, đổi `project.title` chỉ đổi key render_youtube/output chứ không TTS/Audio, thumbnail dùng `channel.name` + `project.title`); **daemon THẬT** build bằng Go (health + feature flags, tra cứu theo key, chưa đăng nhập ⇒ AUTH); kiểm đột biến (ghi đè gói, tạo job lặp, retry lỗi vĩnh viễn, tái dùng số đã release, cắt title im lặng).
**Sửa trên đường:** thumbnail của render_youtube dùng tiêu đề nguồn/id kênh ⇒ nay `project.title` + `channel.name` (D-44); README render báo `0.0 MB` cho file nhỏ.
**Chưa kiểm chứng / chưa làm:** upload thật lên YouTube (cần OAuth client + tài khoản Google), nén thumbnail trên ảnh thật, playlist/schedule với daemon thật, vòng đời daemon (Phase 7), TikTok chỉ xuất file (D-06), `rerender --from` cho job đã xong (phiên bản mới = job mới `from_job` + `start_stage=output`). Mặc định `adapters.publish` vẫn là `fake`.
Không bắt đầu Phase 7.

## Phase 7 — Auto Mode / UX cho người lười / setup máy mới ✅ (đã xong; TTS thật và upload thật chưa chạy)

**Đã làm (D-82…D-87):** `cf go <url> --channel K` (một lệnh), channel preset (TTS profile, pool, render, tiktok, audio, watermark, publishing), Auto Resume/Retry (có từ 2.9; `go` chờ + chỉ dẫn), Auto Cleanup, Auto Naming (làm sạch hình thức), Auto TTS profile, Auto pool, `doctor`, `setup.ps1`, `update.ps1`, `start.ps1`, `cf demo`, CLI cơ bản vs `--advanced`, E2E URL → gói output.
**Chưa làm:** ≥3 job chồng stage ở quy mô thật (đo), giám sát daemon uploader, thông báo, UI, Linux/macOS, TTS thật (Phase 8), upload thật.

## Phase 8 — Kiểm chứng production và phát hành ✅ (đã xong trong phạm vi môi trường cho phép)

**Đã làm (D-88):** harness `scripts/validate_real.py` (URL thật → source thật → audio ffmpeg thật → ContentFlow thật → output; SUBTITLE_ONLY; VIDEO_ONLY dài), `tests/test_phase8.py` (video-only không cần story, chẩn đoán, bí mật, upload không chặn render, kill/restart với ffmpeg + ContentFlow thật), sửa lỗi tìm thấy, `docs/{USER_GUIDE,TROUBLESHOOTING,ARCHITECTURE,PRODUCTION_CHECKLIST}.md`.
**Chưa kiểm chứng:** Story thật (token), TTS thật, upload thật, NVENC, video dài 10–60 phút, Linux/macOS (xem `docs/PRODUCTION_CHECKLIST.md` §B).

## Phase 8b — Engine TTS thật + Auto-Profile bằng AI (HANDOFF §7–8)

**Đã có từ Phase 3:** Analyzer tĩnh, Auto Tune, `CommandTTS`, profile có evidence (D-57…D-63).
**Làm:** onboard engine TTS thật đầu tiên và engine thứ hai (kiểm chứng contract tổng quát), nối LLM thật cho `ai_infer` của Analyzer và `AISegmentPlanner` (kiểm chứng chất lượng, chi phí), chạy Auto Tune trên engine thật, human review nghe thử, adapter HTTP tổng quát nếu engine cần, dọn dẹp cache TTS.
**Xong khi:** thêm một TTS mới chỉ bằng repo/docs reference, không nhập tham số tay, và nghe được kết quả chấp nhận được.

## Phụ thuộc

```text
P0 audit -> P1 core -> P2 Source + Story -> P3 TTS framework -> P4 Audio -> P5 Render -> P6 Publishing -> P7 Async + vận hành -> P8 TTS Auto-Profile
                  \-> (Việc chờ: kiểm chứng Story thật, spike ContentFlow/yt_uploader, doctor, job control layer) chạy song song khi cần
```
Từ P3 trở đi là đánh số hiện hành (D-49); nội dung chi tiết là đề xuất. P3 cần `story.txt`, có thể dùng `FakeStory` cho tới khi Story thật được kiểm chứng. Thumbnail (P5) cần `project.title` và `channel.name`; Metadata Builder và Sequence Manager (P6) cần Channel Config.

## Chưa nằm trong phạm vi

Đăng TikTok tự động (D-06), tách nhiều kênh YouTube trong một login, giao diện đồ họa hoàn chỉnh, hỗ trợ ngoài Windows, Redis/Kafka (HANDOFF §15).
