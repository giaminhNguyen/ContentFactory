# Audit UI/UX (Phase 9)

## 1. Hiện trạng trước Phase 9

- **Không có giao diện đồ hoạ.** Stack frontend: không có; điều khiển duy nhất là CLI (`cf go/status/...`, lệnh nâng cao `--advanced`) và file cấu hình JSON. Vì vậy không có design system, component trùng lặp, spacing/typography/màu cần hợp nhất — phần "audit UI hiện có" của hợp đồng quy về audit **trải nghiệm CLI** và các khoảng trống mà một GUI phải lấp.
- **Điểm mạnh của lõi** cần giữ: Auto Mode (preset kênh, tự chọn), phân loại lỗi/giữ job rõ ràng, `diagnose.explain` (Phase 8), chống tạo trùng ở tầng core (CAS, idempotency).

### Khoảng trống UX của CLI (đầu vào cho thiết kế GUI)

| Vấn đề | Hệ quả với người dùng "lười" | Giải pháp trong GUI |
|---|---|---|
| Phải gõ đường dẫn/cờ (`--mode`, `--artifact kind=path`) để chạy một phần pipeline | Lộ `start_stage`/`target_stage` | Nhận dạng đầu vào + chỉ đề xuất chế độ hợp lệ (`service.detect_input`) |
| Không thấy bước nào sẽ chạy/bỏ qua trước khi chạy | Chạy xong mới biết | Xem trước kế hoạch + tự chọn (`preview_run`) |
| Trạng thái giữ (`PAUSED_*`) là mã kỹ thuật | Không biết chờ gì/làm gì | Nhóm 6 trạng thái, nhãn tiếng Việt, hướng dẫn + nút (`diagnose`) |
| Tiến độ là dòng chữ | Khó theo dõi nhiều job | Danh sách lọc theo nhóm, thanh tiến độ, pipeline từng bước và từng part TikTok |
| Kênh = file JSON | Sai cú pháp/khoá | Form có cấu trúc, kiểm tra bằng đúng validator của core, xem trước tiêu đề |
| Không có nơi xem sức khoẻ hệ thống | Lỗi cài đặt khó đoán | Doctor 12 hạng mục + cách sửa |
| Cần dữ liệu thật mới thử được | Người mới không thử được gì | Dữ liệu mẫu (truyện, phụ đề, audio, video nền, thumbnail template) |
| Bấm Enter 2 lần = 2 job | Job rác | Chống trùng hai lớp (UI + backend) |

## 2. Kiểm tra sau khi dựng (trình duyệt thật: Chrome + axe-core, `scripts/ui_qa/qa.mjs`)

Phạm vi: 7 trang × 4 cỡ cửa sổ (1440, 1024, 768, 390) × 2 giao diện (sáng/tối), luồng hằng ngày, chế độ một phần, trạng thái giữ/lỗi, kênh/TTS/cài đặt/Doctor, bàn phím, reduced-motion, rò rỉ, quy mô; cộng với ứng dụng thật (ffmpeg + ContentFlow thật). Các lỗi tìm thấy và đã sửa:

| # | Phát hiện | Mức | Sửa |
|---|---|---|---|
| 1 | **Vòng lặp xử lý job SẬP** với `OverflowError` khi một tài nguyên (mạng/uploader) hỏng liên tục rất lâu (số mũ của backoff vượt float; với cấu hình thật ≈ vài ngày): job mới đứng im vĩnh viễn. Lộ ra khi QA có job "chờ mạng" trong fixture chạy với nhịp nhanh | **Cao (runtime)** | chặn số mũ (`min(failures-1, 30)`), bọc việc nền (`_guarded`) để một tác vụ nền lỗi không bao giờ làm sập lập lịch; 2 test hồi quy |
| 2 | Hoạt họa `autoAlpha` (visibility:hidden) làm **ô nhập vừa focus bị mất focus** khi trang bắt đầu hiện | Cao (a11y) | chỉ dùng `opacity`+`transform`; đưa vào quy ước |
| 3 | Router cướp focus ngay lần mở đầu ⇒ phím Tab đầu không tới liên kết "Bỏ qua điều hướng" | Trung bình | chỉ quản lý focus khi đổi trang |
| 4 | Trang Giọng đọc tràn ngang (390–1024px): phần tử `.sr-only` (absolute) trong bảng cuộn thoát khỏi vùng cuộn | Cao (bố cục) | `.table-wrap { position: relative }` |
| 5 | Trang Doctor tràn ngang ở 390px vì đường dẫn Windows dài không ngắt dòng | Cao (bố cục) | `overflow-wrap: anywhere` cho `.check`, `.mono` |
| 6 | Huy hiệu check Doctor bị kéo thành hình oval; chip "Đang được Auto chọn" thành hình tròn trong cột hẹp | Thấp | `align-items:start`, `white-space:nowrap` |
| 7 | Thông điệp giữ job lặp ý ("Auto Resume bật… Tự tiếp tục khi… nếu Auto Resume bật…") | Trung bình (UX) | `HOLD_INFO` tách điều kiện/hành động; câu ngắn theo chế độ |
| 8 | "0 giây" ở mỗi bước; cảnh báo output dùng biểu tượng đồng hồ cát; `<div>` trong `<p>` | Thấp | chỉ hiện khi ≥ 1 giây; `alertBox({iconName})`; sửa DOM |
| 9 | Mẫu tiêu đề/mô tả của kênh hiện ô trống khi kênh dùng mặc định | Trung bình (UX) | placeholder = mẫu hiệu lực |
| 10 | Hàng job bị cắt tên trên màn hình hẹp (≤ 640px) | Trung bình | bố cục 1 cột |
| 11 | Máy chủ in traceback `ConnectionResetError` khi trình duyệt đóng kết nối giữa chừng | Thấp | `handle_error` bỏ qua lỗi kết nối |
| 12 | Người dùng chưa có truyện/video để thử (phản hồi trực tiếp) | Cao (onboarding) | `cf samples` + nút "Tạo dữ liệu mẫu" ở Chạy và Video nguồn |

Chưa phát hiện/còn lại: xem `docs/PERFORMANCE.md` (đo được gì, chưa đo gì) và mục "Cảnh báo còn lại" trong báo cáo Phase 9 (cảnh báo axe `heading-order` mức nhẹ).
