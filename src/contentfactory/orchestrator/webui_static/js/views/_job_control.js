// Điều khiển của người dùng trên một job: Đổi ảnh thumbnail (có impact preview), Chạy lại với thay đổi, Hủy (có xác nhận). Cập nhật pipeline/Xóa job nằm ở _job_edit.js (Sửa job).
// Không có logic nghiệp vụ ở đây: bước nào chạy lại/giữ nguyên đều do backend trả; frontend chỉ vẽ và gửi lựa chọn của người dùng.
import { api } from "../api.js";
import { h } from "../dom.js";
import { btn, select, alertBox, openDialog, toast, toastError } from "../components.js";

// “Đổi ảnh thumbnail”: backend nói trước việc gì sẽ chạy lại (cùng impact planner), người dùng xác nhận rồi mới rút ảnh khác từ pool.
export async function openRerollThumbnail(job, { after } = {}) {
  let impact;
  try { impact = await api.post(`/api/jobs/${job.id}/reroll-thumbnail/impact`, {}); }
  catch (e) { toastError(e, "Chưa đổi ảnh được"); return false; }
  const todo = impact.ok ? impact.stages.filter((s) => ["RUN", "RERUN"].includes(s.action)) : [];
  const content = h("div", { class: "stack", "aria-live": "polite" },
    h("p", null, "Hệ thống chọn một ảnh KHÁC từ pool rồi dựng lại thumbnail. Chỉ phần bị ảnh hưởng chạy lại; giọng đọc, audio và TikTok giữ nguyên."),
    impact.ok ? (todo.length ? h("ul", { class: "autolist" }, ...todo.map((s) => h("li", null, h("strong", null, s.label + ": "), s.action_label, s.reason ? ` — ${s.reason}` : ""))) : h("p", { class: "muted small" }, "Chưa có bước nào phải chạy lại."))
      : alertBox({ tone: "wait", title: impact.errors[0] || "Không đổi được ảnh lúc này", body: impact.clone_suggested ? "Dùng “Chạy lại với thay đổi” để tạo job mới với ảnh khác." : null }));
  const r = await openDialog({ title: "Đổi ảnh thumbnail?", content, actions: [{ label: "Không đổi", value: null }, { label: "Đổi ảnh", kind: "primary", value: "ok", disabled: !impact.ok, onClick: async () => {
    try { const x = await api.post(`/api/jobs/${job.id}/reroll-thumbnail`, {}); toast({ title: x.message, tone: x.status === "applied" ? "done" : "wait" }); after?.(); return true; }
    catch (e) { toastError(e, "Chưa đổi ảnh được"); return false; }
  } }] });
  return r === "ok";
}

export async function confirmCancel(job, { after } = {}) {
  const r = await openDialog({ title: "Hủy job này?", describe: "Kết quả đã có được giữ lại, nhưng job sẽ không tự chạy lại. Muốn thử lại với thay đổi, dùng “Chạy lại với thay đổi”.",
    content: h("p", null, `Job #${job.id}: ${job.title}`), actions: [{ label: "Không hủy", value: null }, { label: "Hủy job", kind: "danger solid", value: "ok", onClick: async () => {
      try { const x = await api.post(`/api/jobs/${job.id}/cancel`); toast({ title: x.message, tone: "wait" }); after?.(); return true; }
      catch (e) { toastError(e, "Chưa hủy được"); return false; }
    } }] });
  return r === "ok";
}

export async function openClone(job, { navigate } = {}) {
  const desc = await api.get("/api/pipeline");
  const from = select({ options: [["", "Dùng lại mọi kết quả còn hợp lệ (chỉ chạy phần mới)"], ...desc.stages.map((s) => [s.id, `Chạy lại từ: ${s.label}`])] });
  const content = h("div", { class: "stack" }, h("p", { class: "muted small" }, "Tạo job MỚI từ kết quả còn hợp lệ của job này. Job cũ và output của nó không bị thay đổi."),
    h("label", { class: "field" }, h("span", { class: "label" }, "Bắt đầu chạy lại từ"), from));
  await openDialog({ title: "Chạy lại với thay đổi", content, actions: [{ label: "Hủy", value: null }, { label: "Tạo job mới", kind: "primary", value: "ok", onClick: async () => {
    try {
      const r = await api.post(`/api/jobs/${job.id}/clone`, { rerun_from: from.value || null });
      toast({ title: r.message, tone: "done" });
      navigate?.(`/jobs/${r.job_id}`);
      return true;
    } catch (e) { toastError(e, "Chưa tạo được job mới"); return false; }
  } }] });
}
