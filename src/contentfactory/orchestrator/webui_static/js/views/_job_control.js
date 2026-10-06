// Điều khiển của người dùng trên một job: cập nhật pipeline (có impact preview), Chạy lại với thay đổi, Hủy (có xác nhận).
// Không có logic nghiệp vụ ở đây: bước nào bị khóa/chạy lại/giữ nguyên đều do backend (/pipeline-impact) trả; frontend chỉ vẽ và gửi lựa chọn của người dùng.
import { api } from "../api.js";
import { h, clear, patchList } from "../dom.js";
import { icon } from "../icons.js";
import { btn, select, alertBox, openDialog, toast, toastError } from "../components.js";

const POLICY = [["after_current_safe_point", "Áp dụng sau điểm an toàn (khuyên dùng)"], ["pause_and_apply", "Tạm dừng và áp dụng ngay ở ranh giới kế tiếp"]];
const TAG = { KEEP: "check", REUSE: "refresh", RUN: "play", RERUN: "refresh", REMOVE_FROM_PLAN: "skip", CURRENT_CONTINUE: "spinner", BLOCKED: "alert", OFF: "skip" };

function line(label, names) { return names.length ? h("li", null, h("strong", null, label + ": "), names.join(", ")) : null; }

export async function openPipelineDialog(job, { after } = {}) {
  const sel = new Set(job.requested_stages);
  const rows = h("div", { class: "stage-pick", role: "group", "aria-label": "Các bước muốn có kết quả" });
  const summary = h("ul", { class: "autolist", "aria-live": "polite" });
  const problems = h("div", { class: "stack", "aria-live": "polite" });
  const policy = select({ options: POLICY, value: "after_current_safe_point" });
  const policyBox = h("label", { class: "field" }, h("span", { class: "label" }, "Khi nào áp dụng"), policy);
  let seq = 0, impact = null;

  const apply = (el, st) => {
    const fixed = st.role === "locked" || st.role === "provided" || (st.role === "selected" && st.by.length > 0);
    el.dataset.role = st.role;
    el._cb.checked = st.role === "selected" || st.role === "locked";
    el._cb.disabled = fixed;
    el.querySelector(".s-tag").replaceChildren(icon(TAG[st.action] || "skip", { size: 14 }), st.action_label);
    el.querySelector(".s-why").textContent = st.reason + (st.role === "selected" && st.by.length ? " Bỏ chọn bước phía sau trước nếu muốn bỏ bước này." : "");
  };

  async function refresh() {
    const my = ++seq;
    let r;
    try { r = await api.post(`/api/jobs/${job.id}/pipeline-impact`, { pipeline: { requested_stages: [...sel] } }); }
    catch (e) { if (my === seq) { clear(problems); problems.append(alertBox({ tone: "fail", title: e.message, body: e.hint })); } return; }
    if (my !== seq) return;
    impact = r;
    clear(problems);
    if (!r.ok) problems.append(alertBox({ tone: r.errors.length && !r.blocked ? "wait" : "info", title: r.errors[0] || "Không áp dụng được", body: r.errors.slice(1).join(" ") || null }));
    patchList(rows, r.ok ? r.stages : [], (s) => s.id, (s) => {
      const cb = h("input", { type: "checkbox", id: `pd-${s.id}` });
      cb.addEventListener("change", () => { if (cb.checked) sel.add(s.id); else sel.delete(s.id); refresh(); });
      const el = h("label", { class: "pick-row", for: cb.id }, cb, h("span", { class: "s-label" }, s.label), h("span", { class: "s-tag" }), h("span", { class: "s-why" }));
      el._cb = cb;
      apply(el, s);
      return el;
    }, apply);
    const t = r.summary_text || {};
    summary.replaceChildren(...[line("Sẽ chạy", t.will_run || []), line("Giữ nguyên", t.kept || []), line("Bỏ khỏi kế hoạch", t.removed || []), line("Đang chạy, làm nốt", t.continuing || [])].filter(Boolean));
    if (r.rewind_to) summary.append(h("li", null, `Job sẽ lùi về bước “${(r.stages.find((s) => s.id === r.rewind_to) || {}).label}”; kết quả hợp lệ ở các bước trước giữ nguyên.`));
    policyBox.hidden = !r.current_running;
  }

  const content = h("div", { class: "stack" }, h("p", { class: "muted small" }, "Chọn kết quả bạn muốn. Bước cần thiết tự được thêm và khóa lại. Hệ thống chỉ chạy lại phần bị ảnh hưởng."), rows, problems,
    h("div", null, h("div", { class: "label" }, "Thay đổi này sẽ"), summary), policyBox);
  refresh();
  await openDialog({ title: "Cập nhật pipeline của job", content, wide: true, actions: [
    { label: "Hủy", value: null },
    { label: "Áp dụng", kind: "primary", value: "ok", onClick: async () => {
      if (!impact?.ok) { toast({ title: "Chưa có thay đổi hợp lệ để áp dụng", message: impact?.errors?.[0], tone: "wait" }); return false; }
      try {
        const r = await api.post(`/api/jobs/${job.id}/pipeline-revisions`, { pipeline: { requested_stages: [...sel] }, apply_policy: policy.value });
        toast({ title: r.status === "applied" ? "Đã áp dụng" : "Đã ghi thay đổi", message: r.message, tone: r.status === "applied" ? "done" : "wait" });
        after?.();
        return true;
      } catch (e) { toastError(e, "Chưa cập nhật được"); return false; }
    } },
  ] });
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
