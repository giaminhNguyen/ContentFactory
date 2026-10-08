// Worker Runtime — mục "Workers" trong Cài đặt (W1.UI.2, W1.UI.3):
// danh sách worker (health/status, model, pool, hành động) + thêm/sửa bằng dialog.
// Frontend chỉ ra lệnh; mọi quyết định (probe, scan, route) ở backend qua /api/workers*.
import { api } from "../api.js";
import { h, clear, loadCss } from "../dom.js";
import { icon } from "../icons.js";
import { btn, busy, field, input, select, switchCtl, badge, emptyState, errorState, skeleton, toast, toastError, confirmDialog, openDialog, kv, alertBox, disclosure } from "../components.js";
import { createPoller } from "../poller.js";
import { relTime, duration } from "../format.js";

const STATUS = {
  READY: { tone: "done", icon: "check-circle", label: "Sẵn sàng" },
  DETECTED: { tone: "wait", icon: "clock", label: "Phát hiện" },
  AUTH_REQUIRED: { tone: "fail", icon: "lock", label: "Cần đăng nhập" },
  BROKEN: { tone: "fail", icon: "x-circle", label: "Hỏng" },
  NOT_FOUND: { tone: "attn", icon: "search", label: "Mất CLI" },
  DISABLED: { tone: "off", icon: "pause", label: "Đã tắt" },
};
const sm = (s) => STATUS[s] || { tone: "off", icon: "dot", label: s || "—" };

// W2.1: health state (cùng nguồn với router/backend — chỉ hiển thị, không tự phán đoán).
const HEALTH = {
  HEALTHY: { tone: "done", icon: "check-circle", label: "Khoẻ" },
  DEGRADED: { tone: "wait", icon: "alert", label: "Suy giảm" },
  AUTH_BLOCKED: { tone: "fail", icon: "lock", label: "Cần đăng nhập" },
  QUOTA_BLOCKED: { tone: "fail", icon: "zap", label: "Hết quota" },
  COOLDOWN: { tone: "wait", icon: "clock", label: "Cooldown" },
  UNAVAILABLE: { tone: "off", icon: "x-circle", label: "Không dùng được" },
  DISABLED: { tone: "off", icon: "pause", label: "Đã tắt" },
};
const hm = (s) => HEALTH[s] || { tone: "off", icon: "dot", label: s || "—" };
const CIRCUIT_LABEL = { OPEN: "Circuit mở", HALF_OPEN: "Đang probe thử lại", CLOSED: "" };

const ERROR_LABEL = { QUOTA: "Hết quota", AUTH: "Chưa đăng nhập", TIMEOUT: "Quá giờ", INVALID_OUTPUT: "Output hỏng", UNKNOWN: "Lỗi lạ", ORPHANED: "Mồ côi sau restart" };
const errorLabel = (k) => ERROR_LABEL[k] || k;

// W2.UI.3: hệ quả disable/xoá worker/pool — hiện TRƯỚC khi xác nhận phá.
function impactAlert(imp, kind = "Thay đổi sẽ ảnh hưởng") {
  const parts = [];
  if (imp.pools?.length) parts.push(`${imp.pools.length} pool`);
  if (imp.work_types?.length) parts.push(`${imp.work_types.length} work type: ${imp.work_types.join(", ")}`);
  if (imp.running) parts.push(`${imp.running} task ĐANG chạy`);
  if (!parts.length) return h("p", { class: "muted small" }, "Không ảnh hưởng worker nào khác — có thể thay đổi an toàn.");
  return alertBox({ tone: "attn", iconName: "alert", title: kind, body: h("div", { class: "stack", style: "gap: var(--s-2)" },
    h("div", { class: "row", style: "flex-wrap: wrap" }, ...imp.pools.map((p) => h("span", { class: "chip" }, p)), ...imp.work_types.map((w) => h("code", { class: "mono" }, w))),
    imp.running ? h("p", { class: "small" }, `${imp.running} attempt đang chạy trên worker này — nếu thay đổi, attempt có thể bị đứt giữa chừng và sẽ được reconcile khi app khởi động lại.`) : h("p", { class: "small muted" }, "Không có attempt nào đang chạy.")) });
}

