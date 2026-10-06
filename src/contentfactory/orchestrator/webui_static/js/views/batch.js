// Chi tiết Channel Run: tổng quan (kênh nguồn, kênh xuất bản, lựa chọn, pipeline), hành động hàng loạt, bộ lọc, danh sách video con (mỗi con là một job độc lập), chọn nhiều để thao tác.
// Điều kiện nút (actions), trạng thái và đếm do backend tính; ở đây chỉ vẽ + gọi API. Hành động nguy hiểm (hủy) luôn có xác nhận và nằm trong "Thao tác nâng cao".
import { api } from "../api.js";
import { h, clear, patchList } from "../dom.js";
import { icon } from "../icons.js";
import { btn, busy, alertBox, badge, jobBadge, updateBadge, progress, updateProgress, errorState, skeleton, disclosure, select, toast, toastError, openDialog, kv } from "../components.js";
import { createPoller } from "../poller.js";
import { jobStatus } from "../status.js";
import { relTime, pct } from "../format.js";
import { actionsFor } from "./_jobrow.js";
import { BATCH_STATUS_LABEL, ACTION_TEXT, batchAction, countChips, externalLink } from "./_batch_ui.js";

const PAGE = 50;
const TABS = [["all", "Tất cả"], ["running", "Đang chạy"], ["queued", "Chờ"], ["paused", "Tạm dừng"], ["attention", "Cần xử lý"], ["completed", "Hoàn tất"]];
const TAB_COUNT = { all: (c) => c.total, running: (c) => c.running, queued: (c) => c.queued + c.pending_creation, paused: (c) => c.paused + c.waiting, attention: (c) => c.attention + c.failed, completed: (c) => c.completed };
const SCOPES = [["unfinished", "Mọi video chưa hoàn tất (khuyên dùng)"], ["unstarted", "Chỉ video chưa bắt đầu"], ["selected", "Chỉ các video đang chọn"], ["all_compatible", "Tất cả video tương thích"]];
const BULK = { pause: ["Tạm dừng", "pause"], resume: ["Tiếp tục", "play"], retry: ["Chạy lại", "refresh"] };

function selectionText(s) {
  if (s.mode === "newest") return `${s.n} video mới nhất chưa xử lý`;
  if (s.mode === "oldest") return `${s.n} video cũ nhất`;
  if (s.mode === "range") return `video thứ ${s.from}–${s.to} trong danh sách`;
  if (s.mode === "dates") return `đăng ${s.date_from || "…"} → ${s.date_to || "…"}`;
  return `${(s.ids || []).length} video chọn tay`;
}
const ROW_STATUS = { pending: { label: "Đang tạo job", icon: "clock", tone: "queue" }, error: { label: "Không tạo được job", icon: "alert", tone: "attn" }, cancelled: jobStatus("cancelled") };

