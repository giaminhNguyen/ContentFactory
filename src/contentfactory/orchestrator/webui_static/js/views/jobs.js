// Danh sách job: câu hỏi chính là "có việc gì cần tôi xử lý không?". Dải tổng quan nhẹ ở trên, lọc theo trạng thái + tìm kiếm + loại/kênh/thời gian,
// chọn nhiều để làm hàng loạt (backend kiểm từng job), cập nhật tại chỗ, phân trang ("Tải thêm"), không dựng lại cả danh sách mỗi lượt poll.
import { api } from "../api.js";
import { h, clear, patchList } from "../dom.js";
import { btn, busy, emptyState, errorState, pageHead, select, skeleton, confirmDialog } from "../components.js";
import { createPoller } from "../poller.js";
import { publishCounts } from "../router.js";
import { FILTERS } from "../status.js";
import * as motion from "../motion.js";
import * as notify from "../notify.js";
import { makeRow, updateRow, rowKey } from "./_batch_ui.js";
import { BULK, sendBulk, openBulkPipeline, openBulkTemplate } from "./_bulk.js";

const PAGE = 30, CAP = 200;
const LS = { get: (k) => { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* không lưu được thì thôi */ } } };
const KINDS = [["", "Tất cả loại"], ["single", "Job đơn"], ["channel", "Channel Run"]];
const DAYS = [["", "Mọi lúc"], ["1", "24 giờ qua"], ["7", "7 ngày qua"], ["30", "30 ngày qua"]];

