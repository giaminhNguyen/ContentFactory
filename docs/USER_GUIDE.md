# ContentFactory — Hướng dẫn sử dụng

## 1. Dùng hằng ngày (đọc phần này là đủ)

```text
Mở ContentFactory  →  dán link YouTube  →  chọn kênh  →  RUN  →  Mở thư mục Output
```

**Bằng giao diện (khuyên dùng):** bấm đúp `ContentFactory.cmd` (hoặc `.\cf.cmd ui`). Màn hình **Chạy**: dán link (hoặc chọn file phụ đề/truyện/audio), chọn kênh nếu cần, bấm **RUN**; hệ thống chuyển tới trang theo dõi job và khi xong có nút **Mở thư mục output**. Chi tiết từng màn hình: `docs/UI_GUIDE.md`.

**Chưa có truyện/video để thử?** Chạy → "Chưa có truyện hoặc video để thử?" → **Tạo dữ liệu mẫu** → "Dùng truyện mẫu" → chọn "Đọc + dựng video + đóng gói + đăng" → đặt tên → RUN. (Dòng lệnh: `.\cf.cmd samples`.)

**Không cần giao diện (một lệnh):**

```powershell
.\cf.cmd go "https://www.youtube.com/watch?v=..." --channel kenh_a --open
```

- Hệ thống tự làm: lấy phụ đề → viết truyện → đọc thành giọng → chuẩn hoá audio → dựng video YouTube + các video TikTok → đóng gói → (đăng YouTube nếu đã cấu hình).
- Bạn **không** phải chọn giọng đọc, thư mục video nền, tên file, số tập: hệ thống suy ra từ **kênh** và in ra "đã chọn gì, vì sao".
- Kết quả nằm trong `output\<ngày>_<tên>\` (thư mục của bạn; hệ thống không bao giờ sửa/xoá nó):

```text
README.txt   project.json   story.txt
youtube\   video.mp4  thumbnail.jpg  title.txt  description.txt
tiktok\    part_01.mp4  part_02.mp4 ...        ← đăng theo thứ tự part
```

Nếu job **dừng giữa chừng** (mất mạng, hết quota, cần đăng nhập…), `go` nói rõ nguyên nhân và việc cần làm; làm xong chạy `.\cf.cmd resume <job>`. Việc tạm thời (mạng, quota) thì tự chạy tiếp nếu bật Auto Resume và có `.\cf.cmd start` đang chạy.

> Video có đăng tiêu đề của người khác? Hãy đặt tên riêng: `--title "Tên truyện của bạn"`. Không đặt thì hệ thống dùng tiêu đề video nguồn đã làm sạch và **cảnh báo** trong `README.txt`. Video mặc định đăng ở chế độ **private**.

## 2. Lần đầu trên máy mới

```powershell
git clone <repo> ContentFactory ; cd ContentFactory
.\setup.ps1                                   # cài mọi thứ cần thiết, hỏi vài thông tin (Enter để bỏ qua)
.\cf.cmd doctor                               # còn thiếu gì thì nói rõ phải làm gì
.\cf.cmd channel-init kenh_a --name "Kênh Truyện A"
```

Những việc còn phải tự làm (hệ thống không làm thay được):

| Việc | Khi nào cần |
|---|---|
| Đăng nhập Claude Code (`claude` một lần) | để viết truyện bằng AI |
| Đặt video nền (mp4) vào thư mục video nguồn; setup sẽ hỏi đường dẫn | render thật |
| Template thumbnail 1648×928 + font (doctor nhắc) | render YouTube thật |
| Tạo OAuth client Google Cloud + `yt-uploader login` | đăng YouTube thật |
| Onboard một engine TTS thật | giọng đọc thật (mặc định là TTS giả để thử) |

## 3. Kênh

Mỗi kênh nhớ cấu hình của nó trong `channels\<id>\channel.json`: tên hiển thị, mẫu tiêu đề/mô tả, số tập đã đăng, watermark, giọng đọc ưa thích, video nền cho YouTube/TikTok, thiết lập đăng. Sửa một lần, dùng mãi. `.\cf.cmd channels` liệt kê kênh.

## 4. Chạy một phần pipeline

Dùng khi chỉ cần một phần (lệnh nâng cao, `cf --advanced -h`):

| Muốn | Lệnh |
|---|---|
| Chỉ lấy phụ đề | `cf submit --input <url> --mode SUBTITLE_ONLY` |
| Chỉ viết truyện | `--mode STORY_ONLY` |
| Đến hết giọng đọc | `--mode THROUGH_TTS` |
| Đọc từ `story.txt` có sẵn | `--mode TTS_ONLY --artifact story_text=story.txt` |
| Dựng video từ audio có sẵn | `--mode VIDEO_ONLY --artifact audio_master=a.wav --metadata-title "Tên"` |

Rồi `cf run`. Phần đã làm xong và còn hợp lệ không bao giờ bị làm lại. Muốn đi tiếp tới đích xa hơn: `cf config <job> --target publish`.

## 5. Khi có sự cố

| Thấy | Nghĩa là | Làm gì |
|---|---|---|
| Đang chờ mạng / quota / hạn mức AI | tài nguyên tạm thời | chờ (Auto Resume) hoặc `cf resume <job>` |
| Cần xử lý credential | đăng nhập/key hết hạn | đăng nhập lại, `cf resume <job>` |
| Thiếu dữ liệu đầu vào | thiếu template/video nguồn… | bổ sung rồi `cf resume <job>` |
| LỖI ở stage X | lỗi vĩnh viễn của stage đó | xem nguyên nhân trong `cf status`, sửa, `cf retry <job>` (chỉ chạy lại stage X) |

Chi tiết: `docs/TROUBLESHOOTING.md`. Kiểm tra sức khoẻ máy: `cf doctor`.

## 6. Dọn dẹp

Hệ thống tự dọn file trung gian sau khi đăng, job cũ và cache quá cỡ — **không đụng** `output\`. Xem trước: `cf cleanup --dry-run`.
