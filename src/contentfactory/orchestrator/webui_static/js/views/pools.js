// Video nguồn (source pool): thư mục video thô, trạng thái đồng bộ, đồng bộ nền dùng chung. Frontend chỉ hiển thị/ra lệnh; không có logic ContentFlow.
import { api } from "../api.js";
import { h, clear, loadCss } from "../dom.js";
import { btn, busy, field, input, select, alertBox, emptyState, errorState, skeleton, pageHead, toast, toastError, confirmDialog, openDialog, badge, tabs } from "../components.js";
import { createPoller } from "../poller.js";
import { createSamples } from "../samples.js";
import { relTime, shortPath } from "../format.js";
import * as motion from "../motion.js";

const STATE = {
  ready: { tone: "done", icon: "check-circle", label: "Sẵn sàng" },
  syncing: { tone: "running", icon: "spinner", label: "Đang đồng bộ", spin: true },
  pending: { tone: "wait", icon: "hourglass", label: "Chờ đồng bộ" },
  problem: { tone: "fail", icon: "x-circle", label: "Có vấn đề" },
  idle: { tone: "queue", icon: "clock", label: "Chưa dùng" },
};
const ORIENT = { landscape: "Ngang", portrait: "Dọc" };
const USED = { youtube: "YouTube", tiktok: "TikTok" };

// Nguồn Media: hai nguồn tách bạch — Video (nền cho render) và Ảnh thumbnail (Image Pool, Phase 8). Mỗi tab là một view riêng; đường dẫn #/pools và #/pools/images.
export async function mount(root, ctx) {
  loadCss("/css/pools.css");
  let tab = ctx.params[0] === "images" ? "images" : "video", sub = null, seq = 0;
  const tabHost = h("div"), panel = h("div", { role: "tabpanel" });
  root.append(pageHead("Nguồn Media", "Nơi hệ thống lấy video nền và ảnh thumbnail cho job."), tabHost, panel);
  tabHost.append(tabs({ items: [["video", "Video"], ["images", "Ảnh thumbnail"]], active: tab, label: "Loại nguồn media", onSelect: (id) => { tab = id; history.replaceState(null, "", id === "images" ? "#/pools/images" : "#/pools"); show(); } }));
  async function show() {
    const my = ++seq;
    sub?.destroy();
    sub = null;
    clear(panel);
    panel.id = `panel-${tab}`;
    panel.setAttribute("aria-labelledby", `tab-${tab}`);
    const s = await (tab === "images" ? (await import("./_image_pools.js")).mount(panel, ctx) : mountVideo(panel, ctx));
    if (my !== seq) { s.destroy(); return; }
    sub = s;
  }
  await show();
  return { destroy() { seq++; sub?.destroy(); } };
}

