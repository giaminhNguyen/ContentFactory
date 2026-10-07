# Template schema v1

Một template là **một tài liệu JSON** (`templates/<scope>/<type>/<id>/v<N>.json`). Cùng một tài liệu: Template Studio sửa, validator kiểm, Template Engine compile, renderer vẽ. Mã: `modules/ContentFlow/templating/schema.py` (validator), `engine.py` (compile).

```json
{
  "schema": 1, "id": "story_frame", "name": "Story Frame", "type": "video",
  "version": 3, "status": "draft", "description": "16:9 trong khung vàng",
  "canvas": {"width": 1920, "height": 1080, "fps": 30, "background_color": "#FFFFFF"},
  "elements": [
    {"id": "source_video", "type": "source_video", "x": 80, "y": 70, "width": 1760, "height": 940, "fit": "cover", "focus_x": 0.5, "focus_y": 0.5, "z": 10},
    {"id": "frame", "type": "image", "asset_id": "frame_video_gold_16x9", "x": 0, "y": 0, "width": 1920, "height": 1080, "z": 20}
  ]
}
```

## Định danh

| Trường | Quy tắc |
|---|---|
| `schema` | `1` |
| `id` | `^[a-z0-9][a-z0-9_]{1,47}$`, duy nhất trên toàn bộ template (mọi loại, builtin + user) |
| `type` | `thumbnail` \| `video` (không đổi sau khi tạo) |
| `version` | số nguyên ≥ 1 (template mới luôn là v1; sửa tại chỗ không đổi version) |
| `status` | luôn đọc ra `published` (file cũ `draft`/`archived` vẫn hợp lệ, được coi là dùng được; xem `TEMPLATE_SYSTEM.md` §4) |
| `name`, `description` | tên hiển thị (bắt buộc), mô tả |

## Canvas

`width`, `height`: số nguyên 16…8192. `fps` (video, tùy chọn, 1…120): **chỉ đặt khi template cần ép fps**; không đặt thì dùng fps của render profile (builtin không đặt ⇒ hành vi cũ). `background_color` (`#RRGGBB[AA]`, thumbnail): màu nền dưới các lớp; video luôn nền đen (muốn nền khác dùng phần tử `image`).

## Phần tử (`elements[]`)

Mọi phần tử có `id` (`^[a-z][a-z0-9_]{0,31}$`, duy nhất trong template), `type`, `z` (**số nguyên duy nhất** 0…10000: thứ tự lớp xác định, không suy từ thời điểm tạo), `enabled` (mặc định `true`).

| `type` | Dùng trong | Trường |
|---|---|---|
| `image` | thumbnail, video | `asset_id` (frame/background/overlay/logo), `x y width height` (có thể tràn canvas một phần; nằm hẳn ngoài canvas là lỗi), `fit`: `stretch`\|`cover`\|`contain`, `opacity` 0..1 |
| `photo` | thumbnail (≤1) | `x y width height` (trong canvas), `corner_radius`, `mask_asset_id` (asset `mask`), `focus_x/y` 0..1, `zoom_min ≤ zoom_max`, `background_color`, `fit: cover` |
| `source_video` | video (đúng 1) | `x y width height` (có thể tràn: phần tràn bị cắt), `fit: cover`, `focus_x/y` 0..1 (neo cắt, mặc định giữa) |
| `text` | thumbnail | `source`: `channel.name`\|`project.title` (mỗi nguồn tối đa một phần tử), `x y width height` (trong canvas), `align: center`, `vertical_align: top\|center\|bottom`, `font`: `{asset_id}` hoặc `{family: "arialbd.ttf"}` (tên file, không thư mục), `font_size_min ≤ font_size_max` (6…400), `max_lines` 1…10, `line_spacing` 0.5…3, `uppercase`, `fill`, `highlight_fill`, `stroke{color,width}`, `outer_stroke{color,width}`, `shadow{color,offset[dx,dy],blur,spread,opacity}`, `prefer` |

