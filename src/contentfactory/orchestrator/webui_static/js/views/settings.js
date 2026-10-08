// Cài đặt & Doctor: tab "Sức khoẻ hệ thống" (Doctor) + một tab cho mỗi nhóm cài đặt. Cài đặt render từ bảng khai báo của backend (thêm tùy chọn = thêm một dòng ở service_admin.SETTINGS).
import { api } from "../api.js";
import { h, loadCss } from "../dom.js";
import { icon } from "../icons.js";
import { badge, btn, busy, alertBox, errorState, skeleton, pageHead, toast, toastError, confirmDialog, disclosure, tabs, select, input, switchCtl, kv } from "../components.js";
import { createPoller } from "../poller.js";
import { bytes, relTime } from "../format.js";
import { uid } from "../dom.js";
import { workersPanel } from "./_worker_runtime.js";

const FALLBACK_GROUPS = [["general", "Chung"], ["audio", "Audio"], ["render", "Render"], ["publishing", "Đăng"], ["resources", "Tài nguyên"], ["storage", "Lưu trữ"], ["advanced", "Nâng cao"]];
const GROUP_META = {
  healthy: { label: "Ổn", icon: "check-circle", tone: "done" }, warning: { label: "Cần xem", icon: "alert", tone: "wait" },
  needs_action: { label: "Cần xử lý", icon: "x-circle", tone: "fail" }, skipped: { label: "Chưa dùng", icon: "skip", tone: "off" },
};
const CHECK_STATUS = { ok: "healthy", warn: "warning", fail: "needs_action", skip: "skipped" };
const ORDER = { needs_action: 0, warning: 1, healthy: 2, skipped: 3 };
const STALE_S = 300;

