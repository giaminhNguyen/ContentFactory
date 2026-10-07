// “Sửa job” (Chi tiết job): Cập nhật pipeline = đổi ĐÍCH chạy theo progress floor + Danger zone → Xóa job.
// Không có logic nghiệp vụ ở đây: bước nào chọn được, vì sao bị khóa và hậu quả của từng lựa chọn đều do backend trả (`job.edit`, PUT /api/jobs/<id>/target);
// backend kiểm lại khi lưu. Dialog gốc <dialog>: bẫy focus, Esc, trả focus; reduced-motion do motion.js xử lý.
import { api } from "../api.js";
import { h } from "../dom.js";
import { icon } from "../icons.js";
import { btn, alertBox, openDialog, toast, toastError } from "../components.js";
import { jobStatus } from "../status.js";
import { stepMark, saveState, deleteReady } from "../job_edit_logic.js";
import * as motion from "../motion.js";

const PASSED_NOTE = "step-passed-note";

// Bước đã qua dùng MỘT chú thích chung (không lặp 7 lần); lý do khác (thiếu dữ liệu, job đã hủy) hiện ngay dưới bước đó.
function stepRow(s, group) {
  const mark = stepMark(s.state);
  const own = !s.selectable && s.reason_kind !== "passed";
  const noteId = own ? `step-note-${group}-${s.id}` : PASSED_NOTE;
  const radio = h("input", { type: "radio", name: group, value: s.id, checked: s.is_target, disabled: !s.selectable, "aria-describedby": noteId });
  const ic = h("span", { class: "step-mark", dataset: { tone: mark.tone }, "aria-hidden": "true" }, icon(mark.icon, { size: 18, cls: mark.spin ? "spin" : "" }));
  const tags = h("span", { class: "step-tags" },
    h("span", { class: "step-state" }, s.state_label),
    s.is_target ? h("span", { class: "chip ok" }, icon("check", { size: 12 }), " Đích hiện tại") : null);
  const row = h("label", { class: "step-row", dataset: { state: s.state, disabled: String(!s.selectable) } }, radio, ic,
    h("span", { class: "step-main" }, h("span", { class: "step-name" }, s.label), tags),
    own ? h("span", { class: "step-note", id: noteId }, s.reason) : null);
  row._radio = radio;
  return row;
}

export async function openEditJob(job, { after, navigate } = {}) {
  const e = job.edit;
  const group = `target-${job.id}`;
  let selected = e.target, saving = false, closeEdit = null, ctl = null;

  const hasPassed = e.stages.some((s) => s.reason_kind === "passed");
  const steps = h("div", { class: "edit-steps", role: "radiogroup", "aria-label": "Chạy job đến bước" }, ...e.stages.map((s) => stepRow(s, group)));
  const passedNote = hasPassed ? h("p", { class: "small muted", id: PASSED_NOTE }, icon("lock", { size: 14 }), ` Các bước đã chạy tới (đến “${e.floor_label}”) không thể đặt làm đích; kết quả của chúng được giữ và dùng lại.`) : null;
  const effect = h("div", { class: "edit-effect", "aria-live": "polite" });
  const why = h("p", { class: "small muted", id: "edit-save-why" });
  const byId = new Map(e.stages.map((s) => [s.id, s]));
  const paintEffect = () => {
    const s = byId.get(selected);
    effect.replaceChildren(icon("info", { size: 16 }), h("span", null, s?.effect || "Chọn bước muốn job chạy đến."));
    motion.swap(effect);
    const st = saveState(e, selected);
    if (ctl?.buttons.save) { ctl.buttons.save.disabled = !st.enabled || saving; ctl.buttons.save.setAttribute("aria-describedby", "edit-save-why"); }
    why.textContent = st.why;
    for (const row of steps.children) row.dataset.selected = String(row._radio.checked);
  };
  steps.addEventListener("change", (ev) => { if (ev.target?.name === group) { selected = ev.target.value; paintEffect(); motion.pulse(ev.target.closest(".step-row").querySelector(".step-mark")); } });
  const checked = steps.querySelector("input:checked");
  if (checked) checked.setAttribute("autofocus", "");

  const danger = h("section", { class: "danger-zone", "aria-labelledby": "danger-h" },
    h("div", null, h("h3", { id: "danger-h" }, "Vùng nguy hiểm"), h("p", { class: "small muted" }, "Xóa job khỏi ContentFactory. Thư mục output đã tạo là của bạn nên KHÔNG bị xóa.")),
    btn({ label: "Xóa job…", icon: "trash", kind: "danger", onClick: async () => { if (await confirmDeleteJob(job, { navigate })) closeEdit?.(); } }));

  const content = h("div", { class: "stack" },
    e.awaiting_run ? alertBox({ tone: "info", title: "Pipeline mới đang chờ bạn bấm “Chạy tiếp”", body: e.awaiting_text }) : null,
    h("section", { class: "stack", "aria-labelledby": "pipe-edit-h" },
      h("div", null, h("h3", { id: "pipe-edit-h" }, "Pipeline"), h("p", { class: "small muted" }, "Chọn bước mà job sẽ chạy đến rồi dừng.")),
      steps, passedNote, effect, why),
    danger);
  paintEffect();

  const r = await openDialog({ title: `Sửa job #${job.id}`, describe: job.title, wide: true, content, onOpen: (c) => { ctl = c; closeEdit = () => c.close(null); paintEffect(); }, actions: [
    { label: "Đóng", value: null },
    { id: "save", label: "Lưu pipeline", kind: "primary", value: "saved", disabled: true, onClick: async () => {
      if (saving) return false;                                                    // chống bấm đúp (backend còn idempotent)
      const st = saveState(e, selected);
      if (!st.enabled) return false;
      saving = true; ctl.buttons.save.disabled = true; ctl.buttons.save.setAttribute("aria-busy", "true");
      try {
        const res = await api.put(`/api/jobs/${job.id}/target`, { target_stage: selected });
        toast({ title: res.message, tone: res.held ? "wait" : "done" });
        after?.();
        return true;
      } catch (err) { toastError(err, "Chưa cập nhật được pipeline"); return false; }
      finally { saving = false; ctl.buttons.save?.removeAttribute("aria-busy"); paintEffect(); }
    } },
  ] });
  return r === "saved";
}