Nội dung chữ **không** nằm trong template: lấy từ `channel.name` / `project.title` của job. `project.title` canonical **không bao giờ bị viết lại** để vừa khung: thứ tự xử lý là xuống dòng → thu cỡ chữ trong `[min,max]` → (cuối cùng) cỡ khẩn cấp của renderer; không dùng AI.

## Lớp (z) và giới hạn của renderer hiện tại

- Thumbnail: ảnh có `z` thấp hơn `photo` ⇒ nền (bake thành `template.background`), cao hơn ⇒ frame phía trên ảnh (`template.foreground`); **chữ luôn vẽ trên cùng** nên `text.z` phải cao hơn mọi ảnh (`TEXT_BELOW_IMAGE`).
- Video: ảnh có `z` thấp hơn `source_video` nằm dưới video (chỉ thấy ngoài vùng video); cao hơn nằm trên. Hai nhóm được "nướng" thành MỘT overlay PNG đúng cỡ canvas (vùng video được đục lỗ); ffmpeg chồng video rồi overlay lên.
- Chữ trong template **video** chưa được hỗ trợ (renderer video không vẽ chữ): schema chấp nhận kiểu `text` nhưng validator báo `UNSUPPORTED_ELEMENT` cho template video cho tới khi renderer hỗ trợ.
- `fit` của `source_video` hiện chỉ `cover`; `align` chỉ `center` (engine căn giữa mọi dòng). Các giá trị khác bị validator từ chối thay vì bị bỏ qua âm thầm.

## Mã lỗi validator (`{level, path, code, message}`)

Thông điệp luôn có template id, version, đường dẫn phần tử/trường và lý do, ví dụ: `template 'story_frame' v3: elements[0] (source_video).width: must be a number > 0, got 0`.

`BAD_SCHEMA, BAD_ID, BAD_TYPE, BAD_VERSION, BAD_STATUS, BAD_NAME, BAD_CANVAS, BAD_CANVAS_SIZE, BAD_FPS, BAD_COLOR, NO_ELEMENTS, BAD_ELEMENT, BAD_ELEMENT_ID, DUPLICATE_ELEMENT_ID, UNSUPPORTED_ELEMENT, BAD_Z, DUPLICATE_Z, BAD_GEOMETRY, OUTSIDE_CANVAS, PARTLY_OUTSIDE_CANVAS (cảnh báo), BAD_FIT, BAD_OPACITY, BAD_FOCUS, BAD_RADIUS, BAD_ZOOM, BAD_ASSET_ID, ASSET_NOT_FOUND, ASSET_WRONG_TYPE, ASSET_FILE_MISSING, VIDEO_REGION, TOO_MANY_PHOTOS, DUPLICATE_TEXT_SOURCE, BAD_TEXT_SOURCE, BAD_ALIGN, BAD_FONT, BAD_FONT_SIZE, BAD_LAYOUT, BAD_EFFECT, TEXT_BELOW_IMAGE, NO_TEXT (cảnh báo)`.

Lỗi vòng đời/registry: `TEMPLATE_NOT_FOUND, TEMPLATE_VERSION_NOT_FOUND, TEMPLATE_ID_EXISTS, TEMPLATE_IMMUTABLE, TEMPLATE_READONLY, TEMPLATE_IS_DRAFT, NO_PUBLISHED_VERSION, DRAFT_EXISTS, TEMPLATE_INVALID, TEMPLATE_CORRUPT, TEMPLATE_CHANGED, TEMPLATE_WRONG_TYPE, ASSET_CHANGED, LOCK_TIMEOUT`.

## Compile → renderer

| Template | Renderer |
|---|---|
| thumbnail | `canvas`, `template.{background, foreground, base_color}` (bake), `photo.*`, `channel.*` (từ `channel.name`), `title.*` (từ `project.title`); nguồn chữ mà template không vẽ bị để trống |
| video | `frame` = overlay đã bake (cùng kích thước canvas), `video_generator.viewport` = vùng `source_video`, `video.fit_mode/focus_x/focus_y`, `video.fps` (nếu template đặt) |
