// Điểm vào: dựng khung (sidebar, banner, theme), nạp bootstrap rồi giao cho router. Mọi logic nghiệp vụ nằm ở backend.
import { api } from "./api.js";
import { h, clear } from "./dom.js";
import { icon } from "./icons.js";
import { btn, toast, alertBox } from "./components.js";
import { app, start, section } from "./router.js";
import { createPoller, net, isOnline } from "./poller.js";
import * as notify from "./notify.js";

const NAV = [
  ["run", "/", "Chạy", "play"],
  ["jobs", "/jobs", "Job", "list"],
  ["channels", "/channels", "Kênh", "tv"],
  ["tts", "/tts", "Giọng đọc", "mic"],
  ["pools", "/pools", "Nguồn Media", "film"],
  ["templates", "/templates", "Template", "layout"],
  ["universe", "/universe", "Kho nhân vật", "database"],
  ["settings", "/settings", "Cài đặt & Doctor", "settings"],
];

// ---------- theme: hệ thống / sáng / tối, nhớ ở localStorage (có thể bị chặn => bọc try) ----------
const THEMES = ["system", "light", "dark"];
function loadTheme() { try { return localStorage.getItem("cf-theme") || "system"; } catch { return "system"; } }
function applyTheme(t) {
  if (t === "system") document.documentElement.removeAttribute("data-theme"); else document.documentElement.dataset.theme = t;
  try { localStorage.setItem("cf-theme", t); } catch { /* bỏ qua */ }
}

function buildShell() {
  document.getElementById("brand").append(icon("zap", { size: 22 }), h("span", null, "ContentFactory"));
  const nav = document.getElementById("nav");
  for (const [id, to, label, ic] of NAV) {
    const a = h("a", { href: "#" + to, dataset: { section: id } }, icon(ic), h("span", null, label));
    if (id === "jobs") a.append(h("span", { class: "count", id: "nav-attn", hidden: true, "aria-label": "job cần xử lý" }));
    nav.append(a);
  }
  const foot = document.getElementById("foot");
  const runner = h("div", { id: "runner-chip", class: "row small" });
  let theme = loadTheme();
  const label = () => ({ system: "Giao diện: theo hệ thống", light: "Giao diện: sáng", dark: "Giao diện: tối" }[theme]);
  const tbtn = btn({ label: label(), icon: theme === "dark" ? "moon" : "sun", kind: "ghost", size: "sm" });
  tbtn.addEventListener("click", () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    applyTheme(theme);
    tbtn.querySelector("span").textContent = label();
    tbtn.querySelector("svg").replaceWith(icon(theme === "dark" ? "moon" : "sun", { size: 16 }));
  });
  foot.append(runner, tbtn);
  document.addEventListener("cf:route", (e) => {
    for (const a of nav.children) { if (a.dataset.section === e.detail.section) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); }
  });
}

function paintRuntime() {
  const rt = app.runtime;
  const el = document.getElementById("runner-chip");
  if (!el || !rt) return;
  clear(el);
  el.append(icon(rt.runner ? "activity" : "pause", { size: 16 }), h("span", null, rt.runner ? "Đang chạy nền" : "Chưa chạy nền"));
  el.title = rt.runner ? "Vòng lặp xử lý job đang chạy trong ứng dụng này." : "Giao diện đang mở ở chế độ xem: job sẽ không được xử lý. Mở lại bằng `cf ui`.";
}

export function setCounts(counts) { app.counts = counts; app.countsAt = Date.now(); paintCounts(); }
document.addEventListener("cf:counts", () => paintCounts());

function paintCounts() {
  const c = app.counts;
  const el = document.getElementById("nav-attn");
  if (!el || !c) return;
  const n = (c.attention || 0);
  el.hidden = n === 0;
  el.textContent = String(n);
}

function paintBanner() {
  const box = document.getElementById("banners");
  clear(box);
  if (!isOnline()) box.append(h("div", { class: "banner error", role: "alert" }, icon("alert-circle", { size: 18 }), "Mất kết nối tới ContentFactory. Đang thử lại… (job vẫn chạy nếu ứng dụng còn mở)"));
}

async function boot() {
  buildShell();
  paintBanner();
  net.addEventListener("change", paintBanner);
  window.addEventListener("unhandledrejection", (e) => {
    if (e.reason && e.reason.name === "AbortError") return;
    console.error(e.reason);
    toast({ title: "Có lỗi không mong đợi", message: "Thử lại thao tác; nếu vẫn lỗi hãy tải lại trang.", tone: "fail" });
  });
  try {
    app.boot = await api.get("/api/bootstrap");
    app.runtime = app.boot.runtime;
  } catch (e) {
    const view = document.getElementById("view");
    view.append(h("h1", { id: "page-title" }, "ContentFactory"), alertBox({ tone: "fail", title: e.message, body: e.hint || "Mở lại ứng dụng bằng `cf ui`.", actions: [btn({ label: "Tải lại", icon: "refresh", size: "sm", onClick: () => location.reload() })] }));
    return;
  }
  paintRuntime();
  // một poller nhẹ cho huy hiệu "cần xử lý" + chip runtime; dùng `since` nên phần lớn lượt chỉ là kiểm tra phiên bản
  let version = null, tick = 0;
  createPoller(async (signal) => {
    if (Date.now() - (app.countsAt || 0) > 4000) {                  // view đang mở đã cập nhật số đếm gần đây thì khỏi hỏi lại (tránh trùng yêu cầu)
      const d = await api.get("/api/jobs", { query: { status: "all", limit: 1, since: version }, signal });
      if (d.changed) {
        version = d.version; setCounts(d.counts);
        if (notify.enabled()) api.get("/api/dashboard", { signal }).then(notify.check).catch(() => {});          // thông báo tuỳ chọn: chỉ khi người dùng đã bật
      }
    }
    if (tick++ % 5 === 0) { app.runtime = await api.get("/api/runtime", { signal }); paintRuntime(); }
    return (app.counts?.running || 0) > 0 ? "fast" : "idle";
  }, { fast: 2500, idle: 8000 }).start();
  start();
}

boot();
export { section };
