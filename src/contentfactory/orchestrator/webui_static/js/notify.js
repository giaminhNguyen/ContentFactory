// Thông báo trình duyệt (tuỳ chọn, mặc định TẮT, chỉ bật khi người dùng bấm). Chỉ hai loại để không ồn: "có job cần bạn xử lý" (số tăng) và
// "một Channel Run vừa chạy xong". Không thông báo theo từng bước/video. Chỉ hiện khi cửa sổ không đang được xem; mỗi loại tối đa 1 lần/60 giây.
const KEY = "cf.notify";
const store = { get: () => { try { return localStorage.getItem(KEY); } catch { return null; } }, set: (v) => { try { localStorage.setItem(KEY, v); } catch { /* không lưu được thì thôi */ } } };

export const supported = () => typeof Notification !== "undefined";
export const permission = () => (supported() ? Notification.permission : "unsupported");
export const enabled = () => supported() && Notification.permission === "granted" && store.get() === "1";

export async function enable() {
  if (!supported()) return "unsupported";
  const p = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
  store.set(p === "granted" ? "1" : "0");
  return p;
}
export function disable() { store.set("0"); }

let prev = null;
const last = new Map();
function show(kind, title, body) {
  const now = Date.now();
  if (now - (last.get(kind) || 0) < 60000) return;
  last.set(kind, now);
  try { new Notification(title, { body, tag: kind }); } catch { /* một số trình duyệt chặn: bỏ qua */ }
}

// Gọi với kết quả /api/dashboard mỗi khi dữ liệu job đổi.
export function check(d) {
  const was = prev;
  prev = { attention: d.attention, batches_running: d.batches_running };
  if (!was || !enabled() || (document.hasFocus() && !document.hidden)) return;
  if (d.attention > was.attention) show("attention", "ContentFactory: có việc cần bạn xử lý", `${d.attention} job cần xử lý. Mở “Job” → “Cần xử lý”.`);
  if (was.batches_running > 0 && d.batches_running < was.batches_running) show("batch", "ContentFactory: Channel Run đã chạy xong", "Mở “Job” để xem kết quả từng video.");
}
