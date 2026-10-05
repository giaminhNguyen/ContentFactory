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

### D-03 ⚠️ Đã bị thay thế bởi D-23 (Phase 2): StoryAdapter dùng oh-story thay vì tự gọi LLM
- Quyết định ban đầu (Phase 0) là S2 (`DirectLLMStoryAdapter`) vì oh-story chỉ viết tiếng Trung và không headless. Chỉ dẫn Phase 2 của người dùng là **dùng StoryAdapter để gọi logic story-branch hiện có, không sửa sâu oh-story**, nên S1 được làm (D-23). S2 giữ lại làm **phương án dự phòng chưa xây**, kích hoạt nếu kiểm chứng thật cho thấy S1 không dùng được cho tiếng Việt (D-23, rủi ro R2).

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
- Xem `IMPLEMENTATION_PHASES.md`: Phase 1 dựng pipeline với adapter giả; Phase 2 làm Source + Story thật; spike ContentFlow/yt_uploader thật xếp sau.

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
- **Quyết định:** config ở `config/config.json` (đã sửa các chỗ nhắc `.yaml` trong D-07, D-15). Các ví dụ YAML của HANDOFF (`channel.yaml`, TTS profile) sẽ được đọc khi tới Phase 3/5; chọn PyYAML hay JSON lúc đó. Test chạy bằng `python -m unittest discover -s tests -t .` (pytest cũng chạy được nếu có).

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

### D-23 ✅ StoryAdapter = `StoryBranchAdapter`: điều khiển story-branch rồi story-long-write qua Claude Code CLI headless
- **Chỉ dẫn:** Phase 2 yêu cầu dùng StoryAdapter gọi logic story-branch hiện có, không sửa sâu oh-story. Thực tế (audit A1) story-branch **chỉ chuẩn bị tư liệu** (正典 → 分支提案 → 分支简报 → 分支设定) và không viết văn; phần viết là `story-long-write`. Adapter vì vậy chạy cả chuỗi: `story-branch analyze → explore → create → handoff → story-long-write 开书 → 写第a-b章` (lô ≤ 3 chương, giới hạn của oh-story), mỗi bước một phiên mới, trạng thái nằm trên đĩa đúng như oh-story thiết kế.
- **Không sửa oh-story:** deploy bằng chính `scripts/bench/deploy.py` của nó vào workspace riêng của job (`story/oh-story/`), transcript sạch đặt ở `拆文库/<nguồn>/原文.md` làm "tác phẩm gốc". Điều kiện xong của mỗi bước là file trên đĩa nên chạy lại bỏ qua bước đã xong và tiếp tục từ chương chưa commit (`追踪/_tracking-state.json`).
- **Giao tiếp:** interface `AgentRunner` (thật: `ClaudeCliRunner`, theo mẫu stream-json của bench; test: `ScriptedOhStory`). Cổng xác nhận được trả lời bằng một câu cố định ("chọn phương án bạn đề xuất…") tối đa `max_follow_ups` lần; quá giới hạn thì `STORY_STEP_INCOMPLETE` (POLICY, không retry mù vì mỗi lượt tốn tiền); `max_turns` chặn tổng số lượt.
- **Chưa kiểm chứng với LLM thật.** Logic điều khiển, resume, deploy oh-story thật và runner (qua tiến trình giả lập CLI) đã test; **chưa có lượt Claude thật nào được chạy** (tốn chi phí tài khoản và cần quyết định quyền, xem D-26). Hai rủi ro chưa biết: (1) oh-story viết tiếng Trung, bộ kiểm "AI-flavor"/đếm chữ CJK có thể chặn commit chương tiếng Việt; (2) agent có thể kẹt ở điểm xác nhận mà câu trả lời cố định không giải quyết được. Cách kiểm chứng: `python scripts/run_real_job.py URL --chapters 3 --max-budget-usd 2`. Nếu (1) xảy ra: dự phòng S2 (D-03).
- **Hệ quả:** story-branch được dùng đúng nghĩa của nó ("nhánh từ một tác phẩm có sẵn"): transcript video là tác phẩm gốc, truyện ra là một nhánh độc lập của nó. Lệch với kỳ vọng "viết lại y nguyên"; ghi nhận để người dùng xác nhận.

