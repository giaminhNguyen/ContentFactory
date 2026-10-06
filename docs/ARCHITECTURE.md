# Kiến trúc ContentFactory (bản tóm tắt)

Thiết kế đầy đủ: `HANDOFF.md`. Quyết định: `docs/DECISIONS.md` (D-01…). Hợp đồng module: `docs/MODULE_CONTRACTS.md`. Tài liệu này chỉ để định hướng nhanh.

## Pipeline

```text
source → story → tts → audio → render_youtube → render_tiktok → output → publish
```

Mỗi stage đọc artifact của stage trước (kiểm tra bằng validator theo `kind`), ghi artifact mới (kèm sha256) và checkpoint. Một job có `start_stage` và `target_stage` (mode có tên: FULL, SUBTITLE_ONLY, STORY_ONLY, THROUGH_TTS, TTS_ONLY, VIDEO_ONLY) hoặc một pipeline spec (`requested_stages`, phụ thuộc tự suy ra — D-98: vd chỉ TikTok, hoặc YouTube + đăng không cần TikTok); stage có output hợp lệ thì bị bỏ qua, không chạy lại. Ảnh thumbnail của job đến từ Image Pool (D-105: chốt một ảnh vào workspace job lúc tạo) và preflight chỉ kiểm thứ kế hoạch cần (D-106). `stage_key` băm đúng các tham số/cấu hình mà stage khai báo (`params_deps`/`config_deps`) nên đổi tiêu đề chỉ chạy lại render/output, không chạy lại TTS.

| Stage | Việc | Adapter (tên trong config) |
|---|---|---|
| source | phụ đề → transcript có cấu trúc | `provider_chain` (Subtitle_supperVip → yt-dlp → local) |
| story | transcript → `story.txt` liền mạch | `story_branch` (Claude Code CLI + oh-story) |
| tts | story → chunk → Master audio | `TTSAdapter` (+ rule/profile, planner, cache) |
| audio | chuẩn hoá, watermark YouTube, x2 + cắt part TikTok | `ffmpeg` |
| render_youtube / render_tiktok | thumbnail, video 16:9, từng part 9:16 | `contentflow` (worker JSON-lines + Source Sync dùng chung) |
| output | gói thư mục cho người dùng | `builtin` |
| publish | upload YouTube | `yt_uploader` (daemon HTTP) |

Mọi adapter có bản `fake` để test/demo; adapter ngoài nạp bằng `module:Class` + `adapter_config`.

## Trạng thái, lỗi, tiếp tục

- **JobStore** (SQLite, migration v0→v2 có sao lưu): `jobs`, `stage_runs`, `artifacts`, `transitions`, `resource_status`, `channel_sequences`. Chuyển trạng thái bằng CAS; runner nhận job bằng **lease** + heartbeat nên crash/kill được nhận lại tự động.
- **Lỗi có lớp** (`ErrorClass`): TRANSIENT (retry có backoff/jitter) · RESOURCE (mạng/đĩa/quota/công cụ ⇒ **giữ job**, không đốt retry) · AUTH (cần credential) · POLICY (lỗi vĩnh viễn ⇒ FAILED) · CANCELLED · AMBIGUOUS_PUBLISH.
- **Hold** là cột trên job (`PAUSED_NETWORK/TOKEN/QUOTA/DISK/RESOURCE/CREDENTIAL/MISSING_INPUT`), không đổi vị trí pipeline. **Resource Monitor** đo lại tài nguyên; Auto Resume chạy tiếp từ checkpoint, có giới hạn "không tiến triển" để không lặp vô tận; Auto Resume tắt thì chỉ chờ người dùng Resume.
- **Snapshot cấu hình theo job** (D-41): đổi config toàn cục không làm đổi job đang chạy.
- **Chẩn đoán** (`orchestrator/diagnose.py`): CLI và UI dùng chung `explain()` / `ui_status()`.

## Phân tách

- **Module cô lập** (AST test): `source, story, tts, audio, render, publish, output, adapters, jobs` chỉ import `contracts`/`fsutil`/chính nó; chỉ `orchestrator` nối chúng. Thay một module không ảnh hưởng module khác.
- **workspace = của hệ thống** (`workspace/job_<id>/…`, cache, DB). **output = của người dùng**: chỉ copy có kiểm sha256, không ghi đè, phiên bản mới `-vN` bên cạnh; pipeline không đọc lại.
- Công cụ ngoài qua subprocess/HTTP: ffmpeg/ffprobe, ContentFlow `media_worker`, daemon `yt-uploader`, Claude CLI, yt-dlp.

## Đồng thời

Pool thread theo job; mỗi stage có **lane tài nguyên** (`limits`: mặc định 2, `gpu`=1 cho render). Story/TTS/Audio của job sau chạy tiếp khi renderer đang bận; upload chậm không chặn render của job khác (đều có test). Source Sync của ContentFlow chạy nền, dùng chung giữa các job, khoá theo pool.

## Auto Mode và vận hành (Phase 7)

`cf go` = submit + chạy + chờ. Preset kênh (`channel.json → preset`) áp vào params lúc tạo job (ưu tiên: người dùng > preset > mặc định); mọi lựa chọn tự động ghi vào `params.auto`. Auto TTS profile/pool, Auto Naming, Auto Cleanup, `doctor`, `setup/update/start`: xem D-82…D-87.

## Template (Phase 10)

Bố cục thumbnail/video là **template có phiên bản** do ContentFlow sở hữu; Channel Config chọn ID, job chốt version + snapshot (`params.templates`), RenderAdapter gửi tham chiếu (không tọa độ), ContentFlow compile template → renderer sẵn có. Xem `docs/TEMPLATE_SYSTEM.md`, D-92…D-97.

```text
Channel Config (template ID) → job: resolve version + snapshot → RenderAdapter → ContentFlow (Template Registry → Template Engine → Asset Registry → renderer)
```

## Cấu hình

`config/config.json` (commit) → `config/config.local.json` (máy này, không commit) → `config/secrets.local.env` (biến môi trường, không commit). Phần ngữ nghĩa vào snapshot của job; phần của máy (đường dẫn công cụ, token) thì không.

## Giao diện

`cf ui` chạy máy chủ cục bộ (stdlib, chỉ `127.0.0.1`, token phiên) cùng vòng lặp orchestrator; giao diện web tĩnh (ES modules, không bước build, GSAP đóng gói cục bộ) nói chuyện qua facade `service.py`/`service_admin.py` (+ `diagnose.py` dùng chung với CLI). Xem `docs/UI_GUIDE.md`, `docs/DESIGN_SYSTEM.md`, D-89…D-91.
