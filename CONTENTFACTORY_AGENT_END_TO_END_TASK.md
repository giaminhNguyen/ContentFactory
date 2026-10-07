# CONTENTFACTORY — END-TO-END IMPLEMENTATION TASK

> **Đây là file lệnh/spec duy nhất cho coding agent.**
>
> Agent phải đọc toàn bộ file này trước khi sửa code, sau đó tự inspect repository, lập kế hoạch nội bộ, triển khai end-to-end, chạy test, tự sửa bug/regression và chỉ kết thúc khi Definition of Done ở cuối file được đáp ứng.
>
> Repository: `giaminhNguyen/ContentFactory`

---

# TIẾN ĐỘ (cập nhật 2026-10-07)

Trạng thái thật so với §47 (kiểm tra bằng code, không suy đoán). `[x]` = đã commit, `[ ]` = chưa làm.

| Phase | Trạng thái | Ghi chú |
|---|---|---|
| 1 — Inspect | một phần | Đã xác nhận: `Runner.set_target` (`orchestrator/runner.py`) + `JobStore.set_target` (`jobs/db.py`) đã có nhưng **chưa có progress floor**, chưa chặn target lùi; chưa có API/UI `Sửa job`, chưa có xóa job. Watermark hiện chỉ là `channel.watermark` (1 file mutable) + upload trong `channels.py`/`channels.js`. Chưa đọc skill UI Pro Max / GSAP. |
| 2 — Watermark domain/storage | [ ] | Chưa có registry/revision/active ref. |
| 3 — Watermark TTS/upload service | [ ] | Chưa có primitive synth đoạn ngắn dùng chung. |
| 4 — Tích hợp pipeline watermark | [ ] | Stage key vẫn theo path; chưa snapshot revision/hash. |
| 5 — Job edit core | [ ] | Cần: progress floor, ngữ nghĩa running/completed/held/failed, xóa job an toàn. |
| 6 — API | [ ] | |
| 7 — UI/UX | [ ] | |
| 8–11 — Test, fix loop, UI QA, docs | [ ] | |

## Đã làm & commit

- [x] **WorkerTTS** — adapter TTS worker sống lâu (nạp model một lần, tái dùng giữa các segment) + `scripts/tts_worker.py` + `tests/test_worker_tts.py` (pass). Commit `128e79d`. Đây là nền cho "TTS dùng chung" (§11.2) nhưng **chưa** có phần watermark nào dùng nó.

## Việc kế tiếp (đề xuất thứ tự commit)

1. Job edit core: progress floor + validate trong `Runner.set_target`, lỗi domain, test §41.
2. Xóa job an toàn (soft-delete + cancel hợp tác, không xóa output).
3. API `target` / `DELETE job` + UI `Sửa job`.
4. Watermark domain/storage (registry, revision, legacy adopt) + test §40.1–40.3.
5. Watermark TTS/upload service, cache/fingerprint, snapshot job, fix stage key.
6. Watermark Library UI, docs, QA cuối.

---

# 0. MỆNH LỆNH THỰC THI CHO AGENT

Bạn đang đứng ở **root của project ContentFactory local**.

Hãy thực hiện task này trực tiếp trên codebase hiện tại.

Không chỉ phân tích, không chỉ viết plan, không chỉ tạo skeleton/TODO. Phải sửa code thật, test thật, fix bug thật và hoàn thiện feature.

## 0.1. Quy tắc làm việc

1. **Code hiện tại là source of truth.**
2. Trước khi sửa, đọc tối thiểu:
   - `README.md`
   - `HANDOFF.md`
   - `docs/MODULE_CONTRACTS.md`
   - `docs/DECISIONS.md`
   - `docs/UI_GUIDE.md`
   - các tài liệu template/asset/job/TTS/audio có liên quan nếu tồn tại.
3. Inspect code thật trước khi quyết định file/schema/API cần sửa.
4. Reuse abstraction hiện có. Không tạo subsystem song song nếu hệ thống đã có primitive tương đương.
5. Giữ backward compatibility tối đa.
6. Không hỏi lại người dùng cho các quyết định kỹ thuật có thể suy ra hợp lý từ code và tài liệu. Tự chọn phương án phù hợp nhất, ghi lại rationale ở báo cáo cuối.
7. Không dừng ở lỗi test đầu tiên. Debug → sửa → chạy lại cho đến khi sạch.
8. Không che lỗi bằng cách xóa/disable test hoặc hạ assertion.
9. Không hardcode dữ liệu mà hệ thống đã có registry/config/resolver.
10. Không để feature ở trạng thái “tạo được nhưng không quản lý được”. Mọi entity/asset mới phải có lifecycle hợp lý.
11. Không tự ý rewrite kiến trúc lớn ngoài phạm vi nếu không thật sự cần.
12. Không phá output cũ, channel cũ, job cũ, config cũ.
13. Không đưa secret/API key vào DB/channel config/manifest/log/output.
14. Mọi write quan trọng phải atomic theo convention hiện có.
15. Nếu phát hiện bug có liên quan trực tiếp đến feature và bug đó ngăn flow hoàn chỉnh, **phải sửa luôn trong task này**.

---

# 1. BẮT BUỘC DÙNG LOCAL SKILLS CHO UI/UX

Phần UI/UX của task này phải được làm chỉn chu, không phải CRUD thô.

Trước khi sửa frontend:

1. Tìm các skill đã cài local tương ứng với:
   - **UI Pro Max**
   - **GASP / GSAP skill** (tên local có thể là `gasp`, `gsap`, hoặc biến thể gần nhất đã được cài).
2. Đọc đầy đủ `SKILL.md` / `skill.md` của hai skill đó.
3. Áp dụng workflow, checklist và guideline của chúng vào việc thiết kế/triển khai UI.
4. Nếu tên skill trên máy khác cách viết trong spec này, dùng skill local có chức năng tương ứng; không bỏ qua chỉ vì khác tên.
5. Không cài framework UI mới nếu project hiện tại không dùng.

## 1.1. Giữ nguyên architectural constraints của UI hiện tại

Theo kiến trúc ContentFactory hiện có, phải tiếp tục tôn trọng:

- frontend ES modules hiện tại, không ép đổi framework/build system;
- chạy offline/local;
- không CDN;
- security localhost/token/Origin/CSP hiện có;
- không dùng `innerHTML` với dữ liệu người dùng;
- accessibility bằng bàn phím;
- focus state rõ;
- light/dark theme;
- `prefers-reduced-motion`;
- không dùng màu là tín hiệu trạng thái duy nhất;
- chống double submit cả frontend + backend;
- progressive disclosure: người dùng bình thường không phải hiểu field kỹ thuật.

## 1.2. GSAP/GASP animation

Project đã có quy ước animation tập trung. Phải inspect code hiện tại và giữ đúng pattern đó.

Yêu cầu tối thiểu:

- mọi GSAP animation đi qua module motion hiện có (ví dụ `js/motion.js` nếu code hiện tại vẫn như tài liệu);
- không rải `gsap.to()` ngẫu nhiên trong các view;
- ưu tiên `opacity` + `transform`;
- vùng collapse có thể animate `height` theo convention hiện tại;
- không dùng `autoAlpha` nếu làm mất focus/visibility semantics;
- animation ngắn, có mục đích, thường khoảng 120–450ms theo UI guide hiện tại;
- `prefers-reduced-motion` → bỏ tween, áp trạng thái cuối ngay;
- khi đổi route/view phải cleanup context/listener/timeline;
- danh sách dài không stagger hàng loạt;
- progress liên tục dùng transform/`scaleX`, không gây layout thrashing;
- không animation gây chậm thao tác vận hành;
- modal/drawer/menu phải có enter/exit tinh tế nhưng không làm chậm interaction;
- loading state phải phản ánh tiến trình thật, không giả progress.

