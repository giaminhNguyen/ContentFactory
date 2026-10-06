# Hệ thống Template (Phase 10)

> Bố cục thumbnail/video là **template có phiên bản** do **ContentFlow** sở hữu. ContentFactory chỉ **chọn template ID**, **chốt version cụ thể lúc tạo job**, **snapshot** nó, và gửi cho ContentFlow dữ liệu ngữ nghĩa — **không bao giờ gửi tọa độ**.
> Quyết định: `DECISIONS.md` D-92…D-97. Schema: `TEMPLATE_SCHEMA.md`. Asset: `ASSET_MANAGEMENT.md`.

## 1. Luồng

```text
Channel Config chọn template ID (thumbnail / youtube_video / tiktok_video)
   ↓  lúc TẠO job
Job resolve version cụ thể (latest_published -> v4)  ──►  snapshot trong params.templates (nội dung + checksum + sha256 asset)
   ↓
Render Manager → RenderAdapter → media_worker (params.template = snapshot, KHÔNG tọa độ)
   ↓
ContentFlow: Template Engine (compile) → Asset Registry → bake lớp ảnh → renderer thumbnail / video
```

Người dùng chọn tên template. Họ **không** nhập `x, y, width, height, font size, crop, z-index…`: đó là việc của template (sửa trong **Template Studio**, trang *Template* của giao diện).

## 2. Ai sở hữu gì

| ContentFlow (`modules/ContentFlow/templating/`) | ContentFactory |
|---|---|
| schema, validator, registry, versioning, discovery | chọn template ID cho kênh (`channel.json → templates`) |
| asset registry (builtin/user), tra cứu ID → file | resolve version khi job bắt đầu, **snapshot** vào `params.templates` |
| canvas, frame, vùng chữ/video, crop/fit, overlay, z-order, font | gửi `template` (snapshot) + dữ liệu ngữ nghĩa (`channel.name`, `project.title`, audio, nguồn) |
| preview / test render | hiển thị lựa chọn, vòng đời qua UI/CLI, invalidation/cache key |

ContentFactory **không import** code ContentFlow: nói chuyện qua `python -m templating <lệnh>` (JSON stdin/stdout, `render/templates_client.py`) và `media_worker` (`params.template`). Source Sync (chuẩn bị pool video) và Audio/TTS **không** thuộc template.

## 3. Lưu trữ và đường dẫn di động

```text
ContentFlow/                        (builtin, đi cùng module, chỉ đọc)
├─ assets/builtin/{frames,backgrounds,overlays,logos,fonts,masks}/  + registry.json
├─ templates/builtin/{thumbnail,video}/<id>/v<N>.json
└─ …                               (không có dữ liệu máy)

<contentflow_user>/                 (dữ liệu người dùng; mặc định <ContentFactory>/contentflow_user/, KHÔNG nằm trong module)
├─ assets/{frames,…}/  + registry.json
├─ templates/{thumbnail,video}/<id>/v<N>.json
└─ cache/{baked,previews,test_render,samples}/      (sinh lại được)
```

- Template tham chiếu asset bằng **ID** (`frame_gold_01`), registry lưu **đường dẫn tương đối** (`frames/gold_01.png`); gốc lấy từ biến môi trường/cấu hình: `CONTENTFLOW_ASSET_ROOT`, `CONTENTFLOW_TEMPLATE_ROOT`, `CONTENTFLOW_USER_ROOT` (ContentFactory đặt từ `tools.contentflow.user_root`), `CONTENTFLOW_CACHE_ROOT`. Clone/chuyển máy không làm hỏng dữ liệu (có test: không có đường dẫn tuyệt đối trong dữ liệu di động; chuyển thư mục user vẫn resolve).
- Cập nhật code **không bao giờ xóa** asset/template của người dùng (nằm ngoài module, trong thư mục riêng). Builtin chỉ đọc trong UI; muốn sửa thì **Duplicate**.

## 4. Vòng đời, version, immutability

`draft` (sửa được, không bao giờ tự được chọn cho production) → `published` (bất biến, chọn được) → `archived` (không chào cho job mới, vẫn resolve được theo version chính xác để job cũ tái hiện).

