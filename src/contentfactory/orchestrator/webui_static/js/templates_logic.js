// Logic thuần của Template Studio (không DOM, test bằng node --test tests/ui_js/templates.test.mjs).
// Mọi hàm làm việc trên CHÍNH tài liệu template mà ContentFlow render (schema v1): giao diện không có schema riêng.

export const MIN_SIZE = 8;          // cạnh nhỏ nhất của một vùng (px canvas)
export const TOUCH = 20;            // ảnh/video được tràn khỏi canvas nhưng phải còn ít nhất ngần này px nằm trong
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const int = (v) => Math.round(v);

// ---------- id / tên ----------
export function slug(text, max = 48) {
  const s = String(text ?? "").normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/đ/g, "d").replace(/Đ/g, "D").toLowerCase()
    .replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, max).replace(/_+$/, "");
  return /^[a-z0-9]/.test(s) ? s : "";
}
// Id asset cho phép thêm dấu '-' (khớp ASSET_ID_RX phía ContentFlow); tối thiểu 2 ký tự.
export function assetSlug(name) { return slug(String(name ?? "").replace(/\.[^.]+$/, ""), 64); }
export const validTemplateId = (s) => /^[a-z0-9][a-z0-9_]{1,47}$/.test(s);
export const validAssetId = (s) => /^[a-z0-9][a-z0-9_-]{1,63}$/.test(s);
export function uniqueId(base, taken) {
  const t = new Set(taken);
  const b = base || "item";
  if (!t.has(b)) return b;
  for (let n = 2; ; n++) if (!t.has(`${b}_${n}`)) return `${b}_${n}`;
}

// ---------- tài liệu ----------
export const clone = (o) => JSON.parse(JSON.stringify(o));
export const canon = (v) => (Array.isArray(v) ? v.map(canon) : v && typeof v === "object" ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, canon(v[k])])) : v);
export const canonStr = (o) => JSON.stringify(canon(o));
// Các trường do máy chủ đóng dấu: không tính vào "đã sửa".
export function content(doc) { const { scope, status, created_at, published_at, parent, ...rest } = doc || {}; return rest; }
export const isDirty = (doc, base) => canonStr(content(doc)) !== canonStr(content(base));

export const byId = (doc, id) => (doc.elements || []).find((e) => e.id === id);
export const elementsTopDown = (doc) => [...(doc.elements || [])].sort((a, b) => (b.z ?? 0) - (a.z ?? 0));

// Mô hình hộp: text/photo phải nằm TRONG canvas; image/source_video được tràn (bị cắt) nhưng không được hoàn toàn ngoài.
export const boxMode = (el) => (el.type === "text" || el.type === "photo" ? "inside" : "touch");
export const hasBox = (el) => ["x", "y", "width", "height"].every((k) => typeof el[k] === "number");

// ---------- z-order xác định ----------
// Đánh số lại z = 10, 20, 30… theo thứ tự từ dưới lên (không suy từ thời điểm tạo).
export function renumberZ(elementsBottomUp) {
  elementsBottomUp.forEach((e, i) => { e.z = 10 * (i + 1); });
  return elementsBottomUp;
}
// dir = +1: đưa lên trên một lớp; -1: xuống dưới. Trả true nếu có đổi. Đánh số lại toàn bộ để z luôn duy nhất.
export function moveLayer(doc, id, dir) {
  const order = [...doc.elements].sort((a, b) => (a.z ?? 0) - (b.z ?? 0) || doc.elements.indexOf(a) - doc.elements.indexOf(b));
  const i = order.findIndex((e) => e.id === id);
  const j = i + (dir > 0 ? 1 : -1);
  if (i < 0 || j < 0 || j >= order.length) return false;
  [order[i], order[j]] = [order[j], order[i]];
  renumberZ(order);
  return true;
}
export function nextZ(doc) { return Math.max(0, ...(doc.elements || []).map((e) => e.z ?? 0)) + 10; }