Mục tiêu là **polished operational UI**, không phải animation để trang trí.

---

# 2. TƯ DUY SẢN PHẨM BẮT BUỘC — KHÔNG TẠO FEATURE CỤT

Bất kỳ thứ gì user có thể tạo ra phải được xem xét theo vòng đời:

```text
Create
→ Persist
→ List/View
→ Preview
→ Select/Use
→ Update
→ Version (nếu cần)
→ Delete/Archive
→ Reference protection
→ Cache/Invalidation
→ Restart persistence
→ Error recovery
```

Đối với mỗi feature mới, tự audit các câu hỏi:

```text
Nó lưu ở đâu?
Restart app còn không?
User tìm lại ở đâu?
Có dùng lại được không?
Có sửa/update được không?
Sửa có làm hỏng job cũ không?
Có xóa/archive được không?
Đang được tham chiếu thì sao?
Cache nhận ra version mới thế nào?
UI thể hiện trạng thái thật thế nào?
Lỗi giữa chừng rollback ra sao?
```

Nếu chưa trả lời đủ thì feature chưa hoàn chỉnh.

---

# 3. PHẠM VI TASK

Task gồm 2 nhóm chức năng chính:

## A. Watermark hoàn chỉnh

- Watermark Library theo channel.
- Upload watermark vẫn tồn tại.
- Generate watermark bằng TTS từ nội dung text.
- TTS watermark dùng cùng TTS system với narration/audio.
- Persist/list/preview/select/update/regenerate/version/delete/archive.
- Active watermark của Channel.
- Backward compatibility với watermark cũ.
- Job snapshot watermark revision bất biến.
- Stage invalidation đúng theo content hash/fingerprint.
- Không TTS lại truyện khi watermark đổi.

## B. Sửa Job

Trong Chi tiết Job có chức năng **Sửa Job** với ít nhất:

- **Cập nhật Pipeline** theo semantics mới.
- **Xóa Job** an toàn.
- Loại bỏ/replace flow “Cập nhật Pipeline” cũ nếu UI/API hiện tại đã có implementation cũ hoặc trùng chức năng.

Cập nhật pipeline phải dựa trên `target_stage`/planner/orchestrator hiện có, không tạo pipeline model thứ hai.

---

# 4. KIẾN TRÚC HIỆN TẠI CẦN GIỮ

Trước khi code, verify lại trên source hiện tại. Tài liệu hiện mô tả các điểm sau:

- ContentFactory là orchestrator cấp cao duy nhất.
- Pipeline là chuỗi stage nối bằng artifact, không bắt buộc chạy từ đầu đến cuối.
- Job có `start_stage` và `target_stage`.
- Core hiện có primitive `Orchestrator.submit/plan/set_target`.
- Stage skip nếu artifact + validator + stage key vẫn hợp lệ.
- Output của user là tài sản người dùng; cleanup không được tự xóa output.
- Job snapshot config để reproducible.
- TTS stage độc lập với watermark.
- Watermark là Channel Asset và chỉ ảnh hưởng nhánh audio YouTube/downstream tương ứng.
- `AudioProcessor.build_youtube_audio(master, watermark, ...)` chỉ nhận audio file, không biết TTS text.
- TTS framework đã có profile/resolver/adapter/manager/cache/QA.
- UI hiện là local web app có facade service, API JSON, frontend ES module, GSAP vendor.

Nếu code hiện tại đã thay đổi so với tài liệu: **theo code hiện tại**, nhưng giữ các invariant chức năng trong spec này.

---

# PHẦN A — WATERMARK ASSET LIFECYCLE

# 5. WATERMARK KHÔNG ĐƯỢC LÀ FILE TẠM BỊ BỎ QUÊN

Không triển khai kiểu:

```text
Generate TTS
→ channels/<id>/watermark.wav
→ xong
```

Watermark tạo ra phải có chỗ quản lý lâu dài.

Thiết kế semantic mong muốn:

```text
Channel
  └── Watermark Library
        ├── Watermark A
        │     ├── revision 1
        │     └── revision 2
        ├── Watermark B
        └── Watermark C

Channel.active_watermark -> Watermark + revision cụ thể
```

Không nhất thiết schema/file tree đúng như trên, nhưng behavior phải tương đương.

---

# 6. WATERMARK LIBRARY

Trong màn hình **Kênh → Watermark**, user phải quản lý được watermark đã upload/generated.

Tối thiểu cần:

- danh sách watermark;
- tên watermark;
- nguồn: `TTS` / `Upload` / `Legacy`;
- trạng thái đang dùng;
- duration;
- ngày tạo/cập nhật nếu có;
- TTS profile/voice/engine summary nếu là TTS;
- audio preview;
- chọn làm watermark đang dùng;
- sửa;
- generate lại/thay file;
- xóa hoặc lưu trữ hợp lý.

Không hiển thị raw filesystem path như dữ liệu chính dành cho user.

---

# 7. WATERMARK ENTITY VÀ REVISION

Mỗi Watermark phải có identity ổn định, tách khỏi revision audio.

Semantic gợi ý:

```json
{
  "id": "wm_xxx",
  "name": "Intro Truyện Đêm",
  "source": "tts",
  "current_revision": 3,
  "created_at": "...",
  "updated_at": "..."
}
```

Mỗi revision phải đủ metadata để tái hiện và debug:

```json
{
  "revision": 3,
  "audio_path": "...",
  "sha256": "...",
  "duration_sec": 8.2,
  "source": "tts",
  "tts": {
    "text": "Bạn đang nghe truyện tại...",
    "profile": "...",
    "engine": "...",
    "voice": "...",
    "model": "...",
    "settings": {}
  },
  "fingerprint": "...",
  "created_at": "..."
}
```

Đây là semantic reference, không bắt buộc schema y hệt.

Agent phải chọn schema phù hợp nhất với codebase hiện tại.

## 7.1. Quy tắc revision

- Revision đã được tạo thành công là **immutable**.
- Sửa text/TTS/thay file → revision mới.
- Không overwrite audio revision cũ.
- Nếu tạo revision mới lỗi → revision active cũ vẫn nguyên vẹn.
- Không để job cũ bị đổi watermark sau khi Channel đổi.

---

# 8. STORAGE

Ưu tiên đặt Watermark data trong vùng Channel/ContentFactory hiện có.

File tree gợi ý:

```text
channels/
  channel_a/
    channel.json
    watermarks/
      registry.json
      wm_xxx/
        rev_0001.wav
        rev_0001.json
        rev_0002.wav
        rev_0002.json
```

Nếu project có registry/asset store abstraction tốt hơn thì dùng nó.

Bắt buộc:

- relative path;
- portable giữa máy theo nguyên tắc project hiện tại;
- atomic metadata write;
- audio write `*.part` rồi finalize;
- không chứa secret;
- restart app không mất library;
- corrupted/incomplete `.part` không được xem là revision hợp lệ.

---

# 9. BACKWARD COMPATIBILITY WATERMARK CŨ

Channel hiện tại có thể có:

```json
{
  "watermark": "watermark.wav"
}
```

Không được phá.

Behavior bắt buộc:

- Channel cũ load bình thường.
- Pipeline cũ vẫn resolve được watermark.
- UI vẫn preview được watermark legacy nếu file còn hợp lệ.
- Có thể lazy-adopt/register vào Library nếu phù hợp, nhưng không ép migration thủ công.
- Không đổi một loạt channel config chỉ để thỏa schema mới nếu không cần.

---

# 10. ACTIVE WATERMARK CỦA CHANNEL

Channel phải biết watermark/revision nào đang active.

Có thể dùng semantic dạng:

```json
{
  "watermark": "watermarks/wm_xxx/rev_0003.wav",
  "watermark_ref": {
    "id": "wm_xxx",
    "revision": 3
  }
}
```

hoặc giải pháp tương đương.

