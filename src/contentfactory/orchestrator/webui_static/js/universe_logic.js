// Logic thuần của Kho nhân vật: chuẩn hóa form, so sánh thay đổi, kiểm tra (khớp server — server vẫn là nơi quyết định), nhãn.
export const STATUS_LABEL = { active: "Đang dùng", archived: "Lưu trữ", staged: "Chờ QA" };
export const ORIGIN_LABEL = { original_generated: "AI tạo", user_created: "Bạn tạo", approved_import: "Nhập từ Excel" };
export const ACTION_LABEL = { create: "Tạo mới", update: "Cập nhật", unchanged: "Không đổi", conflict: "Xung đột", error: "Lỗi" };
export const ACTION_TONE = { create: "done", update: "running", unchanged: "off", conflict: "wait", error: "fail" };

export const listFromText = (t) => [...new Set(String(t ?? "").split(/\r?\n/).map((x) => x.replace(/\s+/g, " ").trim()).filter(Boolean))];
export const textFromList = (l) => (l || []).join("\n");

/** Chữ cái đầu cho avatar giữ chỗ (không phụ thuộc sinh ảnh). */
export function initials(name) {
  const w = String(name ?? "").trim().split(/\s+/).filter(Boolean);
  if (!w.length) return "?";
  return (w.length === 1 ? w[0].slice(0, 2) : w[w.length - 2][0] + w[w.length - 1][0]).toUpperCase();
}

/** Giá trị form → hồ sơ (list từ textarea nhiều dòng). `values` là {key: string}. */
export function profileFromForm(fields, values) {
  const out = {};
  for (const f of fields) out[f.key] = f.type === "list" ? listFromText(values[f.key]) : String(values[f.key] ?? "").trim();
  return out;
}

export function formFromProfile(fields, ch) {
  const out = {};
  for (const f of fields) out[f.key] = f.type === "list" ? textFromList(ch[f.key]) : ch[f.key] ?? "";
  return out;
}

/** Các khóa đã đổi so với hồ sơ hiện tại. */
export function changedKeys(fields, ch, profile) {
  return fields.filter((f) => JSON.stringify(profile[f.key]) !== JSON.stringify(ch[f.key])).map((f) => f.key);
}

/** {key: thông báo}; rỗng = hợp lệ. Không cắt âm thầm. */
export function validate(fields, profile) {
  const errs = {};
  for (const f of fields) {
    const v = profile[f.key];
    if (f.type === "list") {
      const [mx, ln] = f.limit;
      if (v.length > mx) errs[f.key] = `Tối đa ${mx} mục.`;
      else if (v.some((x) => x.length > ln)) errs[f.key] = `Mỗi mục tối đa ${ln} ký tự.`;
    } else if (v.length > f.limit) errs[f.key] = `Dài ${v.length} ký tự, tối đa ${f.limit}.`;
  }
  if (!profile.display_name) errs.display_name = "Tên hiển thị là bắt buộc.";
  return errs;
}

/** Sửa được những khóa nào: archived = không sửa; locked = không sửa phần cốt lõi. */
export function editability(fields, ch) {
  const ro = new Set();
  if (ch.status === "archived") for (const f of fields) ro.add(f.key);
  else if (ch.locked) for (const f of fields) if (f.core) ro.add(f.key);
  return ro;
}

export function reuseSummary(s) {
  const total = s.reused_appearances + s.new_character_appearances;
  return total ? `${s.reused_appearances} dùng lại / ${s.new_character_appearances} nhân vật mới` : "Chưa có truyện nào";
}

export function importHeadline(rep) {
  const c = rep.counts;
  if (c.error) return { tone: "fail", text: `Có ${c.error} dòng lỗi — không thể nhập. Sửa file rồi thử lại.` };
  if (c.conflict) return { tone: "wait", text: `${c.conflict} dòng xung đột (đã đổi sau khi xuất). Có thể bỏ qua chúng và nhập phần còn lại.` };
  if (!c.create && !c.update) return { tone: "info", text: "Không có gì thay đổi để nhập." };
  return { tone: "done", text: `Sẽ tạo ${c.create} và cập nhật ${c.update} nhân vật.` };
}

export const canApply = (rep, skipConflicts) => !!rep && rep.counts.error === 0 && (rep.counts.create + rep.counts.update > 0) && (rep.counts.conflict === 0 || skipConflicts);

export function fmtTime(ts) {
  if (!ts) return "chưa có";
  const d = new Date(ts * 1000);
  return d.toLocaleString("vi-VN", { dateStyle: "short", timeStyle: "short" });
}
