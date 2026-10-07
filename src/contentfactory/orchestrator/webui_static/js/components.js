// Thành phần dùng chung. Quy tắc: view KHÔNG tự dựng badge/nút/alert/empty/field riêng — dùng các hàm này để giao diện nhất quán.
import { h, clear, uid } from "./dom.js";
import { icon } from "./icons.js";
import { jobStatus, stageState } from "./status.js";
import * as motion from "./motion.js";
import { ApiError } from "./api.js";

// ---------- badge trạng thái (chữ + biểu tượng + màu; không chỉ màu) ----------
export function badge(meta, labelOverride) {
  const el = h("span", { class: "badge", dataset: { tone: meta.tone } });
  el.append(icon(meta.icon, { size: 14, cls: meta.spin ? "spin" : "" }), h("span", null, labelOverride || meta.label));
  return el;
}
export const jobBadge = (s) => badge(jobStatus(s));
export const stageBadge = (s) => badge(stageState(s));
export function updateBadge(el, meta, label) {
  if (el.dataset.tone === meta.tone && el.lastChild.textContent === (label || meta.label)) return;
  const changed = el.dataset.tone !== meta.tone;
  const fresh = badge(meta, label);
  el.dataset.tone = meta.tone;
  el.replaceChildren(...fresh.childNodes);
  if (changed) motion.pulse(el);
}

// ---------- nút ----------
export function btn({ label, icon: ic, kind = "", size = "", onClick, type = "button", disabled = false, title, href, ariaLabel }) {
  const cls = ["btn", kind, size].filter(Boolean).join(" ");
  const el = href ? h("a", { class: cls, href, title }) : h("button", { class: cls, type, title, disabled });
  if (ariaLabel) el.setAttribute("aria-label", ariaLabel);
  if (ic) el.append(icon(ic, { size: size === "sm" ? 16 : 18 }));
  if (label) el.append(h("span", null, label));
  if (onClick) el.addEventListener("click", onClick);
  return el;
}

// Khóa nút trong lúc gọi bất đồng bộ (lớp chống bấm đúp phía giao diện; backend còn có idempotency riêng).
export async function busy(button, fn) {
  if (button.dataset.busy) return;
  button.dataset.busy = "1";
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  const first = button.querySelector("svg");
  const spinner = icon("spinner", { size: 18, cls: "spin" });
  if (first) first.replaceWith(spinner); else button.prepend(spinner);
  try { return await fn(); }
  finally {
    delete button.dataset.busy;
    button.disabled = false;
    button.removeAttribute("aria-busy");
    spinner.replaceWith(first || document.createComment(""));
  }
}

// ---------- form ----------
export function field({ label, hint, error, control, id, required }) {
  const fid = id || control.id || uid("f");
  control.id = fid;
  const lab = h("label", { for: fid }, label, required ? h("span", { class: "muted", "aria-hidden": "true" }, " *") : null);
  const el = h("div", { class: "field" }, lab, control);
  const describe = [];
  if (hint) { const hid = fid + "-hint"; el.append(h("div", { class: "hint", id: hid }, hint)); describe.push(hid); }
  const errBox = h("div", { class: "error", id: fid + "-err", role: "alert" });
  el.append(errBox);
  const setError = (msg) => {
    clear(errBox);
    if (msg) { errBox.append(icon("alert-circle", { size: 14 }), h("span", null, msg)); control.setAttribute("aria-invalid", "true"); control.setAttribute("aria-describedby", [...describe, errBox.id].join(" ")); }
    else { control.removeAttribute("aria-invalid"); if (describe.length) control.setAttribute("aria-describedby", describe.join(" ")); else control.removeAttribute("aria-describedby"); }
  };
  if (describe.length) control.setAttribute("aria-describedby", describe.join(" "));
  el.setError = setError;
  if (error) setError(error);
  return el;
}

export function input(opts = {}) {
  const { onInput, ...rest } = opts;
  const el = h("input", { class: "input", type: "text", autocomplete: "off", spellcheck: false, ...rest });
  if (onInput) el.addEventListener("input", () => onInput(el.value));
  return el;
}

export function textarea(opts = {}) {
  const { onInput, value, ...rest } = opts;
  const el = h("textarea", { class: "textarea prose", spellcheck: false, ...rest });
  if (value != null) el.value = value;
  if (onInput) el.addEventListener("input", () => onInput(el.value));
  return el;
}

export function select({ options, value, onChange, id, disabled }) {
  const el = h("select", { class: "select", id, disabled });
  for (const o of options) { const [v, l] = Array.isArray(o) ? o : [o, o]; el.append(h("option", { value: v }, l)); }
  if (value != null) el.value = value;
  if (onChange) el.addEventListener("change", () => onChange(el.value));
  return el;
}