Điểm quan trọng:

- Audio pipeline vẫn resolve thành audio path + hash/fingerprint.
- `AudioProcessor` không phải hiểu registry.
- Active selection là explicit.
- User có thể đổi active watermark mà không xóa watermark cũ.

---

# 11. TẠO WATERMARK BẰNG TTS

UI tạo mới tối thiểu:

```text
Tên watermark
[ Intro Truyện Đêm ]

Nguồn
(•) Tạo bằng TTS
( ) Tải file audio

Nội dung watermark
[ Bạn đang nghe truyện tại Kênh ABC... ]

Giọng đọc / TTS
[ Auto ▼ ]

[Tạo watermark]
```

## 11.1. Nội dung watermark

- Text user nhập là nguồn nội dung duy nhất.
- Trim whitespace đầu/cuối.
- Không tự thêm câu quảng cáo.
- Không lấy story/title/description để bù text.
- Empty → validation error rõ ràng bằng tiếng Việt.
- Persist source text để user mở lại/sửa.

## 11.2. TTS phải reuse hệ thống hiện tại

Watermark TTS phải dùng **cùng abstraction TTS hiện có** với narration:

- profile resolver;
- Auto selection;
- engine;
- voice;
- model;
- language;
- relevant settings;
- credential provider;
- capabilities;
- retry/error mapping;
- audio QA;
- cache semantics.

Không được tạo:

```text
WATERMARK_VOICES = [...]
```

Không duplicate adapter.

Không duplicate provider credential logic.

Không tạo TTS framework thứ hai.

Nếu narration UI có reusable selector/model/service, refactor thành shared component/helper và dùng cả hai nơi.

## 11.3. Short text synthesis

Watermark không cần giả lập full Story Job.

Nếu TTS core hiện tại chỉ expose whole-story pipeline, refactor lớp thích hợp để có shared primitive kiểu:

```text
TTS Core
  ├── Story synthesis
  │      └── planner/chunks/assemble
  └── Short text synthesis
         └── Watermark
```

Nhưng vẫn dùng chung adapter/profile/cache/QA.

Nếu watermark text vượt engine limit, reuse planner/segmenter hiện tại.

---

# 12. AUTO TTS

Nếu user chọn `Auto`:

- dùng cùng resolver/selection logic với narration;
- không tạo heuristic riêng cho watermark;
- response/metadata phải cho UI biết cuối cùng engine/profile/voice nào đã được chọn nếu hệ thống hiện tại có thông tin đó;
- fake TTS trong development/test vẫn phải dùng được theo convention hiện tại.

---

# 13. OUTPUT AUDIO WATERMARK

Ưu tiên WAV lossless tương thích pipeline audio hiện tại.

Generated audio phải:

- decode được;
- duration > 0;
- không rỗng;
- không corrupt;
- đi qua validation/QA phù hợp;
- tương thích sample rate/channel conversion hiện tại;
- không master/normalize sai hai lần nếu `AudioProcessor.build_youtube_audio()` đã có logic match loudness/fade/trim watermark.

Watermark generator chỉ chịu trách nhiệm tạo **audio asset hợp lệ**.

`AudioProcessor` tiếp tục chỉ làm:

```text
narration master + watermark audio asset -> youtube audio
```

---

# 14. UPLOAD WATERMARK

Upload hiện tại phải giữ.

Nhưng upload mới phải đi vào cùng Watermark Library abstraction:

```text
upload
→ validate
→ create Watermark/revision
→ persist registry
→ optionally set active
```

Không tiếp tục overwrite một `watermark.wav` mutable nếu làm mất version/reference semantics.

Watermark upload hỗ trợ tối thiểu:

- đổi tên;
- preview;
- chọn active;
- thay file → revision mới;
- delete/archive theo reference rules.

Endpoint cũ nếu đang được dùng phải giữ compatibility và route vào service mới nếu hợp lý.

---

# 15. SỬA WATERMARK

Đã có Create thì phải có Update.

## 15.1. TTS watermark

Cho sửa:

- tên;
- text;
- TTS selection/profile;
- voice/model/settings nếu đúng abstraction UI hiện tại.

Action nên rõ nghĩa, ví dụ:

```text
[Lưu & tạo bản mới]
```

Behavior:

```text
wm_a@v1
  ↓ sửa text/voice
wm_a@v2
```

Nếu `wm_a@v1` đang active và v2 generate thành công → chuyển active sang v2 nếu flow update đó có ý nghĩa “cập nhật watermark đang dùng”.

Nếu generate lỗi → active vẫn v1.

## 15.2. Upload watermark

- sửa tên không cần regenerate audio nếu chỉ là metadata;
- thay file → revision mới;
- revision cũ không mutate.

---

# 16. REGENERATE VÀ CACHE

Watermark TTS có chức năng `Tạo lại`/`Regenerate`.

Fingerprint/cache tối thiểu phụ thuộc:

```text
normalized text
+ engine
+ model
+ voice
+ relevant TTS settings
+ profile version
+ engine version nếu TTS hiện tại dùng
```

Reuse cache key logic hiện có tối đa.

Nếu semantic input không đổi và asset/cache hợp lệ còn tồn tại:

- không gọi provider lại không cần thiết;
- không phát sinh chi phí API vô ích.

Nếu text/voice/profile/model/settings ảnh hưởng audio đổi → fingerprint mới.

---

# 17. PREVIEW

Watermark Library và editor phải cho nghe thử.

Reuse native `<audio controls>` hoặc component hiện tại.

Tối thiểu hiển thị:

- tên;
- source;
- duration;
- TTS summary nếu có;
- player;
- active badge/status.

Preview endpoint không được cho phép đọc path tùy ý ngoài vùng asset được quản lý.

---

# 18. DELETE / ARCHIVE WATERMARK

Không xóa mù quáng.

## 18.1. Watermark không active và không referenced

Có thể physical delete hoặc archive theo storage convention hiện tại.

## 18.2. Watermark đang active

Không xóa trực tiếp mà để Channel trỏ vào dangling reference.

UI phải yêu cầu một trong:

- bỏ watermark khỏi Channel;
- chọn watermark khác;
- hoặc operation xóa thực hiện transaction unset active rõ ràng nếu user xác nhận.

## 18.3. Revision đang được job snapshot

Không physical delete revision đó.

Quy tắc:

```text
referenced revision = immutable + retained
```

Có thể archive/hide Watermark khỏi danh sách mặc định nhưng artifact referenced phải còn để job cũ reproducible.

---

# 19. WATERMARK JOB SNAPSHOT

Khi job được tạo, phải snapshot watermark đủ để job không bị silent mutation.

Tối thiểu:

```text
watermark identity/revision
resolved audio reference/path
sha256 hoặc immutable fingerprint
```

Ví dụ:

```text
job cũ -> wm_a@v1
Channel đổi -> wm_a@v2
job cũ vẫn -> wm_a@v1
job mới -> wm_a@v2
```

Không tự apply watermark mới cho job đã tồn tại.

Nếu sau này muốn “áp dụng watermark mới vào job” thì là một explicit feature khác, không làm ngầm ở task này.

---

# 20. FIX STAGE KEY / INVALIDATION WATERMARK

Tài liệu hiện ghi nhận limitation: stage key của watermark từng dựa vào path thay vì content.

Task này phải xử lý triệt để cho flow mới.

Không được dựa duy nhất vào:

```text
channels/a/watermark.wav
```

Mà phải phụ thuộc một immutable semantic như:

- SHA256 audio content;
- revision fingerprint;
- asset content hash;
- hoặc equivalent chắc chắn.

Hai watermark khác nội dung phải tạo dependency khác nhau dù path logic giống nhau.

Khi đổi watermark:

KHÔNG chạy lại:

- Source;
- Story;
- narration TTS;
- các bước không phụ thuộc watermark;
- TikTok nếu TikTok hiện không dùng watermark.