export function workersPanel(host) {
  loadCss("/css/settings.css");
  let data = null, sig = "", failedOnce = false;
  const list = h("div", { class: "wk-list" });
  const health = h("section", { class: "wk-health", role: "status", "aria-atomic": "true" });

  const scanBtn = btn({ label: "Quét CLI", icon: "refresh", size: "sm" });
  const addBtn = btn({ label: "Thêm worker…", icon: "plus", size: "sm", kind: "primary" });
  host.append(h("div", { class: "row spread" },
      h("div", null, h("h3", null, "Workers"),
        h("p", { class: "muted small" }, "Mỗi worker là một CLI cụ thể (Claude/Codex/Gemini/OpenCode). “Quét CLI” tự tìm CLI có trên PATH; worker chưa vào pool thì job dùng chế độ runtime trực tiếp.")),
      h("div", { class: "row" }, scanBtn, addBtn)),
    health,
    list,
    h("h3", { class: "wk-title" }, "Lịch sử chạy gần đây"),
    timelineBlock());
  list.append(skeleton(3));

  const poller = createPoller(async (signal) => {
    try { data = await api.get("/api/workers", { signal }); failedOnce = false; }
    catch (e) {
      if (e.name === "AbortError") throw e;
      if (!failedOnce) { clear(list); list.append(errorState(e, () => poller.poke())); failedOnce = true; }
      throw e;
    }
    paint();
    return "idle";
  }, { fast: 4000, idle: 10000 });

  function paintHealth() {
    const s = data.summary || {};
    const items = [
      ["HEALTHY", "Khoẻ"],
      ["DEGRADED", "Suy giảm"],
      ["AUTH_BLOCKED", "Cần đăng nhập"],
      ["QUOTA_BLOCKED", "Hết quota"],
      ["COOLDOWN", "Cooldown"],
      ["UNAVAILABLE", "Không dùng được"],
    ].map(([k, label]) => {
      const n = (s.counts || {})[k] || 0;
      const m = hm(k);
      return h("button", { type: "button", class: "wk-health-cell", dataset: { tone: n ? m.tone : "off" },
        title: label, disabled: !n, "aria-label": `${label}: ${n} worker` },
        h("strong", null, String(n)), h("span", null, label));
    });
    const circ = s.circuits && s.circuits.length ? h("span", { class: "wk-health-circ" },
      badge({ tone: "wait", icon: "activity", label: `${s.circuits.length} circuit mở` }),
      h("span", { class: "muted small" }, s.circuits.map((c) => c.name).join(", "))) : null;
    health.replaceChildren(h("div", { class: "row wrap" }, ...items), circ || h("div", null, h("span", { class: "muted small" }, "Không có circuit nào đang mở.")));
  }

  function paint() {
    const s = JSON.stringify([data.workers, data.pools]);
    paintHealth();
    if (s === sig) return;
    sig = s;
    clear(list);
    if (!data.workers.length) {
      list.append(emptyState({ icon: "server", title: "Chưa có worker nào",
        text: "Quét CLI để phát hiện các agent đã cài (tự thêm), hoặc thêm tay một đường dẫn CLI.",
        action: btn({ label: "Quét CLI", icon: "refresh", kind: "primary", onClick: () => scan() }) }));
      return;
    }
    for (const w of data.workers) list.append(card(w));
  }

  function membership(w) {
    const pools = (data.pools || []).filter((p) => p.members.includes(w.id));
    if (!pools.length) return h("span", { class: "muted small" }, "chưa vào pool");
    return pools.map((p) => h("span", { class: "chip" }, p.display_name || p.name));
  }

  function card(w) {
    const st = sm(w.status);
    const hState = hm(w.health);
    const drv = (data.drivers || []).find((d) => d.id === w.driver_id);
    const detailBtn = btn({ label: "Chi tiết", icon: "activity", size: "sm", kind: "ghost", ariaLabel: `Chi tiết sức khoẻ worker ${w.name}` });
    const testBtn = btn({ label: "Test", icon: "stethoscope", size: "sm", title: "Probe lại CLI", disabled: w.status === "DISABLED" });
    const editBtn = btn({ label: "Sửa", icon: "settings", size: "sm", kind: "ghost" });
    const sw = switchCtl({ label: "", checked: !!w.enabled, onChange: (v) => toggle(w, sw, v) });
    sw.input.setAttribute("aria-label", `Bật/tắt worker ${w.name}`);
    sw.querySelector(".state").textContent = w.enabled ? "Bật" : "Tắt";
    const delBtn = btn({ label: "Xoá", icon: "trash", size: "sm", kind: "ghost danger" });
    detailBtn.addEventListener("click", () => detail(w, detailBtn));
    testBtn.addEventListener("click", () => doTest(w, testBtn));
    editBtn.addEventListener("click", () => dialog(w));
    delBtn.addEventListener("click", () => remove(w, delBtn));
    const bits = [w.version ? `v${w.version}` : null, w.auth && w.auth !== "unknown" ? `auth: ${w.auth}` : null].filter(Boolean).join(" · ");
    const notReady = !w.routable.ok && w.enabled ? h("p", { class: "small", style: "color: var(--st-wait-fg)" }, icon("alert", { size: 14 }), " ", w.routable.reason, w.cooldown_s > 0 ? ` (cooldown ${Math.ceil(w.cooldown_s)}s)` : "") : null;
    const detail = w.detail && w.status !== "READY" ? h("p", { class: "small muted" }, w.detail) : null;
    let circuit = null;
    if (w.circuit && w.circuit !== "CLOSED") circuit = badge({ tone: "wait", icon: "activity", label: CIRCUIT_LABEL[w.circuit] || w.circuit });
    const lastErr = w.last_error ? h("p", { class: "small", style: "color: var(--st-fail-fg)" },
      icon("x-circle", { size: 14 }), " ", `${errorLabel(w.last_error.kind) || w.last_error.kind}${w.last_error.code ? ` (${w.last_error.code})` : ""}`, " · ", relTime(w.last_error.at)) : null;
    const meta = kv([
      ["Model mặc định", w.default_model || "—"],
      ["Model khả dụng", w.models.length ? w.models.join(", ") : "—"],
      ["Song song", String(w.concurrency)],
      ["Idle timeout", w.idle_timeout_s ? `${Math.round(w.idle_timeout_s)}s` : "—"],
      ["Hard timeout", w.hard_timeout_s ? `${Math.round(w.hard_timeout_s)}s` : "—"],
    ]);
    return h("section", { class: "card wk-card", "aria-label": `Worker ${w.name}` },
      h("div", { class: "row spread" },
        h("div", { class: "row" }, h("h3", { class: "wk-name" }, w.name), badge(st), badge(hState), circuit, h("span", { class: "chip" }, drv?.label || w.driver_id)),
        h("div", { class: "row" }, detailBtn, testBtn, editBtn, sw, delBtn)),
      h("p", { class: "mono small", title: w.executable }, w.executable),
      bits ? h("p", { class: "small muted" }, bits) : null,
      notReady, detail, lastErr,
      h("div", { class: "wk-meta" }, meta),
      h("div", { class: "row" }, h("span", { class: "muted small" }, "Pool:"), membership(w)));
  }

  async function scan() {
    await busy(scanBtn, async () => {
      try {
        const r = await api.post("/api/workers/scan", {});
        toast({ title: "Đã quét CLI", message: `Thêm ${r.added.length}, đã có ${r.existing.length}${r.missing.length ? `; mất: ${r.missing.join(", ")}` : ""}.`, tone: "done" });
        sig = ""; poller.poke();
      } catch (e) { toastError(e, "Không quét được"); }
    });
  }

  async function doTest(w, b) {
    await busy(b, async () => {
      try {
        const r = await api.post(`/api/workers/${w.id}/probe`, {});
        toast({ title: "Đã kiểm tra", message: `${r.name}: ${(STATUS[r.status] || sm(r.status)).label}${r.detail ? " — " + r.detail : ""}`, tone: r.status === "READY" ? "done" : "wait" });
        sig = ""; poller.poke();
      } catch (e) { toastError(e, "Probe lỗi"); }
    });
  }

  async function toggle(w, sw, checked) {
    if (!checked && w.enabled) {
      try {
        const imp = await api.get(`/api/workers/${w.id}/impact`);
        if (imp.running > 0 || imp.work_types.length) {
          if (!(await confirmDialog({ title: `Tắt worker “${w.name}”?`, body: impactAlert(imp), confirmLabel: "Tắt worker", danger: true }))) {
            sw.input.checked = true; sw.querySelector(".state").textContent = "Bật"; return;
          }
        }
      } catch (e) { toastError(e, "Không xem được ảnh hưởng"); }
    }
    try {
      await api.post(`/api/workers/${w.id}/${checked ? "enable" : "disable"}`, {});
      w.enabled = checked;
      sw.querySelector(".state").textContent = checked ? "Bật" : "Tắt";
      toast({ title: checked ? "Đã bật" : "Đã tắt", message: `${w.name} ${checked ? "trở lại sẵn sàng" : "không được route nữa"}.`, tone: "done" });
      sig = ""; poller.poke();
    } catch (e) { sw.input.checked = !checked; sw.querySelector(".state").textContent = !checked ? "Bật" : "Tắt"; toastError(e, "Chưa đổi được"); }
  }

  async function remove(w, b) {
    let imp = null;
    try { imp = await api.get(`/api/workers/${w.id}/impact`); } catch (e) { /* để confirm mặc định */ }
    const used = (data.pools || []).filter((p) => p.members.includes(w.id));
    const dep = used.length ? ` Worker sẽ bị gỡ khỏi pool: ${used.map((p) => p.display_name || p.name).join(", ")}.` : "";
    const body = h("div", { class: "stack", style: "gap: var(--s-3)" },
      h("p", null, `Xoá khỏi cấu hình; lịch sử attempt vẫn giữ.${dep}`),
      imp ? impactAlert(imp, "Hệ quả xoá") : null);
    if (!(await confirmDialog({ title: `Xoá worker “${w.name}”?`, body, confirmLabel: "Xoá worker", danger: true }))) return;
    await busy(b, async () => {
      try { await api.del(`/api/workers/${w.id}`); toast({ title: "Đã xoá worker", tone: "done" }); }
      catch (e) { toastError(e, "Không xoá được"); }
      sig = ""; poller.poke();
    });
  }

  function dialog(w) {
    const edit = !!w;
    const drivers = data.drivers || [];
    const nameIn = input({ placeholder: "Claude chính", value: w?.name || "" });
    const drvSel = select({ options: drivers.map((d) => [d.id, d.label]), value: w?.driver_id || "claude_cli" });
    const exeIn = input({ placeholder: "claude (tên trên PATH hoặc đường dẫn tuyệt đối)", value: w?.executable || "" });
    const exeHint = h("div", { class: "hint" });
    const setExeHint = () => {
      const d = drivers.find((x) => x.id === drvSel.value);
      exeHint.textContent = d ? `Biết tên CLI: ${d.exe_names.join(", ")} — để trống để dùng tên mặc định.` : "";
    };
    drvSel.addEventListener("change", setExeHint);
    setExeHint();
    const modelsIn = input({ placeholder: "để trống dùng mặc định của driver", value: (w?.models || []).join(", ") });
    const concIn = input({ type: "number", min: 1, max: 16, step: 1, value: w?.concurrency ?? 1 });
    const toIn = input({ type: "number", min: 60, max: 86400, step: 60, value: w?.timeout_s ?? 3600 });
    const nameF = field({ label: "Tên hiển thị", control: nameIn, required: true });
    const drvF = field({ label: "Driver (loại CLI)", control: drvSel });
    const exeF = field({ label: "Executable", control: exeIn, hint: edit ? "Sửa nếu CLI đổi chỗ." : null, required: true });
    const modelsF = field({ label: "Model", control: modelsIn, hint: "Danh sách cách nhau dấu phẩy." });
    const concF = field({ label: "Số job chạy song song", control: concIn });
    const toF = field({ label: "Timeout (giây)", control: toIn });
    const probeSw = !edit ? switchCtl({ label: "Kiểm tra CLI ngay khi lưu", checked: true }) : null;
    const content = h("div", { class: "stack" }, nameF, drvF, exeF, exeHint, modelsF, h("div", { class: "grid-2" }, concF, toF), probeSw ? h("div", { class: "row" }, probeSw) : null);
    openDialog({ title: edit ? `Sửa worker “${w.name}”` : "Thêm worker", content, wide: true, actions: [
      { label: "Huỷ", value: null },
      { label: "Lưu worker", kind: "primary", value: "ok", onClick: async () => {
        nameF.setError(null); exeF.setError(null); concF.setError(null);
        if (!nameIn.value.trim()) { nameF.setError("Đặt tên cho worker."); return false; }
        if (!exeIn.value.trim()) { exeF.setError("Điền tên CLI hoặc đường dẫn."); return false; }
        const c = Number(concIn.value); if (!Number.isInteger(c) || c < 1 || c > 16) { concF.setError("Số nguyên từ 1 đến 16."); return false; }
        const body = { name: nameIn.value.trim(), driver_id: drvSel.value, executable: exeIn.value.trim(), concurrency: c, timeout_s: Number(toIn.value) || 3600 };
        if (modelsIn.value.trim()) body.models = modelsIn.value.split(",").map((s) => s.trim()).filter(Boolean);
        if (!edit) body.probe = !!(probeSw && probeSw.input.checked);
        try {
          const r = edit ? await api.put(`/api/workers/${w.id}`, body) : await api.post("/api/workers", body);
          toast({ title: edit ? "Đã cập nhật worker" : "Đã thêm worker", message: `${r.name}: ${(STATUS[r.status] || sm(r.status)).label}`, tone: r.status === "READY" ? "done" : "wait" });
          return true;
        } catch (e) { (e.code === "INVALID_DRIVER" ? drvF : nameF).setError([e.message, e.hint].filter(Boolean).join(" ")); return false; }
      } }] });
  }

  scanBtn.addEventListener("click", scan);
  addBtn.addEventListener("click", () => dialog());
  poller.start();
  return { destroy() { poller.stop(); } };
}