// ---------- hình học ----------
export function clampBox(b, canvas, mode) {
  const cw = canvas.width, ch = canvas.height;
  let { x, y, width: w, height: h } = b;
  w = Math.max(MIN_SIZE, int(w)); h = Math.max(MIN_SIZE, int(h)); x = int(x); y = int(y);
  if (mode === "inside") {
    w = Math.min(w, cw); h = Math.min(h, ch);
    x = clamp(x, 0, cw - w); y = clamp(y, 0, ch - h);
  } else {
    const tx = Math.min(TOUCH, w), ty = Math.min(TOUCH, h);
    x = clamp(x, -(w - tx), cw - tx); y = clamp(y, -(h - ty), ch - ty);
  }
  return { x, y, width: w, height: h };
}
export function moveBox(b, dx, dy, canvas, mode) { return clampBox({ ...b, x: b.x + dx, y: b.y + dy }, canvas, mode); }
// handle: n s e w ne nw se sw. dx,dy theo đơn vị canvas. keepRatio: giữ tỉ lệ (góc).
export function resizeBox(b, handle, dx, dy, canvas, mode, { keepRatio = false } = {}) {
  let l = b.x, t = b.y, r = b.x + b.width, bt = b.y + b.height;
  if (handle.includes("w")) l += dx;
  if (handle.includes("e")) r += dx;
  if (handle.includes("n")) t += dy;
  if (handle.includes("s")) bt += dy;
  if (r - l < MIN_SIZE) { if (handle.includes("w")) l = r - MIN_SIZE; else r = l + MIN_SIZE; }
  if (bt - t < MIN_SIZE) { if (handle.includes("n")) t = bt - MIN_SIZE; else bt = t + MIN_SIZE; }
  if (keepRatio && handle.length === 2) {
    const ratio = b.width / b.height;
    const w = r - l, h = bt - t;
    if (w / h > ratio) { const nh = w / ratio; if (handle.includes("n")) t = bt - nh; else bt = t + nh; }
    else { const nw = h * ratio; if (handle.includes("w")) l = r - nw; else r = l + nw; }
  }
  if (mode === "inside") { l = Math.max(0, l); t = Math.max(0, t); r = Math.min(canvas.width, r); bt = Math.min(canvas.height, bt); }
  else {
    // chỉ cần còn phần nằm trong canvas: kéo cạnh trái/phải/trên/dưới không được vượt quá canvas quá xa
    l = Math.min(l, canvas.width - TOUCH); t = Math.min(t, canvas.height - TOUCH); r = Math.max(r, TOUCH); bt = Math.max(bt, TOUCH);
  }
  return clampBox({ x: l, y: t, width: r - l, height: bt - t }, canvas, mode);
}
export function nudge(b, key, shift, canvas, mode) {
  const d = shift ? 10 : 1;
  const v = { ArrowLeft: [-d, 0], ArrowRight: [d, 0], ArrowUp: [0, -d], ArrowDown: [0, d] }[key];
  return v ? moveBox(b, v[0], v[1], canvas, mode) : null;
}
// Gom về lưới (snap) – tuỳ chọn; trả số nguyên.
export const snap = (v, grid) => (grid > 1 ? Math.round(v / grid) * grid : int(v));

// Thu phóng chỉ là hệ số hiển thị: KHÔNG bao giờ ghi vào template.
export function viewScale(mode, availWidth, canvasWidth) {
  if (mode === "fit") return clamp(availWidth / canvasWidth, 0.05, 2);
  const n = Number(mode);
  return Number.isFinite(n) && n > 0 ? n : 1;
}
export const toCanvas = (px, scale) => px / scale;

