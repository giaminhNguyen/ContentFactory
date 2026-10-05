// Hộp thoại dùng chung cho Template: modal tự quản + Thư viện asset (xem, chọn, tải lên, xoá asset của người dùng).
// Asset do ContentFlow sở hữu: ở đây chỉ gọi API theo ID; không bao giờ duyệt hay nhập đường dẫn tuỳ ý trên máy.
import { api } from "./api.js";
import { h, clear, uid } from "./dom.js";
import { icon } from "./icons.js";
import { btn, busy, field, input, select, alertBox, skeleton, toast, toastError, confirmDialog, badge } from "./components.js";
import * as motion from "./motion.js";
import { assetSlug, validAssetId } from "./templates_logic.js";

export const ASSET_TYPES = { frame: "Khung (frame)", background: "Nền (background)", overlay: "Lớp phủ (overlay)", logo: "Logo", font: "Font", mask: "Mặt nạ (mask)" };
export const IMAGE_ASSET_TYPES = ["frame", "background", "overlay", "logo"];
const isImage = (t) => t !== "font";

// Modal gốc <dialog>: Esc đóng, trả focus về nút gọi. close(value) resolve `done`.
export function modal({ title, content, wide = false, actions = [] }) {
  const opener = document.activeElement;
  const titleId = uid("dlg");
  const dlg = h("dialog", { class: "dlg" + (wide ? " wide" : ""), "aria-labelledby": titleId });
  let resolve;
  const done = new Promise((r) => { resolve = r; });
  let closed = false;
  const close = (value) => { if (closed) return; closed = true; dlg.close(); resolve(value); };
  const bar = h("div", { class: "actions" });
  for (const a of actions) bar.append(btn({ label: a.label, kind: a.kind || "", onClick: () => close(a.value) }));
  dlg.append(h("h2", { id: titleId }, title), content, bar);
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(undefined); });
  dlg.addEventListener("close", () => { dlg.remove(); if (opener && opener.isConnected) opener.focus(); });
  document.getElementById("dialogs").append(dlg);
  dlg.showModal();
  motion.dialogIn(dlg);
  return { dlg, close, done };
}

const msg = (e) => [e.message, e.hint].filter(Boolean).join(" ");