### D-24 ✅ Story Assembler là bước của stage, độc lập với engine; adapter chỉ trả danh sách section
- `StoryResult` đổi thành `{sections: [Path], stats}`. Stage Story chạy `story/assembler.py` rồi validator bất biến cho **mọi** adapter. Assembler gỡ heading (`Chương N`, `Chapter`, `Section`, `Part`, `Phần`, `第N章`, `#…`, `**…**`, `【…】`), đường kẻ, marker, "Còn tiếp/Hết chương", đoạn "Ở chương trước…"; ghi mỗi đoạn cách nhau một dòng trống (bước TTS tách theo dòng trống); bỏ phần đầu section chép lại đuôi section trước; nối câu bị cắt ở ranh giới section; loại câu lặp liền kề, đoạn trùng khít và gần trùng (Jaccard shingle 8 ký tự ≥ 0.85, cửa sổ 40 đoạn).
- **Không xóa mù quáng:** dòng chỉ bị coi là heading khi không kết thúc bằng dấu câu ("Chương 2 đã kết thúc trong im lặng." là câu văn, được giữ; validator chặn nếu còn). Nếu assembler loại > 35% nội dung thì lỗi `ASSEMBLER_REMOVED_TOO_MUCH` thay vì âm thầm làm mất truyện (lưới này đã tự bắt một lỗi dữ liệu fake lúc phát triển).
- Ghi `assembly_report.json` (artifact `story_report`) liệt kê mọi thứ đã gỡ. Blueprint/continuity/sections nằm ở workspace nội bộ (`story/oh-story/`), không nằm trong gói output (có test).
- **Idempotent:** chỉ dựng lại khi sha256 các section hoặc phiên bản assembler đổi.

### D-25 ✅ Source: phụ đề có sẵn > auto, giữ nguyên raw, dựng câu bằng timestamp (provider/bố cục file: xem D-29…D-34)
> Cập nhật: yt-dlp không còn là downloader duy nhất mà là provider **dự phòng**; tên file và cache đã đổi (D-33, D-34). Các quy tắc chọn track, ba tầng dữ liệu và dựng câu bằng timestamp bên dưới vẫn đúng.
- **Công cụ:** `yt-dlp` gọi qua subprocess (lệnh cấu hình được: `youtube.yt_dlp_cmd`), không phải phụ thuộc Python; `health()` báo thiếu. Chọn track: thủ công (ngôn ngữ ưu tiên `vi,en`) > thủ công (ngôn ngữ gốc video) > auto ngôn ngữ gốc (`*-orig`) > auto ưu tiên > thủ công bất kỳ; không có gì → `NO_SUBTITLES` (POLICY). **Không có fallback ASR/Whisper** (ngoài phạm vi).
- **Ba tầng dữ liệu, đúng yêu cầu:** `subtitle_raw.<ext>` nguyên byte; `transcript_structured.json` (cue với `start,end,text,gap_before` + câu + đoạn, kèm `internal_pauses`, `provenance`); `transcript_clean.txt` sạch không timestamp, sinh **sau** khi dựng lại.
- **Dựng lại câu/đoạn bằng timestamp** (`source/reconstruct.py`), tại mỗi ranh giới cue: nối tiếp (caption bị cắt giữa câu) / kết thúc câu (có dấu câu, hoặc pause ≥ 0.8 s, hoặc câu quá dài) / kết thúc đoạn (pause ≥ 2.0 s hoặc đoạn ≥ 900 ký tự). Câu kết thúc ngay *giữa* một cue cũng được tách; timestamp và `cue_range` nội suy theo vị trí ký tự. Phục hồi dấu câu ở mức an toàn: thêm dấu chấm, viết hoa đầu câu; vị trí các pause giữa câu được giữ để bước khôi phục dấu phẩy bằng LLM dùng sau. Ngưỡng cấu hình ở `youtube.reconstruct`.
- **Khử auto-caption "rolling"** (mỗi cue lặp dòng của cue trước, thẻ `<c>`/timing từng từ, `&nbsp;`, `[Âm nhạc]`, `>>`), nhưng không xóa các dòng lặp hợp lệ ("Không." / "Không.").
- **Idempotent theo từng bước + cache liên job:** `subtitle_raw.meta.json` (sha256), `transcript_structured.json` → `provenance` (sha256 raw + phiên bản parser + hash cấu hình), `clean_sha256`; cache `runtime/cache/youtube/<video_id>/<hash ngôn ngữ>/` để job mới cho cùng URL không tải lại. `refresh_source: true` ép tải lại.
- **Đã kiểm chứng với YouTube thật** (video công khai, yt-dlp 2026.08.19 trong venv tạm, không cài vào môi trường của bạn): phụ đề thủ công được ưu tiên hơn auto; auto-caption thật (103 cue rolling → 47 cue sạch, không còn thẻ); chạy lại 0,01 s không đụng mạng. **Chưa kiểm chứng** với phụ đề tiếng Việt thật.

