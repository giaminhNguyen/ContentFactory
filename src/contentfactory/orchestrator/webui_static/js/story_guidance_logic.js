// Logic thuần của "Đề xuất truyện" (D-112): kiểm độ dài, dựng payload, nhãn nguồn. Backend vẫn là nơi kiểm lại và quyết định đề xuất hiệu lực.
export const MAX_LEN = 8000;
export const MODES = ["inherit", "custom", "none"];

const norm = (t) => String(t ?? "").replace(/\r\n?/g, "\n").trim();

/** Thông báo lỗi (hoặc null). Không bao giờ cắt âm thầm: quá dài thì báo để người dùng tự rút gọn. */
export function validateGuidance(mode, text, max = MAX_LEN) {
  if (mode !== "custom") return null;
  const t = norm(text);
  if (!t) return "Nhập nội dung, hoặc chọn “Dùng đề xuất trong Cài đặt”.";
  if (t.length > max) return `Dài ${t.length} ký tự, tối đa ${max}. Hãy rút gọn.`;
  return null;
}

/** Đề xuất mặc định trong Cài đặt: rỗng được phép, nhưng không được quá dài. */
export function validateDefault(text, max = MAX_LEN) {
  const t = norm(text);
  return t.length > max ? `Dài ${t.length} ký tự, tối đa ${max}. Hãy rút gọn.` : null;
}

/** Payload tạo job: chỉ gửi khi dùng đề xuất riêng (inherit = không gửi gì → backend dùng Cài đặt). */
export function createPayload(mode, text) {
  return mode === "custom" ? { mode: "custom", text: norm(text) } : null;
}

/** Payload sửa đề xuất của job (gửi đủ mode; text chỉ có nghĩa khi custom). */
export function savePayload(mode, text) {
  return { mode, text: mode === "custom" ? norm(text) : "" };
}

export const SOURCE_LABEL = { settings: "từ Cài đặt", job: "riêng của job", none: "không dùng đề xuất" };

export function currentLabel(source) {
  return source === "job" ? "Đang dùng đề xuất riêng của job" : source === "settings" ? "Đang dùng đề xuất từ Cài đặt" : "Không dùng đề xuất";
}

export function counter(text, max = MAX_LEN) {
  const n = norm(text).length;
  return { n, over: n > max, label: `${n}/${max}` };
}

/** Chạy Truyện lúc này thì đề xuất nào hiệu lực (khớp backend: custom > Cài đặt > không có) — chỉ để xem trước; backend mới là nguồn sự thật. */
export function previewDefault(defaultText) {
  const t = norm(defaultText);
  return t ? { text: t, empty: false } : { text: "", empty: true };
}
