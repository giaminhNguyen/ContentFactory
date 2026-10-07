// Logic thuần của Watermark Library (không DOM, test bằng node). Backend là authority (kiểm tra, quy tắc xóa/lưu trữ, fingerprint); ở đây chỉ chọn nhãn, gợi ý
// hậu quả trước khi bấm và đoán loại thao tác để đặt tên nút đúng nghĩa.

export const SOURCE_LABEL = { tts: "Giọng đọc (TTS)", upload: "Tải lên", legacy: "File cũ" };
export const sourceLabel = (s) => SOURCE_LABEL[s] || s;

// "giong_a · fake · giong-b" — tóm tắt TTS cho người dùng thường (không fingerprint/đường dẫn).
export function ttsSummary(t) {
  if (!t) return "";
  return [t.profile || "tự chọn", t.engine, t.voice, t.model].filter(Boolean).join(" · ");
}

export function durationText(sec) {
  if (sec == null) return "—";
  return sec < 10 ? `${sec.toFixed(1).replace(".", ",")} giây` : `${Math.round(sec)} giây`;
}

// Xóa: backend quyết định xóa vật lý hay lưu trữ (có job tham chiếu) và đòi bỏ khỏi kênh nếu đang dùng. Ở đây báo TRƯỚC hậu quả để người dùng không bất ngờ.
export function deleteMode(item) {
  const jobs = item.in_use_by_jobs || 0;
  if (item.active) {
    return { mode: "unset_delete", unset: true, title: `Bỏ “${item.name}” khỏi kênh và xóa?`, confirm: "Bỏ khỏi kênh và xóa",
      body: "Watermark này đang là watermark của kênh. Sau khi xóa, job mới sẽ chạy KHÔNG có watermark cho tới khi bạn chọn watermark khác.",
      note: jobs ? `${jobs} job đang dùng nó nên watermark chỉ được lưu trữ (ẩn); job cũ không bị ảnh hưởng.` : "" };
  }
  if (jobs) {
    return { mode: "archive", unset: false, title: `Lưu trữ “${item.name}”?`, confirm: "Lưu trữ",
      body: `${jobs} job đang dùng watermark này nên không xóa hẳn được: nó sẽ được lưu trữ (ẩn khỏi danh sách) và job cũ vẫn tái lập được.`, note: "" };
  }
  return { mode: "delete", unset: false, title: `Xóa “${item.name}”?`, confirm: "Xóa watermark", body: "Watermark và mọi bản của nó sẽ bị xóa khỏi thư viện. Không hoàn tác được.", note: "" };
}

// Sửa watermark TTS: đổi tên thì chỉ metadata; đổi nội dung/giọng mới tạo bản mới. Dùng để đặt nhãn nút (“Lưu” / “Lưu & tạo bản mới”).
export function editKind(item, form) {
  const nameChanged = (form.name || "").trim() !== item.name;
  const textChanged = item.source === "tts" && (form.text || "").trim() !== (item.text || "").trim();
  const ttsChanged = item.source === "tts" && form.tts != null && form.tts !== (item.tts?.selection || "auto") && form.tts !== (item.tts?.profile || "auto");
  if (textChanged || ttsChanged) return "revision";
  return nameChanged ? "rename" : "none";
}
export const EDIT_LABEL = { revision: "Lưu & tạo bản mới", rename: "Lưu tên", none: "Lưu" };

// Gợi ý nhập liệu phía giao diện (backend kiểm lại và là nguồn sự thật): trả {name?, text?}.
export function createErrors(form) {
  const e = {};
  if (!(form.name || "").trim()) e.name = "Hãy đặt tên cho watermark.";
  if (form.source === "tts") {
    const t = (form.text || "").trim();
    if (!t) e.text = "Hãy nhập nội dung watermark.";
    else if (t.length > 1000) e.text = "Nội dung quá dài (tối đa 1000 ký tự).";
  } else if (!form.file) e.file = "Chọn một file audio.";
  return e;
}

// Lỗi backend → ô cần đánh dấu (để hiện ngay cạnh ô nhập, không chỉ ở đầu form).
export const FIELD_OF_CODE = { WATERMARK_NAME_EMPTY: "name", WATERMARK_TEXT_EMPTY: "text", WATERMARK_TEXT_TOO_LONG: "text", WATERMARK_AUDIO_INVALID: "file", TTS_PROFILE_NOT_FOUND: "tts", INVALID_TTS_PROFILE: "tts" };

export function activeItem(lib) { return (lib?.items || []).find((i) => i.active) || null; }