### D-26 ✅ Quyền của agent: mặc định thận trọng, transcript là dữ liệu không tin cậy
- Transcript YouTube do bên thứ ba viết và được đưa cho một agent có công cụ ghi file/chạy lệnh ⇒ rủi ro prompt injection. Mặc định `permission_mode=acceptEdits` + allowlist lệnh (`python`, `node`, `bash`, `git`, `ls`, …); `--setting-sources project,local` + `--strict-mcp-config` để không nạp CLAUDE.md/plugin/hook của người dùng; biến môi trường `CLAUDE*`/`OMC_*` bị bỏ khỏi tiến trình con (như bench của oh-story). Nội dung transcript **không bao giờ** nằm trong prompt, chỉ nằm trong file `原文.md` (có test), và prompt nói rõ đó là dữ liệu.
- `bypassPermissions` (cách bench của oh-story chạy) là tùy chọn có chủ đích, chỉ trong workspace cách ly của job. Nếu mặc định thận trọng làm agent kẹt vì thiếu quyền, đây là chỗ cần nới (chưa biết trước vì chưa chạy LLM thật).

### D-27 ✅ Artifact mới ở stage Source/Story (đã chỉnh bởi D-31, D-33)
- Source sinh `subtitle_raw`, `transcript_structured`, `transcript`, `metadata`; Story sinh `story_text`, `story_report`. Thay đổi `SourceResult`/`StoryResult`/`SourceBundle` (thêm `source_language`) ghi ở `MODULE_CONTRACTS.md` §10. `metadata.json` **không** chứa `description` của video gốc (tránh chép mô tả của người khác thành mô tả video của ta); stage output/publish dùng 300 ký tự đầu của `story.txt`.

### D-28 ✅ Đánh số phase theo chỉ dẫn của người dùng
- Phase 2 = Source + Story thật (phần "Phase 3" trong lộ trình ban đầu). Các phase sau được đánh số lại liên tục (3 = TTS, 4 = Audio + Render, 5 = Publish + Output, 6 = Bất đồng bộ + vận hành, 7 = TTS Auto-Profile) và chỉ là đề xuất. Các spike ContentFlow/yt_uploader thật chưa làm, xếp trong "Việc chờ" của `IMPLEMENTATION_PHASES.md`. Không bắt đầu Phase 3.

