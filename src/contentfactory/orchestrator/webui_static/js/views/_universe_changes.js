// Tab "Nhật ký cập nhật" của Kho nhân vật: mỗi truyện đạt QA tạo một bản cập nhật (nhân vật mới + lịch sử xuất hiện + quan hệ). Xem được, và hoàn tác được khi an toàn.
import { api } from "../api.js";
import { h, clear } from "../dom.js";
import { btn, busy, badge, emptyState, errorState, skeleton, toast, toastError, confirmDialog } from "../components.js";
import * as L from "../universe_logic.js";

export function mountChanges(host, { onChanged }) {
  let alive = true;
  const box = h("div", { class: "stack" });
  host.append(box);
  async function load() {
    clear(box);
    box.append(skeleton(3));
    try {
      const { changes } = await api.get("/api/universe/changes");
      if (!alive) return;
      clear(box);
      if (!changes.length) { box.append(emptyState({ icon: "layers", title: "Chưa có bản cập nhật nào", text: "Mỗi khi một truyện Story Remix đạt QA, Kho nhân vật được cập nhật tự động và bản cập nhật hiện ở đây. Bạn có thể hoàn tác nếu muốn." })); return; }
      box.append(h("ul", { class: "stack uv-changes", "aria-label": "Các bản cập nhật kho" }, ...changes.map(card)));
    } catch (e) { if (alive) { clear(box); box.append(errorState(e, load)); } }
  }
  function card(c) {
    const undo = L.canRevert(c) ? btn({ label: "Hoàn tác", icon: "undo", size: "sm", onClick: (e) => revert(c, e.currentTarget) }) : null;
    return h("li", { class: "uv-member" }, h("div", { class: "row spread" }, h("strong", null, c.story_id),
      badge({ tone: c.status === "applied" ? "done" : "off", icon: c.status === "applied" ? "check-circle" : "undo", label: L.CHANGE_STATUS[c.status] || c.status })),
      h("p", { class: "small" }, L.changeSummary(c)), h("p", { class: "muted small" }, `${L.fmtTime(c.created_at)}${c.reverted_at ? " · hoàn tác " + L.fmtTime(c.reverted_at) : ""} · ${c.publish_id}${c.job_id ? " · job " + c.job_id : ""}`), undo);
  }
  async function revert(c, button) {
    const ok = await confirmDialog({ title: `Hoàn tác cập nhật của “${c.story_id}”?`, body: "Nhân vật mới do truyện này tạo sẽ bị gỡ khỏi kho và lịch sử xuất hiện của truyện bị xoá. Chỉ làm được khi không có truyện khác đang dùng và nhân vật chưa bị sửa; nếu không, hệ thống sẽ báo rõ lý do và không đổi gì.", confirmLabel: "Hoàn tác", danger: true });
    if (!ok) return;
    await busy(button, async () => {
      try { await api.post(`/api/universe/changes/${c.publish_id}/revert`, {}); toast({ title: "Đã hoàn tác", tone: "done" }); onChanged?.(); load(); }
      catch (e) { toastError(e, "Chưa hoàn tác được"); }
    });
  }
  load();
  return { destroy() { alive = false; }, reload: load };
}
