# Xử lý sự cố

Bắt đầu bằng: `cf doctor` (mỗi mục lỗi kèm cách sửa) rồi `cf status <job>` (nguyên nhân, stage, provider, số lần thử, checkpoint, cách đi tiếp). Log chi tiết của job: `workspace\job_<id>\job.log.jsonl` (mỗi dòng một sự kiện JSON); log chung: `runtime\logs\orchestrator.jsonl`. Thêm `-v` để in log ra màn hình.

## Mã lỗi hay gặp

| Mã / hiện tượng | Nguyên nhân | Cách xử lý |
|---|---|---|
| `NO_SUBTITLES` | video không có phụ đề (không có ASR) | dùng video khác hoặc `--input <file phụ đề>` |
| `YOUTUBE_SIGNIN_REQUIRED` | video giới hạn tuổi/thành viên | không tải được; dùng cookie qua yt-dlp (nâng cao) hoặc video khác |
| `MISSING_MADE_FOR_KIDS` | kênh chưa khai báo COPPA | `cf go ... --not-kids` (hoặc `--kids`), hoặc `channel-init <id> --force --kids yes/no` |
| `TTS_PROFILE_NOT_FOUND` | preset kênh trỏ tới profile không có trong `tts_profiles\` | sửa `preset.tts_profile` hoặc xoá để tự chọn |
| `AUDIO_QA_FAILED` (EMPTY, EXCESSIVE_SILENCE, CLIPPING…) | audio lỗi/quá nhiều im lặng | xem `audio_report.json` trong job; chỉnh `audio.qa` của kênh (preset) nếu giọng đọc chủ ý có nhiều quãng nghỉ |
| `CONTENTFLOW_MISSING` (đang giữ, `PAUSED_RESOURCE`) | không có thư mục ContentFlow hoặc Python của nó | `cf setup` (clone + cài), kiểm tra `tools.contentflow.root/python` |
| `MISSING_INPUT` ở render YouTube | thiếu template thumbnail/font hoặc thư mục video nguồn trống | đặt `assets/template.png` vào `config\contentflow\` (doctor chỉ rõ), thêm video vào thư mục nguồn |
| `UPLOADER_UNREACHABLE` | daemon `yt-uploader` chưa chạy | `cf start` (tự bật) hoặc `tools\yt-uploader.exe serve --headless` |
| `UPLOADER_UNAUTHORIZED` | token API của daemon sai | kiểm tra `tools.yt_uploader.token` / `data_dir`; xoá để dùng `api_token` của daemon |
| hết quota upload | YouTube giới hạn mỗi ngày | tự tiếp tục sau khi quota reset (cần `cf start`) |
| `INVALID_JOBSPEC: thiếu artifact đầu vào` | chọn mode/start không có dữ liệu đi kèm | thêm `--artifact kind=đường_dẫn` hoặc `--from-job` |
| `IMPORT_INVALID` | dùng lại artifact đã bị Auto Cleanup dọn (job > 14 ngày) | chạy lại từ stage đầu |
| `UNEXPECTED` | lỗi chưa lường trước trong stage | gửi `job.log.jsonl` (có stack trace ở sự kiện `stage_exception`) |

## Tình huống

**Job đứng yên, không chạy:** có thể đang bị giữ (`cf status`); hoặc chưa có tiến trình runner (`cf go` tự chạy; `cf submit` cần `cf run` hoặc `cf start`).

**Tắt máy/đóng cửa sổ giữa chừng:** an toàn. Chạy lại `cf start`/`cf run`: job tiếp tục từ checkpoint (sau khi lease hết hạn, vài giây); TTS dùng lại các chunk đã xong, render dùng lại video/part đã hợp lệ.

**Một part TikTok lỗi:** `cf retry <job>` chạy lại đúng part đó; muốn dựng lại một part đã xong: `cf retry-part <job> <số>`.

**Upload lỗi nhưng đã có video:** gói output vẫn ở `output\`; `cf retry <job>` chỉ chạy lại upload (cùng khoá idempotency ⇒ không tạo video thứ hai).

**Hết chỗ đĩa:** job tự giữ (`PAUSED_DISK`). `cf cleanup`, giải phóng ổ, `cf resume <job>`.

**Muốn chạy lại hoàn toàn:** tạo job mới (`cf go` lại); cache nguồn/TTS giúp phần đã có không tốn lại.

## Bí mật

Token/key/OAuth không bao giờ được ghi vào output, log, DB hay workspace (có test kiểm tra). Đặt chúng ở `config\secrets.local.env`, `config\config.local.json` hoặc biến môi trường — **không** đặt trong `config\config.json` (được commit; `doctor` cảnh báo nếu có).