Chỉ invalidate/rerun đúng downstream phụ thuộc YouTube watermark theo pipeline thực tế.

Bên trong stage audio, tiếp tục reuse Narration Master/TikTok nếu code hiện tại hỗ trợ cache trung gian đó.

---

# 21. WATERMARK API / SERVICE

Không bắt buộc endpoint đúng tên dưới đây. Hãy theo convention hiện có.

Semantic operations tối thiểu:

```text
List watermarks of channel
Get watermark/revisions
Create by TTS
Create by upload
Update metadata
Create new revision by TTS
Create new revision by replacing upload
Set active watermark
Unset active watermark
Delete/archive watermark
Preview/read managed audio safely
```

Ví dụ REST có thể là:

```text
GET    /api/channels/<id>/watermarks
POST   /api/channels/<id>/watermarks
GET    /api/channels/<id>/watermarks/<wm_id>
PUT    /api/channels/<id>/watermarks/<wm_id>
POST   /api/channels/<id>/watermarks/<wm_id>/regenerate
POST   /api/channels/<id>/watermarks/<wm_id>/activate
DELETE /api/channels/<id>/watermarks/<wm_id>
```

Nhưng agent phải inspect router hiện tại và chọn shape ít phá kiến trúc nhất.

API error phải theo envelope hiện có, không stack trace ra frontend.

Các error semantic cần cover tương đương:

```text
WATERMARK_TEXT_EMPTY
WATERMARK_NOT_FOUND
WATERMARK_REVISION_NOT_FOUND
WATERMARK_IN_USE
WATERMARK_AUDIO_INVALID
TTS_PROFILE_NOT_FOUND
TTS_UNAVAILABLE
TTS_GENERATION_FAILED
```

Tên code có thể khác nếu taxonomy hiện tại tốt hơn.

---

# 22. WATERMARK UI/UX CHỈN CHU

Dùng UI Pro Max + GASP/GSAP skill local để thiết kế.

## 22.1. Information architecture

Không nhồi toàn bộ setting vào một khối.

Ưu tiên:

```text
Watermark đang dùng

[Active Watermark Card + preview + sửa]

Thư viện Watermark
[Search/filter nếu thật sự cần]
[+ Tạo watermark]

[Watermark cards/list]
```

Create/Edit có thể là modal/drawer/page tùy pattern hiện tại.

## 22.2. Progressive disclosure

Mặc định chỉ hiện:

- tên;
- nguồn;
- nội dung;
- giọng đọc/profile;
- action chính.

Engine-specific advanced setting chỉ mở khi UI TTS hiện tại cũng expose và user cần.

## 22.3. Interaction states

Phải thiết kế:

- empty library;
- loading;
- generating;
- success;
- validation error;
- provider error;
- stale/deleted asset;
- active;
- archived nếu dùng archive;
- disabled action với lý do rõ.

## 22.4. Generate animation

Khi generate:

- button disabled;
- chống double submit;
- text `Đang tạo watermark…`;
- progress thật nếu backend có;
- card/result reveal mượt khi xong;
- error state chuyển nhẹ, không rung/chớp quá mức;
- reduced motion phải hoạt động.

## 22.5. Microcopy

Dùng tiếng Việt rõ nghĩa.

Không dùng thuật ngữ nội bộ kiểu `ArtifactRef`, `stage_key`, `fingerprint` trong primary UI.

Thông tin kỹ thuật có thể nằm trong vùng `Nâng cao`/`Chi tiết kỹ thuật`.

---

# PHẦN B — SỬA JOB

# 23. MỤC TIÊU

Trong **Chi tiết Job**, thêm một entry point rõ ràng:

```text
[Sửa job]
```

Màn hình/dialog/drawer `Sửa job` tối thiểu có:

```text
Pipeline
Danger zone
```

Operations trong scope:

1. **Cập nhật Pipeline**.
2. **Xóa Job**.

Không mở quyền sửa tùy tiện mọi field snapshot/config của job.

`start_stage` sau khi job tạo được coi là immutable trong task này.

---

# 24. LOẠI BỎ FLOW “CẬP NHẬT PIPELINE” CŨ

Nếu UI/backend hiện tại đã có chức năng cập nhật pipeline cũ hoặc action trùng:

- loại bỏ UX cũ;
- không để hai nút làm cùng business operation;
- không để hai service implementations độc lập cùng sửa target;
- migration callers/test sang canonical operation mới.

**Nhưng không xóa core primitive tốt đang có.**

Tài liệu hiện nói core đã có:

```text
Orchestrator.set_target
```

Hãy inspect implementation thật và reuse/refactor/strengthen nó.

Mục tiêu:

```text
UI / API / CLI (nếu expose)
        ↓
ONE canonical service/business operation
        ↓
Orchestrator target-stage semantics
```

Không tạo một “pipeline_update_service” song song nếu `set_target` đã đúng chỗ.

---

# 25. SEMANTICS CẬP NHẬT PIPELINE — QUY TẮC QUAN TRỌNG

`Cập nhật Pipeline` trong task này thực chất là cập nhật **đích chạy (`target_stage`)**.

Không rewrite lịch sử pipeline.

Không rollback stage đã đi qua.

Không thay `start_stage`.

## 25.1. Progress floor

Hãy tính một **progress floor** dựa trên pipeline/order/state thực tế.

Semantic:

```text
progress_floor = stage xa nhất mà job đã bắt đầu hoặc đã hoàn thành hợp lệ
```

Cần inspect state machine để chọn cách tính chính xác, tránh dựa vào string name.

Nếu job đang chạy stage A:

```text
new_target >= A
```

Nếu job đã hoàn thành A và chưa bắt đầu B:

```text
new_target >= A
```

Nếu job đã hoàn thành tới D:

```text
new_target >= D
```

User **không được đặt target về stage đứng trước progress_floor**.

Ví dụ:

```text
Source -> Story -> TTS -> Audio -> RenderYT -> RenderTT -> Output -> Publish
                         ^
                     đã tới Audio
```

Không cho chọn:

```text
Source
Story
TTS
```

Cho chọn:

```text
Audio
RenderYT
RenderTT
Output
Publish
```

Tùy pipeline thật và branch/dependency, phải dùng planner/order abstraction hiện tại, không hardcode array duplicated ở UI.

---

# 26. CÓ THỂ RÚT NGẮN TARGET SO VỚI TARGET CŨ, NHƯNG KHÔNG ĐƯỢC LÙI QUA TIẾN ĐỘ

Ví dụ:

```text
current running stage = Story
a target cũ = Publish
new target = Audio
```

Được phép, vì Audio nằm sau Story.

Job sẽ tiếp tục đến Audio rồi dừng hoàn tất ở target mới.

Nhưng:

```text
current running stage = Audio
new target = Story
```

không được.

Điều này quan trọng: “không cập nhật lùi pipeline” nghĩa là **không đặt target trước tiến độ thực tế**, không nhất thiết là target mới luôn phải >= target cũ.

---

# 27. JOB ĐANG CHẠY KHI CẬP NHẬT TARGET

Nếu job đang chạy và user đổi target hợp lệ:

- không kill stage hiện tại;
- không restart từ đầu;
- không xóa artifact;
- atomic update target;
- runner đọc target mới tại boundary an toàn;
- job tiếp tục đến target sau cập nhật;
- nếu target mới chính là stage đang chạy, hoàn tất stage hiện tại rồi dừng ở trạng thái done tương ứng;
- nếu target mới xa hơn, chạy tiếp bình thường.

UI phải nói rõ trước khi lưu, ví dụ semantic:

```text
Job hiện đang ở “Giọng đọc”.
Sau cập nhật, job sẽ tiếp tục đến “Audio” rồi dừng.
```

Không cần đúng câu chữ, nhưng user phải hiểu effect.

