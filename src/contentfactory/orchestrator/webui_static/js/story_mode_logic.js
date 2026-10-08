// Logic thuần của "Chế độ truyện" (Story hiện có | Story Remix): trạng thái form dựng từ schema do server trả về, kiểm tra khớp server, dựng payload.
// Server vẫn là nơi kiểm lại và quyết định cấu hình hiệu lực; ở đây chỉ để báo lỗi sớm và không gửi gì thừa.
export const LEGACY = "story_branch";
export const REMIX = "story_remix";
export const SECTIONS = [["story", "Story Remix"], ["character_universe", "Kho nhân vật"]];

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
  return { mode: info.default_mode || LEGACY, story: clone(info.defaults.story), character_universe: clone(info.defaults.character_universe) };
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
  if (state.mode !== REMIX) return errs;
  for (const [sec] of SECTIONS) for (const f of info.schema[sec]) {
    const m = validateField(f, state[sec][f.key]);
    if (m) errs[`${sec}.${f.key}`] = m;
  }
  return errs;
}

/** Payload tạo job: Story hiện có = không gửi gì (backend dùng mặc định, job y như trước). Remix = đủ cấu hình. */
export function createPayload(info, state) {
  return state.mode === REMIX ? { mode: REMIX, story: clone(state.story), character_universe: clone(state.character_universe) } : null;
}

export const modeLabel = (info, id) => info.modes.find((m) => m.id === id)?.label || id;

/** Số mục khác mặc định (để tóm tắt "đã tuỳ chỉnh N mục"). */
export function changedCount(info, state) {
  let n = 0;
  for (const [sec] of SECTIONS) for (const f of info.schema[sec]) if (JSON.stringify(state[sec][f.key]) !== JSON.stringify(f.default)) n++;
  return n;
}