### D-29 ✅ Subtitle_supperVip được tích hợp qua SourceAdapter; ContentFactory vẫn là orchestrator cấp cao
- **Quyết định:** `Subtitle_supperVip` là **implementation chính của `SourceAdapter`** (provider `supervip`), không phải orchestrator. ContentFactory giữ pipeline job, state từng stage, tham chiếu artifact và retry/resume trong DB của nó. Subtitle_supperVip chỉ làm phần *thu thập phụ đề* (resolve nguồn, chọn track, lấy phụ đề, metadata nếu có key).
- **Kiến trúc:** `YouTube URL → SourceAdapter (ProviderChain) → provider (supervip → yt-dlp → …) → SourceResult → Transcript Processor → Story`. `SourceAdapter` không hardcode provider: thêm provider = thêm một lớp `SourceProvider` (hiện có `supervip`, `ytdlp`, `local`, `text`).
- **Bằng chứng (audit, `CURRENT_SYSTEM_AUDIT.md` §6):** module là app quản lý theo **kênh** (FastAPI + SQLite + worker + UI), không có entrypoint cho một video lẻ, có state machine và queue riêng. Dùng nguyên app sẽ tạo hai state machine cho cùng một job.

### D-30 ✅ Cách tích hợp: bridge gọi lại code acquisition của module, không chạy API/worker/DB, không sửa module
- **Bridge** (`source/bridge/supervip_bridge.py`) chạy bằng Python env riêng có `youtube-transcript-api`, gọi `fetch_selected`/`serialize`/exceptions của `app.services.subtitles` và `YouTubeDataClient._video_details` (khi có `YOUTUBE_API_KEY`). Orchestrator vẫn chỉ cần stdlib.
- **Đã loại:** (a) gọi HTTP API của module: chỉ nhận URL kênh, cần Data API key chỉ để resolve kênh, và tạo state thứ hai; (b) copy/fork code vào ContentFactory: lệch phiên bản; (c) import trực tiếp vào tiến trình orchestrator: kéo dependency (`pydantic-settings`, …) và `.env`/DB của module vào.
- **Cô lập:** bridge chạy ở thư mục tạm (không nạp `.env`/`data/`), `PYTHONDONTWRITEBYTECODE=1`; test khẳng định cây thư mục của module không đổi và không có `*.db`. Không import `app.main/worker/models/database/services.jobs`.
- **Reuse:** `fetch_selected` (liệt kê/chọn/lấy phụ đề + phân loại lỗi), `serialize` (raw JSON), `YouTubeDataClient._video_details` (metadata). **Không dùng:** channel/scan/sync, `jobs.py`, `worker.py`, `models.py`/DB/alembic, FastAPI, React, Docker, `dev.ps1`/`start.ps1`, `requests_per_minute` (khai báo nhưng không có tác dụng).
- **Code Phase 2 cũ:** `YouTubeSourceProcessor` (nguyên khối) **đã gỡ** sau khi tích hợp mới được chứng minh (116 test + chạy thật hai provider cho transcript sạch giống hệt nhau, similarity 1.0). Downloader yt-dlp được **giữ** thành `YtDlpProvider` (fallback); parser/dựng câu thành `TranscriptProcessor`; cache thành `ProviderChain`.

### D-31 ✅ Ownership của state; mô tả/tiêu đề của nguồn không dùng làm của ta
- **State:** DB của ContentFactory là nguồn sự thật duy nhất của pipeline. Provider không đọc/ghi DB của ContentFactory; DB của Subtitle_supperVip không được tạo, đọc hay ghi (test `test_module_state_is_never_touched`). Lỗi của provider chỉ trở thành lỗi của **stage `source`** (TRANSIENT retry theo backoff; POLICY/RESOURCE/AUTH → `FAILED` ở stage đó), không bao giờ làm đổi state job khác.
- **Mô tả:** `SourceResult.description` (nếu có) chỉ là dữ liệu tham khảo; output/publish luôn lấy 300 ký tự đầu của `story.txt` làm mô tả (D-27). Tiêu đề hiện vẫn lấy từ nguồn (giới hạn đã biết).

