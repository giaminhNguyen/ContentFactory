# ContentFactory

Từ **một link YouTube** ra **1 video YouTube (16:9) + N video TikTok (9:16, giọng x2)** và một thư mục sẵn sàng đăng.

```
link YouTube → phụ đề → truyện (Story) → giọng đọc (TTS) → audio chuẩn hoá → render YouTube + TikTok → thư mục output → (đăng YouTube)
```

## Chạy hằng ngày

```text
Mở ContentFactory  →  dán link YouTube  →  chọn kênh  →  RUN  →  Mở thư mục output
```

Mở bằng cách bấm đúp **`ContentFactory.cmd`** (hoặc `.\cf.cmd ui`): trình duyệt mở giao diện, ứng dụng xử lý job ngay trong nền. Hoặc không cần giao diện, một lệnh:

```powershell
.\cf.cmd go "https://www.youtube.com/watch?v=..." --channel kenh_a --open
```

Chưa có truyện/video để thử? Trong giao diện: **Chạy → "Chưa có truyện hoặc video để thử?" → Tạo dữ liệu mẫu** (hoặc `.\cf.cmd samples`): hệ thống tạo truyện, phụ đề, audio và video nền mẫu rồi cấu hình giúp bạn.

Không hỏi gì giữa chừng; mọi thứ suy ra được thì hệ thống tự chọn và **cho bạn thấy đã chọn gì, vì sao** (giọng đọc, video nền, tên, số tập).