export function switchCtl({ label, checked, onChange, id, describedBy, disabled }) {
  const fid = id || uid("sw");
  const cb = h("input", { type: "checkbox", id: fid, checked: !!checked, disabled, role: "switch" });
  if (describedBy) cb.setAttribute("aria-describedby", describedBy);
  const state = h("span", { class: "state", "aria-hidden": "true" }, checked ? "Bật" : "Tắt");
  cb.addEventListener("change", () => { state.textContent = cb.checked ? "Bật" : "Tắt"; onChange?.(cb.checked); });
  const el = h("label", { class: "switch", for: fid }, cb, h("span", { class: "track", "aria-hidden": "true" }), h("span", null, label), state);
  el.input = cb;
  return el;
}

// ---------- tiến độ ----------
export function progress(fraction, tone = "running", label = "Tiến độ") {
  const bar = h("i");
  const el = h("div", { class: "progress", role: "progressbar", "aria-label": label, "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round((fraction || 0) * 100)), dataset: { tone } }, bar);
  bar.style.transform = `scaleX(${fraction || 0})`;
  return el;
}
export function updateProgress(el, fraction, tone) {
  const f = fraction || 0;
  el.dataset.tone = tone;
  el.setAttribute("aria-valuenow", String(Math.round(f * 100)));
  motion.setProgress(el.firstChild, f);
}

// ---------- alert / empty / error / skeleton ----------
const ALERT_ICON = { wait: "hourglass", attn: "alert", fail: "x-circle", done: "check-circle", info: "info" };
export function alertBox({ tone = "info", title, body, actions = [], role, iconName }) {
  const el = h("div", { class: "alert", dataset: { tone }, role: role || (tone === "fail" ? "alert" : "status") });
  el.append(icon(iconName || ALERT_ICON[tone], { size: 20 }));
  const b = h("div", { class: "body" });
  if (title) b.append(h("div", { class: "title" }, title));
  if (body) b.append(typeof body === "string" ? h("div", null, body) : body);
  if (actions.length) b.append(h("div", { class: "row" }, ...actions));
  el.append(b);
  return el;
}

export function emptyState({ icon: ic = "folder", title, text, action }) {
  return h("div", { class: "empty" }, icon(ic, { size: 40 }), h("h2", null, title), text ? h("p", null, text) : null, action || null);
}

export function errorState(err, retry) {
  const e = err instanceof ApiError ? err : new ApiError(err?.message || "Có lỗi không mong đợi.");
  return alertBox({ tone: "fail", title: e.message, body: e.hint || null, actions: retry ? [btn({ label: "Thử lại", icon: "refresh", size: "sm", onClick: retry })] : [] });
}

export function skeleton(rows = 3) {
  const el = h("div", { class: "stack", "aria-busy": "true", "aria-label": "Đang tải" });
  for (let i = 0; i < rows; i++) { const s = h("div", { class: "skeleton" }); s.style.height = i ? "18px" : "28px"; s.style.width = `${90 - i * 12}%`; el.append(s); }
  return el;
}

// ---------- toast ----------
export function toast({ title, message, tone = "info", sticky = false }) {
  const box = document.getElementById("toasts");
  if (!box) return;
  while (box.children.length >= 3) box.firstChild.remove();
  const close = btn({ icon: "x", kind: "ghost", size: "sm", ariaLabel: "Đóng thông báo" });
  const el = h("div", { class: "toast", dataset: { tone }, role: tone === "fail" ? "alert" : "status" },
    icon({ done: "check-circle", fail: "x-circle", wait: "hourglass", info: "info" }[tone] || "info", { size: 20 }),
    h("div", { class: "t-body" }, h("div", { class: "t-title" }, title), message ? h("div", { class: "t-msg" }, message) : null), close);
  const remove = () => motion.toastOut(el, () => el.remove());
  close.addEventListener("click", remove);
  box.append(el);
  motion.toastIn(el);
  if (!sticky && tone !== "fail") setTimeout(() => el.isConnected && remove(), 4500);
}

export function toastError(e, title = "Không làm được") {
  const err = e instanceof ApiError ? e : new ApiError(e?.message || "Có lỗi không mong đợi.");
  toast({ title, message: [err.message, err.hint].filter(Boolean).join(" "), tone: "fail", sticky: true });
}

// ---------- dialog (thẻ <dialog> gốc: tự bẫy focus, Esc để đóng, trả focus về nút gọi) ----------
export function openDialog({ title, content, actions, wide = false, describe, onOpen }) {
  return new Promise((resolve) => {
    const opener = document.activeElement;
    const titleId = uid("dlg");
    const dlg = h("dialog", { class: "dlg" + (wide ? " wide" : ""), "aria-labelledby": titleId });
    const bar = h("div", { class: "actions" });
    const buttons = {};                                     // theo `id` của action: để người gọi bật/tắt nút khi nội dung đổi (vd Lưu chỉ bật khi có thay đổi)
    const close = (value) => { dlg.close(); resolve(value); };
    for (const a of actions) {
      const b = btn({ label: a.label, kind: a.kind || "", disabled: a.disabled, onClick: async () => { if (a.onClick) { const r = await a.onClick(); if (r === false) return; } close(a.value); } });
      if (a.id) buttons[a.id] = b;
      bar.append(b);
    }
    dlg.append(...[h("h2", { id: titleId }, title), describe ? h("p", { class: "muted small" }, describe) : null, content, bar].filter(Boolean));            // Node.append(null) sẽ chèn chữ "null"
    dlg.addEventListener("cancel", (ev) => { ev.preventDefault(); close(undefined); });
    dlg.addEventListener("close", () => { dlg.remove(); if (opener && opener.isConnected) opener.focus(); });
    document.getElementById("dialogs").append(dlg);
    dlg.showModal();
    motion.dialogIn(dlg);
    onOpen?.({ close, buttons, dialog: dlg });
    (dlg.querySelector("[autofocus], input, select, textarea") || bar.lastElementChild)?.focus();
  });
}

export async function confirmDialog({ title, body, confirmLabel = "Đồng ý", danger = false }) {
  const r = await openDialog({ title, content: h("p", null, body), actions: [{ label: "Huỷ", value: false }, { label: confirmLabel, kind: danger ? "danger solid" : "primary", value: true }] });
  return r === true;
}

// ---------- vùng thu gọn (Nâng cao): nút + region, aria-expanded, animate height ----------
export function disclosure({ label, content, open = false, onToggle }) {
  const id = uid("disc");
  const region = h("div", { id, class: "disc-region" }, content);
  const chev = icon("chevron", { size: 16, cls: "chev" });
  const b = h("button", { type: "button", class: "btn ghost sm", "aria-expanded": String(open), "aria-controls": id }, chev, h("span", null, label));
  chev.style.transition = "transform 200ms";
  const set = (v, animate = true) => {
    b.setAttribute("aria-expanded", String(v));
    chev.style.transform = v ? "rotate(90deg)" : "";
    if (v) animate ? motion.expand(region) : (region.hidden = false);
    else animate ? motion.collapse(region) : (region.hidden = true);
    onToggle?.(v);
  };
  region.hidden = !open;
  chev.style.transform = open ? "rotate(90deg)" : "";
  b.addEventListener("click", () => set(b.getAttribute("aria-expanded") !== "true"));
  return h("div", null, b, region);
}

// ---------- tabs (WAI-ARIA tabs, mũi tên trái/phải) ----------
export function tabs({ items, active, onSelect, label }) {
  const bar = h("div", { class: "tabs", role: "tablist", "aria-label": label });
  const btns = items.map(([id, text]) => {
    const b = h("button", { type: "button", role: "tab", id: `tab-${id}`, "aria-selected": String(id === active), tabindex: id === active ? "0" : "-1", "aria-controls": `panel-${id}` }, text);
    b.addEventListener("click", () => select(id));
    return b;
  });
  const select = (id) => {
    btns.forEach((b, i) => { const on = items[i][0] === id; b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1; });
    onSelect(id);
  };
  bar.addEventListener("keydown", (e) => {
    const i = btns.indexOf(document.activeElement);
    if (i < 0 || !["ArrowRight", "ArrowLeft", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const n = e.key === "ArrowRight" ? (i + 1) % btns.length : e.key === "ArrowLeft" ? (i - 1 + btns.length) % btns.length : e.key === "Home" ? 0 : btns.length - 1;
    btns[n].focus();
    select(items[n][0]);
  });
  bar.append(...btns);
  return bar;
}

export function pageHead(title, subtitle, actions) {
  return h("div", { class: "page-head" }, h("div", null, h("h1", { id: "page-title" }, title), subtitle ? h("p", null, subtitle) : null), actions ? h("div", { class: "row" }, actions) : null);
}

export function kv(pairs) {
  const dl = h("dl", { class: "kv" });
  for (const [k, v] of pairs) if (v != null && v !== "") dl.append(h("dt", null, k), h("dd", null, v));
  return dl;
}
