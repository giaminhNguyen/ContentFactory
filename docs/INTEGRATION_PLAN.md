# INTEGRATION_PLAN

> Cách nối 3 project cũ vào ContentFactory mà **không sửa source cũ**. Đối chiếu: `HANDOFF.md`, `CURRENT_SYSTEM_AUDIT.md` (mã A1…A21), `MODULE_CONTRACTS.md`.

## 1. Nguyên tắc

1. `modules/*` là repo độc lập, **chỉ đọc** với ContentFactory. Mọi thay đổi để nối nằm trong orchestrator (adapter, shim, config, asset).
2. Giao tiếp bằng **subprocess/HTTP + file artifact + manifest**, không `import` code module cũ vào tiến trình orchestrator.
3. Phiên bản module pin theo SHA (`modules.lock`).
4. Nếu bắt buộc sửa module cũ ⇒ ghi vào §4 và `DECISIONS.md`, làm thành patch nhỏ có test, đẩy về repo gốc (không giữ bản fork ngầm trong ContentFactory).

## 2. Sơ đồ nối

```text
                        ContentFactory (orchestrator, SQLite + workspace/)
  ┌───────────────────────────────────────────────────────────────────────────────┐
  │ SourceProcessor(mới) → StoryAdapter ──→ TTS Manager + TTSAdapter(mới)         │
  │                                          → AudioProcessor(mới, ffmpeg)        │
  │                    ┌─────────────────────────────┴────────────────────────┐   │
  │               YouTube audio (+watermark)                    TikTok audio ×2, split│
  │                    │                                              │       │   │
  │               RenderAdapter ───────────────────────────────── RenderAdapter  │
  │                    │                                              │       │   │
  │              OutputPublisher ──────────────────────────────────────┘       │   │
  │                    │                                                       │   │
  │              PublishAdapter(YouTube)                                       │   │
  └─────────┬──────────────┬───────────────────┬─────────────────────────────────┘
            │              │                   │
   claude -p / LLM     python -m media_worker   HTTP 127.0.0.1:8973
   (oh-story,      + source_sync shim       yt-uploader serve --headless
    story-branch)         (ContentFlow)            (yt_uploader)
```

## 3. Kế hoạch tích hợp theo module

### 3.1 ContentFlow → RenderAdapter (khả thi, rủi ro thấp–trung bình)

| Việc | Cách | Sửa ContentFlow? |
|---|---|---|
| Render video | `python -m media_worker run --request req.json`, parse JSON-lines, exit code 0/3/4/2 | Không |
| Thumbnail | worker `type=thumbnail` | Không (cần bổ sung asset ngoài repo, R5) |
| Source Sync | shim trong orchestrator gọi `source_sync.sync_videos(SyncOptions)` bằng Python của ContentFlow; sau đó đọc `_synced/` và `source_profile.json` | Không |
| Profile YouTube/TikTok | frame PNG + `config_overrides` (`video_generator.viewport`, `frame_layouts`) truyền qua `params.config`; lưu ở `config/render_profiles/` của orchestrator | Không |
| Speed ×2, split part, watermark | AudioProcessor (ffmpeg) trước khi gọi worker | Không |
| Reconcile sau crash | `media_worker status --output-dir D --key K` + `idempotency_key` | Không |
| Health | `media_worker health` | Không |

Việc phải làm nhưng nằm ngoài code ContentFlow:
- Tạo **frame 16:9 1920×1080** + layout cho profile YouTube và kiểm thử nó (hiện chỉ frame 9:16 được dùng; A8, R6).
- Cung cấp `assets/template.png` (+ font) cho thumbnail (R5). Đặt trong thư mục asset của orchestrator, truyền qua config/`inputs`, hoặc cài vào `modules/ContentFlow/assets/` bằng `setup.ps1` (file đã bị gitignore ở repo gốc nên không làm bẩn repo).
- Mỗi lần gọi dùng `output_dir` riêng (worker ghi `.worker_state.json`, `.worker_state.json.lock`, `.work/` vào đó) ⇒ không dùng chung một thư mục cho nhiều job song song (R13).

### 3.2 yt_uploader → PublishAdapter (khả thi, rủi ro trung bình vì chưa test thật)

- Chạy `yt-uploader serve --headless` như dịch vụ nền do `start.ps1` quản (build bằng `go build ./cmd/yt-uploader`, không CGO).
- Adapter = HTTP client loopback; token đọc từ `<datadir>/api_token`; dùng `idempotency_key = stage_key`.
- **Thiết lập một lần, người làm:** tạo OAuth Desktop client trên Google Cloud, `yt-uploader login --print-url` cho từng account. Ghi vào `setup.ps1`/`doctor.ps1` như bước hướng dẫn, không tự động hóa được.
- Map `channel` trong `channels/<name>/channel.yaml` → `account_id` của yt_uploader (không có chọn kênh thật, A14).
- Retry job `failed` do orchestrator quyết định theo `error_class` (R4).

