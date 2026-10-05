// Định dạng hiển thị (thuần, test được bằng node).
export function relTime(ts, now = Date.now() / 1000) {
  if (!ts) return "—";
  const d = Math.max(0, now - ts);
  if (d < 10) return "vừa xong";
  if (d < 60) return `${Math.floor(d)} giây trước`;
  if (d < 3600) return `${Math.floor(d / 60)} phút trước`;
  if (d < 86400) return `${Math.floor(d / 3600)} giờ trước`;
  if (d < 86400 * 30) return `${Math.floor(d / 86400)} ngày trước`;
  return new Date(ts * 1000).toLocaleDateString("vi-VN");
}

export function bytes(n) {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${u[i]}`;
}

export function duration(sec) {
  if (sec == null) return "";
  const s = Math.round(sec);
  if (s < 60) return `${s} giây`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} phút ${String(s % 60).padStart(2, "0")} giây`;
  return `${Math.floor(m / 60)} giờ ${m % 60} phút`;
}

export const pct = (f) => `${Math.round((f || 0) * 100)}%`;

export function shortPath(p, max = 52) {
  if (!p || p.length <= max) return p || "";
  return p.slice(0, 14) + "…" + p.slice(-(max - 15));
}
