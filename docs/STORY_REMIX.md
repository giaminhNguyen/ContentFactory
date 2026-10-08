# Story Remix + Kho nhân vật (Living Character Universe)

Chế độ truyện thứ hai, **song song** với Story hiện có (`story_branch`, giữ nguyên là mặc định). Học mô-típ/thể loại/cơ chế cảm xúc của nguồn rồi viết một truyện **ORIGINAL**
(nhân vật, xung đột, chuỗi nhân-quả, twist, thoại, kết thúc khác hẳn), tự chọn/tạo nhân vật từ Kho nhân vật, và tự cập nhật Kho sau khi truyện đạt QA.
Đầu ra vẫn là `story.txt` đi qua **đúng** Assembler/Validator cũ → TTS/video không đổi.

## 1. Dùng hằng ngày (người không rành kỹ thuật)

1. Màn **Chạy**: dán nguồn → mục **Chế độ truyện** → chọn **Story Remix — Xào truyện theo mô-típ** → **RUN**. Mọi thứ còn lại (phân tích nguồn, ý tưởng, chọn nhân vật, đại cương, viết, QA, cập nhật Kho) tự động.
2. Muốn chọn một lần cho mọi job: **Lưu thành mẫu** → tick “Đặt làm mặc định cho job mới”. Chọn **Story hiện có** bất cứ lúc nào để chạy cách cũ.
3. Mặc định Kho nhân vật (đã chọn): tự chọn nhân vật BẬT · ưu tiên dùng lại BẬT (chỉ là ưu tiên, không ép) · dòng thời gian độc lập BẬT · tự cập nhật Kho sau QA BẬT.
4. Trước khi chạy có **Ước tính** (lượt gọi AI/token; USD “không rõ” nếu chưa cấu hình giá). Ngân sách tuỳ chọn: job dừng an toàn khi chi phí ĐÃ BIẾT vượt ngân sách, tiếp tục được sau khi nâng ngân sách (các bước xong được giữ).
5. Thẻ **Kế hoạch Story Remix** trên trang job: ý tưởng được chọn + lý do loại, dàn nhân vật, kiểm tra độ giống nguồn, nhịp thưởng, tiến độ chương, QA cuối, chi phí. Khi job dừng sẽ có nút tiếp tục an toàn (đã xem báo cáo / nâng ngân sách / thêm lượt sửa).
6. **Kho nhân vật** (menu trái): danh mục có tìm/lọc, sửa hồ sơ (có revision, khóa cốt lõi, lưu trữ), dàn nhân vật theo truyện (vì sao chọn, sơ đồ quan hệ, tuỳ chọn thay), nhật ký cập nhật + hoàn tác, xuất/nhập Excel (xem trước, báo xung đột).

Kho **bắt đầu rỗng** và lớn dần từ các truyện đạt QA. Không có 48 nhân vật cài sẵn; file Excel mẫu cũ chỉ là tuỳ chọn nhập thủ công.

## 2. Kiến trúc

```
Run (story_mode=story_remix) → StoryModeRouter → StoryRemixAdapter
  source_dna        (ĐỌC nguồn → mô tả TRỪU TƯỢNG; kiểm rò tên/n-gram)
  premises (N)      (KHÔNG thấy nguồn)  → select (điểm tất định, giải thích; yếu ⇒ dừng TRƯỚC khi viết dài)
  autocast          (universe.casting: fit + chọn cả dàn + tạo mới STAGED; đóng băng dàn)
  story_bible, outline (chỉ dùng character_id của dàn đã chốt)
  originality gate  (số đo từ vựng + nhận xét mô hình; block ⇒ lập lại 1 lần; review ⇒ dừng chờ người)
  dopamine gate     (nhịp thưởng) + tối đa N lượt sửa đại cương
  chapters          (story_memory, QA từng chương, sửa có mục tiêu, checkpoint theo chương)
→ Assembler/Validator HIỆN CÓ → story.txt → finalize: QA cuối → publish Kho (nguyên tử, idempotent)
```