- Sửa bản đã publish = **tạo draft version mới** (`new-draft`: v2 → v3), Test Render, rồi Publish. Version đã publish **không bao giờ bị ghi đè** (`TEMPLATE_IMMUTABLE`). Mỗi template tối đa một draft mở.
- Publish **validate** trước (không publish được template lỗi) và **idempotent** (bấm đúp không sao). Ghi file nguyên tử + khóa theo scope: hai Save/Publish đè nhau hoặc crash giữa chừng không làm hỏng registry.
- `latest_published` = version published cao nhất. Template không có version published ⇒ lỗi rõ ràng (`NO_PUBLISHED_VERSION`), **không** âm thầm chọn template khác.
- Hành động theo trạng thái (backend quyết định, `service_templates.actions_for` → `row.actions`; giao diện chỉ vẽ nút, D-103): **builtin** = chỉ Xem/Nhân bản; **bản nháp của user** = Sửa / Nhân bản / **Xoá bản nháp** (ngay ở danh sách và trên thanh công cụ Studio; xác nhận ngắn; bấm đúp an toàn); **đã publish** = Nhân bản / Bản nháp mới / **Lưu trữ** (không bao giờ xoá để job cũ tái lập được); **đã lưu trữ** = Nhân bản / **Khôi phục** (= bản nháp mới từ version gần nhất, rồi publish lại). Không có hard-delete cho published/archived: ContentFlow không cung cấp và job cũ có thể ghim version. Xoá bản nháp DUY NHẤT của một template (template biến mất) khi kênh đang chọn nó bị chặn (`TEMPLATE_IN_USE`, nêu tên kênh + cách xử lý); xoá nháp của template đã publish thì luôn được (kênh dùng bản publish).
- Xóa: chỉ **draft** được xóa; published ⇒ **archive**. Asset đang được template dùng không xóa được (`ASSET_IN_USE`) trừ khi `force`.
- **Checksum** = sha256 nội dung chuẩn hóa của (schema, type, id, version, canvas, elements) — không gồm status/mô tả/thời gian. **Fingerprint** của snapshot = checksum + sha256 từng asset ⇒ asset đổi thì fingerprint đổi.

## 5. Channel Config

```json
{ "name": "Truyện Audio",
  "templates": {
    "thumbnail":     {"id": "thumb_default",   "version_policy": "latest_published"},
    "youtube_video": {"id": "youtube_framed",  "version_policy": "latest_published"},
    "tiktok_video":  {"id": "tiktok_default",  "version_policy": 3, "fallback": "tiktok_framed"} } }
```

- `version_policy`: `latest_published` (mặc định) hoặc số version **ghim**. `fallback`: chỉ dùng khi **khai báo rõ ràng**; không có thì template hỏng ⇒ từ chối tạo job (`INVALID_CHANNEL_TEMPLATE`, nêu kênh/template/mã lỗi/cách sửa). Chuỗi trần `"thumbnail": "thumb_gold"` = id + `latest_published`.
- Chưa chọn ⇒ mặc định toàn cục `config.templates.defaults` (builtin `thumb_default`, `youtube_default`, `tiktok_default`). Mọi lựa chọn tự động nằm trong `params.auto` ("template.youtube = youtube_framed@v2 vì …").
- Khóa lạ (`x`, `title_y`, `viewport`…) bị từ chối khi nạp/lưu kênh: **Channel Config không chứa tọa độ**.
- CLI: `cf templates use <kênh> <khóa> <id> [--version N] [--fallback ID]`. UI: Kênh → Template (ô chọn tên; Advanced: version, fallback, checksum, lỗi).

## 6. Job snapshot và tái hiện

Lúc tạo job (chỉ khi đích tới render): `params.templates = {thumbnail, youtube, tiktok}` mỗi cái = `{id, version, type, checksum, fingerprint, template (cả tài liệu), assets{id → sha256,type,scope,path}, summary{canvas,fps}}`.

- Job **luôn** render bằng đúng snapshot: retry/resume/render lại, kể cả sau khi khởi động lại hoặc publish v5, **không** resolve lại `latest_published`. Chỉ hành động explicit đổi được: `cf retemplate <job> <thumbnail|youtube|tiktok> <id>` (resolve lại một kind, ghi lịch sử, chỉ stage render đó hết hạn).
- **Muốn video đã dựng đổi sang template mới:** `cf rerender <job>` tạo job MỚI (start `render_youtube`, đích `render_tiktok`, không đăng lại) dùng lại audio + metadata của job cũ và template HIỆN TẠI của kênh — Source/Story/TTS/Audio không chạy lại, job cũ giữ nguyên snapshot.
- ContentFlow kiểm checksum tài liệu và sha256 từng asset khi render: asset bị thay sau snapshot ⇒ `ASSET_CHANGED` (POLICY `MISSING_INPUT`: job giữ chờ người xử lý, không render ra thứ khác lặng lẽ).
- Job tạo với đích chưa tới render (vd chỉ Story) thì chưa chốt; chốt khi `set_target` mở rộng sang render (sai thì báo, đích không đổi).

