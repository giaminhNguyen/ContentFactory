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

### Template (giao diện thumbnail/video)

Mỗi kênh chọn **3 template**: Thumbnail, YouTube, TikTok (Kênh → Template, hoặc `cf templates use <kênh> youtube_video youtube_framed`). Bạn **không** nhập tọa độ: khung, vùng video, vị trí chữ nằm trong template.

Muốn kiểu mới: trang **Template** → *Mới* (hoặc *Duplicate* một mẫu có sẵn) → kéo/chỉnh trong Studio → *Lưu* → *Kiểm tra* → *Render thử* → *Chọn cho kênh*. Không có bước Publish: template tạo ra dùng được ngay và sửa lại bất cứ lúc nào; job đã tạo giữ đúng bản lúc tạo, job mới lấy bản vừa lưu. Có thể xoá cả template (trừ khi kênh còn chọn nó). Template/asset của bạn nằm ở `contentflow_user\` (không mất khi cập nhật). Chi tiết: `docs/TEMPLATE_SYSTEM.md`.

Đang dùng cấu hình bố cục cũ (frame/viewport trong config)? `cf doctor` sẽ nhắc; `cf templates migrate --apply` chuyển sang template (có sao lưu).

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

## 4A. Đề xuất truyện và Chạy lại từng bước

**Đề xuất truyện** (chỉ dẫn sáng tạo tự do cho agent viết truyện: hướng cốt truyện, không khí, ngôi kể, kết thúc, chi tiết giữ/tránh…):
- Mặc định cho mọi job: *Cài đặt → Truyện → Cài đặt nâng cao → Đề xuất truyện*.
- Riêng cho một job: ô *Đề xuất truyện cho job này* khi tạo job, hoặc thẻ *Đề xuất truyện* ở trang job (Dùng đề xuất trong Cài đặt / Dùng đề xuất riêng / Không dùng). Đề xuất riêng **thay hoàn toàn** mặc định, không nối.
- Đề xuất được đọc **lúc bước Truyện chạy** và ghi lại theo từng lần chạy; sửa Cài đặt về sau không đổi lịch sử. Muốn áp dụng cho truyện đã có: *Chạy lại → Truyện*.

**Chạy lại** (nút ở trang job, dùng được cả với job đã đăng): tích đúng các bước muốn làm lại (Phụ đề, Truyện, Giọng đọc, Audio, Video YouTube/TikTok, Gói output, Đăng YouTube).
- Chỉ các bước đã chọn chạy, theo thứ tự pipeline; bước không chọn không chạy. Kết quả mới thay kết quả hiện hành khi bước xong; lỗi giữa chừng thì kết quả cũ còn nguyên.
- Làm lại một bước làm các bước phụ thuộc thành **không đồng bộ** (nhãn “Không đồng bộ”) nhưng không tự chạy chúng. Muốn chạy bước sau mà bước trước đang không đồng bộ, chọn thêm bước trước (hệ thống nói rõ cần chọn thêm gì).
- *Đăng YouTube* luôn tạo **một video mới** trên YouTube; video đã đăng trước đó không bị xóa hay sửa.
- Dòng lệnh (nâng cao): `cf rerun <job>` liệt kê bước chạy lại được; `cf rerun <job> story tts --plan` kiểm tra; `cf rerun <job> story tts` chạy.
- Mỗi bước hiện số lần đã chạy lại (tự thử lại khi lỗi mạng không tính); *Lịch sử chạy lại* liệt kê từng lượt, đề xuất truyện đã dùng và mã/URL video đã đăng.

## 4B. Story Remix và Kho nhân vật

Chọn **Story Remix — Xào truyện theo mô-típ** ở màn Chạy để viết truyện original theo mô-típ của nguồn (nhân vật/xung đột/diễn biến khác hẳn), tự chọn hoặc tạo nhân vật từ **Kho nhân vật** và tự cập nhật Kho sau khi truyện đạt QA.
Mặc định chỉ cần chọn nguồn rồi bấm RUN; có thể lưu cấu hình thành **mẫu** và đặt làm mặc định. Xem **docs/STORY_REMIX.md** (cách dùng, kiến trúc, giới hạn, phần đã/chưa chứng minh).

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