// ------------------------------------------------------------------ W2.UI.1 worker detail (health + history)
function attemptLine(a) {
  const meta = a.state === "SUCCESS" ? { tone: "done", icon: "check-circle", label: "OK" }
    : a.state === "RUNNING" ? { tone: "wait", icon: "clock", label: "Đang chạy" }
    : { tone: "fail", icon: "x-circle", label: errorLabel(a.error_kind) || "Lỗi" };
  const bits = [a.job_id ? h("code", { class: "mono small" }, a.job_id) : null,
    a.work_type ? h("span", { class: "chip" }, a.work_type) : null,
    a.duration_s ? h("span", { class: "muted small" }, duration(a.duration_s)) : null,
    h("span", { class: "muted small" }, relTime(a.started_at))].filter(Boolean);
  return h("div", { class: "row wrap wk-tl-cell" }, h("span", { class: "wk-tl-worker" }, a.worker_name || a.worker_id),
    badge(meta), a.error_code ? h("code", { class: "mono small" }, a.error_code) : null, ...bits);
}

async function detail(w, b) {
  await busy(b, async () => {
    try {
      const d = await api.get(`/api/workers/${w.id}/detail`);
      const hB = hm(d.health);
      const circ = d.circuit && d.circuit !== "CLOSED" ? badge({ tone: "wait", icon: "activity", label: CIRCUIT_LABEL[d.circuit] || d.circuit }) : null;
      const st = sm(d.status);
      const stats = d.stats || {};
      const hist = d.health_history?.length ? h("div", { class: "stack", style: "gap: var(--s-2)" }, ...d.health_history.map((r) =>
        h("div", { class: "row wrap" }, badge(hm(r.state)), h("span", { class: "muted small" }, relTime(r.changed_at)), r.reason ? h("span", { class: "small" }, r.reason) : null))) : h("p", { class: "muted small" }, "Chưa có lịch sử.");
      const atts = d.recent_attempts?.length ? h("div", { class: "stack", style: "gap: var(--s-2)" }, ...d.recent_attempts.map(attemptLine)) : h("p", { class: "muted small" }, "Chưa có attempt nào.");
      const body = h("div", { class: "stack" },
        h("div", { class: "row wrap" }, h("h3", null, d.name), badge(st), badge(hB), circ),
        h("dl", { class: "kv" },
          h("dt", null, "Executable"), h("dd", { class: "mono" }, d.executable),
          h("dt", null, "Driver"), h("dd", null, d.driver_id),
          h("dt", null, "Tỉ lệ thành công"), h("dd", null, stats.success_rate != null ? `${stats.success_rate}%` : "—"),
          h("dt", null, "Attempts"), h("dd", null, String(stats.attempts ?? 0)),
          h("dt", null, "Thời lượng TB"), h("dd", null, stats.avg_duration_s ? duration(stats.avg_duration_s) : "—"),
          h("dt", null, "Thành công gần nhất"), h("dd", null, relTime(d.last_success_at)),
          h("dt", null, "Idle / Hard timeout"), h("dd", null, `${d.idle_timeout_s ? Math.round(d.idle_timeout_s) : "—"}s / ${d.hard_timeout_s ? Math.round(d.hard_timeout_s) : "—"}s`)),
        d.last_error ? alertBox({ tone: "fail", title: "Lỗi gần nhất", body: `${errorLabel(d.last_error.kind) || d.last_error.kind}${d.last_error.code ? ` (${d.last_error.code})` : ""} — ${relTime(d.last_error.at)}` }) : null,
        h("h4", { class: "wk-title" }, "Sức khoẻ theo thời gian"), hist,
        h("h4", { class: "wk-title" }, "Attempt gần đây"), atts);
      openDialog({ title: `Chi tiết worker “${d.name}”`, content: body, wide: true, actions: [{ label: "Đóng", value: null }] });
    } catch (e) { toastError(e, "Không tải được chi tiết"); }
  });
}