## 7. Invalidation và cache

- `render_youtube.params_deps` gồm `templates.youtube`, `templates.thumbnail`; `render_tiktok` gồm `templates.tiktok`. Đổi template nào chỉ làm hết hạn output render tương ứng; **Source/Story/TTS/Audio không đổi `stage_key`** (có test).
- Khóa nội dung từng output (sidecar `.key.json`) gồm `template {id, version, fingerprint}`: version/asset mới ⇒ render lại, không tái dùng bố cục cũ; retry một part TikTok vẫn riêng lẻ.
- Idempotency key gửi `media_worker` cũng chứa fingerprint. ContentFlow cache các lớp ảnh "nướng" (bake) theo hash nội dung (template + sha256 asset) nên không bake lại.
- Kích thước output + pool: canvas của template quyết định độ phân giải profile; Source Sync chuẩn hóa pool theo canvas (giữ nguyên hành vi cũ khi dùng template builtin 1920×1080 / 1080×1920).

## 8. Tương thích layout cũ

Profile render cũ có `frame_path`/`viewport`/`config_overrides`, `thumbnail.config_overrides`, hoặc đổi `resolution` = bố cục kiểu cũ:

- Không chọn template rõ ràng ⇒ **giữ chạy kiểu cũ** + quyết định `template.<kind> = legacy` ("deprecated"), `cf doctor` cảnh báo.
- Đã chọn template ⇒ template thắng, layout cũ bị bỏ qua **có ghi** (`template.<kind>.legacy_ignored`).
- `cf templates migrate [--apply]`: chuyển cấu hình cũ (toàn cục `render.profiles.*` và `preset.render` của kênh) thành template user đã publish (`legacy_*`; file ảnh/font được **import thành asset**), trỏ `templates.defaults`/kênh tới đó, bỏ khóa cũ, sao lưu `.bak`; idempotent.
- Module ContentFlow cũ chưa có `templating` ⇒ job chạy kiểu cũ + quyết định `templates = unavailable`; Doctor báo.

## 9. Thêm template / asset

- **Qua giao diện**: Template → *Mới* (hoặc *Duplicate* một template builtin) → chỉnh trong Studio → Lưu nháp → Kiểm tra → Xem trước → **Render thử** → Publish → Kênh → chọn.
- **Qua CLI**: `cf templates duplicate youtube_default my_yt`, sửa JSON ở `<contentflow_user>/templates/video/my_yt/v1.json` hoặc dùng API, `cf templates validate my_yt`, `cf templates test-render my_yt`, `cf templates publish my_yt 1`.
- **Asset**: `PUT /api/assets/<id>?type=frame&name=file.png` (UI: thư viện asset) hoặc `python -m templating import-asset` (`{"id","type","file"}`). Loại: frame, background, overlay, logo, font, mask. Chi tiết `ASSET_MANAGEMENT.md`.
- **Builtin mới** (trong repo ContentFlow): sửa `scripts/make_builtin.py`, chạy lại, commit.

## 10. Template Studio (hợp đồng)

Studio chỉnh **đúng tài liệu** ContentFlow render (không có schema thứ hai). API (`/api/templates…`, `/api/assets…`, `service_templates.py`): list, get, create/duplicate/new-draft, save-draft, validate, preview, test-render, publish, archive, delete-draft, asset list/import/delete/file. Tệp xem trước/test render phục vụ **theo tên** từ cache của ContentFlow (không nhận đường dẫn tùy ý). Ghi cùng một template được khóa tuần tự (chống bấm đúp). Zoom canvas **không** đổi tọa độ thật; undo/redo chỉ trên bản nháp đang sửa. Chi tiết UI: `UI_GUIDE.md`.

## 11. An toàn

Không `../` trong đường dẫn (resolve asset kiểm nằm trong gốc; tên file import được làm sạch, không ghi đè file khác, kiểm định dạng thật chứ không tin đuôi file; font phải đúng magic TTF/OTF); không thực thi file upload; không shell từ trường template (chỉ dữ liệu); JSON parse an toàn; template export không chứa bí mật; `font.family` chỉ là tên file (không thư mục).

## 12. Hiệu năng

Render không liệt kê toàn bộ template/asset: tra theo `id@version` (vài lần `stat`); registry asset cache theo mtime; lớp ảnh bake theo hash. Preview/Test Render chạy ở tiến trình ContentFlow riêng, không chặn pipeline.
