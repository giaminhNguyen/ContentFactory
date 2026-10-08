// Worker Runtime — mục "Workers" trong Cài đặt (W1.UI.2, W1.UI.3):
// danh sách worker (health/status, model, pool, hành động) + thêm/sửa bằng dialog.
// Frontend chỉ ra lệnh; mọi quyết định (probe, scan, route) ở backend qua /api/workers*.
import { api } from "../api.js";
import { h, clear, loadCss } from "../dom.js";
import { icon } from "../icons.js";
import { btn, busy, field, input, select, switchCtl, badge, emptyState, errorState, skeleton, toast, toastError, confirmDialog, openDialog, kv } from "../components.js";
import { createPoller } from "../poller.js";

const STATUS = {
  READY: { tone: "done", icon: "check-circle", label: "Sẵn sàng" },
  DETECTED: { tone: "wait", icon: "clock", label: "Phát hiện" },
  AUTH_REQUIRED: { tone: "fail", icon: "lock", label: "Cần đăng nhập" },
  BROKEN: { tone: "fail", icon: "x-circle", label: "Hỏng" },
  NOT_FOUND: { tone: "attn", icon: "search", label: "Mất CLI" },
  DISABLED: { tone: "off", icon: "pause", label: "Đã tắt" },
};
const sm = (s) => STATUS[s] || { tone: "off", icon: "dot", label: s || "—" };

export function workersPanel(host) {
  loadCss("/css/settings.css");
  let data = null, sig = "", failedOnce = false;
  const list = h("div", { class: "wk-list" });

  const scanBtn = btn({ label: "Quét CLI", icon: "refresh", size: "sm" });
  const addBtn = btn({ label: "Thêm worker…", icon: "plus", size: "sm", kind: "primary" });
  host.append(h("div", { class: "row spread" },
      h("div", null, h("h3", null, "Workers"),
        h("p", { class: "muted small" }, "Mỗi worker là một CLI cụ thể (Claude/Codex/Gemini/OpenCode). “Quét CLI” tự tìm CLI có trên PATH; worker chưa vào pool thì job dùng chế độ runtime trực tiếp.")),
      h("div", { class: "row" }, scanBtn, addBtn)),
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
    const s = JSON.stringify([data.workers, data.pools]);
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
    const drv = (data.drivers || []).find((d) => d.id === w.driver_id);
    const testBtn = btn({ label: "Test", icon: "stethoscope", size: "sm", title: "Probe lại CLI", disabled: w.status === "DISABLED" });
    const editBtn = btn({ label: "Sửa", icon: "settings", size: "sm", kind: "ghost" });
    const sw = switchCtl({ label: "", checked: !!w.enabled, onChange: (v) => toggle(w, sw, v) });
    sw.input.setAttribute("aria-label", `Bật/tắt worker ${w.name}`);
    sw.querySelector(".state").textContent = w.enabled ? "Bật" : "Tắt";
    const delBtn = btn({ label: "Xoá", icon: "trash", size: "sm", kind: "ghost danger" });
    testBtn.addEventListener("click", () => doTest(w, testBtn));
    editBtn.addEventListener("click", () => dialog(w));
    delBtn.addEventListener("click", () => remove(w, delBtn));
    const bits = [w.version ? `v${w.version}` : null, w.auth && w.auth !== "unknown" ? `auth: ${w.auth}` : null].filter(Boolean).join(" · ");
    const notReady = !w.routable.ok && w.enabled ? h("p", { class: "small", style: "color: var(--st-wait-fg)" }, icon("alert", { size: 14 }), " ", w.routable.reason, w.cooldown_s > 0 ? ` (cooldown ${Math.ceil(w.cooldown_s)}s)` : "") : null;
    const detail = w.detail && w.status !== "READY" ? h("p", { class: "small muted" }, w.detail) : null;
    const meta = kv([
      ["Model mặc định", w.default_model || "—"],
      ["Model khả dụng", w.models.length ? w.models.join(", ") : "—"],
      ["Song song", String(w.concurrency)],
      ["Timeout", w.timeout_s ? `${w.timeout_s}s` : "—"],
    ]);
    return h("section", { class: "card wk-card", "aria-label": `Worker ${w.name}` },
      h("div", { class: "row spread" },
        h("div", { class: "row" }, h("h3", { class: "wk-name" }, w.name), badge(st), h("span", { class: "chip" }, drv?.label || w.driver_id)),
        h("div", { class: "row" }, testBtn, editBtn, sw, delBtn)),
      h("p", { class: "mono small", title: w.executable }, w.executable),
      bits ? h("p", { class: "small muted" }, bits) : null,
      notReady, detail,
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
    try {
      await api.post(`/api/workers/${w.id}/${checked ? "enable" : "disable"}`, {});
      w.enabled = checked;
      sw.querySelector(".state").textContent = checked ? "Bật" : "Tắt";
      toast({ title: checked ? "Đã bật" : "Đã tắt", message: `${w.name} ${checked ? "trở lại sẵn sàng" : "không được route nữa"}.`, tone: "done" });
      sig = ""; poller.poke();
    } catch (e) { sw.input.checked = !checked; sw.querySelector(".state").textContent = !checked ? "Bật" : "Tắt"; toastError(e, "Chưa đổi được"); }
  }

  async function remove(w, b) {
    const used = (data.pools || []).filter((p) => p.members.includes(w.id));
    const dep = used.length ? ` Worker sẽ bị gỡ khỏi pool: ${used.map((p) => p.display_name || p.name).join(", ")}.` : "";
    if (!(await confirmDialog({ title: `Xoá worker “${w.name}”?`, body: `Xoá khỏi cấu hình; lịch sử attempt vẫn giữ.${dep}`, confirmLabel: "Xoá worker", danger: true }))) return;
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