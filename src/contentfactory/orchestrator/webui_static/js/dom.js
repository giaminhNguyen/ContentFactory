// Trợ giúp DOM tối giản (không framework). h() KHÔNG bao giờ gán innerHTML từ dữ liệu: mọi chuỗi đi qua textContent => không XSS.
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k in el && k !== "list" && k !== "form" && typeof v !== "object") { try { el[k] = v; } catch { el.setAttribute(k, v); } }
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

export function setText(el, text) { if (el.textContent !== text) el.textContent = text; }

// Đồng bộ danh sách con theo khóa: giữ nguyên node cũ (không dựng lại toàn bộ), chỉ thêm/xóa/sắp xếp lại. Trả về các node mới thêm (để làm hoạt họa).
export function patchList(parent, items, keyOf, create, update) {
  const existing = new Map();
  for (const ch of [...parent.children]) if (ch.dataset.key != null) existing.set(ch.dataset.key, ch);
  const added = [];
  let prev = null;
  for (const item of items) {
    const key = String(keyOf(item));
    let node = existing.get(key);
    if (node) { update(node, item); existing.delete(key); }
    else { node = create(item); node.dataset.key = key; added.push(node); }
    const want = prev ? prev.nextSibling : parent.firstChild;
    if (node !== want) parent.insertBefore(node, want);
    prev = node;
  }
  for (const stale of existing.values()) stale.remove();
  return added;
}

export function uid(prefix = "id") { return `${prefix}-${Math.random().toString(36).slice(2, 9)}`; }

// Nạp CSS riêng của một view (một lần). View phụ có thể có file CSS riêng thay vì làm app.css phình ra.
const loadedCss = new Set();
export function loadCss(href) {
  if (loadedCss.has(href)) return;
  loadedCss.add(href);
  document.head.append(Object.assign(document.createElement("link"), { rel: "stylesheet", href }));
}
