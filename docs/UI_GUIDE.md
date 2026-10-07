# Giao diện ContentFactory — hướng dẫn

## 1. Dùng hằng ngày

```text
Mở ContentFactory (ContentFactory.cmd hoặc `cf ui`)  →  dán link  →  (chọn kênh)  →  RUN  →  Mở thư mục output
```

`cf ui` mở trình duyệt tới máy chủ cục bộ `http://127.0.0.1:8765` **và chạy luôn vòng lặp xử lý job trong cùng tiến trình** (tự bật daemon upload, đồng bộ video nền, tự tiếp tục khi hết sự cố, dọn dẹp). Đóng cửa sổ dòng lệnh = dừng; job dở dang tự chạy tiếp lần sau. `--port N`, `--no-open`, `--no-runner` (chỉ xem, không xử lý job).

Chưa có truyện/video để thử? Màn hình **Chạy → "Chưa có truyện hoặc video để thử?" → Tạo dữ liệu mẫu** (hoặc `cf samples`): sinh truyện, phụ đề, audio, video nền ngang/dọc, template thumbnail trong `samples/`, đăng ký pool giúp bạn; rồi bấm "Dùng truyện mẫu / phụ đề mẫu / audio mẫu" và RUN.

## 2. Các màn hình

| Màn hình | Việc |
|---|---|
| **Chạy** | Ô đầu vào (link YouTube hoặc đường dẫn file; nút Chọn file…) → hệ thống nhận dạng (Link YouTube / Phụ đề / Truyện / Audio / Project) và **chỉ đề xuất chế độ hợp lệ**; chọn kênh; ô tên truyện chỉ bắt buộc khi đầu vào không có tiêu đề (truyện, audio); khai báo "dành cho trẻ em" chỉ hỏi khi kênh chưa khai; xem trước "Hệ thống sẽ làm" (các bước chạy/bỏ qua) và những gì **tự chọn** (giọng đọc, video nền, tập kế tiếp, chế độ đăng); Auto Resume bật/tắt; RUN. Bên dưới: 5 job gần nhất. |
| **Job** | Lọc: Tất cả / Đang chạy / Đang chờ / Cần xử lý / Hoàn tất (kèm số lượng); mỗi dòng: tên, kênh, bước hiện tại, tiến độ, trạng thái, nút đúng ngữ cảnh. Phân trang "Tải thêm". |
| **Chi tiết job** | Pipeline 8 bước (xong / dùng lại / có sẵn / đang chạy / chờ / tạm dừng / lỗi / không chạy) kèm tiến độ và từng part TikTok; khung giải thích **vì sao đang dừng và hệ thống sẽ làm gì** + nút (Tiếp tục, Thử lại ngay, Chạy lại stage lỗi, Bật/Tắt Auto Resume); công tắc Auto Resume; khi xong: **Mở thư mục output**, link YouTube, danh sách file, tiêu đề/mô tả sẽ dùng, cảnh báo; "Chi tiết kỹ thuật" (tự chọn, các lần chạy, nhật ký) thu gọn. |
| **Kênh** | Form có cấu trúc (không cần biết JSON): tên, mẫu tiêu đề/mô tả, số tập, watermark (tải lên/chọn), giọng đọc ưa thích, video nền cho YouTube/TikTok, **Template thumbnail/YouTube/TikTok** (chọn tên; Nâng cao: version ghim, fallback, checksum, lỗi), fps, tốc độ + độ dài part TikTok, chế độ đăng/tài khoản/tag/playlist/khai báo trẻ em, **xem trước** tiêu đề YouTube + chữ thumbnail; JSON thô ở mục Nâng cao. |
| **Template** | Danh sách template (builtin có khóa, của bạn, đã lưu trữ), lọc theo loại; Mới / Nhân bản / Lưu trữ; mở **Template Studio**: Lớp (z duy nhất, đổi thứ tự = đánh số lại 10,20,30…), canvas kéo/đổi cỡ/phím mũi tên/zoom (zoom chỉ để xem, không đổi tọa độ), bảng thuộc tính (số) cho ảnh/ảnh nhân vật/vùng video/chữ/canvas, thư viện asset (tải lên, chọn, xóa), Hoàn tác/Làm lại (Ctrl+Z / Ctrl+Shift+Z), Lưu nháp, Kiểm tra (bản đang sửa, bấm lỗi để chọn phần tử), Xem trước (ContentFlow thật), Render thử (thumbnail → ảnh, video → clip ngắn), Publish (xác nhận, sau đó "Chọn cho kênh"), Tạo bản nháp mới khi sửa bản đã publish. Builtin/đã publish chỉ đọc. |
| **Giọng đọc** | Engine đang dùng + sức khoẻ (cảnh báo rõ khi là giọng giả), "TTS: Auto — <profile>", **Nhịp đọc (Prosody)**: chọn Tự nhiên/Nhanh/Kịch tính/Tùy chỉnh + thang khoảng nghỉ, **Nghe thử ~25 giây** và **So sánh A/B** (không cần chạy job), bảng profile (trạng thái, ngôn ngữ, Auto Tune, độ tin cậy, thứ còn thiếu, credential chỉ hiện **tên biến** có/không), chi tiết bằng chứng; thêm engine mới chỉ bằng repo/docs (Analyzer tự phân tích). |
| **Video nguồn** | Pool: thư mục, số video, hướng khung hình, trạng thái đồng bộ, lần gần nhất, vấn đề; Đồng bộ ngay/tất cả, thêm/sửa/xoá; tạo video mẫu. |
| **Cài đặt & Doctor** | Tab Sức khoẻ hệ thống (12 hạng mục: Story, Phụ đề, TTS, FFmpeg, ContentFlow, GPU/NVENC, YouTube uploader, Nguồn video, Ổ đĩa, Cơ sở dữ liệu, Kênh, Hệ thống; trạng thái Ổn / Cần xem / Cần xử lý / Chưa dùng + cách sửa); tab theo nhóm: Chung, Audio, Render, Đăng, Tài nguyên, Lưu trữ (dùng dung lượng + dọn dẹp xem trước/xác nhận), Nâng cao (cấu hình đang áp dụng, đã che bí mật). Tự lưu khi đổi; thay đổi nguy hiểm hỏi xác nhận. |