export async function mount(root, ctx) {
  const { app, scope, navigate, query } = ctx;
  const saved = LS.get("cf.jobs.filters") || {};                                      // nhớ lựa chọn lọc gần nhất (không nhớ ô tìm kiếm: nó mang tính tức thời)
  let status = FILTERS.some(([k]) => k === query.get("status")) ? query.get("status") : "all";
  const f = { q: "", kind: saved.kind || "", channel: saved.channel || "", days: saved.days || "" };
  let shown = PAGE, version = null, last = null, failedOnce = false, selecting = false, dash = null, dashSig = "";
  const picked = new Set();

  // ---- dải tổng quan ----
  const dashHost = h("section", { class: "card stack dash", "aria-label": "Tổng quan", "aria-live": "polite" });
  // ---- lọc ----
  const filterBar = h("div", { class: "filters", role: "group", "aria-label": "Lọc job theo trạng thái" });
  const searchIn = h("input", { type: "search", class: "input", placeholder: "Tìm theo tiêu đề, link, mã video, mã job…", "aria-label": "Tìm job", autocomplete: "off", spellcheck: "false" });
  const kindSel = select({ options: KINDS, value: f.kind });
  const chanSel = select({ options: [["", "Mọi kênh xuất bản"], ...(app.boot?.channels || []).filter((c) => c.ok).map((c) => [c.id, c.name || c.id])], value: f.channel });
  const daysSel = select({ options: DAYS, value: f.days });
  kindSel.setAttribute("aria-label", "Loại job"); chanSel.setAttribute("aria-label", "Kênh xuất bản"); daysSel.setAttribute("aria-label", "Thời gian tạo");
  const clearBtn = btn({ label: "Xoá lọc", icon: "x", size: "sm", kind: "ghost", onClick: () => { f.q = ""; f.kind = f.channel = f.days = ""; searchIn.value = ""; kindSel.value = chanSel.value = daysSel.value = ""; changed(); } });
  const selBtn = btn({ label: "Chọn nhiều", icon: "check", size: "sm", onClick: () => { selecting = !selecting; if (!selecting) picked.clear(); paintSel(); repaintRows(); } });
  const tools = h("div", { class: "row wrap jobs-tools", role: "search", "aria-label": "Tìm và lọc job" }, searchIn, kindSel, chanSel, daysSel, clearBtn, selBtn);
  const bulkBar = h("div", { class: "bulkbar row wrap", hidden: true, role: "region", "aria-label": "Thao tác với job đã chọn" });
  const list = h("ul", { class: "joblist", "aria-label": "Danh sách job" });
  const body = h("div", null);
  const more = h("div", { class: "row", hidden: true, style: "padding: var(--s-4); justify-content: center" });
  const card = h("div", { class: "card flush" }, list, body, more);
  const summary = h("p", { class: "muted small", "aria-live": "polite" });
  root.append(pageHead("Job", "Theo dõi mọi job; job cần bạn xử lý nằm ở nhóm “Cần xử lý”."), dashHost, filterBar, tools, bulkBar, card, summary);

  const active = () => !!(f.q || f.kind || f.channel || f.days);
  function changed() {
    LS.set("cf.jobs.filters", { kind: f.kind, channel: f.channel, days: f.days });
    shown = PAGE; version = null; picked.clear(); paintSel();
    clearBtn.hidden = !active();
    poller.poke();
  }
  let timer = null;
  searchIn.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { f.q = searchIn.value.trim(); changed(); }, 300); });
  kindSel.addEventListener("change", () => { f.kind = kindSel.value; changed(); });
  chanSel.addEventListener("change", () => { f.channel = chanSel.value; changed(); });
  daysSel.addEventListener("change", () => { f.days = daysSel.value; changed(); });
  clearBtn.hidden = !active();

  function paintFilters(counts) {
    filterBar.replaceChildren(...FILTERS.map(([k, label]) => {
      const b = h("button", { type: "button", "aria-pressed": String(k === status) }, label, h("span", { class: "n" }, counts ? `(${counts[k] ?? 0})` : ""));
      b.addEventListener("click", () => { if (k === status) return; status = k; shown = PAGE; version = null; picked.clear(); paintSel(); navigate(k === "all" ? "/jobs" : `/jobs?status=${k}`, { replace: true }); });
      return b;
    }));
  }
  paintFilters(app.counts);
  body.append(skeleton(3));

  // ---- tổng quan ----
  function paintDash() {
    const sig = JSON.stringify(dash) + notify.permission() + notify.enabled();
    if (sig === dashSig) return;
    dashSig = sig;
    clear(dashHost);
    if (!dash) return;
    const chip = (label, n, tone) => h("span", { class: `chip${n && tone ? " " + tone : ""}`, title: label }, h("strong", null, String(n)), ` ${label}`);
    const lane = (l) => h("span", { class: `chip${l.used >= l.limit ? " warn" : ""}`, title: "Số job đang dùng / giới hạn đồng thời" }, `${l.label}: `, h("strong", null, `${l.used}/${l.limit}`));
    const notifyBtn = !notify.supported() ? null : notify.enabled()
      ? btn({ label: "Tắt thông báo", icon: "bell", size: "sm", kind: "ghost", onClick: () => { notify.disable(); dashSig = ""; paintDash(); } })
      : notify.permission() === "denied" ? h("span", { class: "muted small" }, "Thông báo bị chặn trong trình duyệt")
        : btn({ label: "Bật thông báo", icon: "bell", size: "sm", kind: "ghost", title: "Chỉ báo khi có job cần xử lý hoặc Channel Run xong, không báo từng bước.", onClick: async () => { const p = await notify.enable(); if (p === "granted") notify.check(dash); dashSig = ""; paintDash(); } });
    dashHost.append(h("div", { class: "row spread" }, h("h2", { class: "h3", id: "dash-h" }, dash.headline), notifyBtn),
      h("div", { class: "row wrap" }, chip("đang chạy", dash.running), chip("đang chờ", dash.queued + dash.waiting), chip("tạm dừng", dash.paused), chip("cần xử lý", dash.attention, "warn"), chip("hoàn tất hôm nay", dash.completed_today),
        ...dash.lanes.map(lane), ...dash.disk.map((d) => h("span", { class: `chip${d.low ? " warn" : ""}`, title: "Dung lượng trống" }, `${d.label}: `, h("strong", null, `${d.free_gb} GB`), d.low ? " (sắp đầy)" : " trống"))),
      dash.needs_attention.length ? h("ul", { class: "autolist", "aria-label": "Job cần xử lý" }, ...dash.needs_attention.map((n) => h("li", null, h("a", { href: `#/jobs/${n.id}` }, n.title), ` — ${n.stage_label || ""}${n.reason ? `: ${n.reason}` : ""}`))) : null);
  }

  // ---- chọn nhiều + hàng loạt ----
  function decorate(li, j) {
    let cb = li._cb;
    const want = selecting;
    if (want && !cb) {
      cb = h("input", { type: "checkbox", class: "job-pick", "aria-label": `Chọn ${j.title || j.id}` });
      cb.addEventListener("change", () => { if (cb.checked) picked.add(j.id); else picked.delete(j.id); paintSel(); });
      li._cb = cb;
      li.prepend(cb);
      li.classList.add("selectable");
    } else if (!want && cb) { cb.remove(); li._cb = null; li.classList.remove("selectable"); }
    if (li._cb) { li._cb.checked = picked.has(j.id); li._cb.setAttribute("aria-label", `Chọn ${j.title || j.id}`); }
    return li;
  }
  const repaintRows = () => { for (const li of list.children) { const j = last?.jobs.find((x) => rowKey(x) === li.dataset.key) || null; if (j) decorate(li, j); } };
  function paintSel() {
    selBtn.querySelector("span").textContent = selecting ? "Xong chọn" : "Chọn nhiều";
    selBtn.setAttribute("aria-pressed", String(selecting));
    bulkBar.hidden = !selecting;
    if (!selecting) return;
    const n = picked.size, hasBatch = [...picked].some(isBatch);
    bulkBar.replaceChildren(h("strong", null, n ? `Đã chọn ${n} mục` : "Chọn job bằng ô tích bên trái"),
      btn({ label: "Chọn tất cả đang hiện", size: "sm", kind: "ghost", onClick: () => { for (const j of last?.jobs || []) picked.add(j.id); decorateAll(); paintSel(); } }),
      ...Object.entries(BULK).map(([action, [label, ic]]) => btn({ label, icon: ic, size: "sm", disabled: !n || (hasBatch && action !== "delete"), title: hasBatch && action !== "delete" ? "Channel Run chỉ xóa được ở đây; bỏ chọn Channel Run để dùng thao tác này." : undefined, kind: action === "cancel" || action === "delete" ? "danger" : "", onClick: (e) => runBulk(action, e.currentTarget) })),
      n ? btn({ label: "Bỏ chọn", size: "sm", kind: "ghost", onClick: () => { picked.clear(); decorateAll(); paintSel(); } }) : null);
  }
  const decorateAll = repaintRows;
  const isBatch = (id) => /^B\d+$/.test(id);
  async function runBulk(action, button) {
    const ids = [...picked];
    let args;
    if (action === "update_pipeline") { args = await openBulkPipeline(ids); if (!args) return; }
    else if (action === "template") { args = await openBulkTemplate(ids); if (!args) return; }
    else if (action === "cancel" && !(await confirmDialog({ title: `Hủy ${ids.length} job?`, body: "Kết quả đã có được giữ lại, nhưng các job sẽ không tự chạy lại. Job đã hoàn tất không bị đổi.", confirmLabel: "Hủy job", danger: true }))) return;
    else if (action === "delete") {
      const runs = ids.filter(isBatch).length, running = (last?.jobs || []).filter((j) => picked.has(j.id) && j.status === "running").length;
      if (!(await confirmDialog({ title: `Xóa ${ids.length} mục?`, body: `${runs ? `${runs} Channel Run sẽ bị xóa cùng TẤT CẢ job con bên trong. ` : ""}Thư mục output đã tạo được giữ nguyên.${running ? ` ${running} job đang chạy sẽ được dừng ở điểm an toàn rồi xóa.` : ""}${runs ? " Job con của Channel Run đang chạy cũng được dừng." : ""}`, confirmLabel: "Xóa", danger: true }))) return;
    }
    await busy(button, async () => {
      const r = await sendBulk(action, ids, args);
      if (r) { picked.clear(); paintSel(); version = null; poller.poke(); }
    });
  }

  // ---- tải ----
  const poller = createPoller(async (signal) => {
    let d;
    const qs = { status, limit: Math.min(shown, CAP), since: version, q: f.q, kind: f.kind, channel: f.channel, days: f.days };
    try {
      [d, dash] = await Promise.all([api.get("/api/jobs", { query: qs, signal }), api.get("/api/dashboard", { signal }).catch((e) => { if (e.name === "AbortError") throw e; return dash; })]);
      failedOnce = false;
    } catch (e) { if (e.name === "AbortError") throw e; if (!failedOnce) { body.replaceChildren(errorState(e, () => poller.poke())); failedOnce = true; } throw e; }
    paintDash();
    if (dash) notify.check(dash);
    if (!d.changed) return last?.jobs.some((j) => j.status === "running" || j.status === "queued") ? "fast" : "idle";
    version = d.version;
    if (!active()) publishCounts(d.counts);                                          // huy hiệu "cần xử lý" của khung luôn là số THẬT, không phải số đã lọc
    last = d;
    paintFilters(d.counts);
    body.replaceChildren();
    if (!d.jobs.length) {
      body.append(active()
        ? emptyState({ icon: "list", title: "Không có job nào khớp bộ lọc", text: f.q ? `Không thấy “${f.q}”. Thử từ khoá ngắn hơn hoặc bỏ bớt bộ lọc.` : "Bỏ bớt bộ lọc để thấy nhiều hơn.", action: btn({ label: "Xoá lọc", icon: "x", onClick: () => clearBtn.click() }) })
        : status === "all"
          ? emptyState({ icon: "list", title: "Chưa có job nào", text: "Dán một link YouTube ở màn hình Chạy để bắt đầu.", action: btn({ label: "Tới màn hình Chạy", icon: "play", kind: "primary", href: "#/" }) })
          : emptyState({ icon: "check-circle", title: "Không có job nào trong nhóm này", text: status === "attention" ? "Tốt: không có gì cần bạn xử lý." : "Thử nhóm khác." }));
    }
    const added = patchList(list, d.jobs, rowKey, (j) => { const li = makeRow(j, () => poller.poke()); li.dataset.key = rowKey(j); return decorate(li, j); }, (li, j) => { updateRow(li, j); decorate(li, j); });
    scope.add(() => motion.itemsEnter(added));
    more.hidden = !d.has_more || shown >= CAP;
    more.replaceChildren(btn({ label: `Tải thêm (${d.total - d.jobs.length} job nữa)`, icon: "plus", onClick: () => { shown = Math.min(CAP, shown + PAGE); version = null; poller.poke(); } }));
    summary.textContent = d.total > CAP && shown >= CAP ? `Hiển thị ${CAP} job mới nhất trong ${d.total}. Dùng bộ lọc để thu hẹp.` : `${d.total} job${active() ? " khớp bộ lọc" : ""}`;
    return d.jobs.some((j) => j.status === "running" || j.status === "queued") ? "fast" : "idle";
  });
  paintSel();
  poller.start();
  return { destroy() { clearTimeout(timer); poller.stop(); } };
}
