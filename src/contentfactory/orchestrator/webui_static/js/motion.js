// Chuyển động bằng GSAP (core, vendor/gsap.min.js). Quy ước (docs/UI_GUIDE.md):
//  - chỉ transform + opacity (KHÔNG dùng autoAlpha: visibility:hidden làm phần tử mất focus, vd ô nhập vừa focus bị blur khi view bắt đầu hiện); trừ mở/đóng vùng thu gọn (height) vì nội dung cần đẩy phần bên dưới;
//  - mỗi view có một `scope` (gsap.context) => huỷ view là revert + kill toàn bộ tween, không rò rỉ;
//  - tôn trọng prefers-reduced-motion: KHÔNG tween, trạng thái cuối được áp ngay (phản hồi vẫn nhìn thấy: đổi màu/chữ);
//  - ngắn (<= 300ms), không chặn thao tác, không lặp vô hạn (spinner dùng CSS và đã bị CSS reduced-motion tắt).
const mq = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false, addEventListener() {} };
export const reduced = () => !!mq.matches;
const gs = () => window.gsap || null;
const live = () => gs() && !reduced();

export function scope(root) {
  const g = gs();
  const ctx = g ? g.context(() => {}, root) : null;
  return {
    add(fn) { if (ctx) ctx.add(fn); else fn(); },
    revert() { if (ctx) ctx.revert(); },
  };
}

const CLEAR = "opacity,transform";

export function enterView(el) {
  if (!live()) return;
  gs().fromTo(el, { opacity: 0, y: 8 }, { opacity: 1, y: 0, duration: 0.22, ease: "power2.out", clearProps: CLEAR });
}

export function itemsEnter(nodes) {
  if (!live() || !nodes.length) return;
  const many = nodes.length > 10;
  gs().fromTo(nodes, { opacity: 0, y: many ? 0 : 8 }, { opacity: 1, y: 0, duration: 0.22, ease: "power2.out", stagger: many ? 0 : { each: 0.03, amount: 0.2 }, clearProps: CLEAR });
}

export function pulse(el) {
  if (!live()) return;
  gs().fromTo(el, { scale: 1 }, { scale: 1.08, duration: 0.12, yoyo: true, repeat: 1, ease: "power1.out", clearProps: "transform", overwrite: "auto" });
}

export function setProgress(bar, fraction) {
  const f = Math.max(0, Math.min(1, fraction || 0));
  const g = gs();
  if (!g || reduced()) { bar.style.transform = `scaleX(${f})`; return; }
  g.to(bar, { scaleX: f, duration: 0.4, ease: "power2.out", overwrite: "auto" });
}

export function expand(el) {
  const g = gs();
  el.hidden = false;
  if (!live()) return;
  g.fromTo(el, { height: 0, opacity: 0 }, { height: "auto", opacity: 1, duration: 0.22, ease: "power2.out", clearProps: "height,opacity", overwrite: "auto" });
}

export function collapse(el, done) {
  const g = gs();
  if (!live()) { el.hidden = true; done?.(); return; }
  g.to(el, { height: 0, opacity: 0, duration: 0.18, ease: "power2.in", overwrite: "auto", onComplete: () => { el.hidden = true; g.set(el, { clearProps: "height,opacity" }); done?.(); } });
}

export function successMark(svg) {
  if (!live()) return;
  const path = svg.querySelector("path");
  if (!path) return;
  const len = path.getTotalLength ? path.getTotalLength() : 30;
  gs().fromTo(path, { strokeDasharray: len, strokeDashoffset: len }, { strokeDashoffset: 0, duration: 0.45, ease: "power2.out", clearProps: "strokeDasharray,strokeDashoffset" });
  gs().fromTo(svg, { scale: 0.8 }, { scale: 1, duration: 0.35, ease: "back.out(2)", clearProps: "transform" });
}

export function toastIn(el) {
  if (!live()) return;
  gs().fromTo(el, { opacity: 0, x: 24 }, { opacity: 1, x: 0, duration: 0.2, ease: "power2.out", clearProps: CLEAR });
}

export function toastOut(el, done) {
  if (!live()) { done(); return; }
  gs().to(el, { opacity: 0, x: 24, duration: 0.15, ease: "power2.in", onComplete: done });
}

export function dialogIn(dlg) {
  if (!live()) return;
  gs().fromTo(dlg, { opacity: 0, y: 12, scale: 0.98 }, { opacity: 1, y: 0, scale: 1, duration: 0.18, ease: "power2.out", clearProps: CLEAR });
}

// Nội dung đổi tại chỗ (vd mô tả hậu quả khi chọn bước khác): mờ nhẹ rồi hiện, không đổi layout.
export function swap(el) {
  if (!live()) return;
  gs().fromTo(el, { opacity: 0.35 }, { opacity: 1, duration: 0.16, ease: "power1.out", clearProps: "opacity", overwrite: "auto" });
}

export function kill(el) { gs()?.killTweensOf(el); }