async function mountVideo(root, ctx) {
  const { scope } = ctx;
  let data = null, sig = "", task = null, firstPaint = true, failedOnce = false;

  const notice = h("div", { class: "stack", "aria-live": "polite" });
  const status = h("p", { class: "muted small", "aria-live": "polite" });
  const body = h("div", null);
  const addBtn = btn({ label: "Thêm pool", icon: "plus", onClick: () => poolDialog() });
  const syncAll = btn({ label: "Đồng bộ tất cả", icon: "refresh", onClick: (e) => startSync(null, e.currentTarget) });
  root.append(h("div", { class: "row spread" }, h("p", { class: "muted" }, "Hệ thống chuẩn hoá video nguồn một lần và dùng chung cho mọi job; việc đồng bộ chạy nền."), h("div", { class: "row" }, syncAll, addBtn)),
    h("div", { class: "stack" }, notice, status, h("div", { class: "card flush" }, body)));
  body.append(skeleton(3));

  // ---------- tải + vẽ ----------
  const poller = createPoller(async (signal) => {
    try { data = await api.get("/api/pools", { signal }); failedOnce = false; }
    catch (e) {
      if (e.name === "AbortError") throw e;
      if (!failedOnce) { clear(body); body.append(errorState(e, () => poller.poke())); failedOnce = true; }
      throw e;
    }
    if (data.syncing_task && !task) track(data.syncing_task, false);
    paint();
    return task || data.pools.some((p) => p.state === "syncing") ? "fast" : "idle";
  }, { fast: 2000, idle: 6000 });

  function paint() {
    const s = JSON.stringify([data.uses_pool, data.pools, !!task]);
    syncAll.disabled = !!task || !data.uses_pool || !data.pools.length;
    status.textContent = task ? "Đang đồng bộ… (chạy nền, bạn có thể làm việc khác)" : "";
    if (s === sig) return;
    sig = s;
    clear(notice);
    if (!data.uses_pool) notice.append(alertBox({ tone: "info", title: "Adapter render hiện không dùng video nền", body: `Render đang là “${data.render_adapter}” (bản giả hoặc không cần pool), nên đồng bộ chưa có tác dụng. Bật ContentFlow bằng setup và cấu hình ở Cài đặt & Doctor.`, actions: [btn({ label: "Mở Cài đặt & Doctor", href: "#/settings", size: "sm" })] }));
    clear(body);
    if (!data.pools.length) {
      body.append(emptyState({ icon: "film", title: "Chưa có pool video nguồn nào", text: "YouTube dùng một pool video NGANG (16:9) và TikTok dùng một pool video DỌC (9:16). Thêm thư mục chứa video (mp4/mov/mkv…) cho mỗi loại.",
        action: h("div", { class: "row", style: "justify-content:center" }, btn({ label: "Thêm pool", icon: "plus", kind: "primary", onClick: () => poolDialog() }),
          btn({ label: "Tạo video mẫu để thử", icon: "film", onClick: async (e) => { if (await createSamples(e.currentTarget)) { sig = ""; poller.poke(); } } })) }));
      return;
    }
    const rows = data.pools.flatMap(row);
    body.append(h("div", { class: "table-wrap" }, h("table", { class: "table pools-table" },
      h("caption", { class: "sr-only" }, "Danh sách pool video nguồn"),
      h("thead", null, h("tr", null, ...["Pool", "Thư mục", "Video", "Trạng thái", "Đồng bộ gần nhất", "Dùng cho", "Thao tác"].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", null, ...rows))));
    if (firstPaint) { firstPaint = false; scope.add(() => motion.itemsEnter(rows)); }
  }

  function row(p) {
    const o = p.orientation;
    const tr = h("tr", null,
      h("td", null, h("strong", null, p.name), " ", h("span", { class: "chip", title: p.declared_orientation ? "Đã khai báo" : "Tự nhận theo tên pool" }, o ? ORIENT[o] : "Chưa rõ hướng")),
      h("td", null, h("span", { class: "mono pool-path", title: p.raw_dir }, shortPath(p.raw_dir, 44))),
      h("td", null, p.files == null ? "—" : String(p.files), p.todo ? h("div", { class: "muted small" }, `${p.todo} chờ chuẩn hoá`) : null),
      h("td", null, badge(STATE[p.state] || STATE.idle), p.reason && p.state !== "ready" ? h("div", { class: "muted small" }, p.reason) : null),
      h("td", null, p.last_sync ? relTime(p.last_sync) : "—"),
      h("td", null, p.used_by.length ? p.used_by.map((u) => USED[u] || u).join(", ") : h("span", { class: "muted" }, "—")),
      h("td", null, h("div", { class: "row" }, ...actions(p))));
    if (!p.problems.length) return [tr];
    tr.classList.add("has-problems");
    return [tr, h("tr", { class: "pool-problems" }, h("td", { colspan: "7" },
      h("ul", { class: "autolist", "aria-label": `Vấn đề của pool ${p.name}` }, ...p.problems.map((x) => h("li", null, x)))))];
  }

  function actions(p) {
    const out = [];
    if (p.can_sync) out.push(btn({ label: "Đồng bộ ngay", icon: "refresh", size: "sm", disabled: !!task || p.state === "syncing", onClick: (e) => startSync(p.name, e.currentTarget) }));
    out.push(btn({ label: "Sửa", icon: "settings", size: "sm", kind: "ghost", ariaLabel: `Sửa pool ${p.name}`, onClick: () => poolDialog(p) }));
    out.push(btn({ label: "Xoá", icon: "trash", size: "sm", kind: "ghost danger", ariaLabel: `Xoá pool ${p.name}`, onClick: (e) => removePool(p, e.currentTarget) }));
    return out;
  }

  // ---------- đồng bộ (tác vụ nền) ----------
  async function startSync(name, button) {
    if (task) return;
    await busy(button, async () => {
      try {
        const r = await api.post("/api/pools/sync", name ? { name } : {});
        toast({ title: r.already ? "Đang có lần đồng bộ chạy sẵn" : "Đã bắt đầu đồng bộ", message: "Chạy nền; kết quả hiện ở đây khi xong.", tone: "info" });
        track(r.task_id, true);
      } catch (e) { toastError(e, "Không đồng bộ được"); }
    });
  }

  function track(taskId, notify) {
    if (task) return;
    const tp = createPoller(async (signal) => {
      let t;
      try { t = await api.get(`/api/tasks/${taskId}`, { signal }); }
      catch (e) {
        if (e.name === "AbortError" || e.network || e.status !== 404) throw e;
        t = { state: "error", error: { message: "Tác vụ đồng bộ không còn (ứng dụng đã khởi động lại?)." } };   // 404: không poll mãi
      }
      if (t.state === "running") return "fast";
      tp.stop();
      task = null;
      if (notify) summarize(t);
      sig = "";
      poller.poke();
    }, { fast: 1000, idle: 1000 });
    task = { id: taskId, poller: tp };
    tp.start();
    if (data) paint();
  }

  function summarize(t) {
    if (t.state === "error") return toast({ title: "Đồng bộ lỗi", message: [t.error?.message, t.error?.hint].filter(Boolean).join(" "), tone: "fail", sticky: true });
    const res = Object.entries(t.result || {});
    const bad = res.filter(([, v]) => v.error);
    if (bad.length) return toast({ title: `Đồng bộ xong, ${bad.length} pool lỗi`, message: bad.map(([k, v]) => `${k}: ${v.message || v.error}`).join(" · "), tone: "fail", sticky: true });
    const files = res.reduce((n, [, v]) => n + (v.files || 0), 0);
    toast({ title: "Đồng bộ xong", message: res.length ? `${res.length} pool, ${files} video${res.every(([, v]) => v.reused) ? " (không có gì thay đổi)" : ""}.` : "Không có pool nào cần đồng bộ.", tone: "done" });
  }

  // ---------- thêm / sửa / xoá ----------
  async function poolDialog(p) {
    const edit = !!p;
    const nameIn = input({ placeholder: "gameplay", value: p?.name || "", disabled: edit });
    const dirIn = input({ placeholder: "D:\\video\\gameplay", value: p?.raw_dir || "" });
    const pickBtn = btn({ label: "Chọn thư mục…", icon: "folder", onClick: (e) => busy(e.currentTarget, async () => {
      try {
        const r = await api.post("/api/pick", { kind: "folder", title: "Chọn thư mục video nguồn" });
        if (r.unsupported) toast({ title: "Máy không có hộp thoại chọn thư mục", message: r.message, tone: "wait" });
        else if (r.path) dirIn.value = r.path;
      } catch (err) { toastError(err); }
    }) });
    const ori = select({ options: [["", "Tự nhận (theo tên / khai báo)"], ["landscape", "Ngang (16:9) — cho YouTube"], ["portrait", "Dọc (9:16) — cho TikTok"]], value: p?.declared_orientation || "" });
    const nameF = field({ label: "Tên pool", control: nameIn, hint: edit ? "Không đổi được tên; xoá rồi tạo lại nếu cần." : "Chữ không dấu, số, _ và -. Tên chứa “doc/dọc/vertical” sẽ được nhận là video dọc.", required: !edit });
    const dirF = field({ label: "Thư mục video nguồn", control: dirIn, required: true });
    const content = h("div", { class: "stack" }, nameF, h("div", { class: "stack" }, dirF, h("div", null, pickBtn)), field({ label: "Hướng khung hình", control: ori }));
    const r = await openDialog({ title: edit ? `Sửa pool “${p.name}”` : "Thêm pool video nguồn", content, actions: [{ label: "Huỷ", value: null }, { label: "Lưu pool", kind: "primary", value: "ok", onClick: async () => {
      nameF.setError(null); dirF.setError(null);
      if (!/^[\w-]{1,40}$/.test(nameIn.value.trim())) { nameF.setError("Tên pool chỉ gồm chữ không dấu, số, _ và -, tối đa 40 ký tự."); return false; }
      if (!dirIn.value.trim()) { dirF.setError("Chọn hoặc dán đường dẫn thư mục video nguồn."); return false; }
      try { await api.put(`/api/pools/${encodeURIComponent(nameIn.value.trim())}`, { raw_dir: dirIn.value.trim(), orientation: ori.value || null }); return true; }
      catch (e) { (e.code === "INVALID_POOL_NAME" ? nameF : dirF).setError([e.message, e.hint].filter(Boolean).join(" ")); return false; }
    } }] });
    if (r !== "ok") return;
    toast({ title: "Đã lưu pool", message: "Việc chuẩn hoá video chạy nền; bấm “Đồng bộ ngay” để làm ngay.", tone: "done" });
    sig = "";
    poller.poke();
  }

  async function removePool(p, button) {
    if (!(await confirmDialog({ title: `Xoá pool “${p.name}”?`, body: "Chỉ bỏ cấu hình pool; video gốc trong thư mục KHÔNG bị xoá. Kênh/job đang dùng pool này sẽ cần chọn pool khác.", confirmLabel: "Xoá pool", danger: true }))) return;
    await busy(button, async () => {
      try { await api.del(`/api/pools/${encodeURIComponent(p.name)}`); toast({ title: "Đã xoá pool", tone: "done" }); sig = ""; poller.poke(); }
      catch (e) { toastError(e, "Không xoá được pool"); }
    });
  }

  poller.start();
  return { destroy() { poller.stop(); task?.poller.stop(); task = null; } };
}
