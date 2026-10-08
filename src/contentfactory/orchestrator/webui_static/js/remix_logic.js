// Logic thuần của thẻ Kế hoạch Story Remix: nhãn quyết định cổng, dòng chi phí trung thực (thiếu dữ liệu = "không rõ"), danh sách ý tưởng.
const DECISION = {
  pass: { tone: "done", icon: "check-circle", label: "Không thấy điểm giống đáng kể" },
  pass_with_note: { tone: "done", icon: "info", label: "Có điểm giống ở mức trung bình — bạn đã khai báo quyền sử dụng" },
  review: { tone: "wait", icon: "alert", label: "Cần bạn xem lại trước khi viết dài" },
  block: { tone: "fail", icon: "x-circle", label: "Quá giống nguồn — đã dừng" },
};

export const decisionView = (d) => DECISION[d] || { tone: "off", icon: "info", label: String(d) };

/** Chi phí: chỉ nêu số nhà cung cấp báo; thiếu ⇒ nói rõ "không rõ" (không ước đoán). */
export function costLine(c) {
  const known = `$${Number(c.known_cost_usd || 0).toFixed(2)}`;
  const unk = c.calls_with_unknown_cost ? ` · ${c.calls_with_unknown_cost}/${c.calls} lượt không rõ chi phí` : "";
  const budget = c.budget_usd ? ` · ngân sách $${Number(c.budget_usd).toFixed(2)}` : "";
  return `Chi phí đã biết của kế hoạch: ${known}${unk}${budget}${c.retries ? ` · ${c.retries} lần thử lại` : ""}.`;
}

export function premiseRows(list) {
  return [...list].sort((a, b) => Number(b.selected) - Number(a.selected) || (b.total ?? 0) - (a.total ?? 0))
    .map((p) => ({ id: p.id, logline: p.logline, selected: !!p.selected, score: p.total == null ? "—" : p.total.toFixed(2), reason: p.reason || "" }));
}

export const qualityIssues = (q) => (q.issues || []).map((i) => i.message);