// ---------- lịch sử Hoàn tác / Làm lại ----------
export class History {
  constructor(limit = 100, windowMs = 700) { this.limit = limit; this.windowMs = windowMs; this.reset(null); }
  reset(state) { this.states = [state]; this.idx = 0; this.lastKey = null; this.lastAt = 0; }
  get current() { return this.states[this.idx]; }
  // key: gom các lần sửa liên tiếp cùng một thao tác (kéo, gõ vào một ô) thành một bước Hoàn tác.
  record(state, key = null, now = Date.now()) {
    if (state === this.current) return false;
    this.states.length = this.idx + 1;                         // sửa mới xoá phần "Làm lại"
    if (key && key === this.lastKey && now - this.lastAt < this.windowMs && this.idx > 0) this.states[this.idx] = state;
    else { this.states.push(state); this.idx++; }
    this.lastKey = key; this.lastAt = now;
    if (this.states.length > this.limit + 1) { this.states.shift(); this.idx--; }
    return true;
  }
  canUndo() { return this.idx > 0; }
  canRedo() { return this.idx < this.states.length - 1; }
  undo() { if (!this.canUndo()) return null; this.idx--; this.lastKey = null; return this.current; }
  redo() { if (!this.canRedo()) return null; this.idx++; this.lastKey = null; return this.current; }
}

// ---------- thêm phần tử ----------
export function newElement(type, doc, { assetId = null, source = "project.title" } = {}) {
  const cw = doc.canvas.width, ch = doc.canvas.height;
  const ids = (doc.elements || []).map((e) => e.id);
  const z = nextZ(doc);
  const centered = (w, h) => ({ x: int((cw - w) / 2), y: int((ch - h) / 2), width: int(w), height: int(h) });
  if (type === "image") return { id: uniqueId("image", ids), type, asset_id: assetId, ...centered(cw / 3, ch / 3), fit: "stretch", z };
  if (type === "photo") return { id: uniqueId("photo", ids), type, ...centered(cw * 0.35, ch * 0.7), z };
  if (type === "text") {
    const base = source === "channel.name" ? "channel_name" : "project_title";
    return { id: uniqueId(base, ids), type, source, ...centered(cw * 0.4, ch * 0.18), fill: "#FFFFFF", z };
  }
  if (type === "source_video") return { id: uniqueId("source_video", ids), type, ...centered(cw, ch), fit: "cover", z };
  throw new Error(`loại phần tử không hỗ trợ: ${type}`);
}

// Đường dẫn lỗi của validator: "elements[2] (project_title).width" | "canvas.width" | "elements"
export function parseIssuePath(path) {
  const m = /^elements\[(\d+)\](?: \(([^)]*)\))?(?:\.(.+))?$/.exec(path || "");
  if (m) return { index: Number(m[1]), id: m[2] || null, field: m[3] || null };
  if ((path || "").startsWith("canvas")) return { canvas: true, field: path.split(".")[1] || null };
  return { other: true };
}

// Hộp giới hạn tối thiểu của template để hiển thị (kích thước canvas hợp lệ).
// Chỉ kết quả của yêu cầu MỚI NHẤT được dùng: mỗi lần gửi lấy một vé; kết quả cũ về muộn (hoặc đã bị huỷ) bị bỏ, không ghi đè bản mới (Phase 7).
export class LatestGate {
  constructor() { this.n = 0; }
  next() { return ++this.n; }
  isLatest(token) { return token === this.n; }
}

// Chọn nguồn mẫu kế tiếp khi bấm “Đổi mẫu”: đi hết các ảnh của một mẫu chữ rồi sang mẫu chữ kế (ví dụ 3 mẫu × 4 ảnh = 12 tổ hợp).
export function nextSample(sel, samples, images) {
  const si = Math.max(0, samples.findIndex((s) => s.id === sel.id)), ii = Math.max(0, images.findIndex((i) => i.id === sel.image));
  if (images.length > 1 && ii + 1 < images.length) return { ...sel, image: images[ii + 1].id };
  return { ...sel, id: samples[(si + 1) % samples.length].id, image: images[0]?.id || "builtin" };
}

export const canvasOk = (c) => c && Number.isInteger(c.width) && Number.isInteger(c.height) && c.width > 0 && c.height > 0;
