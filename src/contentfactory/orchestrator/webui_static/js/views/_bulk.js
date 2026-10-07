// Hành động hàng loạt trên các JOB ĐÃ CHỌN (Phase 9). Backend kiểm TỪNG job và trả kết quả từng job; giao diện chỉ gửi lựa chọn và báo
// thành công một phần RÕ RÀNG (không job nào bị bỏ lặng lẽ). Không có logic nghiệp vụ ở đây.
import { api } from "../api.js";
import { h } from "../dom.js";
import { field, select, openDialog, toast, toastError } from "../components.js";
import { openTargetDialog } from "./_job_edit.js";

export const BULK = {
  pause: ["Tạm dừng", "pause"], resume: ["Tiếp tục", "play"], retry: ["Chạy lại", "refresh"],
  update_pipeline: ["Cập nhật pipeline…", "layers"], template: ["Đổi template…", "layout"], cancel: ["Hủy…", "x"], delete: ["Xóa…", "trash"],
};
const KIND_LABEL = { thumbnail: "Thumbnail", youtube: "Video YouTube", tiktok: "Video TikTok" };

// Gửi một lô và báo kết quả. Trả kết quả của backend (hoặc null khi lỗi chung).
export async function sendBulk(action, ids, args, label = BULK[action]?.[0] || action) {
  try {
    const r = await api.post("/api/jobs/bulk", { action, job_ids: ids, args });
    const c = r.counts, total = r.results.length;
    const bad = r.results.filter((x) => x.result === "skipped" || x.result === "error");
    toast({ title: `${label.replace("…", "")}: ${c.done}/${total} job`, message: bad.length ? `${bad.length} job không áp dụng được: ${bad[0].reason}${bad.length > 1 ? " …" : ""}` : (c.unchanged ? `${c.unchanged} job không đổi.` : ""), tone: bad.length ? "wait" : "done" });
    if (bad.length) {
      await openDialog({ title: "Một số job không áp dụng được", describe: `${c.done} job thành công, ${bad.length} job được giữ nguyên.`,
        content: h("ul", { class: "autolist", "aria-label": "Job không áp dụng được" }, ...bad.slice(0, 50).map((x) => h("li", null, h("a", { href: `#/jobs/${x.job_id}`, class: "mono" }, `#${x.job_id}`), ` — ${x.reason}`))),
        actions: [{ label: "Đóng", kind: "primary", value: true }] });
    }
    return r;
  } catch (e) { toastError(e, "Chưa làm được"); return null; }
}

// Cập nhật pipeline cho nhiều job = đổi ĐÍCH (đúng thao tác của Sửa job). Mỗi job được backend kiểm riêng theo progress floor của nó.
export async function openBulkPipeline(ids) {
  return openTargetDialog({ title: `Cập nhật pipeline của ${ids.length} job`,
    intro: `Chọn bước mà ${ids.length} job đã chọn sẽ chạy đến rồi dừng. Mỗi job được kiểm riêng: bước job đã chạy tới không bị lùi lại, kết quả đã có luôn được giữ; job đã hoàn tất lưu pipeline mới và chờ bạn bấm “Chạy tiếp”.` });
}

// Đổi template cho nhiều job chưa kết thúc (job đang chạy/đã xong bị backend từ chối kèm lý do).
export async function openBulkTemplate(ids) {
  let opt;
  try { opt = await api.get("/api/templates/options"); } catch (e) { toastError(e, "Chưa tải được danh sách template"); return null; }
  const kindSel = select({ options: Object.entries(KIND_LABEL), value: "thumbnail" });
  const tplSel = select({ options: [] });
  const fill = () => {
    const key = { thumbnail: "thumbnail", youtube: "youtube_video", tiktok: "tiktok_video" }[kindSel.value];
    tplSel.replaceChildren(...(opt.options[key] || []).map((t) => h("option", { value: t.id }, `${t.name || t.id} (v${t.version})`)));
  };
  kindSel.addEventListener("change", fill);
  fill();
  const content = h("div", { class: "stack" }, h("p", { class: "muted small" }, `Áp dụng cho ${ids.length} job đã chọn. Chỉ job chưa kết thúc và không đang chạy được đổi; bước dựng liên quan sẽ chạy lại, các bước khác giữ nguyên.`),
    field({ label: "Loại template", control: kindSel }), field({ label: "Template", control: tplSel }));
  const r = await openDialog({ title: `Đổi template của ${ids.length} job`, content, actions: [{ label: "Hủy", value: null }, { label: "Áp dụng", kind: "primary", value: "ok" }] });
  return r === "ok" && tplSel.value ? { kind: kindSel.value, template_id: tplSel.value } : null;
}
