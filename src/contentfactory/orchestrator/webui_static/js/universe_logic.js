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

// ---- dàn nhân vật theo truyện -------------------------------------------------------------------
export const ROLE_LABEL = { protagonist: "Nhân vật chính", deuteragonist: "Chính thứ hai", antagonist: "Phản diện", rival: "Đối thủ", foil: "Tương phản", mentor: "Người dẫn đường",
  ally: "Đồng minh", love_interest: "Người được yêu", confidant: "Người tâm giao", comic_relief: "Gây cười", catalyst: "Chất xúc tác", gatekeeper: "Người gác cổng", wildcard: "Ẩn số" };
export const STORY_STATE = { staged: "Chờ truyện đạt QA", published: "Đã vào kho" };
export const ISSUE_LABEL = { NO_CONTRAST: "Chính và phản diện quá giống nhau", SAME_VOICE: "Hai nhân vật nói chuyện giống nhau", SAME_NAME: "Trùng tên", CONTRADICTORY_RELATION: "Quan hệ mâu thuẫn" };

export const roleLabel = (r) => ROLE_LABEL[r] || r;
export const fitText = (m) => (m.fit == null ? "Nhân vật mới" : `Hợp ${Math.round(m.fit * 100)}%`);

/** Bố cục tròn cho đồ thị quan hệ: node theo vòng tròn; cạnh tới node không có trong dàn bị bỏ (không để mồ côi). */
export function graphLayout(members, rels, size = 320) {
  const n = members.length, r = size / 2 - 52, c = size / 2;
  const pos = new Map(members.map((m, i) => {
    const a = n === 1 ? 0 : (2 * Math.PI * i) / n - Math.PI / 2;
    return [m.character_id, { id: m.character_id, x: Math.round(n === 1 ? c : c + r * Math.cos(a)), y: Math.round(n === 1 ? c : c + r * Math.sin(a)) }];
  }));
  const edges = rels.filter((e) => pos.has(e.a_id) && pos.has(e.b_id)).map((e) => ({ ...e, from: pos.get(e.a_id), to: pos.get(e.b_id) }));
  return { nodes: [...pos.values()], edges, size };
}

export const relText = (rel, nameOf) => `${nameOf(rel.a_id)} — ${rel.type.replaceAll("_", " ")} — ${nameOf(rel.b_id)}`;

// ---- nhật ký cập nhật kho (publish) -------------------------------------------------------------
export const CHANGE_STATUS = { applied: "Đã áp dụng", reverted: "Đã hoàn tác" };

/** Tóm tắt một bản cập nhật bằng ngôn ngữ thường: ai được thêm/dùng lại/gộp. */
export function changeSummary(c) {
  const parts = [];
  parts.push(c.created.length ? `thêm ${c.created.length} nhân vật mới (${c.created.map((x) => x.display_name).join(", ")})` : "không thêm nhân vật mới");
  if (c.reused) parts.push(`dùng lại ${c.reused}`);
  if (c.merged) parts.push(`gộp ${c.merged} nhân vật trùng vào nhân vật có sẵn`);
  parts.push(`${c.appearances} lần xuất hiện`, `${c.relationships} quan hệ`);
  return parts.join(" · ");
}

export const canRevert = (c) => c.status === "applied";
