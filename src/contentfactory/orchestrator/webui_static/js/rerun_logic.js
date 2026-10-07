// Logic thuần của "Chạy lại" (D-113). KHÔNG có luật nghiệp vụ: danh sách/thứ tự stage, bước nào chọn được, bước cần chọn thêm đều do backend trả
// (rerun-options / rerun-plan). Ở đây chỉ: chuẩn hóa lựa chọn, dựng payload, chống kết quả cũ về muộn, và chữ hiển thị.
export const PUBLISH_WARNING = "Đăng YouTube sẽ tạo một video mới trên YouTube. Video đã đăng trước đó sẽ không bị xóa.";

export const STAGE_STATE_LABEL = { pending: "Chờ", running: "Đang chạy", succeeded: "Xong", failed: "Lỗi", skipped: "Bỏ qua", cancelled: "Đã hủy" };
export const RESULT_LABEL = { queued: "Đang chờ", running: "Đang chạy", succeeded: "Thành công", failed: "Lỗi", partial: "Xong một phần", cancelled: "Đã hủy" };
export const GUIDANCE_SOURCE = { job: "đề xuất riêng của job", settings: "đề xuất từ Cài đặt", none: "không dùng đề xuất" };

/** Các id chọn được (backend nói `eligible`), theo thứ tự backend trả. */
export const selectableIds = (options) => (options?.stages || []).filter((s) => s.eligible).map((s) => s.id);

/** Lựa chọn → mảng id theo thứ tự backend (backend vẫn sắp/dedupe lại). */
export function orderedSelection(options, selected) {
  const set = new Set(selected);
  return (options?.stages || []).map((s) => s.id).filter((id) => set.has(id));
}

/** Request id ổn định cho cùng (lần mở hộp thoại, tập lựa chọn): bấm đúp/gửi lại không tạo hai phiên; đổi lựa chọn thì đổi id. */
export function requestIds(make) {
  const m = new Map();
  return (ids) => { const k = [...ids].sort().join("|"); if (!m.has(k)) m.set(k, make()); return m.get(k); };
}

export const payload = (ids, requestId) => ({ stages: [...ids], request_id: requestId });

/** Chống kết quả bất đồng bộ về muộn: mỗi lần gọi `next()` lấy một số thứ tự; chỉ kết quả của số mới nhất được dùng. */
export function latest() {
  let n = 0;
  return { next: () => ++n, isCurrent: (i) => i === n };
}

export const countLabel = (n) => (n > 0 ? `Đã chạy lại ${n} lần` : "Chưa chạy lại lần nào");
export const countBadge = (n) => (n > 0 ? `Rerun ×${n}` : "");

const labelOf = (options, id) => (options?.stages || []).find((s) => s.id === id)?.label || id;
export const labels = (options, ids) => (ids || []).map((i) => labelOf(options, i)).join(", ");

/** Tóm tắt thực thi theo thứ tự backend: {title, lines}. */
export function summary(plan, options) {
  if (!plan?.order?.length) return { title: "Chưa chọn bước nào.", lines: [] };
  const lines = plan.order.map((id, i) => `${i + 1}. ${labelOf(options, id)}`);
  return { title: `Sẽ chạy lại ${plan.order.length} bước:`, lines };
}

/** Vấn đề của từng stage đã chọn (từ rerun-plan) → {id: [message]} */
export function problemsById(plan) {
  const out = {};
  for (const s of plan?.stages || []) if (s.problems?.length) out[s.id] = s.problems.map((p) => p.message);
  return out;
}

export const hasHard = (plan) => !!(plan?.stages || []).some((s) => (s.problems || []).some((p) => p.hard));

/** Nút "Chọn các bước cần thiết": thêm gì (chữ) — không tự tick âm thầm. */
export function suggestionText(plan, options) {
  return plan?.suggested?.length ? `Thêm: ${labels(options, plan.suggested)}` : "";
}
export const withSuggested = (selected, plan) => [...new Set([...selected, ...(plan?.suggested || [])])];

/** Có thể gửi không: có chọn, backend nói ok, không bị chặn; và nếu có Publish thì đã xác nhận. */
export function canSubmit({ selected, plan, options, confirmed }) {
  if (!selected.length || !plan || !plan.ok || options?.blocked) return false;
  return !plan.publishes || !!confirmed;
}

/** Dòng mô tả phiên đang chạy: "Đang chạy lại #3: Truyện ✓ → Giọng đọc 3/12 → Audio". */
export function activeLine(s) {
  if (!s) return "";
  const parts = (s.stages || []).map((x) => {
    const mark = { succeeded: " ✓", failed: " ✗", cancelled: " ✗", skipped: " –" }[x.state] || "";
    const pr = x.state === "running" && x.progress?.total ? ` ${x.progress.done ?? 0}/${x.progress.total}` : "";
    return `${x.label}${mark}${pr}`;
  });
  return `${s.state === "queued" ? "Đang chờ chạy lại" : "Đang chạy lại"} #${s.number}: ${parts.join(" → ")}`;
}

/** Tín hiệu để biết phiên vừa kết thúc (đang hoạt động → hết): trả {id, number, result} hoặc null. */
export function finishedSession(prevActive, nextActive) {
  return prevActive && !nextActive ? { id: prevActive.id, number: prevActive.number } : null;
}
