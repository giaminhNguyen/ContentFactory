// Ánh xạ trạng thái backend -> hiển thị. Nguồn sự thật cho nhóm trạng thái là backend (diagnose.ui_status); file này chỉ quyết định NHÃN, BIỂU TƯỢNG và TONE.
// Thêm một trạng thái mới: thêm một dòng ở đây (docs/UI_GUIDE.md "Cách thêm một tùy chọn / trạng thái").
export const JOB_STATUS = {
  running: { label: "Đang chạy", icon: "spinner", tone: "running", spin: true },
  queued: { label: "Đang xếp hàng", icon: "clock", tone: "queue" },
  waiting: { label: "Đang chờ", icon: "hourglass", tone: "wait" },
  attention: { label: "Cần bạn xử lý", icon: "alert", tone: "attn" },
  failed: { label: "Lỗi", icon: "x-circle", tone: "fail" },
  completed: { label: "Hoàn tất", icon: "check-circle", tone: "done" },
  paused: { label: "Tạm dừng", icon: "pause", tone: "wait" },
  cancelled: { label: "Đã hủy", icon: "skip", tone: "off" },
};

export const STAGE_STATE = {
  done: { label: "Xong", icon: "check-circle", tone: "done" },
  reused: { label: "Dùng lại", icon: "check-circle", tone: "done" },
  provided: { label: "Có sẵn", icon: "check-circle", tone: "done" },
  running: { label: "Đang chạy", icon: "spinner", tone: "running", spin: true },
  waiting: { label: "Chờ tới lượt", icon: "clock", tone: "queue" },
  held: { label: "Tạm dừng", icon: "hourglass", tone: "wait" },
  failed: { label: "Lỗi", icon: "x-circle", tone: "fail" },
  not_planned: { label: "Không chạy", icon: "skip", tone: "off" },
};

// Timeline chuẩn hoá của một bước trong job (Phase 9): backend quyết định, frontend chỉ vẽ nhãn/biểu tượng (không dựa vào màu).
export const TIMELINE = {
  DONE: { label: "Xong", icon: "check-circle", tone: "done" },
  REUSED: { label: "Dùng lại", icon: "refresh", tone: "done" },
  AVAILABLE: { label: "Có sẵn", icon: "check-circle", tone: "done" },
  RUNNING: { label: "Đang chạy", icon: "spinner", tone: "running", spin: true },
  QUEUED: { label: "Chờ tới lượt", icon: "clock", tone: "queue" },
  PAUSED: { label: "Tạm dừng", icon: "hourglass", tone: "wait" },
  FAILED: { label: "Lỗi", icon: "x-circle", tone: "fail" },
  NOT_REQUESTED: { label: "Không yêu cầu", icon: "skip", tone: "off" },
  INVALIDATED: { label: "Cần chạy lại", icon: "refresh", tone: "wait" },
};
export const BRANCH_LABEL = { youtube: "YouTube", tiktok: "TikTok", package: "Gói output" };
export function timelineState(t) { return TIMELINE[t] || TIMELINE.QUEUED; }

export const PART_STATE = { done: "Xong", running: "Đang dựng", failed: "Lỗi", pending: "Chờ", reused: "Dùng lại", queued: "Chờ" };

export const FILTERS = [
  ["all", "Tất cả"], ["running", "Đang chạy"], ["waiting", "Đang chờ"], ["attention", "Cần xử lý"], ["completed", "Hoàn tất"],
];

export const ACTION_LABEL = {
  pause: "Tạm dừng", resume: "Tiếp tục", resume_now: "Thử lại ngay", retry: "Chạy lại stage lỗi", enable_auto_resume: "Bật Auto Resume", disable_auto_resume: "Tắt Auto Resume",
};

export function jobStatus(s) { return JOB_STATUS[s] || JOB_STATUS.queued; }
export function stageState(s) { return STAGE_STATE[s] || STAGE_STATE.waiting; }

// Job còn "sống" (cần theo dõi nhanh)?
export function isActive(s) { return s === "running" || s === "queued"; }