### D-32 ✅ Chính sách chọn provider và phụ đề
- **Thứ tự provider:** theo `source.providers` (mặc định `supervip, ytdlp, local, text`; mỗi provider chỉ nhận loại nguồn nó hỗ trợ). Provider không khả dụng bị bỏ qua và ghi vào `attempts`.
- **Fallback:** mọi lỗi chuyển sang provider kế, **trừ** lỗi dứt khoát về đầu vào/video (`NOT_YOUTUBE_URL`, `BAD_VIDEO_ID`, `VIDEO_UNAVAILABLE`, `FILE_NOT_FOUND`, `UNSUPPORTED_FORMAT`, `EMPTY_SUBTITLE`). Hết provider: ưu tiên báo lỗi TRANSIENT (còn hy vọng retry), nếu không thì lỗi của provider chính.
- **Chọn phụ đề (supervip):** 3 pass bằng chính hàm của module — manual (ngôn ngữ ưu tiên `vi,en`) → auto (ưu tiên + `original`) → manual bất kỳ ngôn ngữ; `allow_translation=false` (không dịch máy của YouTube; Story lo ngôn ngữ đích). Lý do: `choose_transcript` của module với `any` có thể chọn auto-vi trước manual-en, trái yêu cầu "ưu tiên phụ đề có sẵn".
- **Bị chặn IP:** `YOUTUBE_BLOCKED` là RESOURCE (không retry mù, chuyển sang fallback).

### D-33 ✅ Ngữ nghĩa "raw subtitle" và bố cục artifact
- Raw = **đúng như provider trả về**: supervip → snippet JSON `{text,start,duration}` (module không giữ timedtext gốc); yt-dlp → VTT/SRT; local → copy nguyên byte; text → `.txt`. `subtitle_format` ghi rõ; Transcript Processor đọc cả bốn.
- Bố cục `workspace/<job>/source/`: `source.json` (kind `metadata`), `subtitle_raw.<ext>`, `transcript_structured.json`, `transcript_clean.txt` (kind `transcript`); nội bộ: `subtitle_raw.meta.json`, `_acq/`. Output final không chứa file tạm.

### D-34 ✅ Cache và vô hiệu hóa theo từng tầng
- **Phụ đề:** khóa `sha256(kind, định danh nguồn, ngôn ngữ ưu tiên)`; dấu vân tay trong job (`subtitle_raw.meta.json`) + cache chung `runtime/cache/source/<key>/`; sha256 raw sai thì khôi phục từ cache rồi mới tới mạng; khóa theo khóa cache để job song song cùng nguồn chỉ tải một lần (test race, đã mutation-check). Đổi ngôn ngữ ưu tiên ⇒ khóa khác ⇒ tải lại; `refresh_source` ép tải lại.
- **Transcript:** dùng lại structured khi `(raw_sha256, format, parser_version, config_hash)` không đổi; clean theo `clean_sha256`. Đổi raw/định dạng/phiên bản/cấu hình ⇒ dựng lại, không tải lại.
- **Story:** assembly chỉ dựng lại khi sha256 các section đổi; adapter lưu dấu vân tay đầu vào (`sha256(transcript)`, tiêu đề, ngôn ngữ, tên sách, phiên bản adapter) trong `adapter_state.json`: không đổi ⇒ không gọi agent nào; đổi ⇒ cất workspace oh-story cũ sang `oh-story.stale-<fp>` và làm lại từ đầu. Số chương mục tiêu không nằm trong dấu vân tay (tăng số chương chỉ viết tiếp).
- **Giới hạn:** chưa có `rerun --from <stage>`: vô hiệu hóa theo tầng xảy ra khi một stage chạy lại (crash/retry/job mới), không phải khi người dùng bắt chạy lại giữa pipeline.

