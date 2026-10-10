// Thẻ "Remix bám sự việc" trên Chi tiết job: bản đồ cảnh, kế hoạch thay đổi (cấp 1/2), cảnh đã viết lại, QA liên tục, chi phí, và hành động tiếp tục an toàn khi job dừng.
// Không bao giờ hiển thị "đã xác minh bản quyền": quyền nguồn do chủ job khai báo, hệ thống chỉ chặn khi thiếu.
import { api } from "../api.js";
import { h, clear } from "../dom.js";
import { badge, btn, busy, disclosure, alertBox, field, input, toast, toastError } from "../components.js";
import { costLine, stopActions, budgetValid } from "../remix_logic.js";

export function sceneRemixCard(jobId, d, { after } = {}) {
  const host = h("section", { class: "card stack", "aria-labelledby": "sr-h" }, h("h2", { id: "sr-h" }, "Remix bám sự việc"), h("p", { class: "muted small" }, "Đang tải…"));
  let timer = null;
  const live = d.status === "running" || d.status === "queued";
  load();
  async function load() {
    try { paint(await api.get(`/api/jobs/${jobId}/remix`)); }
    catch (e) { clear(host); host.append(h("h2", { id: "sr-h" }, "Remix bám sự việc"), alertBox({ tone: "wait", title: "Chưa đọc được kế hoạch", body: e.message })); }
    if (live && host.isConnected !== false) { clearTimeout(timer); timer = setTimeout(() => { if (host.isConnected) load(); }, 4000); }
  }

  async function resume(patch) {
    try {
      await api.put(`/api/jobs/${jobId}/story-mode`, { story: patch, retry: true });
      toast({ title: "Đã tiếp tục", message: "Các cảnh đã xong được giữ nguyên.", tone: "done" });
      after?.();
      load();
    } catch (e) { toastError(e, "Chưa tiếp tục được"); }
  }

  function stopBox(p) {
    const buttons = [];
    for (const a of stopActions(p.stop, p.mode?.story || {})) {
      if (a.needs === "budget_usd") {
        const inp = input({ type: "number", min: 1, max: 5000, step: "any", placeholder: "USD", "aria-label": "Ngân sách mới (USD)", value: p.cost?.budget_usd ? Math.ceil(p.cost.budget_usd * 2) : "" });
        const b = btn({ label: a.label, kind: "primary", onClick: (e) => { if (!budgetValid(inp.value)) { inp.focus(); toast({ title: "Nhập ngân sách 1–5000 USD", tone: "wait" }); return; } busy(e.currentTarget, () => resume({ budget_usd: Number(inp.value) })); } });
        buttons.push(h("div", { class: "row" }, field({ label: "Ngân sách mới (USD)", control: inp }), b));
      } else buttons.push(btn({ label: a.label, kind: "primary", onClick: (e) => busy(e.currentTarget, () => resume(a.patch)) }));
    }
    return alertBox({ tone: "wait", title: p.stop.message, body: [p.stop.hint, p.stop.reason && `Lý do: ${p.stop.reason}`].filter(Boolean).join(" ") || null, actions: buttons });
  }

  function paint(p) {
    clear(host);
    host.append(h("h2", { id: "sr-h" }, "Remix bám sự việc"));
    if (p.stop) host.append(stopBox(p));
    if (!p.map) { if (!p.stop) host.append(h("p", { class: "muted" }, "Bản đồ cảnh và kế hoạch sẽ hiện ở đây khi bước Truyện bắt đầu. Bạn không cần làm gì.")); return; }
    host.append(h("p", { class: "small" }, `${p.map.scene_count} cảnh · ${p.map.pov} (ước lượng) · hook ở ${p.map.hook_scene}` + (p.affected ? ` · ${p.affected.length}/${p.map.scene_count} cảnh cần viết lại (${p.scenes_done} xong)` : "")));
    if (p.plan) {
      const rows = p.plan.changes.map((c) => h("tr", null, h("td", null, c.id), h("td", null, `“${c.old}” → “${c.new}”`), h("td", null, badge({ tone: c.level === 1 ? "done" : "wait", label: `Cấp ${c.level}` })), h("td", null, c.scene_ids.join(", "))));
      host.append(disclosure({ label: `Kế hoạch remix: ${p.plan.changes.length} thay đổi (cấp 1 là mặc định, cấp 3 không tự chạy)`, open: !!p.stop, content: h("div", { class: "stack small" },
        h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", null, h("tr", null, ...["Mã", "Thay đổi", "Mức", "Cảnh"].map((t) => h("th", null, t)))), h("tbody", null, ...rows))),
        ...p.plan.warnings.map((w) => alertBox({ tone: "info", title: w })), p.plan.global_rules.length ? h("ul", null, ...p.plan.global_rules.map((r) => h("li", null, r))) : null) }));
    }
    if (p.qa) {
      host.append(disclosure({ label: `QA liên tục: ${p.qa.issues.length ? `${p.qa.issues.length} lỗi chưa sửa` : "đạt"} · ${p.qa.repair_passes}/${p.qa.passes_max} lượt sửa`, open: p.qa.issues.length > 0, content: h("div", { class: "stack small" },
        badge({ tone: p.qa.issues.length ? "fail" : "done", icon: p.qa.issues.length ? "alert" : "check-circle", label: p.qa.issues.length ? "Cần xem" : "Đạt" }),
        ...(p.qa.issues.length ? [h("ul", null, ...p.qa.issues.map((x) => h("li", null, `${x.scene_id}: ${x.problem}`)))] : [])) }));
    }
    if (p.cost) host.append(h("p", { class: "small muted" }, costLine(p.cost)));
    if (p.estimate && !p.cost) host.append(h("p", { class: "small muted" }, `Ước tính: ${p.estimate.calls.min}–${p.estimate.calls.max} lượt gọi AI.`));
  }
  return host;
}