---

# 28. JOB ĐÃ HOÀN TẤT KHI CẬP NHẬT TARGET

Ví dụ job trước đó hoàn tất ở:

```text
Audio
```

User sửa target thành:

```text
Output
```

Behavior:

- chỉ cập nhật plan/target;
- **không tự chạy ngay chỉ vì bấm Lưu**;
- job hiển thị rằng có pipeline mới chưa thực thi;
- khi user bấm `Chạy tiếp` / action tương đương, runner tiếp tục từ stage kế tiếp cần thiết;
- reuse artifact hợp lệ;
- không chạy lại Source/Story/TTS/Audio nếu không cần;
- chạy đến target mới.

Nếu job đã hoàn tất tới Output, không cho update target về Audio/Story/etc.

Nếu target mới == progress floor hiện tại → no-op hợp lệ hoặc UI disable Save tùy convention, nhưng không được tạo side effect kỳ lạ.

---

# 29. JOB PAUSED / FAILED / QUEUED

Phải định nghĩa behavior và test.

## Queued chưa bắt đầu

Progress floor có thể là `start_stage`/chưa stage nào chạy theo planner thật.

Cho phép chọn target hợp lệ miễn planner spec hợp lệ.

## Paused/Held

- update target không tự release hold;
- target mới phải không trước progress floor;
- sau khi nguyên nhân hold được xử lý và Resume, job chạy theo target mới.

## Failed

- update target không tự giả vờ failure đã được giải quyết;
- không xóa failed stage/history;
- Retry/Resume semantics hiện có vẫn được giữ;
- target mới không được trước progress floor/failed stage nếu điều đó làm lịch sử vô nghĩa;
- nếu failed stage nằm sau target mới nhưng stage đó đã thực sự bắt đầu, không cho target lùi trước nó.

---

# 30. VALIDATION PHẢI Ở BACKEND

UI disable stage lùi chỉ là UX.

Backend là authority.

Canonical operation phải validate:

- job tồn tại;
- target stage tồn tại;
- target thuộc plan/pipeline hợp lệ;
- target không trước progress floor;
- transition không vi phạm `start_stage <= target_stage`;
- race condition với runner;
- current state cho phép update;
- optimistic concurrency/CAS nếu DB layer hiện có hỗ trợ.

Nếu UI gửi target invalid → API trả lỗi domain rõ ràng.

Ví dụ semantic:

```text
PIPELINE_TARGET_BEFORE_PROGRESS
PIPELINE_TARGET_INVALID
JOB_NOT_FOUND
JOB_UPDATE_CONFLICT
```

Tên thật theo taxonomy codebase.

---

# 31. SNAPSHOT / MANIFEST KHI UPDATE TARGET

Hiện job config snapshot có `start_stage/target_stage` theo tài liệu.

Agent phải inspect implementation để đảm bảo update target không tạo inconsistency giữa:

- jobs table;
- job params/spec;
- config snapshot;
- manifest;
- detail API;
- UI pipeline calculation;
- runner/planner.

Sau update, mọi nơi đọc target phải thấy cùng một giá trị authoritative.

Nếu snapshot được coi immutable cho phần semantic khác, chỉ update field planning được phép theo explicit operation này và ghi provenance/audit phù hợp.

Nên lưu tối thiểu audit info nếu kiến trúc hiện có hỗ trợ:

```text
old_target
new_target
updated_at
```

Không cần xây audit subsystem lớn nếu chưa có.

---

# 32. UI CẬP NHẬT PIPELINE

Dùng UI Pro Max + GASP/GSAP skill.

## 32.1. Không bắt user hiểu stage code

Hiển thị label thân thiện:

```text
Phụ đề
Truyện
Giọng đọc
Audio
Video YouTube
Video TikTok
Đóng gói
Đăng YouTube
```

Theo pipeline thật.

Technical stage id chỉ ở advanced/debug nếu cần.

## 32.2. Pipeline selector có context

Không chỉ dùng dropdown thô.

Ưu tiên một stepper/vertical timeline/segmented list thể hiện:

- đã hoàn thành;
- đang chạy;
- target hiện tại;
- có thể chọn;
- không thể chọn vì đã ở phía trước lịch sử;
- target mới preview.

Các stage trước progress floor:

- disabled;
- vẫn nhìn thấy;
- có lý do rõ khi hover/focus/nearby helper text.

Ví dụ:

```text
✓ Phụ đề          Đã hoàn thành
✓ Truyện          Đã hoàn thành
● Giọng đọc       Đang chạy
○ Audio           Có thể chọn
○ Video YouTube   Có thể chọn
○ Video TikTok    Có thể chọn
○ Đóng gói        Có thể chọn
○ Đăng YouTube    Có thể chọn
```

## 32.3. Preview effect trước Save

Khi user chọn target mới, UI mô tả hậu quả:

### Job đang chạy

```text
Sau khi lưu, job sẽ tiếp tục từ bước hiện tại đến “Video YouTube” rồi dừng.
```

### Job đã hoàn thành ở target cũ

```text
Pipeline mới sẽ được lưu. Job sẽ không tự chạy.
Bấm “Chạy tiếp” sau đó để hoàn tất đến “Đóng gói”.
```

## 32.4. Animation

- mở editor/drawer mượt;
- target indicator transition bằng transform/opacity;
- disabled stages không bounce/shake khó chịu;
- save success feedback nhẹ;
- pipeline detail hiện tại update không nhấp nháy toàn view;
- reduced motion đầy đủ.

---

# 33. XÓA JOB

`Sửa job` phải có **Danger zone → Xóa Job**.

Đây phải là chức năng thật, không chỉ xóa row UI.

## 33.1. Nguyên tắc dữ liệu

- Không bao giờ xóa `output/` của user chỉ vì xóa job khỏi ContentFactory.
- Output đã tạo là tài sản user.
- Không làm hỏng job khác đang reuse/reference artifact nếu có.
- Không để worker tiếp tục ghi vào record đã bị physical delete.

## 33.2. Job không chạy

Với queued/paused/failed/completed không có worker active:

- xác nhận xóa;
- xóa/soft-delete job theo DB architecture phù hợp;
- dọn internal workspace chỉ nếu an toàn và theo cleanup semantics hiện có;
- output ngoài vùng internal giữ nguyên;
- references phải được xử lý an toàn.

Ưu tiên soft-delete/tombstone nếu hard delete làm mất referential integrity/history đang được job khác dùng.

## 33.3. Job đang chạy

Không được hard-delete DB row trong khi runner còn giữ lease/đang chạy stage.

Inspect primitives hiện tại (`CancelToken`, lease/runner/state). Nếu chưa có public cancel, implement **minimal safe deletion lifecycle** phù hợp architecture:

Semantic mong muốn:

```text
User bấm Xóa
→ mark delete/cancel requested atomically
→ signal cooperative cancellation
→ runner/stage dừng ở điểm an toàn
→ release lease
→ finalize deleted/tombstoned state
→ UI loại job khỏi danh sách mặc định
```

Không cần xây full pause/rerun framework ngoài scope, nhưng phải đủ để delete running job an toàn.

Nếu một external subprocess cần terminate, dùng mechanism hiện có và đảm bảo cleanup/timeout.

Không giết tiến trình một cách có thể corrupt artifact finalized.

`*.part` có thể dọn; artifact checkpoint hợp lệ đã hoàn tất không được biến thành file nửa chừng.

## 33.4. Delete confirmation UX

Không dùng browser `confirm()` nếu app hiện có modal pattern tốt hơn.

Dialog phải nói rõ:

- tên/job title;
- trạng thái hiện tại;
- job đang chạy sẽ bị dừng nếu applicable;
- **thư mục output đã tạo sẽ không bị xóa**;
- action destructive rõ.

