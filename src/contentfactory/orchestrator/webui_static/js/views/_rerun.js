// "Chạy lại" (D-113): chọn chính xác các bước muốn chạy lại cho một job, banner phiên đang chạy, lịch sử các lần chạy lại.
// Không có luật nghiệp vụ ở đây: bước nào chọn được/cần chọn thêm gì/thứ tự chạy do backend trả (rerun-options, rerun-plan); giao diện chỉ vẽ và gửi lựa chọn.
import { api, newRequestId } from "../api.js";
import { h, clear } from "../dom.js";
import { icon } from "../icons.js";
import { btn, alertBox, openDialog, toast, toastError, disclosure } from "../components.js";
import { relTime, duration } from "../format.js";
import { externalLink } from "./_batch_ui.js";
import * as R from "../rerun_logic.js";

const tone = { succeeded: "done", failed: "fail", running: "running", queued: "queue", cancelled: "wait", partial: "wait", pending: "queue", skipped: "queue" };
const pill = (state, text) => h("span", { class: "badge", dataset: { tone: tone[state] || "queue" } }, text);

// ====================================================================================== hộp thoại chọn bước
export async function openRerunDialog(job, { after } = {}) {
  let opts;
  try { opts = await api.get(`/api/jobs/${job.id}/rerun-options`); }
  catch (e) { toastError(e, "Chưa mở được “Chạy lại”"); return false; }
  const selected = new Set();
  const rid = R.requestIds(newRequestId);
  const seq = R.latest();
  let plan = null, planning = false, confirmed = false, submitting = false, timer = null, goBtn = null, gone = false;

  const list = h("fieldset", { class: "rr-list" }, h("legend", { class: "label" }, "Các bước của pipeline"));
  const toolbar = h("div", { class: "row wrap" });
  const blockedHost = h("div", { "aria-live": "polite" });
  const noteHost = h("div", { class: "stack" });
  const summaryHost = h("div", { class: "rr-summary", "aria-live": "polite", "aria-atomic": "true" });
  const rows = new Map();

  function build() {
    list.replaceChildren(list.querySelector("legend"));
    rows.clear();
    for (const s of opts.stages) {
      const cb = h("input", { type: "checkbox", id: `rr-${s.id}`, value: s.id });
      const msg = h("div", { class: "rr-msg small", id: `rr-msg-${s.id}` });
      cb.setAttribute("aria-describedby", msg.id);
      cb.addEventListener("change", () => { cb.checked ? selected.add(s.id) : selected.delete(s.id); changed(); });
      const chips = h("span", { class: "rr-chips" });
      const label = h("label", { class: "rr-row", for: cb.id }, cb, h("span", { class: "rr-main" }, h("span", { class: "rr-name" }, s.label), h("span", { class: "small muted rr-count" }, R.countLabel(s.rerun_count)), chips));
      const li = h("div", { class: "rr-item" }, label, msg);
      rows.set(s.id, { cb, msg, chips, li, s });
      list.append(li);
    }
    selected.forEach((id) => { if (!rows.get(id)?.s.eligible) selected.delete(id); });
  }

  function paintRows() {
    const probs = R.problemsById(plan);
    const blocked = !!opts.blocked;
    for (const { cb, msg, chips, li, s } of rows.values()) {
      cb.disabled = blocked || !s.eligible;
      cb.checked = selected.has(s.id);
      li.dataset.disabled = cb.disabled ? "1" : "";
      const bits = [];
      if (s.stale) bits.push(h("span", { class: "chip warn", title: s.stale_by?.length ? `Không còn đồng bộ với: ${R.labels(opts, s.stale_by)}` : "Tham số/đầu vào đã đổi sau lần chạy gần nhất" }, icon("alert", { size: 12 }), "Cũ · không đồng bộ"));
      chips.replaceChildren(...bits);
      let text = "", cls = "";
      if (blocked) text = "";
      else if (!s.eligible) { text = s.hint || "Chưa chạy lại được bước này."; cls = "off"; }
      else if (selected.has(s.id) && probs[s.id]?.length) { text = probs[s.id].join(" "); cls = "warn"; }
      else if (selected.has(s.id) && !s.ready_alone && !plan) text = s.hint || "";
      msg.textContent = text;
      msg.dataset.tone = cls;
    }
  }

  function paintSummary() {
    const sum = R.summary(plan, opts);
    const kids = [h("div", { class: "rr-sum-title" }, selected.size ? (planning && !plan ? "Đang kiểm tra lựa chọn…" : sum.title) : "Chưa chọn bước nào.")];
    if (selected.size && plan?.order?.length) kids.push(h("ol", { class: "autolist rr-order" }, ...plan.order.map((id, i) => h("li", null, sum.lines[i].replace(/^\d+\.\s*/, "")))));
    if (selected.size && plan && !plan.ok && !opts.blocked) {
      const sug = R.suggestionText(plan, opts);
      const first = (plan.stages || []).flatMap((s) => s.problems || [])[0];
      kids.push(alertBox({ tone: "wait", title: first?.message || "Lựa chọn này chưa chạy được.", body: first?.hint && !sug ? first.hint : null, actions: sug ? [btn({ label: "Chọn các bước cần thiết", icon: "plus", size: "sm", onClick: addSuggested })] : [] }));
      if (sug) kids.push(h("p", { class: "small muted" }, sug + "."));
    }
    summaryHost.replaceChildren(...kids);
  }

  function paintNotes() {
    const kids = [];
    if (selected.has("story") && opts.story_guidance) {
      const g = opts.story_guidance;
      kids.push(h("p", { class: "small", role: "note" }, g.text ? [`Truyện sẽ dùng ${R.GUIDANCE_SOURCE[g.source] || g.source}: `, h("em", null, g.text.length > 160 ? g.text.slice(0, 160) + "…" : g.text)] : "Truyện sẽ chạy không kèm đề xuất truyện."));
    }
    if (selected.has("publish")) {
      const cb = h("input", { type: "checkbox", id: "rr-confirm-publish", checked: confirmed });
      cb.addEventListener("change", () => { confirmed = cb.checked; sync(); });
      kids.push(alertBox({ tone: "wait", iconName: "alert", title: R.PUBLISH_WARNING, body: h("label", { class: "rr-confirm", for: cb.id }, cb, "Tôi hiểu và muốn đăng một video mới") }));
    } else confirmed = false;
    noteHost.replaceChildren(...kids);
  }

  function sync() {
    if (goBtn) goBtn.disabled = submitting || planning || !R.canSubmit({ selected: [...selected], plan, options: opts, confirmed });
  }
  function paintAll() { paintRows(); paintSummary(); paintNotes(); sync(); }

  function changed() {
    plan = null;
    clearTimeout(timer);
    if (!selected.size) { seq.next(); planning = false; paintAll(); return; }
    planning = true;
    paintAll();
    timer = setTimeout(refreshPlan, 250);
  }
  async function refreshPlan() {
    if (gone || !selected.size) return;
    const n = seq.next();
    try {
      const p = await api.post(`/api/jobs/${job.id}/rerun-plan`, { stages: R.orderedSelection(opts, selected) });
      if (!seq.isCurrent(n) || gone) return;
      plan = p;
    } catch (e) { if (!seq.isCurrent(n)) return; plan = null; toastError(e, "Không kiểm tra được lựa chọn"); }
    planning = false;
    paintAll();
  }
  function addSuggested() {
    for (const id of R.withSuggested([...selected], plan)) if (rows.get(id)?.s.eligible) selected.add(id);
    changed();
  }

  async function reload() {
    try { opts = await api.get(`/api/jobs/${job.id}/rerun-options`); } catch { return; }
    build(); paintBlocked(); changed();
  }
  function paintBlocked() {
    blockedHost.replaceChildren(...(opts.blocked ? [alertBox({ tone: "wait", iconName: "alert", title: opts.blocked.message, body: opts.blocked.hint })] : []));
    all.disabled = none.disabled = !!opts.blocked;
  }

  const all = btn({ label: "Chọn tất cả bước chạy được", size: "sm", onClick: () => { R.selectableIds(opts).forEach((i) => selected.add(i)); changed(); } });
  const none = btn({ label: "Bỏ chọn", size: "sm", kind: "ghost", onClick: () => { selected.clear(); changed(); } });
  toolbar.append(all, none);
  build();
  paintBlocked();
  paintAll();

  const content = h("div", { class: "stack rr-dialog" },
    h("p", { class: "muted small" }, "Chỉ các bước bạn tích mới chạy, theo đúng thứ tự pipeline; không bước nào tự được thêm. Kết quả cũ được giữ trong lịch sử, bước phía sau không được chọn sẽ hiện “không đồng bộ”."),
    blockedHost, list, toolbar, noteHost, summaryHost);

  const r = await openDialog({ title: "Chạy lại các bước", wide: true, content, onOpen: ({ buttons }) => { goBtn = buttons.go; sync(); },
    actions: [{ label: "Hủy", value: null }, { id: "go", label: "Chạy lại các bước đã chọn", kind: "primary", value: "ok", disabled: true, onClick: async () => {
      if (submitting || !R.canSubmit({ selected: [...selected], plan, options: opts, confirmed })) return false;
      submitting = true; sync();
      const ids = R.orderedSelection(opts, selected);
      try {
        const x = await api.post(`/api/jobs/${job.id}/rerun`, R.payload(ids, rid(ids)));
        toast({ title: x.message || "Đã xếp lượt chạy lại", tone: "done" });
        after?.();
        return true;
      } catch (e) {
        toastError(e, "Chưa chạy lại được");
        submitting = false;
        await reload();
        return false;
      }
    } }] });
  gone = true;
  clearTimeout(timer);
  return r === "ok";
}

