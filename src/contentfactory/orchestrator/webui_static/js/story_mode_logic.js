// Logic thuần của "Chế độ truyện" (Story hiện có | Story Remix): trạng thái form dựng từ schema do server trả về, kiểm tra khớp server, dựng payload.
// Server vẫn là nơi kiểm lại và quyết định cấu hình hiệu lực; ở đây chỉ để báo lỗi sớm và không gửi gì thừa.
export const LEGACY = "story_branch";
export const REMIX = "story_remix";
export const SCENE = "story_scene_remix";
export const SECTIONS = [["story", "Story Remix"], ["character_universe", "Kho nhân vật"]];
const SCENE_SECTIONS = [["story", "Remix bám sự việc"]];
export const isRemix = (mode) => mode === REMIX || mode === SCENE;
/** Các phần form của một mode (Remix bám sự việc chỉ có một phần, không Kho nhân vật). */
export const sectionsOf = (mode) => (mode === SCENE ? SCENE_SECTIONS : SECTIONS);
/** Schema các trường của một mode: mode mới dùng `by_mode`, client/server cũ không có thì rơi về schema Story Remix. */
export const schemaOf = (info, mode) => info.by_mode?.[mode]?.schema || info.schema;
export const defaultsOf = (info, mode) => info.by_mode?.[mode]?.defaults || info.defaults;
/** Giá trị đang sửa của một phần form: Remix bám sự việc giữ riêng ở `state.scene` để chuyển qua lại không mất cấu hình. */
export const bagOf = (state, mode, sec) => (mode === SCENE ? state.scene : state[sec]);
export const RIGHTS_OK = ["own", "licensed", "permitted"];

export const OPTION_LABELS = {
  ending: { auto: "Tự động", happy: "Có hậu", bittersweet: "Buồn ngọt", open: "Kết mở", tragic: "Bi kịch" },
  audio_readability: { standard: "Tiêu chuẩn", high: "Cao (câu ngắn, rõ người nói)" },
  source_rights: { unknown: "Không rõ", own: "Của tôi", licensed: "Có giấy phép", permitted: "Được phép sử dụng" },
  reuse_strategy: { reuse: "Ưu tiên dùng lại nhân vật có sẵn", create_new: "Ưu tiên tạo nhân vật mới" },
  canon_mode: { parallel: "Độc lập theo từng truyện" },
};

const clone = (v) => JSON.parse(JSON.stringify(v));

/** Trạng thái ban đầu của form: mode mặc định + giá trị mặc định hệ thống. */
export function initialState(info) {
  const sc = info.by_mode?.[SCENE]?.defaults?.story;
  return { mode: info.default_mode || LEGACY, story: clone(info.defaults.story), character_universe: clone(info.defaults.character_universe), ...(sc ? { scene: clone(sc) } : {}) };
}