### 3.3 oh-story-claudecode → StoryAdapter (đã làm ở Phase 2: `StoryBranchAdapter`, chưa kiểm chứng với LLM thật)

Thực tế (A1–A7): oh-story không phải module tự động; story-branch chỉ chuẩn bị tư liệu. Theo chỉ dẫn Phase 2, adapter **không sửa oh-story** mà điều khiển nó từ ngoài:

| Việc | Cách |
|---|---|
| Triển khai skill | `scripts/bench/deploy.py` của chính oh-story → workspace riêng của job (`story/oh-story/`) |
| Nguồn | transcript sạch → `拆文库/<nguồn>/原文.md` (tác phẩm gốc cho story-branch) |
| Điều khiển | `claude -p` stream-json (`ClaudeCliRunner`): `story-branch analyze → explore → create → handoff → story-long-write 开书 → 写第a-b章`; trả lời cổng xác nhận bằng câu cố định; poll `追踪/_tracking-state.json` |
| Đầu ra | các file `正文/第NNN章*.md` làm section; **Story Assembler** (ở stage, không phải ở oh-story) dựng `story.txt` liền mạch |
| Resume | điều kiện xong của từng bước kiểm bằng file trên đĩa; chạy lại bỏ qua bước đã xong |
| An toàn | `acceptEdits` + allowlist, không nạp cấu hình người dùng, transcript chỉ nằm trong file (D-26) |

Phương án dự phòng chưa xây: S2 `DirectLLMStoryAdapter` (tự gọi LLM theo section) nếu kiểm chứng thật cho thấy oh-story không dùng được cho tiếng Việt (D-23).

### 3.4 Các phần mới (không có trong 3 project)

SourceProcessor, TTS (adapter/profile/segment planner/manager/QA), AudioProcessor, orchestrator (SQLite state, workspace, manifest), OutputPublisher, scripts `setup/update/doctor/start`. Đây là khối lượng công việc chính (xem `IMPLEMENTATION_PHASES.md`).

## 4. Adapter thay vì sửa source cũ

| # | Nhu cầu | Giải pháp bằng adapter/shim | Có thể cần patch upstream sau này (tùy chọn) |
|---|---|---|---|
| 1 | Source Sync từ orchestrator | Shim gọi `sync_videos` | Thêm job type `sync` vào `media_worker` (đồng nhất giao thức); sửa ghi `.part` + atomic profile |
| 2 | Profile YouTube/TikTok | Frame + `config_overrides` ở orchestrator | Không |
| 3 | Tốc độ ×2, cắt part, watermark | AudioProcessor | Không |
| 4 | Gộp chương thành `story.txt` | Story Assembler ở stage (D-24) | Không |
| 5 | Điều khiển story không tương tác | `ClaudeCliRunner` + câu trả lời cố định cho cổng xác nhận (D-23) | Chế độ headless chính thức ở upstream sẽ ổn định hơn; ngoài phạm vi |
| 6 | Seed nền ngẫu nhiên | **Không có đường adapter** (rng không lộ ra qua API). Chấp nhận không tái tạo y hệt (D-08) | Đưa `rng`/`seed` từ `video_utils.choose_next_clip` lên `render_video`/worker `params` |
| 7 | Thumbnail ≤ 2 MiB | Adapter nén lại JPG | Không |
| 8 | Chọn kênh YouTube | Một account/kênh | yt_uploader: dùng `channel_id` thật (không bắt buộc) |
| 9 | Sửa tài liệu lệch code | Ghi trong audit | `yt_uploader/IMPLEMENTATION_STATUS.md` (lỗi thời), `ContentFlow/README.md` (nhắc `thumbnail_tool`), `oh-story` docs (ghi 13 skill) |

Nguyên tắc: các patch upstream ở cột 4 là **tùy chọn**; kế hoạch chạy được mà không cần chúng.

## 5. Rủi ro chính

Xếp theo mức nghiêm trọng đối với mục tiêu "1 video YouTube + N video TikTok, tự động".

