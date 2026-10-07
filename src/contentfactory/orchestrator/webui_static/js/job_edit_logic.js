// Logic thuần của “Sửa job” (không DOM, test bằng node). Backend đã quyết định bước nào chọn được và hậu quả của từng lựa chọn (`job.edit`);
// frontend không có thứ tự pipeline riêng và không tự kiểm đích hợp lệ — ở đây chỉ chọn nhãn/biểu tượng và trạng thái nút Lưu.

// Biểu tượng + tông của từng bước trong bộ chọn (nghĩa luôn có chữ đi kèm, không chỉ màu).
export const STEP_MARK = {
  done: { icon: "check-circle", tone: "done" },
  reused: { icon: "check-circle", tone: "done" },
  provided: { icon: "check-circle", tone: "done" },
  running: { icon: "spinner", tone: "running", spin: true },
  held: { icon: "hourglass", tone: "wait" },
  failed: { icon: "x-circle", tone: "fail" },
  waiting: { icon: "clock", tone: "queue" },
  not_planned: { icon: "skip", tone: "off" },
};
export const stepMark = (state) => STEP_MARK[state] || STEP_MARK.waiting;

// Bước đang được chọn có lưu được không? Nút Lưu tắt kèm LÝ DO (hiển thị gần nút, không chỉ tooltip).
export function saveState(edit, selected) {
  const s = edit.stages.find((x) => x.id === selected);
  if (!s || !s.selectable) return { enabled: false, why: "Chọn một bước có thể đặt làm đích." };
  if (selected === edit.target) return { enabled: false, why: "Đích đang giữ nguyên. Chọn bước khác để thay đổi." };
  return { enabled: true, why: "" };
}

// Xóa job: job đang chạy cần thêm một bước xác nhận (đánh dấu đã hiểu) trước khi nút Xóa bật.
export function deleteReady(del, acknowledged) {
  return !del.running || !!acknowledged;
}