### D-35 ✅ Môi trường chạy của Subtitle_supperVip
- Cần Python env riêng có `youtube-transcript-api` (README của module: 3.12+; máy audit chỉ có 3.10 và 3.13, chạy được trên 3.13 và cả 3.10 qua test với stub). Cấu hình `supervip.python`, hoặc `backend/.venv` nếu có, nếu không thì dùng Python hiện tại. `health()` chạy bridge `health` (nạp code thật của module) để báo thiếu; `doctor`/`setup` tự động cài env để phase sau.
- Không tự cài gói vào máy người dùng. `YOUTUBE_API_KEY` tùy chọn (chỉ để lấy metadata đầy đủ); thiếu thì title lấy qua yt-dlp (nếu có) hoặc dùng video id.
- Pin: ghi trong `modules.lock` kèm cảnh báo cây làm việc có thay đổi chưa commit và remote không clone được từ máy audit.

## 2. Câu hỏi còn mở

Không còn câu hỏi nào chặn phase đang làm. D-04, D-07 được chốt bằng mặc định suy ra từ code/môi trường; Story theo D-23 (chỉ dẫn Phase 2). Còn lại là **điều kiện đầu vào runtime**, không suy ra được từ code; `doctor` sẽ báo thiếu thay vì chặn:

| Điều kiện | Cần ở | Mặc định nếu chưa có |
|---|---|---|
| Google Cloud OAuth client + một kênh thử (R4) | spike upload thật, Phase 5 | Spike upload bỏ qua; `PublishAdapter` chạy bằng `FakePublish`; `doctor` báo "YouTube chưa cấu hình" |
| `assets/template.png` + font tiếng Việt cho thumbnail (R5) | Phase 4 | Thumbnail bị bỏ qua có cảnh báo, video vẫn render; không tự tạo template |
| Frame 16:9 1920×1080 + layout (R6) | Phase 4 | Orchestrator sinh một frame viền đen tối thiểu để chạy được; thiết kế đẹp là việc sau |
| Engine TTS đầu tiên | Phase 3 | Bắt đầu với engine local đã có trong môi trường (VieNeu-TTS, sẽ audit ở Phase 3) |

## 3. Đề xuất chỉnh HANDOFF (đã áp dụng một phần khi tích hợp Subtitle_supperVip; phần còn lại chưa áp dụng, chờ duyệt)

Đã áp dụng vào `HANDOFF.md`: §2 Story và Source/Subtitle, §2A, §3, §4, §4A, §5, §15 (ghi chú state thực tế), §19 (doctor), §20, §21.

| Mục HANDOFF | Chỉnh đề xuất | Mã |
|---|---|---|
| §2 Story ✅ đã áp dụng | Ghi rõ: `story-branch` = chuẩn bị nhánh từ truyện có sẵn; sinh truyện là `story-long-write`; không headless; tiếng Trung | A1–A4 |
| §3/§4 ✅ đã áp dụng | Source là module **mới** (nay: `SourceAdapter` + Transcript Processor, §4A) | A2 |
| §5 ✅ đã áp dụng | Ghi Story Assembler là module mới; chương hiện là file riêng có dòng tiêu đề | A5 |
| §2 Media/§12 | Ghi: ContentFlow chưa có profile 16:9/9:16; profile do orchestrator cấp qua frame PNG + config | A8 |
| §11 | Speed ×2/split/watermark thuộc AudioProcessor của orchestrator | A11–A12 |
| §13 Source Sync | Ghi: hiện là hàm/tab chạy thủ công; nền "background" là việc của orchestrator | A10 |
| §15/§16 | Ghi: `UPLOADING/PUBLISHED` chỉ áp dụng YouTube; TikTok = xuất file | A15 |
| §18 | Ghi: nền video ngẫu nhiên không tái tạo y hệt | A20 |
| §21 | Bổ sung: ngôn ngữ đích `vi` (D-04); Story = StoryBranchAdapter điều khiển oh-story qua Claude CLI (D-23); tên output (D-07); đồng thời render (D-15) | |

## 4. Giới hạn đã biết sau Phase 1