## 3. Cơ bản và Nâng cao

Cơ bản = màn hình Chạy + Job. Mọi chi tiết khác là **tiết lộ dần**: thu gọn trong chi tiết job, tab Nâng cao, trang riêng. Lệnh dòng lệnh nâng cao (`cf --advanced -h`) và cấu hình JSON vẫn đầy đủ.

## 4. Backend → giao diện (trạng thái)

| Backend (`jobs.state`, `hold_reason`) | Nhóm hiển thị (`diagnose.ui_status`) | Nhãn | Người dùng làm gì |
|---|---|---|---|
| `NEW`, `*_READY` không bị giữ | `queued` | Đang xếp hàng | không |
| `*_PROCESSING/RUNNING/RENDERING/PUBLISHING`, `UPLOADING` | `running` | Đang chạy | không |
| `PAUSED_NETWORK/TOKEN/QUOTA/DISK/RESOURCE` | `waiting` | Đang chờ mạng / hạn mức AI / quota / ổ đĩa / công cụ | chờ (Auto Resume bật) hoặc bấm Tiếp tục |
| `PAUSED_CREDENTIAL`, `PAUSED_MISSING_INPUT`, hoặc `needs_user` | `attention` | Cần bạn xử lý | làm theo hướng dẫn rồi Tiếp tục |
| `FAILED` | `failed` | Lỗi | xem nguyên nhân, Chạy lại stage lỗi |
| `PUBLISHED` hoặc đã tới `target_stage` | `completed` | Hoàn tất | Mở output |

Nhóm lọc: Đang chạy = running+queued; Cần xử lý = attention+failed. Trạng thái từng bước (`done, reused, provided, running, waiting, held, failed, not_planned`) do `service._pipeline` tính từ `start_stage/target_stage`, các lần chạy và artifact import (bảng ở `js/status.js`; test `test_ui_js` khoá sự khớp giữa backend và frontend).