// ====================================================================================== banner phiên đang chạy
export function rerunBanner(d) {
  const a = d.rerun?.active;
  if (!a) return null;
  return alertBox({ tone: "info", iconName: "refresh", role: "status", title: R.activeLine(a),
    body: h("div", { class: "small" }, "Các bước khác giữ nguyên; job không bị đưa về đầu pipeline.") });
}

// ====================================================================================== lịch sử
function stageLine(s) {
  const meta = [];
  if (s.started_at && s.ended_at) meta.push(duration(s.ended_at - s.started_at));
  else if (s.started_at) meta.push(relTime(s.started_at));
  if (s.attempts > 1) meta.push(`${s.attempts} lần thử`);
  const state = s.state || s.status;
  const li = h("li", { class: "rr-hist-stage" }, h("span", { class: "rr-hist-name" }, s.label), pill(state, R.STAGE_STATE_LABEL[state] || ({ succeeded: "Xong" }[state]) || state), meta.length ? h("span", { class: "small muted" }, meta.join(" · ")) : null);
  if (s.error?.message) li.append(h("div", { class: "small rr-hist-err" }, s.error.message));
  if (s.remote?.url && /^https:\/\//i.test(s.remote.url)) li.append(externalLink(s.remote.url, `Video đã đăng ${s.remote.id || ""}`.trim()));
  else if (s.remote?.id) li.append(h("span", { class: "small mono" }, `Remote ${s.remote.id}`));
  if (s.guidance) {
    li.append(h("details", { class: "small" }, h("summary", null, "Chi tiết kỹ thuật"),
      h("div", null, `Nguồn đề xuất truyện: ${R.GUIDANCE_SOURCE[s.guidance.source] || s.guidance.source}`),
      s.guidance.text ? h("div", { class: "sg-quote" }, s.guidance.text) : h("div", { class: "muted" }, "Không có đề xuất."),
      s.guidance.hash ? h("div", { class: "mono muted" }, `hash ${s.guidance.hash}`) : null));
  }
  return li;
}

/** {el, reload()}: thẻ "Lịch sử chạy lại" tải lười khi mở. */
export function rerunHistoryCard(jobId) {
  const host = h("div", { class: "stack" }, h("p", { class: "muted small" }, "Đang tải…"));
  let open = false;
  async function load() {
    try {
      const r = await api.get(`/api/jobs/${jobId}/reruns`);
      const kids = [];
      if (!r.sessions.length) kids.push(h("p", { class: "muted small" }, "Chưa có lần chạy lại nào. Các lần chạy lại thủ công sẽ hiện ở đây."));
      for (const s of r.sessions) {
        kids.push(h("section", { class: "rr-hist", "aria-label": `Chạy lại #${s.number}` },
          h("div", { class: "row wrap" }, h("h3", null, `Chạy lại #${s.number}`), pill(s.result, R.RESULT_LABEL[s.result] || s.result), h("span", { class: "small muted" }, relTime(s.created_at))),
          h("ul", { class: "rr-hist-list" }, ...s.stages.map(stageLine))));
      }
      if (r.initial.length) kids.push(h("section", { class: "rr-hist", "aria-label": "Lần chạy ban đầu" }, h("h3", null, "Lần chạy ban đầu"), h("ul", { class: "rr-hist-list" }, ...r.initial.map(stageLine))));
      host.replaceChildren(...kids);
    } catch (e) { host.replaceChildren(h("p", { class: "small" }, e.message || "Không tải được lịch sử.")); }
  }
  const el = h("section", { class: "card", "aria-label": "Lịch sử chạy lại" }, disclosure({ label: "Lịch sử chạy lại", content: host, onToggle: (v) => { open = v; if (v) load(); } }));
  return { el, reload: () => { if (open) return load(); } };
}