// Xác nhận xóa job. Nói rõ job nào, trạng thái, hậu quả với job đang chạy và việc output KHÔNG bị xóa. Job đang chạy cần thêm một ô xác nhận.
export async function confirmDeleteJob(job, { navigate } = {}) {
  const del = job.edit.delete;
  let ack = false, deleting = false, ctl = null;
  const ackBox = del.running ? h("input", { type: "checkbox", id: "del-ack" }) : null;
  const sync = () => { if (ctl?.buttons.del) ctl.buttons.del.disabled = deleting || !deleteReady(del, ackBox?.checked); };
  ackBox?.addEventListener("change", () => { ack = ackBox.checked; sync(); });
  const content = h("div", { class: "stack" },
    h("p", null, h("strong", null, job.title), h("span", { class: "muted" }, ` · Job #${job.id} · ${jobStatus(job.status).label}`)),
    del.warning ? alertBox({ tone: "wait", title: "Job đang chạy", body: del.warning }) : null,
    alertBox({ tone: "info", title: "Output được giữ nguyên", body: del.output_note + " Chỉ dữ liệu làm việc nội bộ của job bị dọn." }),
    ackBox ? h("label", { class: "row", for: "del-ack" }, ackBox, h("span", null, "Tôi hiểu job đang chạy sẽ bị dừng và xóa.")) : null);
  const r = await openDialog({ title: "Xóa job này?", content, onOpen: (c) => { ctl = c; sync(); }, actions: [
    { label: "Không xóa", value: null },
    { id: "del", label: "Xóa job", kind: "danger solid", value: "deleted", disabled: del.running, onClick: async () => {
      if (deleting || !deleteReady(del, ack)) return false;
      deleting = true; sync();
      try {
        const res = await api.del(`/api/jobs/${job.id}`);
        toast({ title: res.message, tone: "done" });
        navigate?.("/jobs");
        return true;
      } catch (err) { toastError(err, "Chưa xóa được job"); return false; }
      finally { deleting = false; sync(); }
    } },
  ] });
  return r === "deleted";
}

// Bộ chọn ĐÍCH cho thao tác hàng loạt (nhiều job): thứ tự/nhãn lấy từ /api/pipeline; mỗi job được backend kiểm riêng theo progress floor của nó.
export function targetPicker(stages, group) {
  let value = null;
  const subs = [];
  const el = h("div", { class: "edit-steps", role: "radiogroup", "aria-label": "Chạy job đến bước" }, ...stages.map((s) => {
    const radio = h("input", { type: "radio", name: group, value: s.id });
    return h("label", { class: "step-row", dataset: { state: "waiting", disabled: "false" } }, radio, h("span", { class: "step-main" }, h("span", { class: "step-name" }, s.label)));
  }));
  el.addEventListener("change", (ev) => { if (ev.target?.name === group) { value = ev.target.value; for (const f of subs) f(value); } });
  return { el, get value() { return value; }, onChange: (f) => subs.push(f) };
}

// Mở dialog chọn đích cho nhiều job; trả {target_stage} hoặc null. `extra` là phần giao diện thêm (vd chọn phạm vi), `confirmLabel` nhãn nút áp dụng.
export async function openTargetDialog({ title, intro, extra, ready = () => true }) {
  let desc;
  try { desc = await api.get("/api/pipeline"); } catch (e) { toastError(e, "Chưa tải được danh sách bước"); return null; }
  const pick = targetPicker(desc.stages, "bulk-target");
  let ctl = null;
  const sync = () => { if (ctl?.buttons.ok) ctl.buttons.ok.disabled = !pick.value || !ready(); };
  pick.onChange(sync);
  const content = h("div", { class: "stack" }, h("p", { class: "muted small" }, intro), pick.el, extra || null);
  const r = await openDialog({ title, wide: true, content, onOpen: (c) => { ctl = c; sync(); }, actions: [{ label: "Hủy", value: null }, { id: "ok", label: "Áp dụng", kind: "primary", value: "ok", disabled: true }] });
  return r === "ok" ? { target_stage: pick.value } : null;
}