Kết quả trong `output\<ngày>_<tên>\`:

```
README.txt  project.json  story.txt
youtube\  video.mp4  thumbnail.jpg  title.txt  description.txt
tiktok\   part_01.mp4  part_02.mp4 ...
```

Thư mục này của bạn: chép/di chuyển/xoá tuỳ ý; hệ thống không bao giờ sửa hay dọn nó.

## Cài máy mới (một lần)

```powershell
git clone <repo> ContentFactory ; cd ContentFactory
.\setup.ps1          # tự cài/clone/build mọi thứ cần, hỏi vài credential (đều có thể Enter bỏ qua)
.\cf.cmd doctor      # kiểm tra: còn thiếu gì thì nói rõ phải làm gì
.\cf.cmd channel-init kenh_a --name "Kênh Truyện A"      # tạo kênh (hoặc setup đã hỏi)
```

`setup.ps1` chạy lại bao nhiêu lần cũng được, không ghi đè cấu hình bạn đã chỉnh. `-DryRun` chỉ in kế hoạch; `-Yes` không hỏi. Nó làm: Python venv + thư viện, clone module theo `modules.lock`, ffmpeg (winget), build `yt-uploader` (Go), kiểm tra Claude Code CLI, lưu credential vào `config\secrets.local.env` / `tools\data` (không commit), hỏi thư mục video nền, rồi chạy doctor.

Cần làm tay (không tự động được): đăng nhập Claude Code (`claude` một lần), tạo OAuth client Google Cloud và `yt-uploader login` (nếu muốn đăng YouTube), đặt video nền vào thư mục video nguồn, đặt template thumbnail (doctor sẽ nhắc), onboard một engine TTS thật (xem "Chưa sẵn sàng").

Cập nhật: `.\update.ps1` (git pull, đưa module về đúng phiên bản, cài lại thư viện khi đổi, migration DB, doctor).

## Lệnh cơ bản

| Lệnh | Việc |
|---|---|
| `cf ui` | mở giao diện (và chạy nền) |
| `cf samples` | tạo dữ liệu mẫu để thử |
| `cf go "<url>" --channel K [--title "..."] [--open]` | chạy hết, ra gói output |
| `cf status` | các job + đường dẫn output + link YouTube |
| `cf open [job]` | mở thư mục output gần nhất |
| `cf resume <job>` / `cf retry <job>` | tiếp tục job đang bị giữ / chạy lại job lỗi đúng stage lỗi |
| `cf doctor [--json]` | máy đã sẵn sàng chưa |
| `cf channels` / `cf channel-init <id>` | danh sách kênh / tạo kênh |
| `cf start` | dịch vụ nền: tự bật uploader, đồng bộ video nền, tự tiếp tục khi có mạng/hết quota, dọn dẹp |
| `cf demo` | chạy thử cả pipeline bằng adapter giả, vài giây, không tốn tiền |

(`cf` = `.\cf.cmd` hoặc `.\cf.ps1`.) Lệnh nâng cao (`submit`, `plan`, `pools`, `retry-part`, `cleanup`, …): `cf --advanced -h`; chi tiết ở `docs/DEVELOPER.md`.

## Kênh (Channel preset)

Mỗi kênh nhớ cấu hình của nó trong `channels\<id>\channel.json` — bạn chỉnh một lần:

- `name`, `title_template` (vd `[Full Audio {sequence}] | {project_title}`), số tập (`sequence.last_used`), watermark (file trong thư mục kênh)
- `publishing`: `privacy` (mặc định **private**), `account_id`, `tags`, `playlists`, `made_for_kids` (khai báo bắt buộc, không đoán)
- `preset`: `tts_profile` (trống = tự chọn), `pools` (video nền cho youtube/tiktok; trống = tự chọn theo hướng khung hình), `render.youtube/tiktok`, `tiktok.speed/target_part_sec`, `audio`, `language`

Thứ tự ưu tiên: tham số bạn gõ > preset kênh > mặc định. Chỉ phần **Nâng cao** (cờ `--advanced`, `--set`, `--params-file`) mới cho đổi từng chi tiết.

## Tự động

- **Tự tiếp tục (Auto Resume):** mất mạng / hết quota / hết token / thiếu đĩa → job được giữ rồi tự chạy tiếp khi hết nguyên nhân (cần `cf start` nếu chờ lâu; `cf go` chờ ngắn rồi cho biết phải làm gì). Việc **cần người** (đăng nhập, thiếu asset) thì dừng ngay và nói rõ.
- **Tự thử lại (Auto Retry):** lỗi tạm thời được retry có backoff; chỉ chạy lại **đúng phần lỗi** (một part TikTok, một lần upload), không làm lại phần đã xong.
- **Tự dọn (Auto Cleanup):** sau khi đăng, xoá file trung gian của job; xoá artifact job cũ (>14 ngày, job lỗi >30 ngày); giữ cache TTS/nguồn dưới trần dung lượng; **không đụng `output\`**. Xem trước: `cf cleanup --dry-run`.
- **Tự đặt tên:** tiêu đề video nguồn được làm sạch (bỏ emoji/hashtag/link) thành tên tạm; hệ thống **vẫn cảnh báo** trong `README.txt` rằng `project.title` chưa do bạn đặt — đừng đăng công khai tiêu đề của người khác (đặt `--title`).
- **Tự chọn giọng đọc / video nền:** theo ngôn ngữ, engine, độ tin cậy của profile; theo hướng khung hình của thư mục video. Mơ hồ thì **không đoán**, dùng preset hoặc báo thiếu.

## Cấu hình máy

`config\config.json` (commit, mặc định an toàn: adapter giả) + `config\config.local.json` (của máy này, do `setup` sinh, không commit, ghi đè từng khóa) + `config\secrets.local.env` (`KEY=VALUE`, không commit). Secrets chỉ nằm ở đây/biến môi trường, không vào DB, log, workspace.

## Chưa sẵn sàng production

- **TTS thật:** chưa có engine TTS thật được onboard; mặc định là TTS giả (âm tổng hợp). `doctor` luôn cảnh báo. (Phase 8)
- **Upload YouTube thật chưa từng chạy** (cần OAuth client + tài khoản); quota, playlist, lịch đăng chưa kiểm chứng.
- **Story thật (Claude CLI) chưa kiểm chứng đủ** với tiếng Việt và video dài; chi phí/token chưa đo.
- Render ContentFlow video dài 10–60 phút và NVENC chưa kiểm chứng ở quy mô thật.
- Thumbnail cần template + font tự đặt (dữ liệu mẫu tạo giúp để thử); giao diện chỉ tiếng Việt và mới thử bằng Chrome; chưa có thông báo (email/Telegram); chỉ Windows được thử (script `.ps1`/`.cmd`).

## Tài liệu

`docs/USER_GUIDE.md` (dùng hằng ngày, chạy một phần pipeline), `docs/UI_GUIDE.md` (giao diện: màn hình, trạng thái, quy ước), `docs/DESIGN_SYSTEM.md`, `docs/PERFORMANCE.md`, `docs/TROUBLESHOOTING.md` (mã lỗi và cách xử lý), `docs/PRODUCTION_CHECKLIST.md` (cái gì đã kiểm chứng thật, cái gì chưa), `docs/ARCHITECTURE.md` (tóm tắt kiến trúc).

## Dành cho người phát triển

`HANDOFF.md` (thiết kế), `docs/DECISIONS.md` (quyết định D-01…), `docs/MODULE_CONTRACTS.md`, `docs/IMPLEMENTATION_PHASES.md`, `docs/DEVELOPER.md` (lệnh nâng cao, nhật ký phase). Lõi chỉ dùng stdlib, Python ≥ 3.10. Test: `python -m unittest discover -s tests -t .`.
