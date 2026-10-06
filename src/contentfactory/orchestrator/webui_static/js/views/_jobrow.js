// Một dòng job (dùng ở Chạy và Job). Cập nhật tại chỗ (không dựng lại) để danh sách đang theo dõi không nhấp nháy.
import { h } from "../dom.js";
import { jobBadge, updateBadge, btn, progress, updateProgress } from "../components.js";
import { jobStatus, ACTION_LABEL } from "../status.js";
import { relTime, pct } from "../format.js";
import { openOutput, resumeJob, retryJob } from "../actions.js";

const TONE_OF_BAR = { running: "running", queued: "running", waiting: "wait", attention: "attn", failed: "fail", completed: "done", paused: "wait", cancelled: "wait" };

export function jobRow(j, onChanged) {
  const li = h("li", { class: "job" });
  li._parts = {
    link: h("a", { href: `#/jobs/${j.id}` }),
    meta: h("div", { class: "meta" }),
    stage: h("div", { class: "stage" }),
    bar: progress(0, "running", `Tiến độ job ${j.id}`),
    badge: jobBadge(j.status),
    action: h("div", { class: "row" }),
  };
  const p = li._parts;
  p.stageText = h("span", { class: "trunc" });
  p.stage.append(p.stageText, p.bar);
  li.append(h("div", { class: "grow" }, h("div", { class: "title trunc" }, p.link), p.meta), p.stage, h("div", { class: "row" }, p.badge, p.action));
  li._onChanged = onChanged;
  updateJobRow(li, j);
  return li;
}

export function updateJobRow(li, j) {
  const p = li._parts;
  const sig = [j.status, j.stage, j.progress, j.fraction, j.next_action, j.output_dir, j.title, j.updated_at, j.pausing].join("|");
  if (li._sig === sig) { return; }
  li._sig = sig;
  const first = li._first === undefined;
  li._first = false;
  if (p.link.textContent !== j.title) p.link.textContent = j.title;
  p.link.title = j.title;
  p.meta.replaceChildren(h("span", null, `Kênh ${j.channel}`), h("span", { class: "mono" }, `#${j.id}`), h("span", null, relTime(j.updated_at)));
  const st = jobStatus(j.status);
  updateBadge(p.badge, st);
  const text = j.status === "completed" ? "Xong tất cả các bước" : j.status === "cancelled" ? "Đã hủy" : j.status === "paused" ? (j.pausing ? "Đang tạm dừng…" : "Bạn đã tạm dừng") : j.hold?.title && j.status !== "running" ? j.hold.title : (j.progress ? j.progress : j.stage_label || "Đang xếp hàng");
  if (p.stageText.textContent !== text) p.stageText.textContent = text;
  updateProgress(p.bar, j.fraction, TONE_OF_BAR[j.status] || "running");
  p.action.replaceChildren(...actionsFor(j, li._onChanged));
  if (first) return;
}

export function actionsFor(j, changed) {
  if (j.status === "completed" && j.output_dir) {
    const b = btn({ label: "Mở output", icon: "folder-open", size: "sm", kind: "primary" });
    b.addEventListener("click", () => openOutput(j.id, b));
    return [b];
  }
  if (j.next_action === "retry") {
    const b = btn({ label: ACTION_LABEL.retry, icon: "refresh", size: "sm" });
    b.addEventListener("click", () => retryJob(j.id, b, { after: changed }));
    return [b];
  }
  if (j.next_action === "resume" || j.next_action === "resume_now") {
    const b = btn({ label: j.next_action === "resume" ? ACTION_LABEL.resume : ACTION_LABEL.resume_now, icon: "play", size: "sm" });
    b.addEventListener("click", () => resumeJob(j.id, b, { now: j.next_action === "resume_now", after: changed }));
    return [b];
  }
  return [];
}

export { pct };
