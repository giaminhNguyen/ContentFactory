// Hành động trên job dùng chung cho mọi view (một chỗ duy nhất => cùng phản hồi, cùng chống bấm đúp, cùng cách báo lỗi).
import { api } from "./api.js";
import { busy, toast, toastError } from "./components.js";

export async function openOutput(jobId, button) {
  return busy(button, async () => {
    try {
      await api.post(`/api/jobs/${jobId}/open-output`);
      toast({ title: "Đã mở thư mục output", tone: "done" });
    } catch (e) { toastError(e, "Không mở được thư mục output"); }
  });
}

export async function resumeJob(jobId, button, { now = false, after } = {}) {
  return busy(button, async () => {
    try {
      const r = await api.post(`/api/jobs/${jobId}/resume`, { now });
      toast({ title: r.message, tone: r.result === "resumed" ? "done" : "wait" });
      after?.();
    } catch (e) { toastError(e, "Không tiếp tục được"); }
  });
}

export async function pauseJob(jobId, button, { after } = {}) {
  return busy(button, async () => {
    try {
      const r = await api.post(`/api/jobs/${jobId}/pause`);
      toast({ title: r.message, tone: r.result === "changed" ? "wait" : "info" });
      after?.();
    } catch (e) { toastError(e, "Không tạm dừng được"); }
  });
}

export async function retryJob(jobId, button, { after } = {}) {
  return busy(button, async () => {
    try {
      const r = await api.post(`/api/jobs/${jobId}/retry`);
      toast({ title: "Đã xếp lại để chạy", message: r.message, tone: "done" });
      after?.();
    } catch (e) { toastError(e, "Không chạy lại được"); }
  });
}

export async function setAutoResume(jobId, enabled, { after } = {}) {
  try {
    const r = await api.post(`/api/jobs/${jobId}/auto-resume`, { enabled });
    toast({ title: r.message, tone: "done" });
    after?.();
    return true;
  } catch (e) { toastError(e, "Không đổi được Auto Resume"); return false; }
}