// ------------------------------------------------------------------ W2.UI.2 attempt timeline (human-readable, raw ở Advanced)
function timelineBlock() {
  const out = h("div", { class: "wk-tl", "aria-live": "polite" });
  const poller = createPoller(async (signal) => {
    const r = await api.get("/api/workers/attempts?limit=40", { signal });
    paint(r.attempts || []);
    return "idle";
  }, { idle: 15000 });
  function paint(atts) {
    if (!atts.length) { out.replaceChildren(h("p", { class: "muted small" }, "Chưa có attempt nào — chạy một lần likes/copy qua routing để thấy chuỗi thử (ví dụ Worker A hết quota → Worker B thành công).")); return; }
    const groups = new Map();
    for (const a of atts) {
      const key = `${a.job_id}::${a.work_type}`;
      if (!groups.has(key)) groups.set(key, { job_id: a.job_id, work_type: a.work_type, attempts: [] });
      groups.get(key).attempts.push(a);
    }
    out.replaceChildren(...[...groups.values()].map(taskLine));
  }
  function seqCell(a) {
    const meta = a.state === "SUCCESS" ? { tone: "done", icon: "check-circle", label: "OK" }
      : a.state === "RUNNING" ? { tone: "wait", icon: "clock", label: "Đang chạy" }
      : { tone: "fail", icon: "x-circle", label: errorLabel(a.error_kind) || "Lỗi" };
    return h("div", { class: "wk-tl-cell", "aria-label": `Attempt: ${a.worker_name} — ${meta.label}` },
      h("span", { class: "wk-tl-worker" }, a.worker_name || a.worker_id),
      badge(meta),
      a.error_code ? h("code", { class: "mono small" }, a.error_code) : null,
      a.duration_s ? h("span", { class: "muted small" }, duration(a.duration_s)) : null);
  }
  function taskLine(g) {
    const ok = g.attempts.some((a) => a.state === "SUCCESS");
    const line = [];
    g.attempts.forEach((a, i) => { if (i) line.push(icon("arrow-right", { size: 14, cls: "wk-tl-arrow" })); line.push(seqCell(a)); });
    const head = h("div", { class: "row wrap" },
      h("code", { class: "mono small" }, g.job_id || "?"),
      h("span", { class: "chip" }, g.work_type),
      badge({ tone: ok ? "done" : "fail", icon: ok ? "check-circle" : "x-circle", label: ok ? "Thành công" : "Thất bại" }),
      h("span", { class: "muted small" }, `${g.attempts.length} attempt`));
    return h("div", { class: "wk-tl-task" }, head, h("div", { class: "wk-tl-seq" }, ...line),
      disclosure({ label: "Chi tiết kỹ thuật (raw)", content: h("pre", { class: "cfg-pre" }, JSON.stringify(g.attempts, null, 2)) }));
  }
  poller.start();
  return out;
}

