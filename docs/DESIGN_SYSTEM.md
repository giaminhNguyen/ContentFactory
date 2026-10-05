# Design system của giao diện ContentFactory

Nguồn chân lý: `src/contentfactory/orchestrator/webui_static/css/app.css` (token + component) và `js/components.js` (thành phần). Hướng thiết kế được dẫn xuất bằng skill `ui-ux-pro-max` cho loại sản phẩm "công cụ vận hành/automation" (chạy lâu, nhiều trạng thái kỹ thuật, nhiều job): **dày thông tin nhưng dễ đọc, trạng thái luôn có chữ + biểu tượng, nền sáng/tối theo hệ thống, hầu như không trang trí**.

## Nguyên tắc

1. **Trạng thái không bao giờ chỉ bằng màu**: mỗi badge = biểu tượng + nhãn chữ + màu (kiểm tra bằng axe + test).
2. **Không tô đỏ mọi thứ không-chạy**: `waiting` (chờ tài nguyên tạm thời) ≠ `attention` (cần bạn) ≠ `failed` (lỗi vĩnh viễn) — ba tone khác nhau.
3. **Một nút chính mỗi ngữ cảnh** (RUN; Mở output; Tiếp tục/Chạy lại). Hành động phụ dùng nút viền/ghost.
4. **Tiết lộ dần**: màn hình cơ bản chỉ hỏi thứ không suy ra được; Advanced nằm trong vùng thu gọn / tab / trang riêng.
5. **Không card lồng card**, không gradient/kính mờ, không chữ xám nhạt: tương phản ≥ 4.5:1 (axe chạy ở cả sáng/tối).
6. **Chuyển động phục vụ thông tin** (xem mục Motion).

## Token

### Màu (biến CSS; sáng mặc định, tối theo `prefers-color-scheme` hoặc nút đổi giao diện)

| Nhóm | Token | Sáng | Tối |
|---|---|---|---|
| Nền | `--bg` / `--surface` / `--surface-2` | `#f4f6f9` / `#fff` / `#eef1f5` | `#0d1117` / `#151b23` / `#1d2530` |
| Viền | `--border` / `--border-strong` | `#d3d9e1` / `#aeb7c4` | `#2c3846` / `#46566a` |
| Chữ | `--text` / `--muted` | `#121821` / `#475465` | `#e6edf3` / `#a3afbf` |
| Nhấn | `--accent` / `--accent-hover` / `--on-accent` / `--focus` | `#1d4ed8` … | `#7db0ff` … |
| Trạng thái | `--st-running`, `--st-done`, `--st-wait`, `--st-attn`, `--st-fail`, `--st-queue` mỗi cái có `-fg` và `-bg` | xanh dương, xanh lá, vàng, cam, đỏ, xám | bản sáng hơn trên nền tối |

### Khoảng cách, cỡ chữ, bo góc

`--s-1…--s-7` = 4, 8, 12, 16, 24, 32, 48 px · chữ: `--fs-xs…--fs-2xl` = 12, 13, 15 (nền), 18, 22, 28 px, `line-height` 1.5, font hệ thống (Segoe UI/system-ui; mono: Cascadia/Consolas) — không tải font ngoài (chạy offline, không CLS) · bo: `--r-1` 6px, `--r-2` 10px, `--r-pill`. Đổ bóng chỉ cho dialog/toast (`--shadow-pop`).

### Bố cục

Sidebar cố định 232px ≥ 920px; dưới 920px thành thanh trên cuộn ngang; nội dung tối đa 1120px. Mọi bảng nằm trong `.table-wrap` (cuộn ngang, `position: relative` để phần tử absolute không làm tràn trang). Ngắt dòng: `overflow-wrap: anywhere` cho đường dẫn/mã (đường dẫn Windows dài không được làm tràn).

## Thành phần (`js/components.js`)

| Thành phần | Dùng cho | Ghi chú truy cập |
|---|---|---|
| `badge(meta)`, `jobBadge`, `stageBadge` | trạng thái job/stage | icon + chữ; `spin` chỉ cho "đang chạy" |
| `btn`, `busy(button, fn)` | mọi nút; khóa nút khi đang gọi API | `aria-busy`, spinner thay icon |
| `field`, `input`, `select`, `switchCtl` | form | label thật, `aria-describedby` hint/lỗi, lỗi `role=alert` |
| `progress`, `updateProgress` | tiến độ (scaleX bằng GSAP) | `role=progressbar`, `aria-valuenow` |
| `alertBox` (tone info/wait/attn/fail/done) | giữ/lỗi/xong/lưu ý | `role=alert` cho fail, `status` còn lại |
| `emptyState`, `errorState`, `skeleton` | trống / lỗi (có Thử lại) / đang tải | skeleton `aria-busy` |
| `toast`, `toastError` | phản hồi hành động (≤ 3 cái, tự tắt; lỗi giữ lại) | vùng `aria-live=polite` |
| `openDialog`, `confirmDialog` | thẻ `<dialog>` gốc | bẫy focus, Esc đóng, trả focus về nút gọi |
| `disclosure`, `tabs` | Nâng cao / nhóm cài đặt | `aria-expanded`/`aria-controls`; tabs có mũi tên/Home/End |
| `pageHead`, `kv` | tiêu đề trang, danh sách khóa-giá trị | `h1#page-title` duy nhất mỗi trang |

## Trạng thái tương tác

Focus: `:focus-visible` 2px `--focus`, offset 2px (không bao giờ bỏ outline). Hover: nền `--surface-2` (nút), đổi `border-color` (ô nhập). Disabled: `opacity .55` + `cursor:not-allowed` + giải thích bằng `title`/dòng ghi chú. Loading: spinner trong nút + `aria-busy`; skeleton cho vùng. Lỗi: viền đỏ + biểu tượng + câu hướng dẫn (không chỉ đổi màu).

## Motion (GSAP)

Quy ước đầy đủ ở `docs/UI_GUIDE.md` §Animation. Tóm tắt: chỉ `opacity` + `transform` (+ `height` cho vùng thu gọn); 120–450 ms; **không dùng `autoAlpha`** (visibility:hidden làm mất focus); mỗi view có `gsap.context` (huỷ view = revert); `prefers-reduced-motion` ⇒ không tween, áp trạng thái cuối ngay; không animation lặp vô hạn ngoài spinner CSS (bị tắt khi reduced-motion); không stagger trên danh sách vận hành > 10 dòng.

## Biểu tượng

SVG inline, nét 2px, lưới 24 (`js/icons.js`, kiểu Lucide). Không emoji làm icon. Icon chỉ trang trí ⇒ `aria-hidden`; nút chỉ có icon phải có `aria-label`.
