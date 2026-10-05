// Template: danh sách template thumbnail/video (do ContentFlow sở hữu). Tạo, nhân bản, lưu trữ, mở Template Studio để sửa.
import { api } from "../api.js";
import { h, clear, loadCss } from "../dom.js";
import { icon } from "../icons.js";
import { btn, busy, field, input, select, badge, alertBox, emptyState, errorState, skeleton, pageHead, toast, toastError, confirmDialog, openDialog, switchCtl } from "../components.js";
import * as motion from "../motion.js";
import { slug, validTemplateId, uniqueId } from "../templates_logic.js";

const TYPE_LABEL = { thumbnail: "Thumbnail", video: "Video" };
const KEY_LABEL = { thumbnail: "Thumbnail", youtube_video: "YouTube", tiktok_video: "TikTok" };
const CANVAS_PRESETS = {
  video: [["1920x1080", "YouTube 16:9 (1920×1080)"], ["1080x1920", "TikTok 9:16 (1080×1920)"], ["custom", "Tuỳ chỉnh…"]],
  thumbnail: [["1648x928", "Thumbnail mặc định (1648×928)"], ["1280x720", "1280×720"], ["custom", "Tuỳ chỉnh…"]],
};

export function statusBadges(r) {
  const out = [];
  if (r.scope === "builtin") out.push(badge({ tone: "off", icon: "lock", label: "Có sẵn" }));
  if (r.latest_published) out.push(badge({ tone: "done", icon: "check-circle", label: `Đã publish v${r.latest_published}` }));
  if (r.draft) out.push(badge({ tone: "wait", icon: "file", label: `Bản nháp v${r.draft}` }));
  if (!r.latest_published && !r.draft) out.push(badge({ tone: "queue", icon: "folder", label: "Đã lưu trữ" }));
  return out;
}

