# Remix bám sự việc (`story_scene_remix`) — mode truyện thứ ba

Mode thứ ba của Story, cạnh **Story hiện có** (`story_branch`, mặc định, không đổi) và **Story Remix** (`story_remix`, viết truyện original + Kho nhân vật).
Dùng để remix **toàn bộ** một truyện có sẵn theo hướng *bám nhịp nguồn, thay chi tiết/sự việc tương đương*, ít gọi AI nhất có thể.
**Chỉ dùng cho nguồn bạn sở hữu hoặc có quyền chuyển thể** — mode giữ phần lớn câu chữ nguồn nên không áp ngưỡng originality dành cho sáng tác độc lập.

## Dùng trên giao diện
Chạy mới (Run) → **Chế độ truyện** → chọn **Remix bám sự việc — Xào theo cảnh**:
1. *Quyền sử dụng nguồn*: Của tôi / Có giấy phép / Được phép (**Không rõ bị chặn ngay khi tạo job**).
2. Tích *Tôi xác nhận có quyền chuyển thể nguồn này*.
3. Tuỳ chọn: độ dễ nghe, số lượt sửa mối nối (0–2), ngân sách USD. Form hiện ước tính số lượt gọi/token trước khi chạy.
Settings → *Chế độ truyện mặc định* có thêm mode này; mặc định hệ thống vẫn là Story hiện có. (Nếu đặt làm mặc định, job không khai báo quyền nguồn sẽ bị từ chối rõ ràng lúc tạo.)
Chi tiết job có thẻ **Remix bám sự việc**: bản đồ cảnh, kế hoạch (cấp 1/2), QA liên tục, chi phí, lý do dừng + nút tiếp tục an toàn.

## Đường ống
`Source → Global Scene Map → Global Remix Plan → Targeted Scene Rewrite → Continuity QA → story.txt` (Story Assembler/Validator HIỆN CÓ, rồi TTS như cũ).

| Bước | Gọi AI | Ghi chú |
|---|---|---|
| Tách cảnh | không | khối ≤ 2.200 ký tự, cắt ở ranh giới đoạn/câu, không đổi chữ |
| Scene Map | 1 lượt / 6 cảnh | event, cause, effect, nhân vật, vật/bí mật, beat (hook/twist/climax/payoff); gộp bằng code: ngôi kể (heuristic), vật xuất hiện lại, hook, kết |
| Remix Plan | 1 lượt (một lần, đóng băng) | cấp 1 mặc định; cấp 2 cần `why_level2`; cấp 3 bị từ chối (không tự chạy) |
| Rewrite | 1 lượt / cảnh bị ảnh hưởng | cảnh KHÔNG bị ảnh hưởng gọi AI = 0 |
| QA | code trước; AI ≤ 1 + số lượt sửa | sửa đúng cảnh được nêu |

Cảnh bị ảnh hưởng = cảnh kế hoạch nêu ∪ **mọi cảnh còn chứa cụm cũ** (tính bằng code), nên chi tiết đổi được cập nhật xuyên chương.
Nếu một kế hoạch lan quá 65% số cảnh (truyện ≥ 12 cảnh) thì bị trả lại để chọn phương án nhẹ hơn **trước khi** tốn chi phí viết văn.

### Constraint-first
- Hợp đồng đầu ra có trong prompt viết lại (không tiêu đề/markdown/marker/ghi chú, độ dài, ngôi kể, tên nhân vật, không tên kênh) và được kiểm ngay sau khi sinh (`story_scene_remix/logic.py::check_scene`).
- Tự sửa bằng code: rào ```, heading/đánh số chương, marker, ghi chú, nhãn cảnh, `**`, đoạn "chương trước…" (`story/validate.py::sanitize_prose`, dùng chung với Story Remix).
- Lỗi cứng (rỗng/quá ngắn/cụm cũ còn sót/dấu vết kênh/định dạng) → **1** lượt thử lại có lý do → vẫn lỗi thì dừng `SCENE_REWRITE_INVALID` (giữ cảnh đã xong).
- JSON: parser sửa dấu phẩy thừa/xuống dòng thô bằng code; lỗi còn lại ≤ 2 lượt sửa gọn (chỉ gửi JSON + lỗi) rồi `REMIX_LLM_INVALID` với `detail.kind = format|content`.

### Phân loại lỗi và tự phục hồi
- **A — tất định, tự sửa bằng code**: heading/marker, JSON phẩy thừa, id cảnh dạng `s1`, bổ sung `scene_ids` còn thiếu, cắt trường quá dài.
- **B — ngữ nghĩa, AI sửa có mục tiêu + giới hạn**: lỗi liên tục (`quality_repair_max_passes` 0–2, bền qua resume), chỉ cảnh bị nêu.
- **C — Hard Stop, giữ checkpoint**: `SOURCE_RIGHTS_REQUIRED`, `BUDGET_EXCEEDED` (preflight + guard từng lượt), `REMIX_NEEDS_LEVEL3` (chỉ thay được bằng đổi cả tuyến), `SCENE_REWRITE_INVALID`, `SCENE_CONTINUITY_REVIEW` (còn lỗi liên tục; bật *Đã xem báo cáo — tiếp tục* để đi tiếp), `EMPTY_STORY`.

### Checkpoint / resume / khoá stage
`story/scene_remix/`: `source_map_NNN.json`, `scene_map.json`, `remix_plan.json`, `affected_scenes.json`, `scenes/sNNN.{json,txt}`, `qa_state.json`, `qa_ai_cache.json`, `continuity_qa.json`, `scene_remix_report.json`, `cost_report.json`, `estimate.json`, `rewritten_story.md`.
Mỗi bước có fingerprint; resume không gọi lại bước đã xong. Khoá cảnh dùng *nguồn* của cảnh lân cận nên sửa một cảnh không làm vô hiệu cache cảnh khác.
Khoá stage `story` có `mode=story_scene_remix` + `audio_readability`; quyền/ghi chú/ngân sách/`review_accepted` không đổi khoá. Không đụng khoá của hai mode cũ.

## Chưa kiểm chứng
Mọi test dùng LLM giả (đường ống, validator, resume, ngân sách). **Chưa** chạy LLM thật nên chất lượng văn, mức "mạch lạc" và tiết kiệm chi phí thực tế so với Story Remix còn phải đo bằng benchmark có chi phí thật trên cùng nguồn có quyền.
Bộ tách cảnh là heuristic độ dài (không phải mô hình ranh giới cảnh); ngôi kể là heuristic đếm đại từ.
