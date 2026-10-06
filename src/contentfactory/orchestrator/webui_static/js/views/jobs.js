// Danh sách job: lọc theo nhóm trạng thái, cập nhật tại chỗ, phân trang ("Tải thêm"), không dựng lại cả danh sách mỗi lượt poll.
import { api } from "../api.js";
import { h, patchList } from "../dom.js";
import { btn, emptyState, errorState, pageHead, skeleton } from "../components.js";
import { createPoller } from "../poller.js";
import { publishCounts } from "../router.js";
import { FILTERS } from "../status.js";
import * as motion from "../motion.js";
import { makeRow, updateRow, rowKey } from "./_batch_ui.js";

const PAGE = 30, CAP = 200;

export async function mount(root, ctx) {
  const { app, scope, navigate, query } = ctx;
  let status = FILTERS.some(([k]) => k === query.get("status")) ? query.get("status") : "all";
  let shown = PAGE, version = null, last = null, failedOnce = false;

  const filterBar = h("div", { class: "filters", role: "group", "aria-label": "Lọc job theo trạng thái" });
  const list = h("ul", { class: "joblist", "aria-label": "Danh sách job" });
  const body = h("div", null);
  const more = h("div", { class: "row", hidden: true, style: "padding: var(--s-4); justify-content: center" });
  const card = h("div", { class: "card flush" }, list, body, more);
  const summary = h("p", { class: "muted small", "aria-live": "polite" });
  root.append(pageHead("Job", "Theo dõi mọi job; job cần bạn xử lý nằm ở nhóm “Cần xử lý”."), filterBar, h("div", { style: "height: var(--s-4)" }), card, summary);

  function paintFilters(counts) {
    filterBar.replaceChildren(...FILTERS.map(([k, label]) => {
      const b = h("button", { type: "button", "aria-pressed": String(k === status) }, label, h("span", { class: "n" }, counts ? `(${counts[k] ?? 0})` : ""));
      b.addEventListener("click", () => { if (k === status) return; status = k; shown = PAGE; version = null; navigate(k === "all" ? "/jobs" : `/jobs?status=${k}`, { replace: true }); });
      return b;
    }));
  }
  paintFilters(app.counts);
  body.append(skeleton(3));

  const poller = createPoller(async (signal) => {
    let d;
    try { d = await api.get("/api/jobs", { query: { status, limit: Math.min(shown, CAP), since: version }, signal }); failedOnce = false; }
    catch (e) { if (e.name === "AbortError") throw e; if (!failedOnce) { body.replaceChildren(errorState(e, () => poller.poke())); failedOnce = true; } throw e; }
    if (!d.changed) return last?.jobs.some((j) => j.status === "running" || j.status === "queued") ? "fast" : "idle";
    version = d.version;
    publishCounts(d.counts);
    last = d;
    paintFilters(d.counts);
    body.replaceChildren();
    if (!d.jobs.length) {
      body.append(status === "all"
        ? emptyState({ icon: "list", title: "Chưa có job nào", text: "Dán một link YouTube ở màn hình Chạy để bắt đầu.", action: btn({ label: "Tới màn hình Chạy", icon: "play", kind: "primary", href: "#/" }) })
        : emptyState({ icon: "check-circle", title: "Không có job nào trong nhóm này", text: status === "attention" ? "Tốt: không có gì cần bạn xử lý." : "Thử nhóm khác." }));
    }
    const added = patchList(list, d.jobs, rowKey, (j) => makeRow(j, () => poller.poke()), updateRow);
    scope.add(() => motion.itemsEnter(added));
    more.hidden = !d.has_more || shown >= CAP;
    more.replaceChildren(btn({ label: `Tải thêm (${d.total - d.jobs.length} job nữa)`, icon: "plus", onClick: () => { shown = Math.min(CAP, shown + PAGE); version = null; poller.poke(); } }));
    summary.textContent = d.total > CAP && shown >= CAP ? `Hiển thị ${CAP} job mới nhất trong ${d.total}. Dùng bộ lọc để thu hẹp.` : `${d.total} job`;
    return d.jobs.some((j) => j.status === "running" || j.status === "queued") ? "fast" : "idle";
  });
  poller.start();
  return { destroy() { poller.stop(); } };
}
