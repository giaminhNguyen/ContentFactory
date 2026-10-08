// Bộ chọn "Chế độ truyện" cho một job: Story hiện có | Story Remix + form cấu hình sinh từ schema của server.
// Story Remix chưa khả dụng ⇒ lựa chọn bị khóa kèm lý do (không có nút Chạy chết); form vẫn xem được ở dạng chỉ-đọc.
import { api } from "../api.js";
import { h, uid, clear } from "../dom.js";
import { field, input, select, switchCtl, textarea, alertBox, disclosure, badge } from "../components.js";
import { REMIX, OPTION_LABELS, initialState, validateField, validateAll, createPayload, listFromText, textFromList, changedCount, modeLabel } from "../story_mode_logic.js";

/** {el, ready, get(), payload(), validate()}: el là fieldset. `onChange` gọi mỗi lần đổi. */
export function storyModeEditor({ onChange } = {}) {
  const name = uid("sm-mode");
  const el = h("fieldset", { class: "sm-editor", hidden: true });
  let info = null, st = null, readOnly = false;
  const fields = {};                                              // "sec.key" -> {node, setErr}
  const radiosBox = h("div", { class: "mode-list sm-modes", role: "radiogroup", "aria-label": "Chế độ truyện" });
  const reasonBox = h("div", { "aria-live": "polite" });
  const formBox = h("div", { class: "stack sm-form" });
  const sumBox = h("div", { class: "sm-summary", "aria-live": "polite" });
  const detailBox = h("div", { class: "stack" });
  el.append(h("legend", { class: "label" }, "Chế độ truyện"), radiosBox, reasonBox, detailBox);

  function changed() { paintSummary(); onChange?.(); }

  function control(sec, f) {
    const v = st[sec][f.key], id = uid("smf");
    if (f.type === "bool") {
      const sw = switchCtl({ label: f.label, checked: v, id, onChange: (x) => { st[sec][f.key] = x; changed(); } });
      sw.input.disabled = readOnly;
      return { node: h("div", { class: "field" }, sw, f.hint ? h("p", { class: "muted small" }, f.hint) : null), setErr() {} };
    }
    let ctl, read;
    if (f.type === "select") {
      ctl = select({ id, options: f.rule.options.map((o) => [o, OPTION_LABELS[f.key]?.[o] || o]), value: v, disabled: readOnly });
      read = () => ctl.value;
    } else if (f.type === "list") {
      ctl = textarea({ id, rows: 2, value: textFromList(v), disabled: readOnly });
      read = () => listFromText(ctl.value);
    } else if (f.type === "int" || f.type === "number_or_null") {
      ctl = input({ id, type: "number", value: v ?? "", min: f.rule.min, max: f.rule.max, step: f.type === "int" ? 1 : "any", disabled: readOnly });
      read = () => (ctl.value === "" ? (f.type === "int" ? NaN : null) : Number(ctl.value));
    } else {
      ctl = input({ id, value: v, disabled: readOnly });
      read = () => ctl.value;
    }
    const fld = field({ label: f.label, control: ctl, hint: f.hint });
    ctl.addEventListener(f.type === "select" ? "change" : "input", () => { const nv = read(); st[sec][f.key] = nv; fld.setError(validateField(f, nv)); changed(); });
    return { node: fld, setErr: (m) => fld.setError(m) };
  }

  function paintForm() {
    clear(formBox);
    for (const k in fields) delete fields[k];
    const sections = [["story", "Story Remix"], ["character_universe", "Kho nhân vật"]];
    for (const [sec, title] of sections) {
      const body = h("div", { class: "stack" });
      for (const f of info.schema[sec]) { const c = control(sec, f); fields[`${sec}.${f.key}`] = c; body.append(c.node); }
      formBox.append(h("h3", { class: "small" }, title), body);
    }
  }

  function paintSummary() {
    const n = changedCount(info, st);
    sumBox.textContent = n ? `Đã tuỳ chỉnh ${n} mục so với mặc định.` : "Đang dùng cấu hình mặc định — chỉ cần bấm RUN.";
  }

  function paintModes() {
    clear(radiosBox);
    for (const m of info.modes) {
      const r = h("input", { type: "radio", name, value: m.id, checked: m.id === st.mode, disabled: !m.available });
      r.addEventListener("change", () => { st.mode = m.id; paintDetail(); changed(); });
      radiosBox.append(h("label", { class: "sm-opt" + (m.available ? "" : " disabled") }, r,
        h("span", null, h("strong", null, m.label), h("span", { class: "muted small" }, " — " + m.description), " ", m.available ? null : badge({ tone: "off", label: "Chưa khả dụng" }))));
    }
  }

  function paintDetail() {
    clear(reasonBox);
    clear(detailBox);
    const remix = info.modes.find((m) => m.id === REMIX);
    if (!remix.available) reasonBox.append(alertBox({ tone: "info", title: "Story Remix chưa khả dụng", body: remix.reason }));
    readOnly = st.mode !== REMIX;
    paintForm();
    paintSummary();
    detailBox.append(disclosure({ label: remix.available ? "Cấu hình Story Remix" : "Xem cấu hình mặc định của Story Remix (chỉ xem)", open: st.mode === REMIX && remix.available,
                                  content: h("div", { class: "stack", style: "padding-top: var(--s-2)" }, sumBox, formBox) }));
  }

  const ready = api.get("/api/story-mode").then((d) => {
    info = d; st = initialState(d);
    el.hidden = false;
    paintModes(); paintDetail();
  }).catch(() => { el.hidden = true; });          // không tải được schema: ẩn hẳn, job chạy như Story hiện có

  return {
    el, ready,
    get: () => st,
    payload: () => (info && st ? createPayload(info, st) : null),
    validate() {
      if (!info || st.mode !== REMIX) return true;
      const errs = validateAll(info, st);
      for (const [k, c] of Object.entries(fields)) c.setErr?.(errs[k] || null);
      const first = Object.keys(errs)[0];
      if (first) fields[first].node.querySelector?.("input,select,textarea")?.focus();
      return !first;
    },
    label: () => (info && st ? modeLabel(info, st.mode) : ""),
  };
}