// types: danh sách loại cho phép (picker) hoặc null = quản lý tất cả. Trả Promise<id | null> (null nếu đóng mà không chọn).
export function openAssetLibrary({ types = null, current = null, title = "Thư viện asset" } = {}) {
  const picking = !!types;
  let assets = [], filter = types && types.length === 1 ? types[0] : "";
  const listBox = h("div", { class: "asset-grid", role: "list" });
  const status = h("div", { "aria-live": "polite" });
  const typeSel = select({ options: [["", "Tất cả loại"], ...Object.entries(ASSET_TYPES).filter(([k]) => !types || types.includes(k))], value: filter });
  typeSel.addEventListener("change", () => { filter = typeSel.value; paint(); });
  const m = modal({ title, wide: true, content: h("div", { class: "stack asset-lib" },
    h("div", { class: "row spread" }, field({ label: "Lọc theo loại", control: typeSel }), h("p", { class: "muted small asset-note" }, "Asset có biểu tượng khoá là asset có sẵn của ContentFlow: không xoá được.")),
    status, listBox, uploadBlock()), actions: [{ label: picking ? "Đóng, không chọn" : "Đóng", value: null }] });
  status.append(skeleton(2));

  async function load() {
    try {
      assets = (await api.get("/api/assets")).assets;
      clear(status);
      paint();
    } catch (e) { clear(status); status.append(alertBox({ tone: "fail", title: e.message, body: e.hint })); }
  }

  function paint() {
    clear(listBox);
    const rows = assets.filter((a) => (!types || types.includes(a.type)) && (!filter || a.type === filter));
    if (!rows.length) { listBox.append(h("p", { class: "muted" }, "Chưa có asset loại này. Tải lên bên dưới.")); return; }
    for (const a of rows) listBox.append(card(a));
  }

  function card(a) {
    const thumb = h("div", { class: "asset-thumb", "aria-hidden": "true" });
    if (isImage(a.type)) {
      const img = h("img", { alt: "" });
      thumb.append(img);
      api.blobUrl(`/api/assets/${a.id}/file`).then((u) => { img.src = u; }).catch(() => thumb.append(icon("image", { size: 24 })));
    } else thumb.append(h("span", { class: "asset-font" }, "Aa"));
    const meta = [ASSET_TYPES[a.type] || a.type, a.metadata?.width ? `${a.metadata.width}×${a.metadata.height}` : null].filter(Boolean).join(" · ");
    const el = h("div", { class: "asset-card", role: "listitem", "aria-current": String(a.id === current) }, thumb,
      h("div", { class: "asset-info" }, h("strong", { class: "asset-name" }, a.name || a.id), h("span", { class: "mono small muted" }, a.id), h("span", { class: "small muted" }, meta),
        h("span", { class: "row small" }, a.scope === "builtin" ? h("span", { class: "chip" }, icon("lock", { size: 12 }), "Có sẵn") : h("span", { class: "chip" }, "Của bạn"))));
    const actions = h("div", { class: "row" });
    if (picking) actions.append(btn({ label: a.id === current ? "Đang dùng" : "Chọn", kind: "primary", size: "sm", onClick: () => m.close(a.id) }));
    if (a.scope === "user") actions.append(btn({ icon: "trash", kind: "ghost danger", size: "sm", ariaLabel: `Xoá asset ${a.name || a.id}`, title: "Xoá asset", onClick: (ev) => remove(a, ev.currentTarget) }));
    el.append(actions);
    return el;
  }

  async function remove(a, button) {
    if (!(await confirmDialog({ title: "Xoá asset?", body: `Xoá “${a.name || a.id}” khỏi thư viện. Template còn dùng nó sẽ báo lỗi cho tới khi bạn chọn asset khác.`, confirmLabel: "Xoá", danger: true }))) return;
    await busy(button, async () => {
      try { await api.del(`/api/assets/${a.id}`); }
      catch (e) {
        if (e.code !== "ASSET_IN_USE") return toastError(e, "Chưa xoá được asset");
        if (!(await confirmDialog({ title: "Asset đang được dùng", body: `${e.message} Vẫn xoá? Các template đó sẽ không publish/render được cho tới khi sửa.`, confirmLabel: "Vẫn xoá", danger: true }))) return;
        try { await api.del(`/api/assets/${a.id}`, { query: { force: 1 } }); } catch (e2) { return toastError(e2, "Chưa xoá được asset"); }
      }
      toast({ title: "Đã xoá asset", tone: "done" });
      await load();
    });
  }

  function uploadBlock() {
    const fileIn = h("input", { type: "file", class: "input", accept: "image/png,image/jpeg,image/webp,.ttf,.otf,.ttc", id: uid("af") });
    const typeIn = select({ options: Object.entries(ASSET_TYPES).filter(([k]) => !types || types.includes(k)), value: types?.[0] || "frame" });
    const idIn = input({ placeholder: "gold_frame" });
    let idTouched = false;
    const idF = field({ label: "Mã asset", control: idIn, hint: "Chữ thường không dấu, số, _ và -. Template tham chiếu asset bằng mã này." });
    idIn.addEventListener("input", () => { idTouched = true; idF.setError(null); });
    const fileF = field({ label: "File (PNG, JPG, WebP, hoặc font TTF/OTF)", control: fileIn });
    fileIn.addEventListener("change", () => {
      const f = fileIn.files[0];
      fileF.setError(null);
      if (!f) return;
      if (/\.(ttf|otf|ttc)$/i.test(f.name)) typeIn.value = "font";
      if (!idTouched) idIn.value = assetSlug(f.name);
    });
    const up = btn({ label: "Tải lên", icon: "upload", kind: "primary" });
    up.addEventListener("click", () => busy(up, async () => {
      const f = fileIn.files[0];
      if (!f) return fileF.setError("Chọn một file.");
      if (f.size > 60 * 1024 * 1024) return fileF.setError("File quá lớn (tối đa 60 MB).");
      const aid = idIn.value.trim();
      if (!validAssetId(aid)) return idF.setError("Mã asset: 2–64 ký tự, chữ thường không dấu, số, _ hoặc -, bắt đầu bằng chữ/số.");
      try {
        await api.upload(`/api/assets/${aid}?type=${encodeURIComponent(typeIn.value)}&name=${encodeURIComponent(f.name)}`, await f.arrayBuffer());
        toast({ title: "Đã tải asset lên", message: aid, tone: "done" });
        fileIn.value = ""; idIn.value = ""; idTouched = false;
        await load();
      } catch (e) { (e.code?.includes("ID") ? idF : fileF).setError(msg(e)); }
    }));
    return h("section", { class: "stack asset-upload", "aria-label": "Tải asset mới" }, h("h3", null, "Tải asset mới"),
      h("div", { class: "grid-2" }, fileF, field({ label: "Loại", control: typeIn })), idF, h("div", null, up));
  }

  load();
  return m.done.then((v) => v ?? null);
}

// Ô chọn asset gọn: thumbnail + tên + nút Đổi/Bỏ. onPick(id|null).
export function assetPicker({ types, value, onPick, disabled = false, optional = false, label = "Asset" }) {
  const thumb = h("div", { class: "asset-thumb sm", "aria-hidden": "true" });
  const name = h("span", { class: "asset-name" });
  const wrap = h("div", { class: "asset-picker" }, thumb, h("div", { class: "asset-info" }, name));
  let cur = value;
  const paint = async () => {
    clear(thumb);
    name.textContent = cur || "— chưa chọn —";
    name.classList.toggle("muted", !cur);
    if (cur && types.some((t) => t !== "font")) {
      const img = h("img", { alt: "" });
      thumb.append(img);
      api.blobUrl(`/api/assets/${cur}/file`).then((u) => { if (cur === value || img.isConnected) img.src = u; }).catch(() => { clear(thumb); thumb.append(icon("alert-circle", { size: 18 })); name.textContent = `${cur} (không tìm thấy)`; });
    } else if (cur) thumb.append(h("span", { class: "asset-font" }, "Aa"));
    else thumb.append(icon("image", { size: 18 }));
  };
  const change = btn({ label: cur ? "Đổi" : "Chọn", size: "sm", disabled, ariaLabel: `${label}: chọn asset`, onClick: async () => {
    const id = await openAssetLibrary({ types, current: cur, title: `Chọn ${label.toLowerCase()}` });
    if (id) { cur = id; change.querySelector("span").textContent = "Đổi"; paint(); onPick(id); }
  } });
  wrap.append(change);
  if (optional) wrap.append(btn({ label: "Bỏ", kind: "ghost", size: "sm", disabled, onClick: () => { cur = null; paint(); onPick(null); } }));
  paint();
  wrap.set = (v) => { if (v !== cur) { cur = v; paint(); } };
  return wrap;
}