Có thể yêu cầu confirm lần hai cho running job nếu UI Pro Max guideline khuyến nghị, nhưng không tạo friction thừa cho completed job.

Keyboard/focus trap/ESC/return focus phải đúng accessibility pattern hiện có.

---

# 34. API JOB EDIT

Theo convention hiện tại, có thể thiết kế semantic dạng:

```text
PUT/POST /api/jobs/<id>/target
DELETE   /api/jobs/<id>
```

hoặc gộp vào endpoint edit nếu codebase hợp lý hơn.

Không bắt buộc URL cụ thể.

Yêu cầu:

- API gọi canonical service/core operation;
- không business logic duplicated trong route;
- idempotent khi request lặp hợp lý;
- error envelope hiện tại;
- Vietnamese user-facing message/hint;
- race-safe;
- request đang xử lý phải disable ở UI.

---

# 35. JOB LIST / DETAIL SAU KHI SỬA

Sau update target:

- pipeline detail phải phản ánh target mới ngay;
- `not_planned` stages tính theo target mới;
- completed artifact cũ vẫn shown đúng;
- không reset progress history;
- nếu completed job được mở rộng target, UI phải có trạng thái/action hợp lý kiểu `Có bước mới để chạy` / `Chạy tiếp` theo design của project;
- polling/version change phải làm UI nhận update mà không reload toàn trang.

Sau delete:

- job biến mất khỏi list mặc định;
- route detail của job đã xóa xử lý đẹp (not found/deleted state), không blank screen;
- polling không resurrect job.

---

# 36. KHÔNG SỬA JOB THEO CÁCH PHÁ REPRODUCIBILITY

Trong scope này không cho user chỉnh trực tiếp:

- input source;
- story text snapshot;
- narration profile đã chạy;
- watermark snapshot của job;
- template snapshot;
- channel snapshot;
- `start_stage`;
- artifact hash.

Nếu UI cũ đang cho sửa những field nguy hiểm trong cùng flow update pipeline, tách/loại bỏ khỏi operation này.

Task này chỉ cho thay **đích pipeline tương lai** và xóa job.

---

# 37. DATA MIGRATION / COMPATIBILITY

Mọi schema mới phải có strategy cho dữ liệu cũ.

Bắt buộc test:

- DB cũ mở được;
- channel cũ load được;
- watermark cũ load được;
- job cũ không có field mới vẫn hiện đúng;
- app restart sau migration vẫn chạy;
- migration idempotent nếu project dùng migration versioning.

Không bắt user xóa DB/config để feature hoạt động.

---

# 38. CONCURRENCY / ATOMICITY

Phải audit race condition tối thiểu:

## Watermark

- double click Generate;
- hai generate cùng watermark;
- activate trong lúc generate;
- delete watermark trong lúc đang active;
- update revision thất bại giữa chừng;
- process crash sau audio finalize nhưng trước registry write;
- process crash sau registry write nhưng trước active update.

Dùng lock/idempotency/atomic rename/transaction theo abstraction hiện có.

## Job

- target update đúng lúc runner chuyển stage;
- target update cùng lúc resume/retry;
- delete cùng lúc runner claim lease;
- delete request lặp;
- browser retry request;
- completed job mở rộng target đúng lúc list poll.

Backend phải là authority.

---

# 39. SECURITY

Giữ toàn bộ security model hiện tại:

- localhost bind;
- Host check;
- per-session token;
- Origin check cho write;
- CSP;
- không arbitrary file read;
- watermark preview chỉ phục vụ managed asset;
- upload validation;
- path traversal protection;
- sanitize filename/ID;
- không secret trong JSON trả frontend;
- không stack trace ra UI;
- không log raw credential.

Watermark text là user input → render bằng safe text APIs.

---

# 40. TEST PLAN BẮT BUỘC — WATERMARK

Viết test theo testing architecture hiện tại.

Tối thiểu phải cover:

## 40.1. Legacy

- Channel `{ "watermark": "watermark.wav" }` vẫn load.
- Existing legacy watermark vẫn dùng trong audio stage.

## 40.2. Library CRUD

- create metadata;
- list;
- get;
- activate;
- update name;
- archive/delete;
- restart/reload persistence.

## 40.3. Upload

- upload valid audio tạo revision;
- invalid audio rejected;
- replace file tạo revision mới;
- revision cũ không mutate.

## 40.4. TTS generate

- text → TTS adapter → valid WAV → registry/revision.
- adapter nhận đúng normalized text.
- selected profile được dùng đúng.
- Auto đi qua resolver hiện tại.
- fake TTS hoạt động trong test.

## 40.5. Failure atomicity

Nếu TTS fail:

- không create broken active revision;
- watermark active cũ còn;
- `.part` không được xem là asset valid;
- metadata không trỏ file thiếu.

## 40.6. Cache/fingerprint

- cùng semantic TTS config reuse hợp lý;
- đổi text → fingerprint/revision mới;
- đổi voice/profile/model/relevant setting → fingerprint mới.

## 40.7. Job snapshot

- job cũ snapshot wm@v1;
- channel đổi wm@v2;
- job cũ vẫn v1;
- job mới v2.

## 40.8. Invalidation

- watermark content/hash đổi → audio YouTube/downstream relevant rerun;
- TTS narration không rerun;
- Story không rerun;
- TikTok không rerun nếu dependency graph hiện tại không dùng watermark;
- same path + changed content vẫn được phát hiện nếu legacy flow còn cho phép.

## 40.9. Reference protection

- không physical delete revision đang được job snapshot;
- active watermark delete được xử lý đúng.

---

# 41. TEST PLAN BẮT BUỘC — JOB EDIT

## 41.1. Update queued job

- target hợp lệ cập nhật được;
- target invalid bị reject;
- planner/detail phản ánh target mới.

## 41.2. Running job mở rộng target

Ví dụ current=TTS, old target=Audio, new target=Output:

- current stage không restart;
- job chạy tiếp tới Output;
- artifact trước đó reuse.

## 41.3. Running job rút target nhưng vẫn >= progress

Ví dụ current=Story, old target=Publish, new target=Audio:

- update allowed;
- finish Story rồi chạy tới Audio;
- dừng ở Audio.

## 41.4. Không cho rollback target trước progress

Ví dụ current/completed=Audio:

- set target Story → reject;
- DB không đổi;
- UI nhận domain error phù hợp.

## 41.5. Running current target

current=Audio, set target=Audio:

- không kill stage;
- hoàn tất Audio rồi dừng.

## 41.6. Completed job mở rộng target

completed at Audio, new target=Output:

- Save không tự run;
- detail cho biết plan mới;
- khi user `Chạy tiếp`, chỉ chạy stage cần thiết Audio+1 → Output;
- không rerun Story/TTS/Audio nếu artifact valid.

## 41.7. Completed job lùi target

completed at Output, set target=Audio → reject.

## 41.8. Held job

- update target được nếu hợp lệ;
- hold không tự release;
- resume sau đó theo target mới.

## 41.9. Failed job

- update không xóa failure history;
- invalid backward target reject;
- retry semantics còn hoạt động.

## 41.10. Concurrency

- target update tại stage transition không corrupt state;
- repeated request idempotent;
- stale update conflict được xử lý hợp lý nếu có version/CAS.

## 41.11. Delete completed/paused/failed

- job bị remove/soft-delete đúng;
- output không bị xóa;
- references không bị hỏng.

## 41.12. Delete running

- request cancel/delete an toàn;
- runner không viết vào record đã xóa;
- lease release;
- part file cleanup hợp lý;
- output finalized trước đó không bị xóa.

---

# 42. UI TEST / QA BẮT BUỘC

Dùng pattern browser/UI test hiện có.

Phải test tối thiểu:

## Watermark UI

- empty state;
- create TTS;
- validation text empty;
- loading/generating;
- success + preview;
- edit/regenerate;
- activate another watermark;
- upload flow còn chạy;
- delete/archive;
- referenced/active delete warning;
- keyboard navigation;
- focus;
- light/dark;
- reduced motion.

