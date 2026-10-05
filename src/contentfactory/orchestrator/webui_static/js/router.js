// Router theo hash (không cần cấu hình máy chủ, có deep link, nút Back đúng). View nạp lười bằng import() => màn hình chính không phải tải các view phụ.
import { h, clear } from "./dom.js";
import * as motion from "./motion.js";
import { errorState, skeleton } from "./components.js";

export const app = { boot: null, counts: null, countsAt: 0, runtime: null };   // trạng thái dùng chung (bootstrap, đếm job, runtime)

const ROUTES = [
  [/^\/$/, "run", "Chạy"],
  [/^\/jobs$/, "jobs", "Job"],
  [/^\/jobs\/([\w-]+)$/, "job", "Chi tiết job"],
  [/^\/channels(?:\/([\w-]+))?$/, "channels", "Kênh"],
  [/^\/tts$/, "tts", "Giọng đọc (TTS)"],
  [/^\/pools$/, "pools", "Video nguồn"],
  [/^\/templates$/, "templates", "Template"],
  [/^\/templates\/([a-z0-9_]+)$/, "studio", "Template Studio"],
  [/^\/settings(?:\/(\w+))?$/, "settings", "Cài đặt & Doctor"],
];

// Các view có số đếm job (danh sách) gọi hàm này để cập nhật huy hiệu 'cần xử lý' mà không cần poller của khung hỏi lại.
export function publishCounts(counts) { app.counts = counts; app.countsAt = Date.now(); document.dispatchEvent(new CustomEvent("cf:counts")); }

let current = null;
let token = 0;
let firstRender = true;      // lần dựng đầu (mở trang): không cướp focus để phím Tab đầu tiên vẫn tới liên kết 'Bỏ qua điều hướng'

export const path = () => (location.hash.replace(/^#/, "") || "/").split("?")[0];
export const query = () => new URLSearchParams((location.hash.split("?")[1]) || "");
export function navigate(to, { replace = false } = {}) {
  const url = "#" + to;
  if (replace) history.replaceState(null, "", url), render(); else location.hash = to;
}

export function section(p = path()) {
  const m = ROUTES.find(([rx]) => rx.test(p));
  if (!m) return null;
  return m[1] === "job" ? "jobs" : m[1] === "studio" ? "templates" : m[1];
}

export async function render() {
  const my = ++token;
  const root = document.getElementById("view");
  const p = path();
  const hit = ROUTES.find(([rx]) => rx.test(p));
  if (current) { try { current.destroy?.(); } catch (e) { console.error(e); } current = null; }
  clear(root);
  const [rx, name, title] = hit || [null, "run", "Chạy"];
  const params = hit ? rx.exec(p).slice(1) : [];
  document.title = `${title} · ContentFactory`;
  document.dispatchEvent(new CustomEvent("cf:route", { detail: { name, section: section(p) } }));
  root.append(skeleton(4));
  try {
    const mod = await import(`./views/${name}.js`);
    if (my !== token) return;                                     // người dùng đã đi tiếp trong lúc nạp
    clear(root);
    const sc = motion.scope(root);
    const inst = (await mod.mount(root, { params, query: query(), app, navigate, scope: sc })) || {};
    if (my !== token) { inst.destroy?.(); sc.revert(); return; }
    current = { destroy() { try { inst.destroy?.(); } finally { sc.revert(); } } };
    sc.add(() => motion.enterView(root));
    announce(title);
    if (!firstRender && (!document.activeElement || document.activeElement === document.body || !root.contains(document.activeElement))) root.focus({ preventScroll: true });
    firstRender = false;
  } catch (e) {
    if (my !== token) return;
    console.error(e);
    clear(root);
    root.append(h("h1", { id: "page-title" }, title), errorState(e, () => render()));
  }
}

function announce(title) {
  const live = document.getElementById("route-live");
  if (live) live.textContent = `Đã mở ${title}`;
}

export function start() {
  window.addEventListener("hashchange", render);
  render();
}
