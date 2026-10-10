// Bộ chọn "Chế độ truyện" cho một job: Story hiện có | Story Remix + mẫu cấu hình + form sinh từ schema của server + ước tính chi phí + cấu hình hiệu lực.
// Mặc định: chỉ cần chọn nguồn rồi bấm RUN; mọi thứ ở đây đều tuỳ chọn. Story Remix chưa khả dụng ⇒ lựa chọn bị khóa kèm lý do (không có nút chạy chết).
import { api } from "../api.js";
import { h, uid, clear } from "../dom.js";
import { btn, busy, field, input, select, switchCtl, textarea, alertBox, disclosure, badge, toast, toastError, openDialog, confirmDialog } from "../components.js";
import { REMIX, SCENE, isRemix, sectionsOf, schemaOf, bagOf, OPTION_LABELS, initialState, validateField, validateAll, createPayload, listFromText, textFromList, changedCount, modeLabel,
         SOURCE_LABEL, SYSTEM_PRESET, stateFromPreset, presetByName, resetToDefaults, formatValue, estimateLines, validPresetName } from "../story_mode_logic.js";

/** {el, ready, get(), payload(), validate()}: el là fieldset. `onChange` gọi mỗi lần đổi. */
export function storyModeEditor({ onChange } = {}) {
  const name = uid("sm-mode");
  const el = h("fieldset", { class: "sm-editor", hidden: true });
  let info = null, st = null, readOnly = false, preset = SYSTEM_PRESET, timer = null;
  const fields = {};                                              // "sec.key" -> {node, setErr}
  const radiosBox = h("div", { class: "mode-list sm-modes", role: "radiogroup", "aria-label": "Chế độ truyện" });
  const reasonBox = h("div", { "aria-live": "polite" });
  const presetBox = h("div", { class: "row sm-presets" });
  const formBox = h("div", { class: "stack sm-form" });
  const sumBox = h("div", { class: "sm-summary", "aria-live": "polite" });
  const estBox = h("div", { class: "stack small sm-estimate", "aria-live": "polite" });
  const effBox = h("div", { class: "stack small" });
  const detailBox = h("div", { class: "stack" });
  el.append(h("legend", { class: "label" }, "Chế độ truyện"), radiosBox, reasonBox, detailBox);

  function changed() { paintSummary(); scheduleEstimate(); onChange?.(); }

  function control(sec, f) {
    const bag = () => bagOf(st, st.mode, sec);
    const v = bag()[f.key], id = uid("smf");
    if (f.type === "bool") {
      const sw = switchCtl({ label: f.label, checked: v, id, onChange: (x) => { bag()[f.key] = x; changed(); } });
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
    ctl.addEventListener(f.type === "select" ? "change" : "input", () => { const nv = read(); bag()[f.key] = nv; fld.setError(validateField(f, nv)); changed(); });
    return { node: fld, setErr: (m) => fld.setError(m) };
  }

  function paintForm() {
    clear(formBox);
    for (const k in fields) delete fields[k];
    const mode = st.mode === SCENE ? SCENE : REMIX;
    for (const [sec, title] of sectionsOf(mode)) {
      const body = h("div", { class: "stack" });
      for (const f of schemaOf(info, mode)[sec]) { const c = control(sec, f); fields[`${sec}.${f.key}`] = c; body.append(c.node); }
      formBox.append(h("h3", { class: "small" }, title), body);
    }
  }

  function paintSummary() {
    const n = changedCount(info, st);
    sumBox.textContent = n ? `Đã tuỳ chỉnh ${n} mục so với mặc định.` : "Đang dùng cấu hình mặc định — chỉ cần bấm RUN.";
  }

  // ---------------------------------------------------------------- mẫu cấu hình
  function paintPresets() {
    clear(presetBox);
    if (st.mode === SCENE) return;                                           // Remix bám sự việc: cấu hình nhỏ, không có mẫu
    const sel = select({ options: [[SYSTEM_PRESET, "Mặc định hệ thống"], ...(info.presets || []).map((p) => [p.name, p.name + (info.default_preset === p.name ? " (mặc định cho job mới)" : "")])], value: preset });
    sel.setAttribute("aria-label", "Mẫu cấu hình Story Remix");
    sel.addEventListener("change", () => {
      const was = st.mode;
      preset = sel.value;
      st = stateFromPreset(info, presetByName(info, preset));
      st.mode = info.modes.find((m) => m.id === REMIX).available && preset ? REMIX : was;          // chọn một mẫu ⇒ dùng Story Remix; chọn "Mặc định hệ thống" giữ chế độ đang chọn
      paintModes(); paintDetail(); changed();
    });
    presetBox.append(h("label", { class: "small", for: (sel.id = uid("smp")) }, "Mẫu"), sel,
      btn({ label: "Lưu thành mẫu…", size: "sm", disabled: readOnly, onClick: saveDialog }),
      preset ? btn({ label: info.default_preset === preset ? "Bỏ mặc định" : "Đặt làm mặc định cho job mới", size: "sm", onClick: (e) => busy(e.currentTarget, () => setDefault(info.default_preset === preset ? null : preset)) }) : null,
      preset ? btn({ label: "Xoá mẫu", size: "sm", kind: "ghost", onClick: removePreset }) : null,
      btn({ label: "Khôi phục mặc định", size: "sm", kind: "ghost", disabled: readOnly, onClick: () => { st = resetToDefaults(info, st); preset = SYSTEM_PRESET; paintPresets(); paintDetail(); changed(); } }));
  }

  async function saveDialog() {
    const nm = input({ placeholder: "Ví dụ: Trinh thám u ám", maxlength: 40, value: preset });
    const nf = field({ label: "Tên mẫu", control: nm, required: true });
    const mk = h("input", { type: "checkbox" });
    const r = await openDialog({ title: "Lưu cấu hình thành mẫu", describe: "Lần sau chỉ cần chọn mẫu này (hoặc đặt làm mặc định để mọi job mới tự dùng).", content: h("div", { class: "stack" }, nf, h("label", { class: "row" }, mk, "Đặt làm mặc định cho job mới")),
      actions: [{ label: "Huỷ", value: null }, { label: "Lưu", kind: "primary", value: "ok", onClick: async () => {
        if (!validPresetName(nm.value)) { nf.setError("Tên gồm chữ, số, khoảng trắng, - hoặc _ (tối đa 40 ký tự)."); return false; }
        if (!validateAllOk()) { nf.setError("Cấu hình đang có lỗi: sửa các trường báo đỏ trước."); return false; }
        try { info = await api.put(`/api/story-presets/${encodeURIComponent(nm.value.trim())}`, { story_mode: { story: st.story, character_universe: st.character_universe }, make_default: mk.checked }); preset = nm.value.trim(); return true; }
        catch (e) { nf.setError(e.message + (e.hint ? " " + e.hint : "")); return false; }
      } }] });
    if (r === "ok") { toast({ title: "Đã lưu mẫu", message: preset, tone: "done" }); paintPresets(); paintModes(); }
  }

  const validateAllOk = () => Object.keys(validateAll(info, { ...st, mode: REMIX })).length === 0;

  async function setDefault(n) {
    try { info = await api.put("/api/story-presets-default", { name: n }); paintPresets(); paintModes(); toast({ title: n ? "Đã đặt mẫu mặc định" : "Đã bỏ mẫu mặc định", tone: "done" }); }
    catch (e) { toastError(e, "Chưa đổi được"); }
  }

  async function removePreset() {
    if (!(await confirmDialog({ title: `Xoá mẫu “${preset}”?`, body: "Các job đã tạo không bị ảnh hưởng.", confirmLabel: "Xoá", danger: true }))) return;
    try { info = await api.del(`/api/story-presets/${encodeURIComponent(preset)}`); preset = SYSTEM_PRESET; paintPresets(); paintModes(); toast({ title: "Đã xoá mẫu", tone: "done" }); }
    catch (e) { toastError(e, "Chưa xoá được"); }
  }

  // ---------------------------------------------------------------- ước tính + hiệu lực
  function scheduleEstimate() { clearTimeout(timer); timer = setTimeout(refreshEstimate, 350); }

  async function refreshEstimate() {
    if (!info || !isRemix(st.mode) || Object.keys(validateAll(info, st)).length) { clear(estBox); return; }
    try {
      const scene = st.mode === SCENE;
      const est = await api.post("/api/story-mode/estimate", { story_mode: scene ? { mode: SCENE, story: st.scene } : { story: st.story, character_universe: st.character_universe } });
      clear(estBox);
      estBox.append(h("strong", null, "Ước tính trước khi chạy"), ...estimateLines(est, (scene ? st.scene : st.story).budget_usd).map((l) => h("div", null, l)), h("div", { class: "muted" }, est.note));
    } catch { clear(estBox); }
  }

  async function refreshEffective() {
    clear(effBox);
    try {
      const mode = st.mode === SCENE ? SCENE : REMIX;
      const eff = await api.post("/api/story-mode/effective", { preset: mode === SCENE ? null : preset || null, story_mode: createPayload(info, { ...st, mode }) });
      const rows = [];
      for (const [sec] of sectionsOf(mode)) {
        for (const f of schemaOf(info, mode)[sec]) { const c = eff[sec][f.key]; rows.push(h("tr", null, h("td", null, f.label), h("td", null, formatValue(f, c.value)), h("td", null, badge({ tone: c.source === "job" ? "running" : c.source === "preset" ? "done" : "off", icon: c.source === "job" ? "settings" : c.source === "preset" ? "layers" : "home", label: SOURCE_LABEL[c.source] })))); }
      }
      effBox.append(h("p", { class: "muted" }, "Giá trị sẽ dùng khi chạy, và nó đến từ đâu (mặc định hệ thống → mẫu → riêng của job)."), h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", null, h("tr", null, ...["Mục", "Giá trị", "Nguồn"].map((t) => h("th", null, t)))), h("tbody", null, ...rows))));
    } catch (e) { effBox.append(alertBox({ tone: "wait", title: "Chưa xem được cấu hình hiệu lực", body: e.message })); }
  }

  function paintModes() {
    clear(radiosBox);
    for (const m of info.modes) {
      const r = h("input", { type: "radio", name, value: m.id, checked: m.id === st.mode, disabled: !m.available });
      r.addEventListener("change", () => { st.mode = m.id; paintDetail(); changed(); });
      radiosBox.append(h("label", { class: "sm-opt" + (m.available ? "" : " disabled") }, r,
        h("span", null, h("strong", null, m.label), h("span", { class: "muted small" }, " — " + m.description), " ", m.available ? null : badge({ tone: "off", label: "Chưa khả dụng" }),
          m.id === REMIX && info.default_preset ? badge({ tone: "done", icon: "layers", label: `Mẫu mặc định: ${info.default_preset}` }) : null)));
    }
  }

  function paintDetail() {
    clear(reasonBox);
    clear(detailBox);
    const cur = st.mode === SCENE ? SCENE : REMIX;
    const remix = info.modes.find((m) => m.id === cur) || info.modes.find((m) => m.id === REMIX);
    const name_ = cur === SCENE ? "Remix bám sự việc" : "Story Remix";
    if (!remix.available) reasonBox.append(alertBox({ tone: "info", title: `${name_} chưa khả dụng`, body: remix.reason }));
    if (cur === SCENE) reasonBox.append(alertBox({ tone: "info", title: "Chỉ dùng cho nguồn bạn có quyền chuyển thể", body: "Chế độ này giữ phần lớn câu chữ của nguồn và chỉ thay chi tiết/cảnh cần thiết. Chọn quyền sử dụng và tích xác nhận ở form bên dưới; ‘Không rõ’ bị chặn ngay khi tạo job." }));
    readOnly = !isRemix(st.mode);
    paintPresets();
    paintForm();
    paintSummary();
    const eff = disclosure({ label: "Xem cấu hình hiệu lực (mặc định → mẫu → job)", content: effBox, onToggle: (open) => { if (open) refreshEffective(); } });
    detailBox.append(disclosure({ label: remix.available ? `Cấu hình ${name_}` : `Xem cấu hình mặc định của ${name_} (chỉ xem)`, open: isRemix(st.mode) && remix.available,
                                  content: h("div", { class: "stack", style: "padding-top: var(--s-2)" }, presetBox, sumBox, formBox, estBox, eff) }));
    scheduleEstimate();
  }

  const ready = api.get("/api/story-mode").then((d) => {
    info = d;
    const pre = d.available && d.default_preset ? presetByName(d, d.default_preset) : null;
    st = pre ? stateFromPreset(d, pre) : initialState(d);
    if (pre) preset = pre.name;                                            // có mẫu mặc định: Story Remix + mẫu đó được chọn sẵn
    el.hidden = false;
    paintModes(); paintDetail();
  }).catch(() => { el.hidden = true; });          // không tải được schema: ẩn hẳn, job chạy như Story hiện có

  return {
    el, ready,
    get: () => st,
    payload: () => (info && st ? createPayload(info, st) : null),
    validate() {
      if (!info || !isRemix(st.mode)) return true;
      const errs = validateAll(info, st);
      for (const [k, c] of Object.entries(fields)) c.setErr?.(errs[k] || null);
      const first = Object.keys(errs)[0];
      if (first) fields[first].node.querySelector?.("input,select,textarea")?.focus();
      return !first;
    },
    label: () => (info && st ? modeLabel(info, st.mode) : ""),
  };
}