| ID | Rủi ro | Mức | Nguồn | Giảm thiểu |
|---|---|---|---|---|
| R1 | **Story module không như HANDOFF mô tả**: story-branch không sinh truyện; oh-story không headless, không nhận transcript. Đã giảm bằng adapter + Assembler (Phase 2) nhưng **chưa chạy với LLM thật** | **Cao** | A1–A3 | `scripts/run_real_job.py --chapters 3 --max-budget-usd 2` để kiểm chứng; dự phòng S2 |
| R2 | **Ngôn ngữ**: oh-story viết tiếng Trung, bộ kiểm "AI-flavor"/đếm chữ CJK có thể chặn commit chương tiếng Việt; đích là tiếng Việt (D-04). **Chưa biết** cho tới khi chạy thật | **Cao** | A4 | Kiểm chứng ở lần chạy thật đầu tiên; nếu chặn: S2 hoặc nới bằng cấu hình; validator ngôn ngữ ở phase sau |
| R3 | Điều khiển oh-story qua `claude -p`: mong manh (cổng xác nhận, quyền công cụ), tốn chi phí (~35K ký tự tài liệu mỗi lượt; 15–25 lượt cho ~14 chương), transcript là dữ liệu không tin cậy | Trung–Cao | A3 | `max_turns`, `max_follow_ups`, `max_budget_usd_per_turn`; `STORY_STEP_INCOMPLETE` thay vì lặp vô hạn; quyền thận trọng (D-26); resume theo file |
| R4 | **yt_uploader chưa từng chạy với Google thật**; OAuth client thủ công; quota ~6 upload/ngày (ngoài repo, chưa xác nhận); project chưa audit có thể ép private; job `failed` không tự retry | Trung–Cao | A14 | Test thật sớm (spike upload, Phase 5) với kênh thử; policy retry ở orchestrator; ghi quota vào `doctor` |
| R5 | **Thumbnail không chạy được** ở checkout hiện tại (thiếu `template.png`/font; font fallback Windows); output 1648×928 có thể >2 MiB giới hạn uploader | Trung | A13 | Cung cấp asset; kiểm kích thước và nén; kiểm font tiếng Việt |
| R6 | **Chưa có profile 16:9**; ContentFlow chỉ được dùng với frame 9:16 | Trung | A8 | Tạo frame 16:9 + test thật ở Phase 4 |
| R7 | Timeout cứng 3600 s mỗi lệnh ffmpeg; render 40–60 phút trên CPU có thể vượt | Trung | §3.4 audit | Ưu tiên NVENC, doctor kiểm; chia nhỏ nếu cần; ghi lại nếu gặp |
| R8 | Source Sync ghi không atomic, skip theo "tồn tại" ⇒ file cụt bị coi là hợp lệ; không phát hiện nguồn đổi bằng hash | Trung | A10 | Shim: kiểm tra bằng ffprobe sau sync, xóa file lỗi; so sánh `source_profile.json`; fingerprint pool ở orchestrator |
| R9 | Nền video ngẫu nhiên không seed ⇒ không reproducible | Thấp | A9, A20 | Ghi `nondeterministic` vào manifest; coi video là artifact |
| R10 | Remote `git@home.com:…` có thể không truy cập được trên máy mới; repo gốc ContentFactory không chứa code module | Trung | A19 | `modules.lock` + script `setup.ps1` clone theo SHA; cân nhắc mirror/submodule (D-01) |
| R11 | Windows-first: DPAPI token, font `C:\Windows\Fonts`, `build.bat`, đường dẫn ổ đĩa | Thấp–Trung | §3.6, §4.3 audit | Chỉ hỗ trợ Windows ở giai đoạn đầu; ghi vào doctor |
| R12 | Tài nguyên: audio dài + nhiều render 1080p + N video TikTok trên một GPU/đĩa; chạy song song (HANDOFF §14) dễ tranh GPU/đĩa | Trung | | Hàng đợi có giới hạn đồng thời theo tài nguyên; kiểm chỗ trống đĩa trước mỗi stage |
| R13 | `media_worker` ghi `.worker_state.json` + lock + `.work/` vào `output_dir`; dùng chung giữa job ⇒ đụng | Thấp | §3.4 audit | Một `output_dir` riêng mỗi lời gọi |
| R14 | Test ContentFlow chưa chạy; không có bằng chứng toàn bộ pass trên máy này | Thấp–Trung | §3.7 audit | Chạy test trong spike ContentFlow sau khi cài Pillow/pytest |
| R15 | Tài liệu trong module lỗi thời (README, IMPLEMENTATION_STATUS) gây hiểu nhầm | Thấp | | Tin code, không tin doc; audit này là nguồn tham chiếu |
| R16 | Hai runtime (Python + Go daemon) cần quản vòng đời tiến trình | Thấp | | `start.ps1` + `doctor` kiểm `/health` |

## 6. Kiểm tra nhất quán với HANDOFF

- Mọi nguyên tắc §20 của HANDOFF được giữ: (1) nhiều module độc lập ✔ (2) artifact+manifest+state ✔ (3) section nội bộ nhưng output liền mạch ✔ qua bất biến StoryAdapter (4–6) TTS adapter/profile/validator ✔ (7) master audio một lần ✔ (8) watermark là channel asset ✔ (9–10) TikTok/YouTube profile riêng ✔ nhưng **phải tự xây profile**, ContentFlow chưa có (11) Source Sync tái dùng ✔ nhưng là **shim chạy thủ công/đặt lịch**, không phải background tự nhiên (12–13) async + retry độc lập ✔ (14–15) workspace/output tách ✔ (16–17) TTS auto-profile ✔ (vẫn ở "chưa chốt").
- Chênh lệch giữa HANDOFF và code **không** được sửa bằng cách sửa HANDOFF; chúng nằm ở `CURRENT_SYSTEM_AUDIT.md` §5 và được đề xuất chỉnh ở `DECISIONS.md` §3.
