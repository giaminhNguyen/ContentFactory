# Hiệu năng giao diện (Phase 9)

Mọi số dưới đây **đo thật** (Chrome 154 headless qua Playwright + CDP, Windows 11, máy phát triển, UI nói chuyện với backend thật trên localhost) bằng `scripts/ui_qa/qa.mjs`. Không có số nào là ước đoán. Những gì **không** đo được ở đây được ghi rõ ở cuối.

## Kết quả đo

| Hạng mục | Kết quả | Ngưỡng QA |
|---|---|---|
| Màn hình chính: DOMContentLoaded / load / First Contentful Paint | 26–30 ms / 27–31 ms / 150–190 ms | load < 2 s |
| Tài nguyên tĩnh của màn hình chính | 17 file, **~158 KB** (JS ~133 KB gồm GSAP core 72 KB; CSS ~25 KB; không font/ảnh ngoài) | < 250 KB |
| View phụ (Kênh, Giọng đọc, Video nguồn, Cài đặt) | **nạp lười** bằng `import()`; không có trong lần tải đầu (test kiểm) | — |
| Danh sách 250 job: dòng dựng ban đầu / node DOM | 30 dòng / ~660 node; "Tải thêm" +30 mỗi lần (tối đa 200 dòng sống) | ≤ 30 dòng đầu |
| Long task (> 50 ms) khi nạp/cuộn danh sách lớn | 0 lần (dài nhất 0 ms) | < 200 ms |
| Log job trong DOM | ≤ 120 dòng (backend phân trang ngược, đọc từ cuối file, ≤ 256 KB mỗi lượt) | ≤ 130 |
| 60 lần chuyển trang (12 vòng × 5 view) | DOM nodes 379 → 388; JS event listeners 47 → **47** (không tăng) | < +400 / < +120 |
| Tween GSAP còn sống sau khi rời view | 0 | 0 |
| Reduced-motion | 0 tween chạy; phần tử ở trạng thái cuối ngay | 0 |
| Poll khi rời trang Job | 0 yêu cầu danh sách job; trang tĩnh ≤ 6 yêu cầu/6 s (huy hiệu + runtime) | ≤ 6 |
| Poll khi tab ẩn | **0** yêu cầu; tab hiện lại poll ngay | 0 |
| Poll danh sách job với job đang chạy | ~1 yêu cầu/giây, mỗi yêu cầu `since=<version>` trả `{changed:false}` vài chục byte khi không đổi | — |

## Quyết định hiệu năng

- **Không dựng lại danh sách mỗi lượt poll:** `patchList` giữ node cũ, chỉ thêm/xoá/sắp xếp; `updateJobRow` so chữ ký trước khi chạm DOM; thanh tiến độ dùng `scaleX` (transform) thay vì `width`.
- **Polling có kỷ luật** (`js/poller.js`): không chồng yêu cầu, dừng khi tab ẩn, nhanh (1.5 s) chỉ khi có job đang chạy, chậm (6–8 s) khi rảnh, lùi dần (×2, trần 20 s) khi lỗi, huỷ yêu cầu đang bay khi rời view. Poller của khung (huy hiệu "cần xử lý") **bỏ qua** khi view đang mở vừa cập nhật số đếm (tránh yêu cầu trùng). Có test đơn vị cho từng hành vi (`tests/ui_js`).
- **`since=<version>`:** `jobs_version()` là một truy vấn tổng hợp (`COUNT`, `MAX(updated_at)`); tiến độ cũng cập nhật `updated_at` nên UI vẫn thấy `3/6 segments` đổi. Danh sách phân trang ở SQL (`job_index` nhẹ + `jobs_by_ids` chỉ cho trang hiện tại), không parse `params` của toàn bộ job.
- **Backend chặn trần:** log đọc từ cuối file với trần 256 KB; `du` của thư mục lưu trữ giới hạn số file quét; `doctor`, onboarding TTS, đồng bộ pool chạy ở **thread nền** (UI không bao giờ đứng chờ).
- **Không tốn AI/token khi dùng UI:** giao diện không gọi LLM; Story/TTS chỉ chạy khi job chạy (`detect`/`preview` thuần cục bộ, không mạng).
- **Không phụ thuộc mạng ngoài:** GSAP và font hệ thống được đóng gói cục bộ (không CDN, không Google Fonts) ⇒ khởi động nhanh, chạy offline, không CLS do font.
- **Hoạt họa:** chỉ `opacity`/`transform`; 120–450 ms; stagger tắt khi danh sách > 10 dòng; spinner CSS.

## Ảnh hưởng tới pipeline lõi

Phase 9 chỉ chạm lõi ở: `set_checkpoint` ghi thêm `updated_at` (một cột trong UPDATE đã có), 3 truy vấn đọc nhẹ mới, khoá ghi manifest (Phase 8), chặn mũ backoff + bọc tick nền. Đo lại bằng `scripts/validate_real.py` sau Phase 9 (cùng máy, audio 240 s, 1080p): audio 10.0 s (Phase 8: 10.1), render YouTube 38.3 s (39.1), render TikTok 23.2 s (23.7), source 3.0 s (2.4–3.0) ⇒ **không thay đổi** ngoài sai số đo.

## Chưa đo / hạn chế (trung thực)

- **FPS/jank của hoạt họa không đo bằng số**: môi trường headless không đo được frame-rate đáng tin cậy. Kiểm chứng định tính: không long task, chỉ transform/opacity, không layout thrash (không đọc layout trong vòng animate), reduced-motion tắt hẳn.
- Chưa đo trên máy yếu/Firefox/Safari; chỉ Chrome. Chưa đo danh sách > 250 job (trần 200 dòng sống + SQL phân trang giữ chi phí gần hằng số theo trang).
- Chưa đo bộ nhớ heap theo thời gian dài (chỉ DOM nodes/listeners qua 60 lần điều hướng).
