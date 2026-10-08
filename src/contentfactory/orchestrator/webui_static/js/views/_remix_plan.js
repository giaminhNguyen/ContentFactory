// Thẻ "Kế hoạch Story Remix" trên Chi tiết job: ý tưởng đã chọn + lý do loại, dàn nhân vật, cổng originality / nhịp thưởng, tiến độ viết chương, QA cuối, chi phí,
// và (khi job dừng) hành động tiếp tục an toàn. Không bao giờ hiển thị "đã xác minh bản quyền": chỉ số đo, bằng chứng, độ không chắc chắn.
import { api } from "../api.js";
import { h, clear } from "../dom.js";
import { badge, btn, busy, disclosure, alertBox, field, input, toast, toastError } from "../components.js";
import { decisionView, costLine, universeLine, premiseRows, qualityIssues, stopActions, writeProgress, budgetValid } from "../remix_logic.js";
import { roleLabel } from "../universe_logic.js";

export function remixPlanCard(jobId, d, { after } = {}) {
  if (d.story_mode?.mode !== "story_remix") return null;
  const host = h("section", { class: "card stack", "aria-labelledby": "rp-h" }, h("h2", { id: "rp-h" }, "Kế hoạch Story Remix"), h("p", { class: "muted small" }, "Đang tải kế hoạch…"));
  let timer = null;
  const live = d.status === "running" || d.status === "queued";
  load();
  async function load() {
    try { paint(await api.get(`/api/jobs/${jobId}/remix`)); }
    catch (e) { clear(host); host.append(h("h2", { id: "rp-h" }, "Kế hoạch Story Remix"), alertBox({ tone: "wait", title: "Chưa đọc được kế hoạch", body: e.message })); }
    if (live && host.isConnected !== false) { clearTimeout(timer); timer = setTimeout(() => { if (host.isConnected) load(); }, 4000); }
  }

  async function resume(patch) {
    try {
      await api.put(`/api/jobs/${jobId}/story-mode`, { story: patch, retry: true });
      toast({ title: "Đã tiếp tục", message: "Các bước đã xong được giữ nguyên.", tone: "done" });
      after?.();
      load();
    } catch (e) { toastError(e, "Chưa tiếp tục được"); }
  }

  async function publishNow() {
    try { const r = await api.post(`/api/jobs/${jobId}/universe/publish`, {}); toast({ title: "Đã cập nhật Kho nhân vật", message: `Trạng thái: ${r.universe_publish.status}`, tone: "done" }); load(); }
    catch (e) { toastError(e, "Chưa cập nhật được"); }
  }

  function stopBox(p) {
    const acts = stopActions(p.stop, p.mode?.story || d.story_mode.story || {});
    const buttons = [];
    for (const a of acts) {
      if (a.needs === "budget_usd") {
        const inp = input({ type: "number", min: 1, max: 5000, step: "any", placeholder: "USD", "aria-label": "Ngân sách mới (USD)", value: p.cost?.budget_usd ? Math.ceil(p.cost.budget_usd * 2) : "" });
        const b = btn({ label: a.label, kind: "primary", onClick: (e) => { if (!budgetValid(inp.value)) { inp.focus(); toast({ title: "Nhập ngân sách 1–5000 USD", tone: "wait" }); return; } busy(e.currentTarget, () => resume({ budget_usd: Number(inp.value) })); } });
        buttons.push(h("div", { class: "row" }, field({ label: "Ngân sách mới (USD)", control: inp }), b));
      } else buttons.push(btn({ label: a.label, kind: "primary", onClick: (e) => busy(e.currentTarget, () => resume(a.patch)) }));
    }
    return alertBox({ tone: "wait", title: p.stop.message, body: p.stop.hint || null, actions: buttons });
  }

  function paint(p) {
    clear(host);
    host.append(h("h2", { id: "rp-h" }, "Kế hoạch Story Remix"));
    if (p.stop) host.append(stopBox(p));
    if (!p.dna && !p.premises) { if (!p.stop) host.append(h("p", { class: "muted" }, "Kế hoạch sẽ hiện ở đây khi bước Truyện bắt đầu phân tích nguồn. Bạn không cần làm gì.")); return; }
    const wp = writeProgress(p);
    if (p.outline && (wp.done || p.writer)) {
      host.append(h("div", { class: "stack", "aria-live": "polite" }, h("div", { class: "row" }, h("progress", { max: wp.total, value: wp.done, "aria-label": `Đã viết ${wp.done}/${wp.total} chương` }), h("span", { class: "small" }, `${wp.done}/${wp.total} chương` + (wp.repaired ? ` · ${wp.repaired} chương đã sửa` : "") + (wp.warned ? ` · ${wp.warned} chương còn cảnh báo` : "")))));
    }
    if (p.final_qa) {
      host.append(disclosure({ label: `QA cuối truyện: ${p.final_qa.accepted ? "đạt" : "chưa đạt"}`, open: !p.final_qa.accepted, content: h("div", { class: "stack small" },
        badge({ tone: p.final_qa.accepted ? "done" : "fail", icon: p.final_qa.accepted ? "check-circle" : "alert", label: p.final_qa.accepted ? "Đạt — đủ điều kiện cập nhật Kho nhân vật" : "Chưa đạt — Kho nhân vật không bị thay đổi" }),
        ...(p.final_qa.problems.length ? [h("ul", null, ...p.final_qa.problems.map((x) => h("li", null, x.message)))] : []),
        p.final_qa.universe_publish ? h("p", { class: "muted" }, universeLine(p.final_qa.universe_publish)) : null,
        p.final_qa.accepted && ["skipped", "failed"].includes(p.final_qa.universe_publish?.status) ? btn({ label: "Cập nhật Kho nhân vật", icon: "database", onClick: (e) => busy(e.currentTarget, publishNow) }) : null) }));
    }
    if (p.dna) host.append(disclosure({ label: "DNA của nguồn (trừu tượng — không có tên/tình tiết riêng)", content: h("div", { class: "stack small" },
      h("p", null, h("strong", null, "Thể loại: "), p.dna.genre), h("p", null, h("strong", null, "Lời hứa cảm xúc: "), p.dna.emotional_promise), h("p", null, h("strong", null, "Cơ chế thưởng: "), p.dna.reward_types.join("; ")),
      h("p", null, h("strong", null, "Nhịp thưởng: "), `điểm đầu trước ${p.dna.payoff_cadence.first_payoff_by_pct}%, ~${p.dna.payoff_cadence.payoffs_per_10pct} điểm/10%`)) }));
    if (p.premises) {
      const rows = premiseRows(p.premises);
      host.append(disclosure({ label: `Ý tưởng: ${rows.filter((r) => r.selected).length ? "đã chọn 1 trong " + rows.length : "chưa có ý tưởng đạt ngưỡng"}`, open: true, content: h("ul", { class: "stack small rp-list" }, ...rows.map((r) => h("li", null,
        badge({ tone: r.selected ? "done" : "off", icon: r.selected ? "check-circle" : "x", label: r.selected ? "Được chọn" : "Bị loại" }), " ", h("strong", null, r.id), ` · điểm ${r.score}`, h("div", null, r.logline), r.reason ? h("div", { class: "muted" }, r.reason) : null))) }));
    }
    if (p.cast) {
      host.append(disclosure({ label: `Dàn nhân vật (${p.cast.members.length}) — đã chốt trước khi viết`, content: h("div", { class: "stack small" }, ...p.cast.members.map((m) => h("div", null,
        h("strong", null, m.display_name), ` · ${roleLabel(m.role_code)} · `, badge({ tone: m.origin === "created" ? "wait" : "done", icon: m.origin === "created" ? "plus" : "refresh", label: m.origin === "created" ? "Mới (chờ QA)" : "Dùng lại" }), h("div", { class: "muted" }, m.rationale))),
        h("a", { href: `#/universe` }, "Mở Kho nhân vật")) }));
    }
    if (p.originality) {
      const v = decisionView(p.originality.decision);
      host.append(disclosure({ label: "Kiểm tra độ giống nguồn", open: v.tone !== "done", content: h("div", { class: "stack small" }, badge({ tone: v.tone, icon: v.icon, label: v.label }), h("p", null, p.originality.next_step),
        ...(p.originality.evidence.length ? [h("ul", null, ...p.originality.evidence.map((e) => h("li", null, `[${e.severity}] ${e.evidence}`)))] : []),
        h("details", null, h("summary", null, "Độ không chắc chắn"), h("ul", null, ...p.originality.uncertainty.map((u) => h("li", null, u)))), h("p", { class: "muted" }, p.originality.legal_note)) }));
    }
    if (p.quality) {
      const issues = qualityIssues(p.quality);
      host.append(disclosure({ label: "Kiểm tra nhịp thưởng cảm xúc", open: !!issues.length, content: h("div", { class: "stack small" }, badge({ tone: p.quality.decision === "pass" ? "done" : p.quality.decision === "skipped" ? "off" : "fail", icon: p.quality.decision === "pass" ? "check-circle" : "alert", label: p.quality.decision === "pass" ? "Đạt" : p.quality.decision === "skipped" ? "Đã tắt" : "Chưa đạt" }),
        p.quality.repairs ? h("p", null, `Đã sửa đại cương ${p.quality.repairs} lượt.`) : null, ...(issues.length ? [h("ul", null, ...issues.map((i) => h("li", null, i)))] : [])) }));
    }
    if (p.writer && p.writer.some((c) => c.issues.length || c.repairs)) {
      host.append(disclosure({ label: "Chương có cảnh báo/đã sửa", content: h("ul", { class: "small rp-list" }, ...p.writer.filter((c) => c.issues.length || c.repairs).map((c) => h("li", null, h("strong", null, `Chương ${c.n}`), c.repairs ? ` · đã sửa ${c.repairs} lượt` : "", ...c.issues.map((i) => h("div", { class: "muted" }, i))))) }));
    }
    if (p.outline) host.append(disclosure({ label: `Đại cương (${p.outline.length} chương)`, content: h("ol", { class: "small rp-list" }, ...p.outline.map((c) => h("li", null, c.title, c.payoff ? h("span", { class: "chip" }, "★ " + c.payoff) : null))) }));
    if (p.cost) host.append(h("p", { class: "muted small" }, costLine(p.cost)));
  }
  host.addEventListener("remove", () => clearTimeout(timer));
  return host;
}