## 5. Kiến trúc

```text
trình duyệt (ES modules, không build, GSAP vendor)
   │  JSON qua HTTP 127.0.0.1, header X-CF-Token
webui.py        máy chủ stdlib: route, token/Host/Origin, tĩnh (CSP), vòng lặp orchestrator
service.py      facade: nhận dạng đầu vào, xem trước, tạo job (idempotent), danh sách/chi tiết, kênh, output
service_admin.py  cài đặt (bảng khai báo), TTS, pool, doctor, dọn dẹp, tác vụ nền
diagnose.py     explain()/ui_status() dùng chung với CLI
```
Frontend không chứa logic nghiệp vụ. Bảo mật cục bộ: chỉ `127.0.0.1`; kiểm `Host` (chống DNS rebinding); mọi `/api` cần token ngẫu nhiên mỗi phiên (nhúng vào `index.html`) và `Origin` cùng nguồn cho thao tác ghi (chống CSRF); không có endpoint nhận đường dẫn tuỳ ý để mở/đọc (mở output chỉ dùng đường dẫn pipeline đã ghi và nằm trong `output/`); CSP `default-src 'self'`; không `innerHTML` với dữ liệu.

### API (tất cả `/api`, JSON; lỗi: `{"error":{"code","message","hint"}}` tiếng Việt, không stack trace)

`GET bootstrap, runtime, jobs?status&limit&offset&since, jobs/<id>, jobs/<id>/log, channels, channels/<id>, channels/<id>/preview, tts, tts/profiles/<n>, pools, settings, config/effective, doctor, tasks/<id>` · `POST detect, preview, runs, jobs/<id>/{resume,retry,auto-resume,open-output}, channels, tts/onboard, pools/sync, cleanup, doctor/run, samples, pick` · `PUT channels/<id>, channels/<id>/asset, pools/<n>, settings` · `DELETE pools/<n>`.
Channel Run (D-101): `POST sources/inspect, sources/youtube/discover, batches, batches/<B…>/{pause,resume,retry-failed,cancel-queued,cancel,rescan,target}` · `GET batches, batches/<B…>?status&limit&offset`.

`jobs?since=<version>` trả `{changed:false}` rất nhẹ khi không có gì mới (version = số job + `updated_at` lớn nhất; tiến độ cũng cập nhật `updated_at`).

## 6. Chống thao tác trùng

Hai lớp: (1) giao diện khóa nút trong lúc gọi (`busy()`), RUN còn khóa bằng cờ cục bộ; (2) backend: `request_id` (gửi lại cùng yêu cầu ⇒ cùng job, nhớ qua khởi động lại) và chữ ký nội dung (cùng đầu vào + kênh + chế độ + tên đang chạy ⇒ trả job đó thay vì tạo thêm). Resume/Retry vốn idempotent ở core (CAS trạng thái). Upload dùng `idempotency_key` của daemon.

## 7. Animation (GSAP)

`js/motion.js` là chỗ duy nhất gọi GSAP. Quy ước: chỉ `opacity`/`transform` (và `height` cho vùng thu gọn); **không `autoAlpha`** (ẩn bằng visibility làm mất focus); 120–450 ms; mỗi view có `scope` (`gsap.context`) và router `revert()` khi rời view ⇒ không tween/listener mồ côi (QA đo); `prefers-reduced-motion` ⇒ không tween, trạng thái cuối áp ngay (phản hồi vẫn thấy qua đổi màu/chữ); vùng cập nhật liên tục (tiến độ) dùng `scaleX`, `overwrite: "auto"`; danh sách vận hành > 10 dòng không stagger. Dùng ở: chuyển view, dòng mới vào danh sách, pulse khi đổi trạng thái, thanh tiến độ, mở/đóng Nâng cao, dấu tích hoàn tất, toast, dialog.

## 8. Quy tắc component