// ------------------------------------------------------------------------------------------ Pools (W1.UI.4)
export function poolsPanel(host) {
  let data = null, sig = "", failedOnce = false;
  const list = h("div", { class: "wk-list" });
  const addBtn = btn({ label: "Tạo pool…", icon: "plus", size: "sm", kind: "primary" });
  host.append(h("div", { class: "row spread" },
      h("div", null, h("h3", null, "Worker Pool"),
        h("p", { class: "muted small" }, "Nhóm worker theo thứ tự ưu tiên. Routing trỏ một công việc (work type) vào pool; worker đầu tiên được dùng trước, hỏng thì rơi xuống worker kế.")),
      addBtn),
    list);
  list.append(skeleton(3));

  const poller = createPoller(async (signal) => {
    try { data = await api.get("/api/workers", { signal }); failedOnce = false; }
    catch (e) {
      if (e.name === "AbortError") throw e;
      if (!failedOnce) { clear(list); list.append(errorState(e, () => poller.poke())); failedOnce = true; }
      throw e;
    }
    paint();
    return "idle";
  }, { fast: 4000, idle: 10000 });

  function paint() {
    const s = JSON.stringify([data.pools, data.workers]);
    if (s === sig) return;
    sig = s;
    clear(list);
    if (!data.pools.length) {
      list.append(emptyState({ icon: "layers", title: "Chưa có pool nào",
        text: "Tạo pool, chọn worker (READY) làm thành viên theo thứ tự ưu tiên. Sau đó gán pool cho work type ở mục Routing.",
        action: btn({ label: "Tạo pool…", icon: "plus", kind: "primary", onClick: () => dialog() }) }));
      return;
    }
    for (const p of data.pools) list.append(card(p));
  }

  const wname = (id) => data.workers.find((w) => w.id === id)?.name || id;

  function card(p) {
    const members = p.members.map((id, i) => ({ id, i }));
    const stratLabel = p.strategy === "least_busy" ? "Ít việc nhất" : "Ưu tiên theo thứ tự";
    const usedBy = routesOf(p.name);
    const editBtn = btn({ label: "Sửa…", icon: "settings", size: "sm", kind: "ghost" });
    const dupBtn = btn({ label: "Nhân bản", icon: "copy", size: "sm", kind: "ghost", ariaLabel: `Nhân bản pool ${p.display_name || p.name}` });
    const delBtn = btn({ label: "Xoá", icon: "trash", size: "sm", kind: "ghost danger" });
    editBtn.addEventListener("click", () => dialog(p));
    dupBtn.addEventListener("click", () => duplicate(p));
    delBtn.addEventListener("click", () => remove(p, delBtn));
    const rows = h("ol", { class: "wk-pool-list", "aria-label": `Thành viên pool ${p.display_name || p.name} theo thứ tự ưu tiên` });
    const renderRows = () => {
      clear(rows);
      for (const m of members) {
        const li = h("li", { class: "wk-pool-row", draggable: "true", dataset: { id: m.id } });
        const name = wname(m.id);
        li.append(h("span", { class: "wk-idx", "aria-hidden": "true" }, String(m.i + 1)),
          icon("grip-vertical", { size: 16, label: `Kéo ${name} để đổi thứ tự` }),
          h("span", { class: "grow" }, name),
          btn({ label: "Lên", icon: "arrow-up", size: "sm", kind: "ghost", ariaLabel: `Đưa ${name} lên trên` }),
          btn({ label: "Xuống", icon: "arrow-down", size: "sm", kind: "ghost", ariaLabel: `Đưa ${name} xuống dưới` }));
        li.querySelector(`[aria-label="Đưa ${name} lên trên"]`).addEventListener("click", () => move(m.i, m.i - 1));
        li.querySelector(`[aria-label="Đưa ${name} xuống dưới"]`).addEventListener("click", () => move(m.i, m.i + 1));
        li.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", m.id); li.classList.add("drg"); });
        li.addEventListener("dragend", () => li.classList.remove("drg"));
        li.addEventListener("dragover", (e) => { e.preventDefault(); li.classList.add("drop"); });
        li.addEventListener("dragleave", () => li.classList.remove("drop"));
        li.addEventListener("drop", (e) => {
          e.preventDefault();
          const from = members.find((x) => x.id === e.dataTransfer.getData("text/plain"));
          if (from && from.id !== m.id) move(from.i, m.i);
        });
        rows.append(li);
      }
    };
    const move = async (from, to) => {
      if (to < 0 || to >= members.length) return;
      const [x] = members.splice(from, 1);
      members.splice(to, 0, x);
      members.forEach((m, i) => { m.i = i; });
      renderRows();
      await saveMembers(p, members.map((m) => m.id));
    };
    renderRows();
    const sw = switchCtl({ label: "", checked: !!p.enabled, onChange: async (v) => {
      try { await api.put(`/api/workers/pools/${encodeURIComponent(p.name)}`, { enabled: v }); toast({ title: v ? "Đã bật pool" : "Đã tắt pool", tone: "done" }); sig = ""; poller.poke(); }
      catch (e) { toastError(e, "Chưa đổi được"); sw.input.checked = !!p.enabled; }
    } });
    sw.input.setAttribute("aria-label", `Bật/tắt pool ${p.display_name || p.name}`);
    sw.querySelector(".state").textContent = p.enabled ? "Bật" : "Tắt";
    return h("section", { class: "card wk-card", "aria-label": `Pool ${p.display_name || p.name}` },
      h("div", { class: "row spread" },
        h("div", { class: "row" }, h("h3", null, p.display_name || p.name), h("span", { class: "chip" }, stratLabel), h("span", { class: "chip" }, `${members.length} worker`), sw),
        h("div", { class: "row" }, editBtn, dupBtn, delBtn)),
      usedBy.length ? h("p", { class: "small muted" }, "Đang dùng cho routing: ", usedBy.map((wt) => h("code", { class: "mono" }, wt))) : h("p", { class: "small muted" }, "Chưa gán cho work type nào."),
      members.length ? h("div", null, h("p", { class: "small muted", style: "margin-bottom: var(--s-2)" }, "Kéo thả hoặc dùng nút mũi tên để đổi thứ tự ưu tiên."), rows) : h("p", { class: "muted small" }, "Pool rỗng — mở “Sửa…” để thêm worker."));
  }

  function routesOf(poolName) {
    const map = data.routing?.routing || data.routing || {};
    return Object.entries(map).filter(([, cfg]) => cfg?.pool === poolName).map(([wt]) => wt);
  }

  async function saveMembers(p, members) {
    try { await api.put(`/api/workers/pools/${encodeURIComponent(p.name)}`, { members }); sig = ""; poller.poke(); }
    catch (e) { toastError(e, "Không lưu được thứ tự"); }
  }

  async function duplicate(p) {
    try {
      const name = p.name + "_copy";
      await api.post("/api/workers/pools", { name, display_name: `${p.display_name || p.name} (bản sao)`, strategy: p.strategy, members: [...p.members] });
      toast({ title: "Đã nhân bản pool", message: name, tone: "done" });
      sig = ""; poller.poke();
    } catch (e) { toastError(e, "Không nhân bản được"); }
  }

  async function remove(p, b) {
    const used = routesOf(p.name);
    let imp = null;
    try { imp = await api.get(`/api/workers/pools/${encodeURIComponent(p.name)}/impact`); } catch (e) { /* confirm mặc định */ }
    const dep = used.length ? ` Đang được gán cho: ${used.join(", ")} — pool bị xoá thì routing đó cũng mất.` : "";
    const body = h("div", { class: "stack", style: "gap: var(--s-3)" },
      h("p", null, `Xoá cấu hình pool; worker không bị xoá.${dep}`),
      imp ? impactAlert(imp, "Hệ quả xoá pool") : null);
    if (!(await confirmDialog({ title: `Xoá pool “${p.display_name || p.name}”?`, body, confirmLabel: "Xoá pool", danger: true }))) return;
    await busy(b, async () => {
      try { await api.del(`/api/workers/pools/${encodeURIComponent(p.name)}`); toast({ title: "Đã xoá pool", tone: "done" }); }
      catch (e) { toastError(e, "Không xoá được pool"); }
      sig = ""; poller.poke();
    });
  }

  function dialog(p) {
    const edit = !!p;
    const nameIn = input({ placeholder: "story_workers", value: p?.name || "" });
    const dispIn = input({ placeholder: "Nhóm viết story", value: p?.display_name || "" });
    const stratSel = select({ options: [["priority", "Ưu tiên theo thứ tự"], ["least_busy", "Ít việc nhất"]], value: p?.strategy || "priority" });
    const nameF = field({ label: "Tên (khoá)", control: nameIn, required: !edit, hint: edit ? "Không đổi được tên; nhân bản hoặc tạo mới nếu cần." : "Chữ thường, số, gạch dưới; work type trỏ vào tên này." });
    const dispF = field({ label: "Tên hiển thị", control: dispIn });
    const stratF = field({ label: "Chiến lược", control: stratSel, hint: "Ưu tiên: dùng worker đầu tiên trong list. Ít việc nhất: chọn worker đang rảnh." });
    const checks = h("div", { class: "wk-checks" });
    const members0 = p?.members || [];
    const renderChecks = () => {
      clear(checks);
      for (const w of data.workers) {
        if (w.status !== "READY" && !members0.includes(w.id)) continue;
        const cb = h("input", { type: "checkbox", dataset: { wid: w.id }, checked: members0.includes(w.id), disabled: w.status !== "READY" });
        const label = h("label", { class: "wk-check" }, cb, h("span", null, w.name, w.default_model ? h("span", { class: "muted small" }, ` · ${w.default_model}`) : null));
        if (w.status !== "READY") label.append(h("span", { class: "badge", dataset: { tone: sm(w.status).tone } }, sm(w.status).label));
        checks.append(label);
      }
      if (![...checks.querySelectorAll("input")].length && (data.workers || []).length) checks.append(h("p", { class: "muted small" }, "Chưa worker nào đang Sẵn sàng để chọn."));
      if (!(data.workers || []).length) checks.append(h("p", { class: "muted small" }, "Tạo worker ở mục Workers trước."));
    };
    renderChecks();
    const content = h("div", { class: "stack" }, nameF, dispF, stratF, h("div", { class: "field" }, h("p", { class: "s-label", style: "margin-bottom: var(--s-2)" }, "Thành viên (thứ tự ưu tiên — kéo thả sau khi tạo)"), checks));
    openDialog({ title: edit ? `Sửa pool “${p.display_name || p.name}”` : "Tạo pool", content, wide: true, actions: [
      { label: "Huỷ", value: null },
      { label: edit ? "Lưu pool" : "Tạo pool", kind: "primary", value: "ok", onClick: async () => {
        nameF.setError(null);
        if (!nameIn.value.trim()) { nameF.setError("Đặt tên cho pool."); return false; }
        const members = [...checks.querySelectorAll("input:checked")].map((cb) => cb.dataset.wid);
        const body = { display_name: dispIn.value.trim(), strategy: stratSel.value, members };
        try {
          if (edit) await api.put(`/api/workers/pools/${encodeURIComponent(p.name)}`, body);
          else await api.post("/api/workers/pools", { name: nameIn.value.trim().toLowerCase(), ...body });
          toast({ title: edit ? "Đã cập nhật pool" : "Đã tạo pool", tone: "done" });
          return true;
        } catch (e) { nameF.setError([e.message, e.hint].filter(Boolean).join(" ")); return false; }
      } }] });
  }

  addBtn.addEventListener("click", () => dialog());
  poller.start();
  return { destroy() { poller.stop(); } };
}