- Tích hợp mode-aware qua `orchestrator/story_router.py` (bọc adapter Story); **không** thêm stage toàn cục. Job cũ không có `params.story_mode` ⇒ đi đường cũ, `stage_key` không đổi.
- Packages `story_remix/` và `universe/` nằm trong nhóm cách ly (`tests/test_architecture.py`): chỉ import `contracts`/`fsutil`; LLM (`TextLLM`), Kho (`UniverseBridge`), publisher do orchestrator tiêm.
- Cô lập nguồn: chỉ phân tích DNA và các cổng đọc transcript; bộ sinh ý tưởng/đại cương/chương không bao giờ nhận transcript.
- Fingerprint theo bước (`core.Steps`, `writer` theo chương): đổi nhãn UI, ngưỡng QA, `rights_ack`/`source_provenance`/`budget_usd`/`review_accepted` **không** viết lại bước đắt.

### Artifact (trong `workspace/job_<id>/story/remix/`)
`source_dna.json`, `premise_candidates*.json`, `selection_report.json`, `character_cast.json`, `story_bible.json`, `outline.json`, `originality_report.json`, `quality_report.json`,
`story_memory.json` (+ `memory/after_NNN.json`), `chapters/ch_NNN.md` (+ `.meta.json`), `writer_report.json`, `final_qa.json`, `cost_report.json`, `remix_state.json`.

### Cấu hình
`params.story_mode = {mode, story{…}, character_universe{…}}` — schema DUY NHẤT ở `story/mode.py` (server kiểm, UI sinh form từ `GET /api/story-mode`). Mặc định hệ thống ghi đè bằng Settings > Truyện;
mẫu có tên lưu ở `runtime/story_presets.json`. Máy: `story_remix.llm` (`auto` = fake khi adapter story là fake, ngược lại Claude CLI), `story_remix.price_usd_per_mtok` (tuỳ chọn, để ước tính USD).

## 3. Kho nhân vật

- SQLite `runtime/universe.db` (WAL, giao dịch IMMEDIATE, migration có version). **DB là nguồn sự thật**; Excel chỉ để xuất/nhập (xuất nhất quán một revision; nhập luôn xem trước, xung đột không bị ghi đè).
- Danh tính cốt lõi toàn cục + ổn định (`character_id`); vai/quan hệ/sống-chết/tình cảm thuộc **từng truyện** (`story_id` + `world_id`, dòng thời gian `parallel`). Variant (AU) lưu riêng.
- Nhân vật mới chỉ **staged** (`character_candidates`) tới khi truyện đạt QA cuối; publish nguyên tử + idempotent (`publish_id = hash(story_id, story.txt)`); gộp trùng; hoàn tác chỉ khi an toàn; mọi thay đổi có audit + revision toàn kho.

## 4. Đã chứng minh / chưa chứng minh

| Hạng mục | Trạng thái |
|---|---|
| Đường ống end-to-end, hợp đồng `story.txt`/TTS, resume, ngân sách, QA, publish, hoàn tác, UI | **Có test tự động + QA trình duyệt** (LLM giả) |
| Cơ chế thưởng theo thể loại giữ được trên 5 nguồn (kinh dị/trinh thám/tình cảm/báo thù/hài) | Chỉ với **LLM giả** (`scripts/remix_bench.py run`): chứng minh đường ống + cổng, không chứng minh chất lượng văn |
| Chất lượng văn thật, độ nguyên bản thật, nhịp nghe thật | **CHƯA chạy thật** (cần Claude CLI + chi phí + người nghe) |
| So sánh chi phí/chất lượng với Story cũ | **Chưa có** baseline cùng nguồn; báo cáo ghi `unknown` (chạy thật: `--llm claude --confirm-spend --baseline DIR`) |
| Báo cáo originality | Tham khảo kỹ thuật (số đo + nhận xét mô hình + độ không chắc chắn); **không** phải xác nhận bản quyền |

## 5. Giới hạn đã biết
- Phát hiện tên riêng bằng heuristic viết hoa: transcript ASR không viết hoa có thể bị sót (ghi trong báo cáo).
- Điểm chọn ý tưởng/QA chương là heuristic tất định, không thay thế việc nghe.
- Ước tính chi phí thô, không gồm thử lại do định dạng sai; token thật không có từ Claude CLI ở lớp này (ghi `unknown`).
- Hoàn tác publish bị chặn nếu nhân vật tạo mới đã được truyện khác dùng hoặc đã bị sửa (lưu trữ thay vì hoàn tác).
- Xem `docs/story_remix/CONTRACT_WITH_SNAPSHOT.md` (snapshot kiến trúc, nhật ký quyết định, bug→fix).