## Job UI

- mở `Sửa job`;
- stage trước progress bị disabled;
- stage sau progress chọn được;
- effect text thay đổi đúng theo running/completed;
- save target;
- detail pipeline update;
- completed expanded target có `Chạy tiếp` hợp lý;
- delete confirmation;
- running delete warning;
- keyboard/focus/ESC;
- reduced motion.

## Regression

- RUN flow cũ;
- Job list cũ;
- Retry/Resume;
- Channel editor;
- TTS settings;
- Template Studio entry;
- audio pipeline.

Nếu repo có axe/a11y suite, không được làm tăng serious/critical violations.

---

# 43. VISUAL / UX QA BẰNG LOCAL SKILLS

Sau khi frontend chạy:

1. Dùng workflow của **UI Pro Max** để review hierarchy, spacing, typography, affordance, destructive action, form state, error state, responsive behavior.
2. Dùng workflow của **GASP/GSAP skill** để review animation implementation, cleanup, reduced-motion và performance.
3. Chạy UI ở các viewport/theme mà project hiện dùng cho regression.
4. Fix các vấn đề phát hiện được, không chỉ ghi chú.
5. Không kết thúc nếu modal bị overflow, focus sai, text vỡ, nút nhảy layout, player tràn khung, animation orphan hoặc reduced-motion không hoạt động.

---

# 44. PERFORMANCE

Không làm UI nặng đáng kể.

- Không thêm framework lớn chỉ cho modal/card.
- Không thêm CDN.
- Không polling watermark library vô tội vạ.
- Reuse jobs polling/version mechanism nếu phù hợp.
- Audio preview không preload toàn bộ library nếu không cần (`preload=metadata` hoặc pattern hiện có).
- Không re-render cả list khi chỉ progress nhỏ đổi nếu frontend architecture cho phép update mục tiêu.
- GSAP animation không gây layout thrash.

Nếu project có performance budget/test, giữ pass.

---

# 45. DOCUMENTATION CẦN CẬP NHẬT

Sau implementation, cập nhật tài liệu liên quan để khớp code.

Tối thiểu xem xét:

- `README.md`
- `HANDOFF.md`
- `docs/MODULE_CONTRACTS.md`
- `docs/DECISIONS.md`
- `docs/UI_GUIDE.md`
- schema/API docs khác nếu có.

Nội dung phải phản ánh:

## Watermark

```text
Watermark là Channel Asset có library/revision.
Có thể upload hoặc generate bằng TTS.
TTS watermark reuse TTS framework.
Job snapshot revision bất biến.
Đổi watermark không TTS lại story.
Stage key dùng content/revision fingerprint đúng.
```

## Job edit

```text
Job cho phép cập nhật target_stage theo progress floor.
Không cho rollback target trước stage đã bắt đầu/hoàn thành.
Running job áp target mới ở boundary an toàn.
Completed job lưu target mới và chỉ chạy tiếp khi user yêu cầu.
Xóa job không xóa output user.
```

Nếu implementation khác spec ở chi tiết kỹ thuật vì code hiện tại yêu cầu, document rationale rõ.

---

# 46. KHÔNG ĐƯỢC LÀM

Không:

- tạo Watermark TTS adapter riêng;
- hardcode voice/profile/engine;
- duplicate TTS selector business logic;
- đưa watermark text vào story;
- làm TTS narration lại khi watermark đổi;
- cho watermark generated biến mất sau restart;
- chỉ lưu path mà không có identity/hash đủ cho reproducibility;
- overwrite revision đang được job tham chiếu;
- xóa watermark referenced mà làm job cũ hỏng;
- tạo API arbitrary file reader;
- tạo pipeline ordering array duplicated giữa backend/frontend nếu core đã có planner metadata;
- cho frontend tự quyết định target update hợp lệ mà backend không validate;
- reset job history khi đổi target;
- đổi `start_stage` của existing job trong feature này;
- tự chạy completed job ngay sau khi chỉ Save target mới;
- hard delete running job record trước khi worker/lease dừng;
- xóa output khi xóa job;
- để hai chức năng “Cập nhật Pipeline” cũ/mới cùng tồn tại;
- dùng browser `alert/confirm` thô nếu app có component/modal pattern tốt;
- thêm animation không cleanup;
- bỏ reduced-motion;
- thêm CDN;
- thay test bằng skip;
- kết thúc task khi test còn fail do code mình sửa.

---

# 47. TRÌNH TỰ THỰC THI BẮT BUỘC

Agent thực hiện theo thứ tự logic sau, có thể điều chỉnh file-level implementation nhưng không bỏ bước:

## Phase 1 — Inspect

1. Đọc docs bắt buộc.
2. Locate:
   - Channel config/service/storage;
   - channel asset upload;
   - TTS schema/profile/resolver/manager/adapter;
   - AudioProcessor/audio stage;
   - stage key computation;
   - job DB/state/runner/lease;
   - planner/start/target/set_target;
   - config snapshot/manifest;
   - Web API router/service facade;
   - Channel UI;
   - Job detail UI;
   - motion/GSAP infrastructure;
   - existing tests.
3. Đọc local UI Pro Max và GASP/GSAP skill.
4. Xác định architecture delta nhỏ nhất để đáp ứng full lifecycle.

## Phase 2 — Watermark domain/storage

1. Thiết kế registry/model/revision phù hợp.
2. Backward compatibility legacy.
3. Atomic store/load.
4. Active reference.
5. Reference protection.

## Phase 3 — Watermark TTS/upload service

1. Shared TTS resolver/synthesis primitive.
2. Generate TTS revision.
3. Upload revision.
4. Cache/fingerprint.
5. QA.
6. Error mapping.

## Phase 4 — Watermark pipeline integration

1. Resolve active watermark at job creation.
2. Snapshot immutable ref/hash.
3. Fix stage key invalidation.
4. Verify AudioProcessor remains file-oriented.
5. Verify no narration rerun.

## Phase 5 — Job edit core

1. Inspect/refactor canonical `set_target`.
2. Implement progress floor.
3. Running/completed/held/failed semantics.
4. Snapshot/manifest consistency.
5. Safe delete lifecycle.
6. Race safety.

## Phase 6 — API

1. Watermark CRUD/generate/upload/activate/delete.
2. Job target update.
3. Job delete.
4. Tasks/progress integration nếu generation lâu và infrastructure có sẵn.
5. Error envelope/security.

## Phase 7 — UI/UX

1. Watermark Library/editor.
2. Shared TTS controls.
3. Preview/player.
4. Edit/regenerate/delete.
5. Job Edit UI.
6. Pipeline selector/progress floor explanation.
7. Danger zone delete.
8. GSAP motion through central motion layer.
9. Responsive/a11y/reduced motion.

## Phase 8 — Tests

1. Unit.
2. Integration.
3. UI/JS.
4. Browser/E2E theo setup hiện tại.
5. Migration/backward compatibility.
6. Concurrency cases có thể test deterministic.

## Phase 9 — Fix loop

Lặp cho tới sạch:

```text
run targeted tests
→ inspect failures
→ fix root cause
→ rerun targeted tests
→ run broader suite
→ fix regression
→ rerun
```

Không coi failure là “existing” mà bỏ qua nếu thay đổi của task có thể làm nó xuất hiện. Nếu xác nhận lỗi hoàn toàn pre-existing/unrelated, ghi bằng chứng ở final report, nhưng vẫn cố tránh làm tình hình xấu hơn.

## Phase 10 — UI QA

1. Chạy app local.
2. Thực hiện flow Watermark end-to-end.
3. Thực hiện Job Edit end-to-end.
4. UI Pro Max review.
5. GASP/GSAP review.
6. Fix UX/animation/a11y issues.

