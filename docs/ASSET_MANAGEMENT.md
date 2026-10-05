# Quản lý asset (ContentFlow Asset Registry)

Asset = file hình/font mà template dùng. Mỗi asset có **ID ổn định**; template chỉ tham chiếu ID, **không bao giờ** đường dẫn máy.

```text
asset_id  →  Asset Registry (registry.json)  →  đường dẫn TƯƠNG ĐỐI  →  gốc asset (CONTENTFLOW_ASSET_ROOT / CONTENTFLOW_USER_ROOT)  →  file
```

## Bản ghi

```json
{"id": "frame_gold_01", "type": "frame", "scope": "user", "path": "frames/gold_01.png", "version": 1, "name": "Gold", "sha256": "…", "size": 12345,
 "metadata": {"width": 1920, "height": 1080, "format": "PNG"}}
```

- `type`: `frame`, `background`, `overlay`, `logo`, `font`, `mask` (thêm loại mới = thêm vào `TYPES` + thư mục).
- `scope`: **builtin** (đi cùng ContentFlow, chỉ đọc, không xóa được) / **user** (người dùng import, quản lý được; nằm trong `contentflow_user/assets/`, ngoài module ⇒ cập nhật/clone lại module không đụng tới).
- ID: `^[a-z0-9][a-z0-9_-]{1,63}$`, duy nhất **qua cả hai scope** (asset user không đè được asset builtin).

## Khả năng

Danh sách (lọc theo loại/scope), tra theo ID, kiểm tra file tồn tại + sha256 + định dạng, phát hiện asset mất/hỏng (`ASSET_FILE_MISSING`, `ASSET_BAD_FILE`), import an toàn, xóa có chính sách (builtin: từ chối; đang được template dùng: `ASSET_IN_USE` trừ `force`), `info` cho Doctor.

## Import an toàn

Tên file được làm sạch (chữ thường, `[a-z0-9._-]`), **không** ghi đè file khác (trùng tên ⇒ thêm hậu tố), không `../`; định dạng kiểm **bằng nội dung thật** (Pillow mở được ảnh, font có magic TTF/OTF/TTC) chứ không tin đuôi file; giới hạn 64 MB; file không bao giờ được thực thi. Ghi nguyên tử + khóa registry theo scope.

## Cách thêm

- UI: Template Studio → Thư viện asset → tải lên (chọn loại, ID gợi ý từ tên file).
- API: `PUT /api/assets/<id>?type=frame&name=file.png` (thân = byte thô).
- CLI ContentFlow: `echo '{"id":"my_frame","type":"frame","file":"C:/x/frame.png"}' | python -m templating import-asset`.
- Asset builtin: đặt vào `assets/builtin/<loại>/`, thêm vào `scripts/make_builtin.py`, chạy lại, commit.

## Thay asset?

Asset đã được snapshot cùng sha256: **đổi nội dung file của một asset ID** làm job đã snapshot báo `ASSET_CHANGED` (để không render ra thứ khác). Muốn thay hình: import asset **ID mới**, tạo draft version mới của template trỏ ID mới, publish.

## Quyền riêng tư

Không có API duyệt hệ thống tệp tùy ý; ảnh asset trả theo ID. Template export không chứa đường dẫn máy hay bí mật.
