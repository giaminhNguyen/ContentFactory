// Thành phần dùng chung cho Channel Run (D-101): thẻ trong danh sách job, chip đếm trạng thái, hành động hàng loạt (một chỗ => cùng phản hồi/chống bấm đúp).
// Mọi trạng thái/điều kiện (batch_status, next_action, actions) do backend tính; ở đây chỉ vẽ và gọi API.
import { api } from "../api.js";
import { h } from "../dom.js";
import { icon } from "../icons.js";
import { btn, busy, progress, updateProgress, jobBadge, updateBadge, toast, toastError, openDialog } from "../components.js";
import { jobStatus } from "../status.js";
import { relTime, pct } from "../format.js";
import { jobRow, updateJobRow } from "./_jobrow.js";

export const BATCH_STATUS_LABEL = {
  QUEUED: "Đang xếp hàng", RUNNING: "Đang chạy", PAUSED: "Tạm dừng", NEEDS_ATTENTION: "Cần xử lý", COMPLETED: "Hoàn tất", COMPLETED_WITH_ERRORS: "Xong, có video lỗi", CANCELLED: "Đã hủy",
};
const BAR_TONE = { queued: "running", running: "running", paused: "wait", attention: "attn", completed: "done", cancelled: "wait" };
// (khóa trong counts, nhãn, biểu tượng): mỗi chip có CHỮ + số + biểu tượng, không chỉ màu
const CHIPS = [["completed", "hoàn tất", "check-circle"], ["running", "đang chạy", "spinner"], ["queued", "chờ", "clock"], ["paused", "tạm dừng", "pause"],
               ["waiting", "chờ tài nguyên", "hourglass"], ["attention", "cần xử lý", "alert"], ["failed", "lỗi", "x-circle"], ["cancelled", "đã hủy", "skip"]];

export function countChips(c) {
  const box = h("div", { class: "count-chips", role: "group", "aria-label": "Tình trạng các video" });
  for (const [k, label, ic] of CHIPS) {
    if (!c[k]) continue;
    box.append(h("span", { class: "chip", dataset: { k } }, icon(ic, { size: 14, cls: k === "running" ? "spin" : "" }), h("strong", null, String(c[k])), h("span", null, ` ${label}`)));
  }
  if (c.pending_creation) box.append(h("span", { class: "chip" }, icon("clock", { size: 14 }), h("strong", null, String(c.pending_creation)), h("span", null, " đang tạo job")));
  if (c.error) box.append(h("span", { class: "chip warn" }, icon("alert", { size: 14 }), h("strong", null, String(c.error)), h("span", null, " không tạo được job")));
  return box;
}

export function externalLink(url, label) {
  if (!url) return null;
  const a = h("a", { class: "btn ghost sm", href: url, target: "_blank", rel: "noopener noreferrer" }, icon("external", { size: 16 }), h("span", null, label));
  a.append(h("span", { class: "sr-only" }, " (mở tab mới)"));
  return a;
}

export const ACTION_TEXT = { pause: "Tạm dừng tất cả", resume: "Tiếp tục", retry_failed: "Chạy lại job lỗi" };
const ACTION_ICON = { pause: "pause", resume: "play", retry_failed: "refresh" };
const ENDPOINT = { pause: "pause", resume: "resume", retry_failed: "retry-failed", cancel_queued: "cancel-queued", cancel: "cancel", rescan: "rescan" };

function summarize(action, r) {
  switch (action) {
    case "pause": return { title: r.paused ? `Đã tạm dừng ${r.paused} video` : "Không có video nào cần tạm dừng", message: r.unchanged ? `${r.unchanged} video đã tạm dừng từ trước.` : "" };
    case "resume": return { title: r.resumed ? `Đã tiếp tục ${r.resumed} video` : "Không có video nào cần tiếp tục", message: r.kept_paused_by_user ? `${r.kept_paused_by_user} video bạn tự tạm dừng được giữ nguyên.` : "" };
    case "retry_failed": return { title: r.retried ? `Đã xếp lại ${r.retried} job lỗi` : "Không có job lỗi để chạy lại", message: "Chỉ các job lỗi chạy lại, các job khác không đổi." };
    case "cancel_queued": return { title: `Đã hủy ${r.cancelled_jobs + r.cancelled_items} việc chưa chạy`, message: r.kept ? `${r.kept} video đang chạy hoặc đã xong được giữ nguyên.` : "" };
    case "cancel": return { title: "Đã hủy Channel Run", message: "Kết quả đã có được giữ lại." };
    default: return { title: r.added ? `Đã thêm ${r.added} video mới` : "Không có video mới", message: "" };
  }
}