## Phase 11 — Docs + final verification

1. Update docs.
2. Review diff.
3. Loại duplicate/dead code/debug print.
4. Chạy final test suite/build/lint/type checks theo project.
5. Chạy một smoke flow thực tế/fake adapter phù hợp.
6. Chỉ sau đó mới báo hoàn thành.

---

# 48. CÁCH TỰ TÌM LỆNH TEST/BUILD

Không giả định project command nếu chưa inspect.

Tìm từ:

- README;
- scripts;
- `pyproject.toml` / requirements/setup files;
- package files nếu có;
- existing CI;
- test docs;
- `.cmd`/`.ps1` helpers.

Ưu tiên command chính thức của project.

Không cài dependency toàn cục bừa bãi.

Nếu dependency dev thiếu nhưng project có setup script chính thức, dùng cách project quy định.

---

# 49. DEFINITION OF DONE — WATERMARK

Chỉ Done khi user có thể thực hiện flow thật:

```text
Mở ContentFactory
→ Kênh
→ chọn Channel
→ Watermark
→ thấy Watermark Library
→ + Tạo watermark
→ chọn Tạo bằng TTS
→ nhập tên
→ nhập nội dung watermark
→ chọn Auto hoặc TTS profile hiện có
→ Generate
→ nghe preview
→ watermark được persist
→ restart app vẫn còn
→ sửa text/TTS
→ tạo revision mới
→ revision active cập nhật đúng
→ watermark cũ không bị mutate
→ chọn một watermark khác làm active
→ tạo job mới
→ job snapshot đúng watermark revision
→ chạy pipeline
→ YouTube audio/video dùng watermark
→ Story/TTS narration không chạy lại chỉ vì watermark đổi
→ job cũ vẫn reproducible với revision cũ
```

Đồng thời flow upload vẫn chạy:

```text
+ Tạo watermark
→ Upload
→ valid audio
→ Library
→ preview
→ activate
→ replace file tạo revision mới
```

Và delete/archive/reference protection hoạt động.

---

# 50. DEFINITION OF DONE — JOB EDIT

Chỉ Done khi các flow sau chạy end-to-end:

## Flow A — running job đổi target

```text
Job đang chạy ở Story
old target = Publish
→ Sửa job
→ chọn target = Audio
→ Save
→ job không restart Story
→ chạy tiếp đến Audio
→ dừng hoàn tất ở Audio
```

## Flow B — không rollback

```text
Job đã tới Audio
→ Sửa job
→ Story/TTS bị disabled
→ cố gọi API target=Story
→ backend reject
→ job không đổi
```

## Flow C — completed job mở rộng pipeline

```text
Job đã hoàn thành ở Audio
→ Sửa job
→ target = Output
→ Save
→ job KHÔNG tự chạy
→ UI hiển thị pipeline mới/chạy tiếp
→ user bấm Chạy tiếp
→ reuse Source/Story/TTS/Audio
→ chạy phần còn lại đến Output
```

## Flow D — delete completed

```text
Completed job
→ Sửa job
→ Xóa
→ confirm
→ job biến mất khỏi list mặc định
→ output folder vẫn còn nguyên
```

## Flow E — delete running

```text
Running job
→ Sửa job
→ Xóa
→ warning rõ
→ confirm
→ cooperative cancel/delete
→ không DB corruption
→ không worker ghi vào deleted row
→ finalized output/artifact không bị phá
```

---

# 51. QUALITY GATES CUỐI CÙNG

Trước khi kết thúc, agent phải tự xác nhận:

```text
[ ] Watermark được persist và quản lý, không phải file bị bỏ quên
[ ] Watermark upload + TTS cùng một abstraction asset
[ ] Có create/list/view/preview/select/update/version/delete/archive hợp lý
[ ] Active watermark reference an toàn
[ ] Legacy channel/watermark vẫn chạy
[ ] Job snapshot watermark revision bất biến
[ ] Stage key nhận biết content watermark thay đổi
[ ] Đổi watermark không TTS lại narration
[ ] Job Edit chỉ sửa target tương lai, không rewrite lịch sử
[ ] Không target lùi trước progress floor
[ ] Running job áp target mới mà không restart
[ ] Completed job không auto-run sau Save; chạy tiếp khi user yêu cầu
[ ] Xóa job an toàn
[ ] Xóa job không xóa output user
[ ] Không duplicate old/new Update Pipeline
[ ] UI dùng UI Pro Max workflow
[ ] Animation dùng GASP/GSAP skill + motion architecture hiện tại
[ ] reduced-motion hoạt động
[ ] keyboard/focus/a11y không regression
[ ] Security localhost/token/origin/CSP/path safety còn đúng
[ ] Targeted tests pass
[ ] Full/broad test suite pass hoặc blocker bên ngoài được chứng minh rõ
[ ] Docs cập nhật đúng implementation
[ ] Không TODO/skeleton/debug leftover
```

---

# 52. FINAL REPORT CỦA AGENT

Khi và chỉ khi implementation đã hoàn thành, trả báo cáo cuối ngắn nhưng đầy đủ theo cấu trúc:

## 1. Completed

Tóm tắt chức năng đã ship.

## 2. Architecture

- Watermark model/storage/revision.
- TTS reuse.
- Active reference + job snapshot.
- Stage invalidation/fingerprint.
- Job target progress-floor semantics.
- Safe job delete.

## 3. UI/UX

- Watermark Library flow.
- Job Edit flow.
- UI Pro Max decisions chính.
- GSAP/GASP motion + reduced motion.

## 4. Files changed

Danh sách file/module quan trọng.

## 5. Tests

Ghi chính xác command và kết quả:

```text
PASS: ...
PASS: ...
```

Nếu có test không thể chạy vì dependency/service bên ngoài, nêu rõ:

- command;
- lỗi;
- tại sao external;
- phần nào đã test thay thế.

Không được ghi “all tests pass” nếu chưa chạy.

## 6. Compatibility

Xác nhận:

```text
✓ Channel cũ
✓ Watermark cũ
✓ Upload watermark cũ
✓ Job cũ
✓ Resume/Retry hiện tại
✓ Output không bị xóa khi delete job
✓ Job cũ không bị watermark mới mutate
```

## 7. Remaining limitations

Chỉ ghi limitation thực sự còn lại do external dependency/engine/provider, không dùng mục này để né phần chưa implement trong spec.

---

# 53. ĐIỀU KIỆN KẾT THÚC

**Không kết thúc sau khi code compile.**

Task chỉ được coi là hoàn thành khi:

```text
implementation
+ migration/backward compatibility
+ UI/UX
+ animation
+ API
+ tests
+ bug fixes
+ docs
+ end-to-end smoke verification
```

đã được xử lý.

Nếu trong quá trình chạy phát hiện bug do implementation mới gây ra:

```text
BUG → FIX → TEST LẠI
```

Tiếp tục vòng lặp cho tới khi feature hoạt động trọn vẹn.

**Không trả lại cho user một danh sách việc còn phải tự làm nếu đó là việc agent có thể hoàn thành ngay trong repository.**

---

# 54. ƯU TIÊN KHI CÓ XUNG ĐỘ

Nếu có xung đột giữa chi tiết ví dụ trong file này và codebase thực tế, ưu tiên theo thứ tự:

1. Invariant sản phẩm trong spec này.
2. Architecture/contracts hiện tại của ContentFactory.
3. Backward compatibility/reproducibility/safety.
4. Code hiện tại.
5. Tên endpoint/schema minh họa trong spec.

Ví dụ endpoint/file tree/schema trong file này là hướng semantic, không ép copy y nguyên nếu project đã có abstraction tốt hơn.

Mục tiêu cuối cùng là **một hệ thống hoàn chỉnh, nhất quán với ContentFactory hiện tại, không phải một patch CRUD rời rạc**.