export async function mount(root, ctx) {
  const [id] = ctx.params;
  const { scope, navigate } = ctx;
  let tab = TABS.some(([k]) => k === ctx.query.get("status")) ? ctx.query.get("status") : "all";
  let shown = PAGE, data = null, failedOnce = false;
  const picked = new Set();                                              // job_id đang chọn (giữ qua các lần poll)

  const head = h("div", null);
  const actionBar = h("div", { class: "row wrap", role: "group", "aria-label": "Hành động cho cả Channel Run" });
  const activity = h("p", { class: "muted small", "aria-live": "polite" });
  const warnHost = h("div", { class: "stack" });
  const tabBar = h("div", { class: "filters", role: "group", "aria-label": "Lọc video theo trạng thái" });
  const bulkBar = h("div", { class: "bulkbar row wrap", hidden: true, role: "region", "aria-label": "Thao tác với video đã chọn" });
  const list = h("ul", { class: "joblist", "aria-label": "Các video trong Channel Run" });
  const body = h("div", null);
  const more = h("div", { class: "row", hidden: true, style: "padding: var(--s-4); justify-content: center" });
  const selAll = h("input", { type: "checkbox", id: "sel-all", "aria-label": "Chọn tất cả video đang hiển thị" });
  const listCard = h("div", { class: "card flush" }, h("div", { class: "listhead row", style: "padding: var(--s-2) var(--s-4)" }, selAll, h("label", { for: "sel-all", class: "small muted" }, "Chọn tất cả đang hiển thị")), list, body, more);
  root.append(h("p", null, btn({ label: "Tất cả job", icon: "list", kind: "ghost", size: "sm", href: "#/jobs" })), head, h("div", { class: "stack" }, activity, warnHost, actionBar, tabBar, bulkBar, listCard));
  head.append(skeleton(2));

  const poller = createPoller(async (signal) => {
    let d;
    try { d = await api.get(`/api/batches/${id}`, { query: { status: tab, limit: shown }, signal }); failedOnce = false; }
    catch (e) {
      if (e.name === "AbortError") throw e;
      if (e.status === 404 || e.code === "BATCH_NOT_FOUND") { root.replaceChildren(h("h1", { id: "page-title" }, "Không tìm thấy Channel Run"), alertBox({ tone: "fail", title: e.message, body: e.hint, actions: [btn({ label: "Về danh sách job", href: "#/jobs", size: "sm" })] })); poller.stop(); return; }
      if (!failedOnce) { body.replaceChildren(errorState(e, () => poller.poke())); failedOnce = true; }
      throw e;
    }
    data = d;
    paint(d);
    return d.status === "RUNNING" || d.status === "QUEUED" ? "fast" : "idle";
  });
  const after = () => poller.poke();

  // ---------- vẽ ----------
  let headSig = "", barSig = "", tabSig = "";
  function paint(d) {
    document.title = `Channel Run · ${d.title} · ContentFactory`;
    const hs = JSON.stringify([d.title, d.status, d.counts, d.fraction, d.control]);
    if (hs !== headSig) { headSig = hs; paintHead(d); }
    const bs = JSON.stringify([d.actions, d.control, d.status]);
    if (bs !== barSig) { barSig = bs; paintBar(d); }
    const ts = JSON.stringify([tab, d.counts]);
    if (ts !== tabSig) { tabSig = ts; paintTabs(d); }
    activity.textContent = d.current_activity?.length ? `Đang làm: ${d.current_activity.join(" · ")}` : "";
    clear(warnHost);
    for (const w of d.warnings || []) warnHost.append(alertBox({ tone: "wait", title: w }));
    if (d.skipped && Object.keys(d.skipped).length) warnHost.append(h("p", { class: "muted small" }, "Khi tạo đã bỏ qua: " + Object.entries(d.skipped).map(([k, n]) => `${n} ${{ processed: "video đã xử lý", livestream: "livestream", upcoming: "video sắp công chiếu", short: "Shorts", unavailable: "video không khả dụng" }[k] || k}`).join(", ") + "."));
    paintRows(d);
  }

  function paintHead(d) {
    const meta = h("div", { class: "row wrap small muted" },
      h("span", null, `Kênh xuất bản: ${d.output_channel.name}`), h("span", null, `Lựa chọn: ${selectionText(d.selection)}`), h("span", null, `Pipeline: ${d.pipeline_label}`),
      h("span", null, `Tạo ${relTime(d.created_at)}`), h("span", { class: "mono" }, `#${d.id}`));
    const bar = progress(0, "running", "Tiến độ cả Channel Run");
    updateProgress(bar, d.fraction, d.ui_status === "attention" ? "attn" : d.ui_status === "completed" ? "done" : d.ui_status === "paused" ? "wait" : "running");
    const live = h("span", { class: "sr-only", "aria-live": "polite" }, `Trạng thái: ${BATCH_STATUS_LABEL[d.status]}`);
    const bd = jobBadge(d.ui_status);
    updateBadge(bd, jobStatus(d.ui_status), BATCH_STATUS_LABEL[d.status]);
    head.replaceChildren(h("div", { class: "page-head" },
      h("div", { class: "grow" }, h("h1", { id: "page-title" }, icon("tv", { size: 26 }), ` Channel Run — ${d.title}`), meta),
      h("div", { class: "row" }, bd, live, externalLink(d.source.channel_url || d.source.url, "Mở kênh nguồn trên YouTube"))),
      h("div", { class: "stack", style: "margin-top: var(--s-3)" }, h("div", { class: "row" }, h("div", { style: "flex: 1" }, bar), h("strong", null, pct(d.fraction))), countChips(d.counts)));
  }

  function barBtn(label, ic, enabled, why, onClick, kind = "") {
    const b = btn({ label, icon: ic, size: "sm", kind, disabled: !enabled, title: enabled ? "" : why });
    if (enabled) b.addEventListener("click", () => onClick(b));
    return b;
  }

  function paintBar(d) {
    const a = d.actions;
    const paused = d.control === "PAUSED";
    actionBar.replaceChildren(
      barBtn(ACTION_TEXT.pause, "pause", a.pause, "Không có video nào đang chạy hoặc chờ để tạm dừng.", (b) => batchAction(id, "pause", b, { after })),
      barBtn("Tiếp tục các video đã tạm dừng", "play", a.resume, "Không có video nào đang tạm dừng bởi Channel Run. (Video bạn tự tạm dừng thì tiếp tục riêng.)", (b) => batchAction(id, "resume", b, { after }), paused ? "primary" : ""),
      barBtn(ACTION_TEXT.retry_failed, "refresh", a.retry_failed, "Không có video nào bị lỗi.", (b) => batchAction(id, "retry_failed", b, { after }), d.counts.failed ? "primary" : ""),
      barBtn("Quét lại kênh (thêm video mới)", "search", a.rescan, "Channel Run đã bị hủy.", (b) => batchAction(id, "rescan", b, { after })),
      disclosure({ label: "Thao tác nâng cao", content: h("div", { class: "row wrap", style: "padding-top: var(--s-2)" },
        barBtn("Cập nhật pipeline…", "layers", a.update_pipeline, "Không còn video nào chưa hoàn tất.", () => openPipelineDialog(d)),
        barBtn("Hủy việc chưa chạy…", "x", a.cancel_queued, "Không có việc nào chưa chạy.", (b) => batchAction(id, "cancel_queued", b, { after, confirm: { title: "Hủy các việc chưa chạy?", text: "Chỉ video chưa bắt đầu bị hủy; video đang chạy hoặc đã xong giữ nguyên.", ok: "Hủy việc chưa chạy" } })),
        barBtn("Hủy cả Channel Run…", "x", a.cancel, "Channel Run không còn việc để hủy.", (x) => batchAction(id, "cancel", x, { after, confirm: { title: "Hủy cả Channel Run?", text: "Mọi video chưa xong bị hủy và sẽ không tự chạy lại. Kết quả đã có vẫn được giữ.", ok: "Hủy Channel Run" } }), "danger")) }));
  }

  function paintTabs(d) {
    tabBar.replaceChildren(...TABS.map(([k, label]) => {
      const b = h("button", { type: "button", "aria-pressed": String(k === tab) }, label, h("span", { class: "n" }, `(${TAB_COUNT[k](d.counts)})`));
      b.addEventListener("click", () => { if (k === tab) return; tab = k; shown = PAGE; picked.clear(); navigate(k === "all" ? `/batches/${id}` : `/batches/${id}?status=${k}`, { replace: true }); });
      return b;
    }));
  }

  // ---------- danh sách video con ----------
  function rowParts(r) {
    const li = h("li", { class: "job child" });
    li._p = { cb: h("input", { type: "checkbox" }), pos: h("span", { class: "mono muted" }), title: h("a", { class: "trunc" }), meta: h("div", { class: "meta" }), bar: progress(0, "running", "Tiến độ video"), badge: h("span", null), act: h("div", { class: "row" }) };
    const p = li._p;
    p.cb.addEventListener("change", () => { const jid = li._jid; if (!jid) return; if (p.cb.checked) picked.add(jid); else picked.delete(jid); paintBulk(); });
    li.append(h("div", { class: "row" }, p.cb, p.pos), h("div", { class: "grow" }, h("div", { class: "title trunc" }, p.title), p.meta), h("div", { class: "stage" }, p.bar), h("div", { class: "row" }, p.badge, p.act));
    return li;
  }
  function updateRow(li, r) {
    const p = li._p, j = r.job;
    li._jid = r.job_id;
    p.pos.textContent = `#${r.position}`;
    p.cb.disabled = !r.job_id;
    p.cb.checked = !!r.job_id && picked.has(r.job_id);
    p.cb.setAttribute("aria-label", `Chọn video ${r.position}: ${r.title || r.video_id}`);
    const t = (j && j.title) || r.title || r.video_id;
    p.title.textContent = t;
    p.title.title = t;
    if (r.job_id) p.title.setAttribute("href", `#/jobs/${r.job_id}`); else p.title.removeAttribute("href");
    const m = [h("span", null, j?.stage_label || (r.status === "pending" ? "Đang tạo job…" : "")), r.published ? h("span", null, r.published) : null];
    if (r.error) m.push(h("span", { class: "chip warn" }, icon("alert", { size: 12 }), r.error));
    const ext = externalLink(r.links.source_video, "Mở video nguồn");
    if (ext) m.push(ext);
    if (r.links.published_video) m.push(externalLink(r.links.published_video, "Mở video đã đăng"));
    p.meta.replaceChildren(...m.filter(Boolean));
    updateProgress(p.bar, j?.fraction || 0, r.status === "attention" || r.status === "failed" ? "attn" : r.status === "completed" ? "done" : r.status === "paused" || r.status === "waiting" ? "wait" : "running");
    p.badge.replaceWith((p.badge = ROW_STATUS[r.status] ? badge(ROW_STATUS[r.status]) : jobBadge(r.status)));
    p.act.replaceChildren(...(j ? actionsFor({ id: j.id, status: r.status, next_action: j.next_action, output_dir: j.output_dir }, after) : []));
  }
  function paintRows(d) {
    body.replaceChildren();
    if (!d.items.length) {
      body.append(h("div", { class: "empty", style: "padding: var(--s-5)" }, icon("list", { size: 32 }), h("h2", null, tab === "all" ? "Channel Run chưa có video nào" : "Không có video nào trong nhóm này"),
        h("p", null, tab === "attention" ? "Tốt: không có video nào cần bạn xử lý." : "Thử nhóm khác.")));
    }
    patchList(list, d.items, (r) => r.video_id, (r) => { const li = rowParts(r); updateRow(li, r); return li; }, updateRow);
    more.hidden = !d.has_more;
    more.replaceChildren(btn({ label: `Tải thêm (${d.total_items - d.items.length} video nữa)`, icon: "plus", onClick: () => { shown += PAGE; poller.poke(); } }));
    selAll.checked = d.items.length > 0 && d.items.every((r) => r.job_id && picked.has(r.job_id));
    paintBulk();
  }
  selAll.addEventListener("change", () => {
    for (const r of data?.items || []) if (r.job_id) { if (selAll.checked) picked.add(r.job_id); else picked.delete(r.job_id); }
    paintRows(data);
  });

  function paintBulk() {
    bulkBar.hidden = picked.size === 0;
    if (!picked.size) return;
    bulkBar.replaceChildren(h("strong", null, `Đã chọn ${picked.size} video`),
      ...Object.entries(BULK).map(([action, [label, ic]]) => btn({ label, icon: ic, size: "sm", onClick: (e) => bulk(action, e.currentTarget) })),
      btn({ label: "Bỏ chọn", size: "sm", kind: "ghost", onClick: () => { picked.clear(); paintRows(data); } }));
  }
  async function bulk(action, button) {
    await busy(button, async () => {
      try {
        const r = await api.post("/api/jobs/bulk", { action, job_ids: [...picked] });
        const c = r.counts, total = r.results.length;
        const bad = r.results.filter((x) => x.result === "skipped" || x.result === "error");
        toast({ title: `${BULK[action][0]}: ${c.done}/${total} video`, message: bad.length ? `${bad.length} video không áp dụng được: ${bad[0].reason}${bad.length > 1 ? " …" : ""}` : (c.unchanged ? `${c.unchanged} video không đổi.` : ""), tone: bad.length ? "wait" : "done" });
        if (bad.length) await openDialog({ title: "Một số video không áp dụng được", describe: `${c.done} video thành công, ${bad.length} video bị bỏ qua.`, content: h("ul", { class: "autolist" }, ...bad.slice(0, 50).map((x) => h("li", null, h("span", { class: "mono" }, `#${x.job_id}`), ` — ${x.reason}`))), actions: [{ label: "Đóng", kind: "primary", value: true }] });
        picked.clear();
        after();
      } catch (e) { toastError(e, "Chưa làm được"); }
    });
  }

  // ---------- cập nhật pipeline cho nhiều video ----------
  async function openPipelineDialog(d) {
    const sel = new Set(d.requested_stages);
    const rows = h("div", { class: "stage-pick", role: "group", "aria-label": "Các bước muốn có kết quả" });
    const problems = h("div", { class: "stack", "aria-live": "polite" });
    const scopeSel = select({ options: SCOPES.filter(([k]) => k !== "selected" || picked.size), value: "unfinished" });
    let seq = 0;
    const apply = (el, st) => {
      const fixed = st.state === "locked" || st.state === "provided" || (st.state === "selected" && st.by.length > 0);
      el.dataset.role = st.state;
      el._cb.checked = st.state === "selected" || st.state === "locked";
      el._cb.disabled = fixed;
      el.querySelector(".s-tag").replaceChildren(...({ locked: [icon("lock", { size: 14 }), "Bắt buộc"], provided: [icon("refresh", { size: 14 }), "Dùng lại"], selected: [icon("check", { size: 14 }), "Đã chọn"] }[st.state] || []));
      el.querySelector(".s-why").textContent = st.state === "not_requested" ? "" : st.reason;
    };
    async function refresh() {
      const my = ++seq;
      let r;
      try { r = await api.post("/api/pipeline/plan", { pipeline_spec: { version: 2, requested_stages: [...sel] }, input_kind: "youtube_url" }); } catch (e) { return; }
      if (my !== seq) return;
      clear(problems);
      if (!r.ok) problems.append(alertBox({ tone: "wait", title: r.errors[0] }));
      patchList(rows, r.stages, (s) => s.id, (s) => {
        const cb = h("input", { type: "checkbox", id: `bp-${s.id}` });
        cb.addEventListener("change", () => { if (cb.checked) sel.add(s.id); else sel.delete(s.id); refresh(); });
        const el = h("label", { class: "pick-row", for: cb.id }, cb, h("span", { class: "s-label" }, s.label), h("span", { class: "s-tag" }), h("span", { class: "s-why" }));
        el._cb = cb;
        apply(el, s);
        return el;
      }, apply);
    }
    refresh();
    const content = h("div", { class: "stack" }, h("p", { class: "muted small" }, `Áp dụng cho các video của Channel Run theo phạm vi bên dưới. Mỗi video được kiểm riêng: video đã hoàn tất không bị đổi tại chỗ (dùng “Chạy lại với thay đổi” ở trang video), video lỗi hoặc không hợp lệ được báo rõ.`),
      rows, problems, h("label", { class: "field" }, h("span", { class: "label" }, "Áp dụng cho"), scopeSel));
    await openDialog({ title: "Cập nhật pipeline của Channel Run", wide: true, content, actions: [{ label: "Hủy", value: null }, { label: "Áp dụng", kind: "primary", value: "ok", onClick: async () => {
      try {
        const r = await api.post(`/api/batches/${id}/pipeline-revisions`, { pipeline: { requested_stages: [...sel] }, scope: scopeSel.value, job_ids: scopeSel.value === "selected" ? [...picked] : undefined });
        const c = r.counts;
        toast({ title: `Đã áp dụng cho ${c.applied + c.pending} video`, message: [c.rejected ? `${c.rejected} video bị từ chối` : "", c.skipped ? `${c.skipped} video bỏ qua (đã xong/đã hủy)` : ""].filter(Boolean).join("; "), tone: c.rejected ? "wait" : "done" });
        after();
        return true;
      } catch (e) { toastError(e, "Chưa cập nhật được"); return false; }
    } }] });
  }

  poller.start();
  return { destroy() { poller.stop(); } };
}
