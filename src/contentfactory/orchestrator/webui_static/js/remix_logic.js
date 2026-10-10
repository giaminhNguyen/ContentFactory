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

/** Hành động tiếp tục (an toàn, KHÔNG đổi nội dung) tương ứng lý do job dừng; backend vẫn kiểm lại. `story` = các khóa gửi cho PUT /story-mode. */
export function stopActions(stop, story = {}) {
  if (!stop) return [];
  if (stop.code === "ORIGINALITY_REVIEW_REQUIRED") return [{ id: "accept", label: "Tôi đã xem báo cáo — tiếp tục", patch: { review_accepted: true } }];
  if (stop.code === "BUDGET_EXCEEDED") return [{ id: "budget", label: "Nâng ngân sách và tiếp tục", needs: "budget_usd" }];
  if (stop.code === "SCENE_CONTINUITY_REVIEW") {
    const cur = Number.isInteger(story.quality_repair_max_passes) ? story.quality_repair_max_passes : 1;
    return [{ id: "accept", label: "Tôi đã xem báo cáo liên tục — tiếp tục", patch: { review_accepted: true } },
            ...(cur >= 2 ? [] : [{ id: "repair", label: `Cho thêm 1 lượt sửa mối nối (hiện ${cur}) và tiếp tục`, patch: { quality_repair_max_passes: cur + 1 } }])];
  }
  if (["CHAPTER_QA_FAILED", "OUTLINE_GATE_FAILED"].includes(stop.code)) {
    const cur = Number.isInteger(story.quality_repair_max_passes) ? story.quality_repair_max_passes : 1;
    return cur >= 3 ? [] : [{ id: "repair", label: `Cho thêm 1 lượt sửa (hiện ${cur}) và tiếp tục`, patch: { quality_repair_max_passes: cur + 1 } }];
  }
  return [];
}

/** Tóm tắt viết chương: {done, total, repaired, warned}. */
export function writeProgress(plan) {
  const total = plan.outline ? plan.outline.length : 0;
  const w = plan.writer || [];
  return { done: plan.chapters_done || 0, total, repaired: w.filter((c) => c.repairs).length, warned: w.filter((c) => c.issues.length).length };
}

export const budgetValid = (v) => v !== "" && Number.isFinite(Number(v)) && Number(v) >= 1 && Number(v) <= 5000;

const PUBLISH_LABEL = { applied: "đã cập nhật (nhân vật mới + lịch sử xuất hiện)", noop: "đã được cập nhật trước đó", already_published: "đã được cập nhật trước đó", skipped: "chưa cập nhật", failed: "cập nhật lỗi — có thể thử lại", not_accepted: "không cập nhật vì truyện chưa đạt QA", reverted_earlier: "bản cập nhật này đã bị hoàn tác" };

export function universeLine(p) {
  return `Kho nhân vật: ${PUBLISH_LABEL[p.status] || p.status}${p.reason ? " — " + p.reason : ""}${p.error ? " — " + p.error : ""}.`;
}