Không tự dựng badge/nút/alert/empty/field trong view: dùng `components.js`. View = `export async function mount(root, ctx)` trả `{destroy()}`; mọi poller/timer/listener tạo trong `mount` phải dừng trong `destroy`. Poller (`js/poller.js`): không chồng yêu cầu, ngừng khi tab ẩn, nhanh khi có việc chạy, chậm khi rảnh, lùi dần khi lỗi, `since` để rẻ. Logic thuần (format, trạng thái, poller) có test `node --test`.

## 9. Cách thêm…

- **Một tùy chọn cài đặt:** thêm một dòng vào `SETTINGS` ở `service_admin.py` (khóa chấm, nhóm, nhãn, kiểu `bool|int|number|text|select|channel`, mô tả, khoảng/`options`/`danger`/`restart`). Giao diện, kiểm tra giá trị, lưu vào `config.local.json` và cập nhật cấu hình sống đều tự có.
- **Một trạng thái/nhãn:** thêm vào `js/status.js` (và `diagnose.py` nếu là nhóm mới); test `test_ui_js` sẽ báo nếu lệch.
- **Một chế độ chạy:** thêm vào `RUN_MODES` + `KIND_MODES` ở `service.py` (ánh xạ vào mode/target của core; không lộ `start_stage`).
- **Job (Phase 9):** dải tổng quan trên cùng (“Có việc cần bạn xử lý?” + đang chạy/chờ/tạm dừng/cần xử lý/hoàn tất hôm nay, làn GPU/TTS, ổ đĩa, tối đa 5 job cần xử lý, nút bật thông báo tuỳ chọn); thanh tìm kiếm + lọc loại/kênh/thời gian (nhớ lựa chọn) + “Xoá lọc”; “Chọn nhiều” → thanh hàng loạt (Tạm dừng, Tiếp tục, Chạy lại, Cập nhật pipeline…, Đổi template…, Hủy… có xác nhận) với báo cáo thành công một phần. Chi tiết job: timeline chuẩn hoá (Xong/Dùng lại/Có sẵn/Đang chạy/Chờ tới lượt/Tạm dừng/Lỗi/Không yêu cầu/Cần chạy lại), nhánh YouTube và TikTok đứng cạnh nhau, “Vì sao?” từng bước thu gọn mặc định. Màn Chạy: khối “Kiểm tra trước khi chạy” chỉ gồm thứ kế hoạch cần (✓/⚠/✗ kèm chữ, mục “Cần sửa trước khi chạy” khoá RUN). QA: `qa.mjs --only jobsui,pipeline`.
- **Nguồn Media (Phase 8):** trang gồm 2 tab — Video (pool video nền, như cũ) và **Ảnh thumbnail** (Image Pool): thẻ mỗi pool có số ảnh hợp lệ/không hợp lệ, cảnh báo, cách chọn ảnh (đổi lưu ngay), kênh đang dùng, “Quét & xem ảnh” (ảnh theo chỉ số, danh sách file hỏng + lý do), Sửa, Xoá (khoá kèm lý do khi còn kênh dùng). Trang Job có thẻ “Ảnh thumbnail” (ảnh đã chốt, nguồn, vì sao, “Đổi ảnh thumbnail…” có hộp xác nhận nêu việc phải chạy lại; tắt kèm lý do khi job đã xong). QA: `qa.mjs --only imgpools`.
- **Studio xem trước (Phase 7):** thanh “Xem trước nhanh” dưới thanh công cụ: 3 chip mẫu nội dung (`aria-pressed`), “Đổi mẫu”, chọn ảnh nền/tên kênh, công tắc “Tự cập nhật khi sửa”; dòng trạng thái `role=status` (đang dựng / sắp cập nhật / đã cập nhật + ms / lỗi + Thử lại). Render thử là nút riêng, bị tắt kèm lý do khi không dùng được. QA: `qa.mjs --only tplprev` (cần fixture ContentFlow thật: `real_templates.py`).
- **Template (Phase 6):** nút trên thẻ template do `row.actions` của backend quyết định (xem `TEMPLATE_SYSTEM.md` §vòng đời): Xoá bản nháp (có xác nhận; tắt kèm lý do + link tới kênh đang dùng), Lưu trữ, Bản nháp mới, Khôi phục; template có sẵn chỉ Nhân bản. QA: `qa.mjs --only tpllife`.
- **Channel Run (Phase 5):** màn Chạy nhận link kênh/playlist (`detect` trả `collection`): panel `views/_channel_run.js` gọi `/api/sources/youtube/discover` (mặc định 10 video mới nhất chưa xử lý; chọn newest/oldest/vị trí/ngày; bộ lọc đã xử lý/livestream/premiere/Shorts; bỏ chọn tay → “Chọn tay”), kế hoạch + kiểm tra made_for_kids/template tính trên video đầu tiên được chọn, nút đổi thành “Tạo Channel Run” và hỏi xác nhận khi >100 job. Danh sách job (`GET /api/jobs`) chỉ gồm Job đơn + thẻ Channel Run (`type: job|batch`; job con ẩn); `views/batch.js` là chi tiết: kênh nguồn + kênh xuất bản + lựa chọn + pipeline, chip đếm (chữ + biểu tượng), tiến độ, tab lọc, hàng video có link mở video nguồn, hành động hàng loạt (tạm dừng/tiếp tục/chạy lại job lỗi/quét lại; “Thao tác nâng cao”: cập nhật pipeline theo phạm vi, hủy việc chưa chạy, hủy cả run — đều xác nhận), chọn nhiều video → `POST /api/jobs/bulk` (backend kiểm từng job, UI báo thành công một phần). Job con có link về Channel Run và các link YouTube từ backend (`links`). QA: `qa.mjs --only channelrun`.
- **Nhịp đọc:** nhịp của kênh nằm ở trang Kênh (`preset.prosody`); nghe thử A/B ở trang Giọng đọc (`views/_prosody.js`, tác vụ nền `/api/tts/prosody/preview`, phát bằng blob có token). “Thao tác nâng cao → Nhịp đọc…” của job liệt kê khoảng nghỉ thật sự được chèn (kèm “Hiện cả ranh giới trong nhóm”), mỗi dòng có ô ms + “Tự động”; áp dụng qua revision (job đang sống) hoặc tạo job mới (job đã xong). QA: `qa.mjs --only prosody`.
- **Watermark Library (D-109):** Kênh → mục Watermark (`views/_watermarks.js`, logic thuần ở `watermark_logic.js`): thẻ “Đang dùng cho kênh” + lưới Thư viện (nguồn Giọng đọc/Tải lên/File cũ, thời lượng, giọng, văn bản, “Đang dùng” bằng chữ), nghe thử tải theo yêu cầu bằng blob có token, “Các bản” (từng bản có Nghe/Dùng bản này), Dùng cho kênh / Bỏ khỏi kênh / Sửa / Tạo lại (TTS) / Thay file (tải lên) / Xóa… (hộp xác nhận nói trước: bỏ khỏi kênh, lưu trữ vì còn job dùng, hay xóa hẳn) / Khôi phục. Dialog Tạo: tên, nguồn (TTS | tải file), nội dung (đếm ký tự), giọng (Tự chọn hoặc profile như trang Giọng đọc), “Dùng làm watermark của kênh”; lỗi hiện cạnh ô; trạng thái “Đang tạo watermark… N giây” (tác vụ nền, nút khóa chống bấm đúp, request_id phía backend), lỗi nhà cung cấp ở lại trong dialog để thử lại, xong có nghe thử ngay. Mọi thao tác lưu NGAY (không qua “Lưu thay đổi” của kênh); sau mỗi thao tác form kênh đồng bộ khóa `watermark/watermark_ref` vào model + base. QA: `qa.mjs --only watermark`.
- **Điều khiển job / Sửa job (D-108):** `GET /api/jobs/{id}` trả `actions` {pause, unpause, cancel, edit, delete, clone}, `control` và `edit` (`floor`, `target`, `stages[]` với `selectable`, `reason`, `reason_kind`, `effect`, `awaiting_run`, `delete`); frontend chỉ vẽ. Nút **Sửa job** ở đầu trang mở dialog `views/_job_edit.js`: bộ chọn đích dạng radio dọc (đã hoàn thành ✓ / đang chạy / đích hiện tại; bước đã qua bị khóa, chú thích chung “đã chạy tới…”, lý do khác hiện dưới bước), mô tả hậu quả của lựa chọn (`aria-live`), “Lưu pipeline” (`PUT /api/jobs/{id}/target`) chỉ bật khi đổi đích và có lý do gần nút; **Vùng nguy hiểm → Xóa job…** (`DELETE /api/jobs/{id}`) có hộp xác nhận nêu output KHÔNG bị xóa, job đang chạy cần thêm ô xác nhận. Job đã xong được mở rộng đích: lưu, KHÔNG tự chạy, banner “Có bước mới chưa chạy” + nút “Chạy tiếp”. Không còn nút “Cập nhật pipeline” riêng trong “Thao tác nâng cao”; Channel Run/hàng loạt “Cập nhật pipeline…” chọn đích (`POST /api/batches/{id}/target`, bulk `update_pipeline` + `target_stage`) qua `openTargetDialog`. Chuyển động: `dialogIn`, `swap` (mờ nhẹ khi đổi mô tả), `pulse` (dấu bước vừa chọn) — đều qua `motion.js`, tắt khi reduced-motion. QA: `qa.mjs --only control`.
- **Chọn bước tùy ý:** công tắc "Tùy chỉnh các bước" ở màn Chạy. Danh sách bước và trạng thái (Đã chọn / Bắt buộc 🔒 / Dùng lại ↻ / không chạy) do `/api/preview` trả theo planner v2; frontend chỉ gửi các bước người dùng chọn (`pipeline: {mode: "custom", requested_stages}`), không có đồ thị phụ thuộc riêng. Bước bị bước phía sau cần thì khóa và giải thích lý do. `GET /api/pipeline` là descriptor (thứ tự/phụ thuộc từ `P.STAGES`); `POST /api/pipeline/plan` xem kế hoạch của một spec bất kỳ. QA: `qa.mjs --only pipeline`.
- **Một view:** `js/views/<tên>.js` + một dòng trong `ROUTES` (`router.js`) + một mục `NAV` (`main.js`).
- **Một endpoint:** hàm trong `Api` (`webui.py`) với `@route`; logic ở facade, có test ở `tests/test_ui.py`.