// Chạy một hành động của batch với khóa nút + thông báo kết quả (kể cả thành công một phần). `confirm` = hộp xác nhận cho hành động nguy hiểm.
export async function batchAction(id, action, button, { after, confirm } = {}) {
  if (confirm && !(await openDialog({ title: confirm.title, describe: confirm.text, content: null, actions: [{ label: "Không", value: null }, { label: confirm.ok, kind: "danger solid", value: "ok" }] }))) return false;
  return busy(button, async () => {
    try {
      const r = await api.post(`/api/batches/${id}/${ENDPOINT[action]}`);
      toast({ ...summarize(action, r), tone: "done" });
      after?.();
      return true;
    } catch (e) { toastError(e, "Chưa làm được"); return false; }
  });
}

// ---------- thẻ Channel Run trong danh sách job ----------
export function batchRow(b, onChanged) {
  const li = h("li", { class: "job batch" });
  li._parts = {
    link: h("a", { href: `#/batches/${b.id}` }), meta: h("div", { class: "meta" }), chips: h("div", null), bar: progress(0, "running", `Tiến độ Channel Run ${b.id}`),
    barText: h("span", { class: "muted small" }), badge: jobBadge(b.status), action: h("div", { class: "row" }),
  };
  const p = li._parts;
  li.append(h("div", { class: "grow" }, h("div", { class: "title trunc" }, icon("tv", { size: 18 }), " ", p.link), p.meta, p.chips),
    h("div", { class: "batch-progress" }, p.bar, p.barText), h("div", { class: "row" }, p.badge, p.action));
  li._onChanged = onChanged;
  updateBatchRow(li, b);
  return li;
}

export function updateBatchRow(li, b) {
  const p = li._parts;
  const sig = JSON.stringify([b.status, b.batch_status, b.counts, b.fraction, b.title, b.next_action, b.updated_at]);
  if (li._sig === sig) return;
  li._sig = sig;
  p.link.textContent = `Kênh nguồn: ${b.title}`;
  p.link.title = b.title;
  p.meta.replaceChildren(h("span", null, b.kind === "youtube_playlist" ? "Channel Run · playlist" : "Channel Run"), h("span", null, `Kênh xuất bản: ${b.output_channel.name}`),
    h("span", null, `${b.counts.total} video`), h("span", { class: "mono" }, `#${b.id}`), h("span", null, relTime(b.updated_at)));
  p.chips.replaceChildren(countChips(b.counts));
  updateBadge(p.badge, jobStatus(b.status), BATCH_STATUS_LABEL[b.batch_status] || undefined);
  updateProgress(p.bar, b.fraction, BAR_TONE[b.status] || "running");
  p.barText.textContent = pct(b.fraction);
  const acts = [];
  if (b.next_action && ACTION_TEXT[b.next_action]) {
    const a = btn({ label: ACTION_TEXT[b.next_action], icon: ACTION_ICON[b.next_action], size: "sm", kind: b.next_action === "pause" ? "" : "primary" });
    a.addEventListener("click", () => batchAction(b.id, b.next_action, a, { after: li._onChanged }));
    acts.push(a);
  }
  acts.push(btn({ label: "Chi tiết", size: "sm", href: `#/batches/${b.id}` }));
  const ext = externalLink(b.source?.channel_url || b.source?.url, "Mở YouTube");
  if (ext) acts.push(ext);
  p.action.replaceChildren(...acts);
}

// Một hàng của danh sách trộn (job đơn hoặc Channel Run): khóa ổn định để patchList không dựng lại.
export const rowKey = (r) => `${r.type === "batch" ? "b" : "j"}:${r.id}`;

export const makeRow = (r, cb) => (r.type === "batch" ? batchRow(r, cb) : jobRow(r, cb));
export const updateRow = (el, r) => (r.type === "batch" ? updateBatchRow(el, r) : updateJobRow(el, r));
