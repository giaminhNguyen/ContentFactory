// Khối "Template" của trang Kênh: chọn template thumbnail / YouTube / TikTok theo TÊN. Không có toạ độ hay thông số bố cục ở đây —
// những thứ đó thuộc template (ContentFlow). Chọn được lưu NGAY (PUT /api/channels/<id>/templates) và kiểm tra phía máy chủ.
import { api } from "../api.js";
import { h, clear } from "../dom.js";
import { btn, field, input, select, alertBox, skeleton, disclosure, kv } from "../components.js";

const KEYS = [["thumbnail", "Thumbnail Template"], ["youtube_video", "YouTube Template"], ["tiktok_video", "TikTok Template"]];
const msg = (e) => [e.message, e.hint].filter(Boolean).join(" ");
const size = (c) => (c && c[0] ? `${c[0]}×${c[1]}` : "?");

// onSaved(key, ref|null): để trang Kênh cập nhật model/base (tránh form báo "chưa lưu" sai).
export function templateBlock(channelId, { onSaved }) {
  const host = h("div", { class: "stack", "aria-live": "polite" }, skeleton(2));
  let opt = null, res = null, alive = true;

  async function load() {
    try {
      [opt, res] = await Promise.all([api.get("/api/templates/options"), api.get(`/api/channels/${channelId}/templates`)]);
      if (alive) paint();
    } catch (e) { if (alive) host.replaceChildren(alertBox({ tone: "wait", title: "Chưa tải được danh sách template", body: msg(e), actions: [btn({ label: "Thử lại", size: "sm", icon: "refresh", onClick: () => { host.replaceChildren(skeleton(2)); load(); } })] })); }
  }

  async function persist(key, patch, ctl, f) {
    const cur = res[key].configured;
    const body = { key, template_id: patch.template_id !== undefined ? patch.template_id : cur?.id ?? null, version_policy: patch.version_policy ?? (patch.template_id !== undefined ? "latest_published" : cur?.version_policy) ?? "latest_published",
      fallback: patch.fallback !== undefined ? patch.fallback : patch.template_id !== undefined ? null : cur?.fallback ?? null };
    try {
      const r = await api.put(`/api/channels/${channelId}/templates`, body);
      onSaved(key, r.template);
      res = await api.get(`/api/channels/${channelId}/templates`);
      f?.setError(null);
    } catch (e) {
      f?.setError(msg(e));
      if (ctl) ctl.value = cur?.id ?? "";
      return;
    }
    paint(key);
  }

  function paint(focusKey) {
    clear(host);
    const sel = h("div", { class: "tpl-sel" });
    for (const [key, label] of KEYS) {
      const r = res[key], options = opt.options[key] || [];
      const defId = opt.defaults[key];
      const defName = options.find((o) => o.id === defId)?.name || defId;
      const cur = r.configured?.id || "";
      const opts = [["", `Mặc định: ${defName}`], ...options.map((o) => [o.id, `${o.name} · v${o.version}`])];
      if (cur && !opts.some(([v]) => v === cur)) opts.push([cur, `${cur} (không dùng được)`]);
      const ctl = select({ options: opts, value: cur });
      ctl.id = `tpl-${key}`;
      const f = field({ label, control: ctl, id: ctl.id, hint: r.is_default ? `Kênh chưa chọn: đang dùng mặc định${r.ok ? ` (v${r.version})` : ""}.` : r.ok ? `Đang dùng bản published mới nhất: v${r.version}.` : null });
      ctl.addEventListener("change", () => persist(key, { template_id: ctl.value || null }, ctl, f));
      if (!r.ok) f.setError(`${r.error.message}`);
      sel.append(f);
      if (focusKey === key) queueMicrotask(() => ctl.focus());
    }
    const adv = h("div", { class: "stack" });
    for (const [key, label] of KEYS) adv.append(advRow(key, label));
    host.append(sel, h("p", { class: "muted small" }, "Chỉ chọn tên: khung, vị trí, font… do template quyết định. Sửa hoặc tạo template ở ", h("a", { href: "#/templates" }, "trang Template"), ". Job luôn chốt đúng version lúc tạo; publish bản mới không đổi job đang có."),
      disclosure({ label: "Nâng cao: version, dự phòng, chi tiết", content: adv }));
  }

  function advRow(key, label) {
    const r = res[key], options = (opt.options[key] || []).filter((o) => o.id !== r.configured?.id);
    const configured = !!r.configured;
    const row = h("div", { class: "tpl-adv-row" }, h("h4", null, label));
    if (!r.ok) row.append(alertBox({ tone: "fail", title: `Template “${r.effective.id}” không dùng được`, body: `${r.error.message} Chọn template khác ở trên, hoặc publish một version cho template đó (Template Studio).`,
      actions: [btn({ label: "Mở Template", size: "sm", href: "#/templates" })] }));
    else row.append(kv([["Template", `${r.name} (${r.effective.id})`], ["Version đang dùng", `v${r.version}`], ["Canvas", size(r.canvas)], ["Checksum", r.checksum.slice(0, 12)]]));
    if (!configured) { row.append(h("p", { class: "muted small" }, "Đang dùng template mặc định. Chọn template ở trên để ghim version hoặc đặt dự phòng.")); return row; }
    const pol = r.configured.version_policy;
    const polSel = select({ options: [["latest_published", "Bản hiện tại của template (khuyên dùng)"], ["pin", "Ghim một version cố định"]], value: pol === "latest_published" ? "latest_published" : "pin" });
    polSel.id = `pol-${key}`;
    const pin = input({ type: "number", min: 1, step: "1", value: pol === "latest_published" ? "" : String(pol), disabled: pol === "latest_published", placeholder: "Số version" });
    pin.id = `pin-${key}`;
    const polF = field({ label: "Chính sách version", control: polSel, id: polSel.id }), pinF = field({ label: "Version ghim", control: pin, id: pin.id });
    polSel.addEventListener("change", () => {
      pin.disabled = polSel.value !== "pin";
      if (polSel.value === "latest_published") persist(key, { version_policy: "latest_published" }, null, polF);
      else { pin.value = String(r.version || 1); persist(key, { version_policy: Number(pin.value) }, null, polF); }
    });
    pin.addEventListener("change", () => {
      const n = Number(pin.value);
      if (!Number.isInteger(n) || n < 1) return pinF.setError("Nhập số version nguyên ≥ 1.");
      pinF.setError(null);
      persist(key, { version_policy: n }, null, pinF);
    });
    const fb = select({ options: [["", "Không có (báo lỗi nếu template không dùng được)"], ...options.map((o) => [o.id, o.name])], value: r.configured.fallback || "" });
    fb.id = `fb-${key}`;
    const fbF = field({ label: "Template dự phòng (tuỳ chọn)", control: fb, id: fb.id, hint: "Chỉ dùng khi template chính không dùng được. Không có dự phòng thì hệ thống báo lỗi, không tự đổi sang template khác." });
    fb.addEventListener("change", () => persist(key, { fallback: fb.value || null }, null, fbF));
    row.append(h("div", { class: "grid-2" }, polF, pinF), fbF);
    return row;
  }

  load();
  host.destroy = () => { alive = false; };
  return host;
}