export const listFromText = (t) => [...new Set(String(t ?? "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean))];
export const textFromList = (l) => (l || []).join("\n");

/** Thông báo lỗi (hoặc null) của MỘT trường — cùng luật với server (story/mode.py). */
export function validateField(f, v) {
  const r = f.rule || {};
  switch (f.type) {
    case "bool": return typeof v === "boolean" ? null : "Phải là bật/tắt.";
    case "int": return Number.isInteger(v) && v >= r.min && v <= r.max ? null : `Nhập số nguyên ${r.min}–${r.max}.`;
    case "number_or_null": return v === null || (typeof v === "number" && Number.isFinite(v) && v >= r.min && v <= r.max) ? null : `Để trống hoặc nhập số ${r.min}–${r.max}.`;
    case "select": return (r.options || []).includes(v) ? null : "Lựa chọn không hợp lệ.";
    case "text": return typeof v === "string" && v.trim().length <= r.max_len ? null : `Tối đa ${r.max_len} ký tự.`;
    case "list": return Array.isArray(v) && v.length <= r.max_items && v.every((x) => x.trim() && x.trim().length <= r.max_len) ? null : `Tối đa ${r.max_items} mục, mỗi mục ≤ ${r.max_len} ký tự.`;
    default: return null;
  }
}

/** {"story.tone": "..."}: mọi lỗi của form; rỗng = hợp lệ. Mode cũ không cần kiểm (cấu hình Remix bị bỏ qua). */
export function validateAll(info, state) {
  const errs = {};
  if (!isRemix(state.mode)) return errs;
  const schema = schemaOf(info, state.mode);
  for (const [sec] of sectionsOf(state.mode)) for (const f of schema[sec]) {
    const m = validateField(f, bagOf(state, state.mode, sec)[f.key]);
    if (m) errs[`${sec}.${f.key}`] = m;
  }
  if (state.mode === SCENE && !errs["story.source_rights"] && !RIGHTS_OK.includes(state.scene.source_rights)) errs["story.source_rights"] = "Chọn Của tôi / Có giấy phép / Được phép (Không rõ bị chặn).";
  if (state.mode === SCENE && !errs["story.rights_ack"] && state.scene.rights_ack !== true) errs["story.rights_ack"] = "Cần tích xác nhận bạn có quyền chuyển thể nguồn này.";
  return errs;
}

/** Payload tạo job: Story hiện có = không gửi gì (backend dùng mặc định, job y như trước). Remix = đủ cấu hình. */
export function createPayload(info, state) {
  if (state.mode === SCENE) return { mode: SCENE, story: clone(state.scene) };
  return state.mode === REMIX ? { mode: REMIX, story: clone(state.story), character_universe: clone(state.character_universe) } : null;
}

export const modeLabel = (info, id) => info.modes.find((m) => m.id === id)?.label || id;

/** Số mục khác mặc định (để tóm tắt "đã tuỳ chỉnh N mục"). */
export function changedCount(info, state) {
  let n = 0;
  const mode = state.mode === SCENE ? SCENE : REMIX;
  for (const [sec] of sectionsOf(mode)) for (const f of schemaOf(info, mode)[sec]) if (JSON.stringify(bagOf(state, mode, sec)[f.key]) !== JSON.stringify(f.default)) n++;
  return n;
}

// ---- mẫu cấu hình / ước tính / cấu hình hiệu lực -----------------------------------------------
export const SOURCE_LABEL = { system: "mặc định hệ thống", preset: "từ mẫu", job: "riêng của job" };
export const SYSTEM_PRESET = "";

/** Trạng thái form từ một mẫu (mode Remix + giá trị mẫu); phần thiếu lấy mặc định hệ thống. */
export function stateFromPreset(info, preset) {
  const st = initialState(info);
  if (!preset) return st;
  return { mode: REMIX, story: { ...st.story, ...clone(preset.story) }, character_universe: { ...st.character_universe, ...clone(preset.character_universe) } };
}

export const presetByName = (info, name) => (info.presets || []).find((p) => p.name === name) || null;

/** Khôi phục mặc định hệ thống nhưng GIỮ nguyên chế độ đang chọn. */
export function resetToDefaults(info, state) {
  const st = initialState(info);
  return { ...st, mode: state.mode };
}

export function formatValue(field, v) {
  if (field.type === "bool") return v ? "Bật" : "Tắt";
  if (field.type === "list") return v.length ? v.join(", ") : "—";
  if (v === null || v === "") return "—";
  if (field.type === "select") return OPTION_LABELS[field.key]?.[v] || String(v);
  return String(v);
}

/** Các dòng ước tính (đã nói rõ giả định) + cảnh báo khi trần USD ước tính vượt ngân sách. */
export function estimateLines(est, budget) {
  const unit = est.scenes ? `${est.scenes} cảnh` : `${est.chapters} chương`;
  const lines = [`Khoảng ${est.calls.min}–${est.calls.max} lượt gọi AI, ~${Math.round(est.input_tokens.min / 1000)}k–${Math.round(est.input_tokens.max / 1000)}k token vào, ~${Math.round(est.output_tokens.min / 1000)}k–${Math.round(est.output_tokens.max / 1000)}k token ra (${unit}).`];
  lines.push(est.usd ? `Chi phí ước tính: $${est.usd.min.toFixed(2)}–$${est.usd.max.toFixed(2)}.` : "Chi phí USD: không rõ (chưa cấu hình giá/triệu token).");
  if (budget != null && est.usd && est.usd.max > budget) lines.push(`⚠ Trần ước tính ($${est.usd.max.toFixed(2)}) cao hơn ngân sách ($${Number(budget).toFixed(2)}): job có thể dừng giữa chừng và tiếp tục được sau khi nâng ngân sách.`);
  if (budget != null && !est.usd) lines.push("Ngân sách chỉ chặn được theo chi phí mà nhà cung cấp báo trong lúc chạy.");
  return lines;
}

export function validPresetName(n) { return /^[\p{L}\p{N}_][\p{L}\p{N}_ \-]{0,39}$/u.test(String(n ?? "").trim()); }
