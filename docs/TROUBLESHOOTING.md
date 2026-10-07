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
| `MISSING_INPUT` ở render YouTube | thư mục video nguồn trống; (bố cục kiểu cũ) thiếu template thumbnail/font | thêm video vào thư mục nguồn; dùng template builtin (không cần file ngoài) hoặc `cf templates migrate` |
| `INVALID_CHANNEL_TEMPLATE` (lúc tạo job) | template kênh chọn không dùng được: không tồn tại (có thể đã xoá), sai loại (thumbnail ↔ video), version ghim không có | thông điệp nêu kênh/template/mã lỗi: chọn template khác (Kênh → Template, hoặc `cf templates use`); muốn tự động dự phòng thì khai `fallback` |
| `MISSING_INPUT` ở render với tên template/asset trong thông điệp (`ASSET_FILE_MISSING`, `ASSET_CHANGED`) | file asset của template mất/bị thay sau khi job chốt snapshot | khôi phục file asset; hoặc import asset ID mới, tạo version mới của template, rồi `cf retemplate <job> <kind> <id>` |
| `TEMPLATES_UNAVAILABLE` / quyết định `templates = unavailable` | module ContentFlow cũ chưa có `templating` | `cf update` (cập nhật module ContentFlow); trong lúc đó job chạy bố cục kiểu cũ |
| `ASSET_IN_USE` | xóa asset đang được template dùng | gỡ khỏi template trước (hoặc `force` nếu chắc chắn) |
| Template validate lỗi (`TEXT_BELOW_IMAGE`, `UNSUPPORTED_ELEMENT`, `OUTSIDE_CANVAS`…) | xem bảng mã ở `docs/TEMPLATE_SCHEMA.md` | thông điệp nêu template, version, phần tử/trường; sửa trong Studio rồi *Kiểm tra* |
| `UPLOADER_UNREACHABLE` | daemon `yt-uploader` chưa chạy | `cf start` (tự bật) hoặc `tools\yt-uploader.exe serve --headless` |
| `UPLOADER_UNAUTHORIZED` | token API của daemon sai | kiểm tra `tools.yt_uploader.token` / `data_dir`; xoá để dùng `api_token` của daemon |
| hết quota upload | YouTube giới hạn mỗi ngày | tự tiếp tục sau khi quota reset (cần `cf start`) |
| `INVALID_JOBSPEC: thiếu artifact đầu vào` | chọn mode/start không có dữ liệu đi kèm | thêm `--artifact kind=đường_dẫn` hoặc `--from-job` |
| `IMPORT_INVALID` | dùng lại artifact đã bị Auto Cleanup dọn (job > 14 ngày) | chạy lại từ stage đầu |
| `UNEXPECTED` | lỗi chưa lường trước trong stage | gửi `job.log.jsonl` (có stack trace ở sự kiện `stage_exception`) |

## Giao diện (`cf ui`)

| Hiện tượng | Nguyên nhân | Cách xử lý |
|---|---|---|
| Trình duyệt không mở / trang trắng | cổng 8765 bận hoặc chặn script | xem dòng "ContentFactory đang chạy: http://…" (tự đổi cổng khi bận); thử `cf ui --port 8800`; tải lại trang (Ctrl+F5) |
| "Thiếu hoặc sai token phiên" | trang mở từ phiên cũ của ứng dụng | tải lại trang |
| Banner "Mất kết nối tới ContentFactory" | ứng dụng (cửa sổ dòng lệnh) đã đóng | mở lại `cf ui`; job dở dang tự chạy tiếp |
| Chip "Chưa chạy nền" | mở bằng `cf ui --no-runner` | mở lại không có `--no-runner` để job được xử lý |
| Job nằm "Đang xếp hàng" mãi | không có tiến trình xử lý | đảm bảo `cf ui` (hoặc `cf start`) đang chạy; xem Doctor |
| "Chọn file…" báo máy không có hộp thoại | Python không có tkinter | dán đường dẫn đầy đủ vào ô Đầu vào |
| Nút RUN không bật | còn mục báo thiếu ở trên nút (tên truyện, khai báo trẻ em, kênh lỗi…) | làm theo dòng hướng dẫn màu cam |
| Log của giao diện | lỗi nội bộ của máy chủ UI | `runtime/logs/ui.log` |

## Tình huống

**Job đứng yên, không chạy:** có thể đang bị giữ (`cf status`); hoặc chưa có tiến trình runner (`cf go` tự chạy; `cf submit` cần `cf run` hoặc `cf start`).

**Tắt máy/đóng cửa sổ giữa chừng:** an toàn. Chạy lại `cf start`/`cf run`: job tiếp tục từ checkpoint (sau khi lease hết hạn, vài giây); TTS dùng lại các chunk đã xong, render dùng lại video/part đã hợp lệ.

**Một part TikTok lỗi:** `cf retry <job>` chạy lại đúng part đó; muốn dựng lại một part đã xong: `cf retry-part <job> <số>`.

**Upload lỗi nhưng đã có video:** gói output vẫn ở `output\`; `cf retry <job>` chỉ chạy lại upload (cùng khoá idempotency ⇒ không tạo video thứ hai).

**Hết chỗ đĩa:** job tự giữ (`PAUSED_DISK`). `cf cleanup`, giải phóng ổ, `cf resume <job>`.

**Muốn chạy lại hoàn toàn:** tạo job mới (`cf go` lại); cache nguồn/TTS giúp phần đã có không tốn lại.

## Bí mật

Token/key/OAuth không bao giờ được ghi vào output, log, DB hay workspace (có test kiểm tra). Đặt chúng ở `config\secrets.local.env`, `config\config.local.json` hoặc biến môi trường — **không** đặt trong `config\config.json` (được commit; `doctor` cảnh báo nếu có).