- Dừng có chủ đích dựa vào handler **hợp tác** (kiểm `ctx.cancel`); handler không hợp tác sẽ chặn shutdown cho tới khi xong hoặc bị kill (rồi quay về cơ chế lease).
- Chưa có: `cancel` job, `rerun --from <stage>` (cần cho "đổi watermark chỉ build lại nhánh YouTube", Phase 4/7), cache-hit liên job theo `stage_key` (đã tính và lưu, chưa dùng), CLI ưu tiên job.
- Chưa có `doctor.ps1`, `setup.ps1`, `update.ps1`, `start.ps1` (HANDOFF §19) — chuyển sang phase sau (doctor/setup bản đầu) và Phase 6. Phase 2 chỉ thêm `health()` cho adapter Source/Story và `scripts/run_real_job.py --dry-run`.
- Thay gói output của chính job khi retry là `rmtree` rồi `rename` (cửa sổ ngắn không có gói); gói vẫn không bao giờ ở trạng thái nửa vời.
- Chỉ kiểm thử trên Windows (kill bằng TerminateProcess). Nhiều orchestrator trên cùng DB được kiểm bằng 2 luồng trong một tiến trình và bằng kill/resume, chưa kiểm bằng 2 tiến trình chạy đồng thời.
- Fake adapter không đổi tốc độ audio thật (chỉ chia part theo `target×speed`), video/thumbnail chỉ là byte giả.

## 5. Giới hạn đã biết sau Phase 2

- **Story thật chưa chạy với LLM thật** (D-23). Ba rủi ro lớn nhất: tiếng Việt vs bộ kiểm tiếng Trung của oh-story; cổng xác nhận; chi phí (mỗi lượt oh-story nạp tới ~35K ký tự tài liệu; ~14 chương mặc định ≈ 15–25 lượt). `target_chars=40000`/`chapter_chars=3000` là **ước lượng** cho 40–60 phút audio, cần đo ở phase TTS.
- Tiêu đề video YouTube vẫn lấy từ tiêu đề video nguồn (`metadata.title`); chưa có bước sinh tiêu đề/mô tả riêng (không nên đăng trùng tiêu đề của người khác).
- Phụ đề không dấu câu và liền mạch (không có pause) cho câu dài vì không có tín hiệu để ngắt; `internal_pauses` đã được giữ cho bước khôi phục dấu câu bằng LLM sau này.
- Không có phụ đề: lỗi `NO_SUBTITLES` (không ASR). Video giới hạn tuổi/thành viên/yêu cầu đăng nhập: `YOUTUBE_SIGNIN_REQUIRED` (AUTH); cookie chỉ truyền được qua `youtube.yt_dlp_args`.
- Assembler dùng heuristic: heading theo mẫu lạ có thể lọt (validator chỉ chặn mẫu quen thuộc); đoạn "gần trùng" có thể là lặp có chủ ý (điệp khúc) — report ghi lại, ngưỡng 35% chặn việc mất nội dung hàng loạt.
- `docs/CURRENT_SYSTEM_AUDIT.md` là ảnh chụp lúc Phase 0, không cập nhật theo các thay đổi này.

## 6. Giới hạn đã biết sau tích hợp Subtitle_supperVip

- Raw của provider chính là snippet đã parse, không phải timedtext gốc: không phục hồi được định dạng/định vị gốc của YouTube.
- Chưa kiểm chứng với phụ đề **tiếng Việt thật** và với video thực sự chỉ có auto-caption qua provider supervip (đã kiểm chứng manual + auto trên video công khai tiếng Anh; stub kiểm chứng các nhánh còn lại).
- `youtube-transcript-api` dựa vào endpoint không công khai: có thể bị chặn IP hoặc đổi hành vi; fallback yt-dlp cũng có thể bị chặn cùng IP.
- Phân loại lỗi trong module dựa vào tên lớp/chuỗi message của thư viện; bridge xếp lỗi lạ vào TRANSIENT.
- Metadata (mô tả, kênh) đầy đủ phụ thuộc yt-dlp (không có trong supervip); thiếu cả hai thì title = video id.
- Chưa có ASR khi video không có phụ đề nào, chưa có `doctor`/`setup` tự dựng env cho supervip.
