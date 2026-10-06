// Hành động hàng loạt trên các JOB ĐÃ CHỌN (Phase 9). Backend kiểm TỪNG job và trả kết quả từng job; giao diện chỉ gửi lựa chọn và báo
// thành công một phần RÕ RÀNG (không job nào bị bỏ lặng lẽ). Không có logic nghiệp vụ ở đây.
import { api } from "../api.js";
import { h, patchList } from "../dom.js";
import { icon } from "../icons.js";
import { btn, busy, field, select, alertBox, openDialog, toast, toastError } from "../components.js";

export const BULK = {
  pause: ["Tạm dừng", "pause"], resume: ["Tiếp tục", "play"], retry: ["Chạy lại", "refresh"],
  update_pipeline: ["Cập nhật pipeline…", "layers"], template: ["Đổi template…", "layout"], cancel: ["Hủy…", "x"],
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

// Cập nhật pipeline cho nhiều job: chọn bước, backend suy dependency (bước bắt buộc bị khoá kèm lý do); từng job được kiểm riêng khi áp dụng.
export async function openBulkPipeline(ids) {
  const sel = new Set(["render_youtube", "render_tiktok", "output"]);
  const rows = h("div", { class: "stage-pick", role: "group", "aria-label": "Các bước muốn có kết quả" });
  const problems = h("div", { class: "stack", "aria-live": "polite" });
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
    try { r = await api.post("/api/pipeline/plan", { pipeline_spec: { version: 2, requested_stages: [...sel] }, input_kind: "youtube_url" }); } catch { return; }
    if (my !== seq) return;
    problems.replaceChildren(...(r.ok ? [] : [alertBox({ tone: "wait", title: r.errors[0] })]));
    patchList(rows, r.stages, (s) => s.id, (s) => {
      const cb = h("input", { type: "checkbox", id: `bk-${s.id}` });
      cb.addEventListener("change", () => { if (cb.checked) sel.add(s.id); else sel.delete(s.id); refresh(); });
      const el = h("label", { class: "pick-row", for: cb.id }, cb, h("span", { class: "s-label" }, s.label), h("span", { class: "s-tag" }), h("span", { class: "s-why" }));
      el._cb = cb;
      apply(el, s);
      return el;
    }, apply);
  }
  refresh();
  const content = h("div", { class: "stack" }, h("p", { class: "muted small" }, `Áp dụng cho ${ids.length} job đã chọn. Mỗi job được kiểm riêng bằng impact planner của nó: job đã hoàn tất/hủy không bị đổi tại chỗ (dùng “Chạy lại với thay đổi”), phần đã xong và còn hợp lệ được giữ nguyên.`), rows, problems);
  const r = await openDialog({ title: `Cập nhật pipeline của ${ids.length} job`, wide: true, content, actions: [{ label: "Hủy", value: null }, { label: "Áp dụng", kind: "primary", value: "ok" }] });
  return r === "ok" ? { pipeline: { requested_stages: [...sel] } } : null;
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