// ---------------------------------------------------------------------------------- Routing & Simulator (W1.UI.5, W1.UI.6)
const RETRY_KINDS = [
  ["TEMPORARY", "Lỗi tạm thời"],
  ["TIMEOUT", "Quá thời gian"],
  ["INVALID_OUTPUT", "Output không hợp lệ"],
  ["UNKNOWN", "Lỗi không nhận dạng"],
  ["QUOTA", "Hết quota"],
  ["AUTH", "Chưa đăng nhập"],
];
const RETRY_OPTS = [[0, "Chuyển worker kế ngay"], [1, "Thử lại 1 lần"], [2, "Thử lại 2 lần"], [3, "Thử lại 3 lần"]];

function policyLine(n) {
  return n > 0 ? `Thử lại ${n} lần cùng worker, rồi sang worker kế` : "Sang worker kế ngay";
}

export function routingPanel(host) {
  loadCss("/css/settings.css");
  let data = null, sig = "", failedOnce = false;
  const list = h("div", { class: "wk-list" });
  const addBtn = btn({ label: "Gán routing…", icon: "plus", size: "sm", kind: "primary" });
  const sim = simBlock(() => data);
  host.append(h("div", { class: "row spread" },
      h("div", null, h("h3", null, "Routing & Độ tin cậy"),
        h("p", { class: "muted small" }, "Trỏ một công việc (work type) vào pool + profile model + chính sách retry/fallback. Công việc chưa có routing chạy thẳng một worker.")),
      addBtn),
    h("section", { class: "card wk-sim" }, sim.head, sim.out),
    h("h3", { class: "wk-title" }, "Routing đã gán"),
    list);
  list.append(skeleton(3));

  const poller = createPoller(async (signal) => {
    data = await api.get("/api/workers/routing", { signal });
    sim.sync();
    failedOnce = false;
    paint();
    return "idle";
  }, { fast: 4000, idle: 10000 });

  function paint() {
    const s = JSON.stringify([data.routing, data.pools, data.profiles]);
    if (s === sig) return;
    sig = s;
    clear(list);
    const routes = Object.entries(data.routing || {});
    if (!routes.length) {
      list.append(emptyState({ icon: "activity", title: "Chưa có routing nào",
        text: "Gán work type cho pool (chọn worker theo config, không sửa code). Công việc chưa gán chạy thẳng worker mặc định.",
        action: btn({ label: "Gán routing…", icon: "plus", kind: "primary", onClick: () => dialog() }) }));
      return;
    }
    for (const [wt, cfg] of routes) list.append(card(wt, cfg));
  }

  function card(wt, cfg) {
    const pol = cfg.policy || {};
    const editBtn = btn({ label: "Sửa…", icon: "settings", size: "sm", kind: "ghost" });
    const delBtn = btn({ label: "Xoá", icon: "trash", size: "sm", kind: "ghost danger" });
    editBtn.addEventListener("click", () => dialog(wt, cfg));
    delBtn.addEventListener("click", async () => {
      if (!(await confirmDialog({ title: `Xoá routing “${wt}”?`, body: "Công việc này sẽ chạy thẳng worker mặc định.", confirmLabel: "Xoá routing", danger: true }))) return;
      try { await api.del(`/api/workers/routing/${encodeURIComponent(wt)}`); toast({ title: "Đã xoá routing", tone: "done" }); sig = ""; poller.poke(); }
      catch (e) { toastError(e, "Không xoá được"); }
    });
    return h("section", { class: "card wk-card", "aria-label": `Routing ${wt}` },
      h("div", { class: "row spread" },
        h("div", { class: "row" }, h("h3", null, wt), h("span", { class: "chip" }, cfg.pool),
          cfg.model_profile ? h("span", { class: "chip" }, `Profile: ${cfg.model_profile}`) : null),
        h("div", { class: "row" }, editBtn, delBtn)),
      h("dl", { class: "wk-policy" },
        ...RETRY_KINDS.map(([k, label]) => h("div", null, h("dt", null, label), h("dd", null, policyLine(pol.retry_on?.[k] ?? 0)))),
        h("div", null, h("dt", null, "Tổng số lần thử / số worker"),
          h("dd", null, `${pol.max_total_attempts ?? 5} lần × tối đa ${pol.max_distinct_workers ?? 3} worker khác nhau`))));
  }

  function dialog(wt, cfg) {
    const edit = !!wt;
    const wtIn = input({ placeholder: "story.write", value: wt || "" });
    const poolSel = select({ options: (data.pools || []).map((p) => [p.name, p.display_name || p.name]), value: cfg?.pool || "" });
    const profSel = select({ options: [["", "Mặc định"], ...(data.profiles || []).map((p) => [p, p])], value: cfg?.model_profile || "" });
    const pol = cfg?.policy || {};
    const maxA = input({ type: "number", min: "1", value: pol.max_total_attempts ?? 5 });
    const maxD = input({ type: "number", min: "1", value: pol.max_distinct_workers ?? 3 });
    const retrySels = RETRY_KINDS.map(([k, label]) => ({ k, sel: select({ options: RETRY_OPTS, value: pol.retry_on?.[k] ?? 0 }), label }));
    const wtF = field({ label: "Work type", control: wtIn, required: true, hint: edit ? "Không sửa được tên; xoá rồi gán lại nếu cần." : "Ví dụ: story.write (dấu chấm để nhóm)." });
    const poolF = field({ label: "Pool", control: poolSel, required: true });
    const profF = field({ label: "Model profile", control: profSel, hint: "Rỗng = model mặc định của worker được chọn." });
    const content = h("div", { class: "stack" }, wtF, poolF, profF,
      h("fieldset", { class: "wk-fset" }, h("legend", null, "Retry / fallback"), h("div", { class: "wk-retry-grid" },
        ...retrySels.map(({ label, sel }) => field({ label, control: sel })),
        h("div", { class: "row" }, field({ label: "Tổng số lần thử", control: maxA, hint: "Kể cả worker đầu." }), field({ label: "Số worker khác nhau tối đa", control: maxD })))));
    openDialog({ title: edit ? `Sửa routing “${wt}”` : "Gán routing", content, wide: true, actions: [
      { label: "Huỷ", value: null },
      { label: edit ? "Lưu routing" : "Gán routing", kind: "primary", value: "ok", onClick: async () => {
        wtF.setError(null); poolF.setError(null);
        if (edit ? !wt : !wtIn.value.trim()) { wtF.setError("Đặt tên work type."); return false; }
        if (!poolSel.value) { poolF.setError("Chọn pool để route."); return false; }
        const body = {
          work_type: edit ? wt : wtIn.value.trim(),
          pool: poolSel.value,
          model_profile: profSel.value,
          policy: {
            max_total_attempts: Math.max(1, Number(maxA.value) || 5),
            max_distinct_workers: Math.max(1, Number(maxD.value) || 3),
            retry_on: Object.fromEntries(retrySels.map(({ k, sel }) => [k, Number(sel.value)])),
          },
        };
        try {
          await api.put("/api/workers/routing", body);
          toast({ title: edit ? "Đã cập nhật routing" : "Đã gán routing", tone: "done" });
          return true;
        } catch (e) { poolF.setError([e.message, e.hint].filter(Boolean).join(" ")); return false; }
      } }] });
  }

  addBtn.addEventListener("click", () => dialog());
  poller.start();
  return { destroy() { poller.stop(); } };
}