export async function mount(root, ctx) {
  loadCss("/css/settings.css");
  const { navigate } = ctx;
  let tab = ctx.params[0] || "doctor";
  let destroyed = false, settings = null, settingsErr = null;
  const timers = new Set();
  let doctorPoller = null;

  const panel = h("div");
  const tabHost = h("div");
  root.append(pageHead("Cài đặt & Doctor", "Kiểm tra máy đã sẵn sàng chưa và chỉnh các thiết lập (mọi thứ đều có mặc định hợp lý)."), tabHost, panel);
  panel.append(skeleton(4));
  try { settings = await api.get("/api/settings"); } catch (e) { settingsErr = e; }
  if (destroyed) return { destroy() {} };

  const groups = settings ? settings.groups.map((g) => [g.id, g.label]) : FALLBACK_GROUPS;
  const items = [["doctor", "Sức khoẻ hệ thống"], ["workers", "Worker Runtime"], ...groups];
  if (!items.some(([id]) => id === tab)) tab = "doctor";
  tabHost.append(tabs({ items, active: tab, label: "Nhóm cài đặt", onSelect: (id) => { tab = id; history.replaceState(null, "", `#/settings/${id}`); show(); } }));

  function show() {
    stopDoctor();
    panel.setAttribute("role", "tabpanel");
    panel.id = `panel-${tab}`;
    panel.setAttribute("aria-labelledby", `tab-${tab}`);
    panel.replaceChildren();
    if (tab === "doctor") return doctorPanel();
    if (tab === "workers") return workersPanel(panel);
    if (settingsErr) { panel.append(errorState(settingsErr, () => location.reload())); return; }
    panel.append(settingsPanel(tab, groups.find(([id]) => id === tab)?.[1] || tab));
  }
  void navigate;

  // ====================================================================== Doctor
  function stopDoctor() { doctorPoller?.stop(); doctorPoller = null; }

  function doctorPanel() {
    const host = h("div", { class: "stack" }, skeleton(4));
    panel.append(host);
    const open = new Map();
    let waiters = [], requested = false;
    const rerun = btn({ label: "Kiểm tra lại", icon: "refresh" });
    const settle = (d) => { if (!d.running) { waiters.forEach((r) => r()); waiters = []; } };

    async function request() {
      await api.post("/api/doctor/run");
      requested = true;
      doctorPoller?.start();
      doctorPoller?.poke();
    }
    rerun.addEventListener("click", () => busy(rerun, async () => {
      try { const done = new Promise((r) => waiters.push(r)); await request(); await done; } catch (e) { toastError(e, "Không chạy được kiểm tra"); }
    }));

    doctorPoller = createPoller(async (signal) => {
      const d = await api.get("/api/doctor", { signal });
      if (destroyed || tab !== "doctor") return;
      const stale = !d.report || (d.finished && Date.now() / 1000 - d.finished > STALE_S);
      if (!d.running && stale && !requested) { requested = true; await api.post("/api/doctor/run"); return "fast"; }
      paint(d);
      settle(d);
      if (!d.running) { doctorPoller.stop(); return; }
      return "fast";
    }, { fast: 1000, idle: 4000 });
    doctorPoller.start();

    function paint(d) {
      const head = [];
      if (d.running) head.push(h("div", { class: "row", role: "status" }, icon("spinner", { size: 18, cls: "spin" }), h("span", null, "Đang kiểm tra máy…")));
      if (d.summary) {
        const s = d.summary;
        const [tone, title, body] = s.needs_action > 0 ? ["fail", "Cần xử lý trước khi chạy thật", `${s.needs_action} nhóm cần xử lý, ${s.warning} nhóm cần xem.`]
          : s.warning > 0 ? ["wait", "Chạy được, nhưng có điểm cần xem", `${s.warning} nhóm có cảnh báo; không chặn việc chạy.`] : ["done", "Sẵn sàng chạy", "Mọi thành phần đang dùng đều ổn."];
        head.push(alertBox({ tone, title, body: h("div", null, body, d.finished ? h("span", { class: "muted small" }, ` Kiểm tra ${relTime(d.finished)}.`) : null), actions: [rerun] }));
      } else if (!d.running) head.push(h("div", null, rerun));
      const list = h("div", { class: "doctor-list" });
      for (const g of (d.groups || []).slice().sort((a, b) => ORDER[a.status] - ORDER[b.status])) list.append(groupView(g, open));
      host.replaceChildren(...head, list);
    }
  }

  function groupView(g, open) {
    const meta = GROUP_META[g.status] || GROUP_META.skipped;
    const el = h("details", { class: "health-group", open: open.has(g.id) ? open.get(g.id) : g.status === "needs_action" || g.status === "warning" });
    el.addEventListener("toggle", () => open.set(g.id, el.open));
    el.append(h("summary", null, icon("chevron", { size: 16 }), h("span", { class: "g-label grow" }, g.label), badge(meta)));
    const body = h("div", { class: "checks" });
    for (const c of g.checks) {
      const cm = GROUP_META[CHECK_STATUS[c.status]] || GROUP_META.skipped;
      const lines = String(c.detail || "").split("\n");
      const long = lines.length > 1 || lines[0].length > 140;
      body.append(h("div", { class: "check" }, badge(cm),
        h("div", null, h("div", { class: "c-name" }, c.name), h("div", null, long ? `${lines[0].slice(0, 140)}…` : lines[0]),
          c.hint ? h("div", { class: "hint-fix" }, h("strong", null, "Cách sửa: "), c.hint) : null,
          long ? h("details", null, h("summary", null, "Chi tiết kỹ thuật"), h("pre", null, c.detail)) : null)));
    }
    el.append(body);
    return el;
  }

  // ====================================================================== Cài đặt
  function settingsPanel(group, label) {
    const box = h("section", { class: "card", "aria-label": label });
    const mine = settings.items.filter((i) => i.group === group);
    if (!mine.length) box.append(h("p", { class: "muted" }, "Nhóm này chưa có tuỳ chọn nào."));
    for (const it of mine) box.append(settingRow(it));
    const wrap = h("div", { class: "stack" }, box);
    if (group === "general") wrap.append(linkCards());
    if (group === "storage") wrap.append(storageCard(), cleanupCard());
    if (group === "advanced") wrap.append(advancedCard());
    return wrap;
  }

  const fmt = (it, v) => it.type === "bool" ? (v ? "Bật" : "Tắt") : it.type === "select" || it.type === "channel" ? (it.options?.find((o) => o[0] === v)?.[1] ?? v) : `${v}${it.unit ? ` ${it.unit}` : ""}`;

  function settingRow(it) {
    const cid = uid("set"), lid = cid + "-l";
    let saved = it.value;
    const err = h("div", { class: "s-err", role: "alert" });
    const note = h("span", { class: "saved", "aria-live": "polite" });
    const mod = h("span", { class: "chip warn", hidden: true }, "Đã đổi so với mặc định");
    const refreshMod = () => { mod.hidden = JSON.stringify(saved) === JSON.stringify(it.default); };
    refreshMod();
    let ctl, read, write;

    if (it.type === "bool") {
      const sw = switchCtl({ label: "", checked: !!it.value, id: cid, onChange: (v) => commit(v) });
      sw.input.setAttribute("aria-labelledby", lid);
      ctl = sw; read = () => sw.input.checked;
      write = (v) => { sw.input.checked = v; sw.querySelector(".state").textContent = v ? "Bật" : "Tắt"; };
    } else if (it.type === "select" || it.type === "channel") {
      const s = select({ options: it.options || [], value: it.value, id: cid, onChange: (v) => commit(v) });
      ctl = s; read = () => s.value; write = (v) => { s.value = v; };
    } else {
      const i = input({ type: it.type === "text" ? "text" : "number", id: cid, value: it.value ?? "", min: it.min, max: it.max, step: it.step || (it.type === "int" ? 1 : "any"), maxlength: it.max_len, inputmode: it.type === "text" ? "text" : "decimal" });
      let t = null;
      i.addEventListener("input", () => { clearTimeout(t); timers.delete(t); t = setTimeout(() => commit(read()), 600); timers.add(t); });
      i.addEventListener("blur", () => { if (t) { clearTimeout(t); timers.delete(t); t = null; commit(read()); } });
      ctl = h("div", { class: "s-line" }, i, it.unit ? h("span", { class: "muted small" }, it.unit) : null);
      read = () => (it.type === "text" ? i.value : i.value === "" ? NaN : Number(i.value));
      write = (v) => { i.value = v ?? ""; };
    }

    function validate(v) {
      if (it.type === "text") return v.trim() ? null : "Không được để trống.";
      if (it.type === "int" || it.type === "number") {
        if (Number.isNaN(v)) return "Nhập một số.";
        if (it.type === "int" && !Number.isInteger(v)) return "Phải là số nguyên.";
        if (it.min != null && v < it.min || it.max != null && v > it.max) return `Nhập giá trị từ ${it.min} đến ${it.max}.`;
      }
      return null;
    }

    let inflight = false;
    async function commit(v) {
      if (JSON.stringify(v) === JSON.stringify(saved) || inflight || destroyed) return;
      const bad = validate(v);
      err.replaceChildren();
      if (bad) { err.append(icon("alert-circle", { size: 14 }), h("span", null, bad)); ctl.querySelector?.("input")?.setAttribute("aria-invalid", "true"); return; }
      ctl.querySelector?.("input")?.removeAttribute("aria-invalid");
      const risky = it.danger && ((it.type === "bool" && v === false) || ((it.type === "int" || it.type === "number") && v < saved));
      if (risky && !(await confirmDialog({ title: "Thay đổi có rủi ro", body: it.danger, confirmLabel: "Vẫn thay đổi", danger: true }))) { write(saved); return; }
      inflight = true;
      try {
        const r = await api.put("/api/settings", { changes: { [it.key]: v } });
        saved = v;
        refreshMod();
        note.textContent = r.restart_needed?.length ? "Đã lưu — có hiệu lực sau khi mở lại ứng dụng" : "Đã lưu";
        const t = setTimeout(() => { timers.delete(t); note.textContent = ""; }, 2000);
        timers.add(t);
      } catch (e) {
        write(saved);
        toastError(e, `Không lưu được “${it.label}”`);
      } finally { inflight = false; }
    }

    const left = h("div", null, h("label", { class: "s-label", id: lid, for: cid }, it.label), it.help ? h("div", { class: "s-help" }, it.help) : null,
      it.danger ? h("div", { class: "s-warn" }, icon("alert", { size: 14 }), h("span", null, it.danger)) : null);
    return h("div", { class: "setting" }, left, h("div", { class: "s-ctl" }, ctl, err, h("div", { class: "s-meta" }, h("span", null, `Mặc định: ${fmt(it, it.default)}`), mod, it.restart ? h("span", null, "Có hiệu lực sau khi mở lại ứng dụng") : null), note));
  }

  function linkCards() {
    const mk = (href, ic, t, s) => h("a", { class: "link-card", href }, icon(ic, { size: 22 }), h("span", null, t, h("small", null, s)));
    return h("section", { class: "card", "aria-labelledby": "go-h" }, h("h2", { id: "go-h" }, "Các khu vực liên quan"), h("div", { class: "link-cards" },
      mk("#/channels", "tv", "Kênh", "Tên, watermark, giọng, video nền, đăng"), mk("#/tts", "mic", "Giọng đọc", "Engine và profile TTS"), mk("#/pools", "film", "Nguồn Media", "Video nền, ảnh thumbnail")));
  }

  function storageCard() {
    const rows = settings.storage.map((s) => h("div", { class: "storage-row" }, h("strong", null, s.name), h("span", { class: "mono trunc", title: s.path }, s.path),
      h("span", { class: "nowrap" }, `${s.bytes == null ? "rất nhiều dữ liệu" : bytes(s.bytes)} · trống ${bytes(s.free_bytes)}`)));
    return h("section", { class: "card", "aria-labelledby": "st-h" }, h("h2", { id: "st-h" }, "Dung lượng đang dùng"), ...rows);
  }

  function cleanupCard() {
    const out = h("div", { "aria-live": "polite" });
    const now = btn({ label: "Dọn ngay", icon: "trash", kind: "danger", disabled: true });
    const preview = btn({ label: "Xem trước dọn dẹp", icon: "search" });
    const show = (r, done) => out.replaceChildren(alertBox({ tone: done ? "done" : "info", title: done ? "Đã dọn xong" : "Sẽ dọn", body: `${r.files} file, ${bytes(r.bytes)}.${Object.keys(r.by_kind || {}).length ? " " + Object.entries(r.by_kind).map(([k, v]) => `${k}: ${v}`).join(", ") : ""}` }));
    preview.addEventListener("click", () => busy(preview, async () => {
      try { const r = await api.post("/api/cleanup", { dry_run: true }); show(r, false); now.disabled = r.files === 0; } catch (e) { toastError(e, "Không xem trước được"); }
    }));
    now.addEventListener("click", async () => {
      if (!(await confirmDialog({ title: "Dọn dẹp ngay?", body: "Xoá file trung gian, dữ liệu job cũ và cache quá cỡ. Thư mục output của bạn không bao giờ bị đụng tới. Không hoàn tác được.", confirmLabel: "Dọn ngay", danger: true }))) return;
      await busy(now, async () => {
        try { const r = await api.post("/api/cleanup", { dry_run: false }); show(r, true); now.disabled = true; toast({ title: "Đã dọn dẹp", tone: "done" }); } catch (e) { toastError(e, "Không dọn được"); }
      });
    });
    return h("section", { class: "card stack", "aria-labelledby": "cl-h" }, h("h2", { id: "cl-h" }, "Dọn dẹp"), h("p", { class: "muted small" }, "Hệ thống tự dọn theo cài đặt phía trên; dùng nút này khi cần giải phóng ổ đĩa ngay. Luôn xem trước trước khi dọn."),
      h("div", { class: "row" }, preview, now), out);
  }

  function advancedCard() {
    const p = settings.paths;
    const pre = h("pre", { class: "cfg-pre", tabindex: "0", "aria-label": "Cấu hình đang áp dụng (JSON)" }, "Đang tải…");
    let loaded = false;
    const body = h("div", { class: "stack" }, kv([["Thư mục gốc", p.root], ["Workspace (của hệ thống)", p.workspace], ["Output (của bạn)", p.output], ["Runtime", p.runtime], ["Cấu hình máy này", p.config_local]]),
      h("p", { class: "muted small" }, "Bí mật (token, key) đã được che. Chỉnh trực tiếp bằng file cấu hình nếu cần."), pre);
    const d = disclosure({ label: "Cấu hình đang áp dụng", content: body, onToggle: async (v) => {
      if (!v || loaded) return;
      try { pre.textContent = JSON.stringify(await api.get("/api/config/effective"), null, 2); loaded = true; } catch (e) { pre.textContent = e.message; }
    } });
    return h("section", { class: "card", "aria-labelledby": "adv-h" }, h("h2", { id: "adv-h", style: "margin-bottom: var(--s-3)" }, "Nâng cao"), d);
  }

  show();
  return { destroy() { destroyed = true; stopDoctor(); timers.forEach(clearTimeout); timers.clear(); } };
}