## 10. Kiểm thử giao diện

```powershell
python -m unittest tests.test_ui tests.test_ui_js tests.test_samples      # facade, HTTP, logic thuần (node), dữ liệu mẫu
cd scripts\ui_qa ; npm install                                            # một lần: playwright-core + axe-core (dùng Chrome đã cài)
python scripts\ui_qa\fixture_server.py --port 8799                        # UI + dữ liệu mẫu (job xong/lỗi/giữ/đang chạy); thêm --many 250 cho danh sách lớn
node scripts\ui_qa\qa.mjs http://127.0.0.1:8799 <root>\fixture.json --shots out   # trang × cỡ cửa sổ × sáng/tối, axe, tràn ngang, luồng, bàn phím, reduced-motion, rò rỉ
python scripts\ui_qa\real_root.py --port 8802                             # UI thật trên ffmpeg + ContentFlow thật; qa.mjs --only real
```

### Template Studio — quy ước

- Logic thuần (slug, đánh số z, kẹp/đổi cỡ, lịch sử undo/redo) ở `js/templates_logic.js`, test bằng node (`tests/ui_js/templates.test.mjs`). Studio chỉnh **đúng tài liệu** ContentFlow render; không có schema thứ hai.
- Ảnh xem trước/video render thử nạp bằng `api.blobUrl` (fetch có token → `blob:`; CSP cho `img-src/media-src blob:`).
- Mọi thao tác ghi khóa nút khi đang chạy; server cũng khóa theo template nên bấm đúp không tạo trùng.
- QA: `python scripts/ui_qa/real_templates.py --port 8803` (ContentFlow thật, dữ liệu tạm) rồi `node scripts/ui_qa/qa.mjs <url> <fixture.json> --only templates`.
