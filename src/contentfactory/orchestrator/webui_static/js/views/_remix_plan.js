// Thẻ "Kế hoạch Story Remix" trên Chi tiết job: ý tưởng đã chọn + lý do loại, dàn nhân vật, cổng originality / nhịp thưởng, chi phí, đại cương.
// Chỉ đọc (xem nếu muốn). Không bao giờ hiển thị "đã xác minh bản quyền": chỉ số đo, bằng chứng, độ không chắc chắn.
import { api } from "../api.js";
import { h, clear } from "../dom.js";
import { badge, disclosure, alertBox } from "../components.js";
import { decisionView, costLine, premiseRows, qualityIssues } from "../remix_logic.js";
import { roleLabel } from "../universe_logic.js";

export function remixPlanCard(jobId, d) {
  if (d.story_mode?.mode !== "story_remix") return null;
  const host = h("section", { class: "card stack", "aria-labelledby": "rp-h" }, h("h2", { id: "rp-h" }, "Kế hoạch Story Remix"), h("p", { class: "muted small" }, "Đang tải kế hoạch…"));
  load();
  async function load() {
    try {
      const p = await api.get(`/api/jobs/${jobId}/remix`);
      paint(p);
    } catch (e) { clear(host); host.append(h("h2", { id: "rp-h" }, "Kế hoạch Story Remix"), alertBox({ tone: "wait", title: "Chưa đọc được kế hoạch", body: e.message })); }
  }
  function paint(p) {
    clear(host);
    host.append(h("h2", { id: "rp-h" }, "Kế hoạch Story Remix"));
    if (!p.dna && !p.premises) { host.append(h("p", { class: "muted" }, "Kế hoạch sẽ hiện ở đây khi bước Truyện bắt đầu phân tích nguồn. Bạn không cần làm gì.")); return; }
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
    if (p.outline) host.append(disclosure({ label: `Đại cương (${p.outline.length} chương)`, content: h("ol", { class: "small rp-list" }, ...p.outline.map((c) => h("li", null, c.title, c.payoff ? h("span", { class: "chip" }, "★ " + c.payoff) : null))) }));
    if (p.cost) host.append(h("p", { class: "muted small" }, costLine(p.cost)));
  }
  return host;
}