export async function mount(root, ctx) {
  loadCss("/css/templates.css");
  const { scope, navigate } = ctx;
  let rows = [], type = "", archived = false, alive = true, animated = false;
  const host = h("div", { class: "stack" });
  const typeSel = select({ options: [["", "Tất cả loại"], ["thumbnail", "Thumbnail"], ["video", "Video"]], value: type, onChange: (v) => { type = v; paint(); } });
  const archSw = switchCtl({ label: "Hiện cả template đã lưu trữ", checked: false, onChange: (v) => { archived = v; load(); } });
  const newBtn = btn({ label: "Template mới", icon: "plus", kind: "primary", onClick: () => openNew() });
  root.append(pageHead("Template", "Bố cục thumbnail và video do ContentFlow quản lý. Chọn template cho từng kênh ở trang Kênh; sửa và thử ở Template Studio.", newBtn),
    h("div", { class: "row tpl-filters" }, field({ label: "Loại", control: typeSel }), archSw), h("div", { "aria-live": "polite" }, host));
  host.append(skeleton(4));

  async function load() {
    try {
      const r = await api.get("/api/templates", { query: { archived: archived ? 1 : 0 } });
      if (!alive) return;
      rows = r.templates;
      paint();
    } catch (e) { if (alive) { clear(host); host.append(errorState(e, load)); } }
  }

  function paint() {
    clear(host);
    const list = rows.filter((r) => !type || r.type === type);
    if (!list.length) {
      host.append(emptyState({ icon: "layout", title: rows.length ? "Không có template nào khớp bộ lọc" : "Chưa có template nào", text: rows.length ? "Đổi bộ lọc ở trên." : "Tạo template đầu tiên để tuỳ chỉnh bố cục thumbnail hoặc video.",
        action: rows.length ? null : btn({ label: "Template mới", icon: "plus", kind: "primary", onClick: () => openNew() }) }));
      return;
    }
    const grid = h("ul", { class: "tpl-grid", "aria-label": "Danh sách template" });
    for (const r of list) grid.append(card(r));
    host.append(grid);
    if (!animated) { animated = true; scope.add(() => motion.itemsEnter([...grid.children])); }
  }

  function card(r) {
    const c = r.canvas || {};
    const ratio = c.width && c.height ? c.width / c.height : 16 / 9;
    const mini = h("div", { class: "tpl-mini", "aria-hidden": "true" }, h("div", { class: "tpl-mini-box" }, h("span", null, `${c.width || "?"}×${c.height || "?"}`)));
    mini.firstChild.style.aspectRatio = String(ratio);
    mini.firstChild.dataset.type = r.type;
    const used = r.used_by?.length ? h("span", { class: "small" }, "Đang dùng bởi: ", r.used_by.map((u) => `${u.channel} (${KEY_LABEL[u.key] || u.key})`).join(", ")) : h("span", { class: "small muted" }, "Chưa kênh nào chọn");
    const def = r.default_for?.length ? h("span", { class: "small muted" }, "Mặc định cho: ", r.default_for.map((k) => KEY_LABEL[k] || k).join(", ")) : null;
    const open = btn({ label: r.scope === "builtin" ? "Xem" : r.draft ? "Sửa bản nháp" : "Mở", icon: "layout", kind: "primary", size: "sm", onClick: () => navigate(`/templates/${r.id}${r.draft ? `?v=${r.draft}` : ""}`) });
    const dup = btn({ label: "Nhân bản", icon: "copy", size: "sm", onClick: () => openDuplicate(r) });
    const arch = r.scope === "user" && r.latest_published ? btn({ label: "Lưu trữ", icon: "folder", kind: "ghost", size: "sm", onClick: (e) => archive(r, e.currentTarget) }) : null;
    return h("li", { class: "card tpl-card", dataset: { id: r.id } }, mini,
      h("div", { class: "tpl-body" },
        h("div", { class: "row spread" }, h("h2", { class: "tpl-name" }, r.name || r.id), h("span", { class: "chip" }, TYPE_LABEL[r.type] || r.type)),
        h("span", { class: "mono small muted" }, r.id),
        h("div", { class: "row" }, ...statusBadges(r)),
        r.description ? h("p", { class: "small muted" }, r.description) : null, used, def,
        h("div", { class: "row tpl-actions" }, open, dup, arch)));
  }

  // ---------- Template mới ----------
  function openNew() {
    let idTouched = false;
    const typeIn = h("div", { class: "radio-row", role: "radiogroup", "aria-label": "Loại template" },
      ...[["video", "Video (YouTube/TikTok)"], ["thumbnail", "Thumbnail"]].map(([v, l], i) => h("label", null, h("input", { type: "radio", name: "tpl-type", value: v, checked: i === 0 }), l)));
    const nameIn = input({ placeholder: "Ví dụ: Story Frame" });
    const idIn = input({ placeholder: "story_frame" });
    const nameF = field({ label: "Tên hiển thị", control: nameIn, required: true });
    const idF = field({ label: "Mã template", control: idIn, hint: "Chữ thường không dấu, số và _ (2–48 ký tự). Không đổi được sau khi tạo." });
    const preset = select({ options: CANVAS_PRESETS.video, value: "1920x1080" });
    const wIn = input({ type: "number", min: 16, max: 8192, step: "1", value: "1920" });
    const hIn = input({ type: "number", min: 16, max: 8192, step: "1", value: "1080" });
    const custom = h("div", { class: "grid-2" }, field({ label: "Rộng (px)", control: wIn }), field({ label: "Cao (px)", control: hIn }));
    custom.hidden = true;
    const curType = () => content.querySelector("input[name=tpl-type]:checked").value;
    const syncPreset = () => { preset.replaceChildren(...CANVAS_PRESETS[curType()].map(([v, l]) => h("option", { value: v }, l))); custom.hidden = true; };
    typeIn.addEventListener("change", syncPreset);
    preset.addEventListener("change", () => { custom.hidden = preset.value !== "custom"; });
    nameIn.addEventListener("input", () => { nameF.setError(null); if (!idTouched) { idIn.value = slug(nameIn.value); } });
    idIn.addEventListener("input", () => { idTouched = true; idF.setError(null); });
    const content = h("div", { class: "stack" }, h("div", { class: "field" }, h("div", { class: "label" }, "Loại"), typeIn), nameF, idF, field({ label: "Kích thước canvas", control: preset }), custom);
    openDialog({ title: "Template mới", content, describe: "Tạo bản nháp trống với bố cục khởi đầu; sửa và publish ở Template Studio.", actions: [{ label: "Huỷ", value: null }, { label: "Tạo và mở Studio", kind: "primary", value: "ok", onClick: async () => {
      const id = idIn.value.trim();
      if (!nameIn.value.trim()) { nameF.setError("Đặt tên cho template."); return false; }
      if (!validTemplateId(id)) { idF.setError("Mã không hợp lệ: 2–48 ký tự, chữ thường không dấu, số, _; bắt đầu bằng chữ hoặc số."); return false; }
      const body = { type: curType(), id, name: nameIn.value.trim() };
      if (preset.value === "custom") { body.width = Number(wIn.value); body.height = Number(hIn.value); }
      else { const [w, hh] = preset.value.split("x").map(Number); body.width = w; body.height = hh; }
      try { await api.post("/api/templates", body); } catch (e) { (e.code === "TEMPLATE_ID_EXISTS" || e.code === "BAD_ID" ? idF : nameF).setError([e.message, e.hint].filter(Boolean).join(" ")); return false; }
      toast({ title: "Đã tạo bản nháp", message: "Chỉnh bố cục rồi Lưu, Kiểm tra, Render thử và Publish.", tone: "done" });
      navigate(`/templates/${id}`);
    } }] });
  }

  // ---------- Nhân bản ----------
  function openDuplicate(r) {
    const takenIds = rows.map((x) => x.id);
    const nameIn = input({ value: `${r.name || r.id} Copy` });
    const idIn = input({ value: uniqueId(slug(`${r.id}_copy`), takenIds) });
    let idTouched = false;
    const nameF = field({ label: "Tên bản sao", control: nameIn, required: true });
    const idF = field({ label: "Mã template mới", control: idIn });
    nameIn.addEventListener("input", () => { if (!idTouched) idIn.value = uniqueId(slug(nameIn.value), takenIds); });
    idIn.addEventListener("input", () => { idTouched = true; idF.setError(null); });
    openDialog({ title: `Nhân bản “${r.name || r.id}”`, content: h("div", { class: "stack" }, nameF, idF), describe: "Bản sao là bản nháp của bạn; template gốc không đổi.",
      actions: [{ label: "Huỷ", value: null }, { label: "Nhân bản và mở", kind: "primary", value: "ok", onClick: async () => {
        const id = idIn.value.trim();
        if (!validTemplateId(id)) { idF.setError("Mã không hợp lệ: chữ thường không dấu, số, _ (2–48 ký tự)."); return false; }
        try { await api.post(`/api/templates/${r.id}/duplicate`, { new_id: id, name: nameIn.value.trim() || null }); } catch (e) { idF.setError([e.message, e.hint].filter(Boolean).join(" ")); return false; }
        toast({ title: "Đã nhân bản", tone: "done" });
        navigate(`/templates/${id}`);
      } }] });
  }

  async function archive(r, button) {
    const used = r.used_by?.length ? ` ${r.used_by.length} kênh đang chọn template này; job mới của các kênh đó sẽ báo lỗi cho tới khi họ chọn template khác.` : "";
    if (!(await confirmDialog({ title: `Lưu trữ “${r.name || r.id}”?`, body: `Template sẽ không còn được đề xuất cho kênh. Job đã tạo vẫn dùng đúng version cũ.${used}`, confirmLabel: "Lưu trữ", danger: true }))) return;
    await busy(button, async () => {
      try {
        const res = await api.post(`/api/templates/${r.id}/archive`, {});
        toast({ title: "Đã lưu trữ template", message: res.warning || undefined, tone: res.warning ? "wait" : "done", sticky: !!res.warning });
        await load();
      } catch (e) { toastError(e, "Chưa lưu trữ được"); }
    });
  }

  await load();
  return { destroy() { alive = false; } };
}