// ---------------------------------------------------------------------------------- Simulator (W1.UI.6)
function simBlock(getData) {
  const wtSel = select({ options: [], disabled: true, id: "wk-sim-wt" });
  const goBtn = btn({ label: "Simulate", icon: "zap", size: "sm", kind: "primary", disabled: true });
  const out = h("div", { class: "wk-sim-out", "aria-live": "polite" });

  const sync = () => {
    const routes = Object.keys(getData()?.routing || {});
    const cur = wtSel.value;
    wtSel.replaceChildren();
    for (const r of routes) wtSel.append(h("option", { value: r }, r));
    wtSel.disabled = !routes.length;
    goBtn.disabled = !routes.length;
    if (routes.includes(cur)) wtSel.value = cur;
  };

  wtSel.addEventListener("change", () => clear(out));
  goBtn.addEventListener("click", async () => {
    const wt = wtSel.value;
    if (!wt) return;
    clear(out);
    out.append(skeleton(2));
    await busy(goBtn, async () => {
      try {
        const r = await api.post("/api/workers/routing/simulate", { work_type: wt });
        clear(out);
        if (!r.rows.length) { out.append(emptyState({ icon: "alert-circle", title: "Không worker nào đủ điều kiện", text: r.reason })); return; }
        for (const row of r.rows) {
          const tone = row.role === "selected" ? "done" : row.role === "excluded" ? "fail" : "wait";
          const meta = row.role === "selected" ? "được chọn" : row.role === "excluded" ? `loại — ${row.blocked_reason}` : "dự phòng";
          out.append(h("div", { class: "wk-sim-row" },
            h("span", { class: "mono" }, row.name),
            badge({ tone, label: row.status }),
            h("span", { class: "chip" }, row.model || "—"),
            h("span", { class: "muted small" }, meta)));
        }
        if (r.reason && r.ok) out.append(h("p", { class: "muted small" }, r.reason));
      } catch (e) { clear(out); out.append(errorState(e, () => goBtn.click())); }
    });
  });

  const head = h("div", { class: "row spread" },
    h("div", null, h("h3", null, "Routing Simulator"),
      h("p", { class: "muted small" }, "Chọn work type đã gán routing, bấm Simulate. Chỉ chạy logic routing — không gọi model thật, không tốn token.")),
    h("div", { class: "row" }, wtSel, goBtn));
  return { sync, head, out };
}