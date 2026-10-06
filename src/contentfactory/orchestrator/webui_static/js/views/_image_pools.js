// Nguồn Media → Ảnh thumbnail (Image Pool, Phase 8): thư mục ảnh làm nguồn cho thumbnail. Template giữ bố cục; pool chỉ cấp ảnh.
// Frontend chỉ hiển thị/ra lệnh: quét, chọn ảnh, chốt cho job đều ở backend. Ảnh xem thử đi qua /api/image-pools/<tên>/images/<chỉ số> (không có đường dẫn máy).
import { api, forgetBlob } from "../api.js";
import { h, clear } from "../dom.js";
import { btn, busy, field, input, select, alertBox, emptyState, errorState, skeleton, toast, toastError, confirmDialog, openDialog, badge } from "../components.js";
import { createPoller } from "../poller.js";
import { relTime, shortPath } from "../format.js";

const STATE = { ready: { tone: "done", icon: "check-circle", label: "Sẵn sàng" }, problem: { tone: "fail", icon: "x-circle", label: "Có vấn đề" } };

export async function mount(root, ctx) {
  let data = null, sig = "", failedOnce = false;
  const shown = new Map();                                                      // tên pool -> Set đường dẫn ảnh xem thử đã tải (để thu hồi blob)
  const scans = new Map();                                                      // tên pool -> kết quả quét gần nhất: vẽ lại thẻ không làm mất ảnh xem thử
  const notice = h("div", { class: "stack", "aria-live": "polite" });
  const body = h("div", { class: "stack" });
  const addBtn = btn({ label: "Thêm pool ảnh", icon: "plus", onClick: () => poolDialog() });
  root.append(h("div", { class: "row spread" },
    h("p", { class: "muted" }, "Thư mục ảnh (jpg/png/webp) làm nguồn ảnh cho thumbnail. Mỗi job được chốt MỘT ảnh lúc tạo — retry luôn dùng đúng ảnh đó; bố cục vẫn do Template quyết định."), addBtn),
    notice, body);
  body.append(skeleton(2));

  const poller = createPoller(async (signal) => {
    try { data = await api.get("/api/image-pools", { signal }); failedOnce = false; }
    catch (e) {
      if (e.name === "AbortError") throw e;
      if (!failedOnce) { clear(body); body.append(errorState(e, () => poller.poke())); failedOnce = true; }
      throw e;
    }
    paint();
    return "idle";
  }, { fast: 4000, idle: 10000 });

  function paint() {
    const s = JSON.stringify(data, (k, v) => (k === "scanned_at" ? undefined : v));      // thời điểm quét đổi mỗi lần hỏi: không tính vào việc "có đổi gì không"
    if (s === sig) return;
    sig = s;
    clear(body);
    if (!data.pools.length) {
      body.append(emptyState({ icon: "image", title: "Chưa có pool ảnh thumbnail nào", text: "Tạo một pool trỏ tới thư mục ảnh, rồi chọn pool đó trong cấu hình Kênh (mục Thumbnail). Chưa có pool thì thumbnail dùng ảnh/bố cục như trước.",
        action: btn({ label: "Thêm pool ảnh", icon: "plus", kind: "primary", onClick: () => poolDialog() }) }));
      return;
    }
    for (const p of data.pools) body.append(card(p));
  }

  function card(p) {
    const modeLabel = Object.fromEntries(data.modes.map((m) => [m.id, m]));
    const modeSel = select({ options: data.modes.map((m) => [m.id, m.label]), value: p.selection_mode, onChange: (v) => saveMode(p, v, modeSel) });
    const preview = h("div", { class: "ip-grid", "aria-live": "polite" });
    const used = p.used_by.length ? p.used_by.map((u) => h("a", { href: `#/channels/${u.channel}`, class: "chip" }, u.name)) : [h("span", { class: "muted" }, "Chưa kênh nào dùng")];
    const delBtn = btn({ label: "Xoá", icon: "trash", size: "sm", kind: "ghost danger", ariaLabel: `Xoá pool ${p.name}`, disabled: p.used_by.length > 0, onClick: (e) => removePool(p, e.currentTarget) });
    const card = h("section", { class: "card stack ip-card", "aria-label": `Pool ảnh ${p.name}` },
      h("div", { class: "row spread" },
        h("div", { class: "row" }, h("h2", { class: "h3" }, p.name), badge(STATE[p.state] || STATE.problem)),
        h("div", { class: "row" },
          btn({ label: "Quét & xem ảnh", icon: "refresh", size: "sm", onClick: (e) => scan(p, preview, e.currentTarget) }),
          btn({ label: "Sửa", icon: "settings", size: "sm", kind: "ghost", ariaLabel: `Sửa pool ${p.name}`, onClick: () => poolDialog(p) }), delBtn)),
      h("p", { class: "mono small", title: p.folder }, shortPath(p.folder, 70)),
      h("div", { class: "row" },
        h("span", null, h("strong", null, String(p.valid)), " ảnh hợp lệ"),
        p.invalid ? h("span", { class: "chip warn" }, `${p.invalid} file không hợp lệ`) : null,
        p.scanned_at ? h("span", { class: "muted small" }, `Quét ${relTime(p.scanned_at)}`) : null),
      p.problems.length ? alertBox({ tone: "fail", title: p.problems[0], body: "Job dùng pool này sẽ bị từ chối cho tới khi sửa (để không âm thầm dùng ảnh khác).", actions: [btn({ label: "Sửa thư mục", size: "sm", onClick: () => poolDialog(p) })] }) : null,
      p.capacity_note ? h("p", { class: "small muted" }, p.capacity_note) : null,
      ...p.warnings.filter((w) => !p.problems.length || !w.startsWith("Chưa có ảnh")).map((w) => h("p", { class: "small muted" }, "⚠ " + w)),
      h("div", { class: "field" }, h("label", { for: `ip-mode-${p.name}` }, "Cách chọn ảnh cho mỗi job"), modeSel, h("div", { class: "hint" }, modeLabel[p.selection_mode]?.help || "")),
      h("div", { class: "row" }, h("span", { class: "muted small" }, "Kênh đang dùng:"), ...used),
      p.used_by.length ? h("p", { class: "small muted" }, "Xoá bị khoá khi còn kênh dùng pool: bỏ chọn pool trong cấu hình kênh trước.") : null,
      preview);
    modeSel.id = `ip-mode-${p.name}`;
    if (scans.has(p.name)) showScan(p, preview, scans.get(p.name));
    return card;
  }

  async function scan(p, host, button) {
    await busy(button, async () => {
      try {
        const r = await api.post(`/api/image-pools/${encodeURIComponent(p.name)}/scan`, {});
        scans.set(p.name, r);
        showScan(p, host, r);
      } catch (e) { toastError(e, "Không quét được pool"); }
    });
  }

  function showScan(p, host, r) {
    for (const u of shown.get(p.name) || []) forgetBlob(u);
    const paths = new Set();
    shown.set(p.name, paths);
    clear(host);
    if (!r.images.length) host.append(h("p", { class: "muted" }, "Không có ảnh hợp lệ để xem."));
    for (const im of r.images) {
      const path = `/api/image-pools/${encodeURIComponent(p.name)}/images/${im.index}`;
      const fig = h("figure", { class: "ip-fig" }, h("div", { class: "ip-img skeleton" }), h("figcaption", { class: "small muted", title: im.rel }, `${shortPath(im.rel, 22)} · ${im.width}×${im.height}`));
      host.append(fig);
      api.blobUrl(path, { fresh: true }).then((u) => { paths.add(path); fig.firstChild.replaceWith(h("img", { class: "ip-img", src: u, alt: `Ảnh mẫu ${im.rel}`, loading: "lazy" })); })
        .catch(() => { fig.firstChild.replaceWith(h("div", { class: "ip-img ip-broken" }, "Không tải được")); });
    }
    if (r.invalid_files.length) host.append(h("details", { class: "ip-invalid" }, h("summary", null, `${r.invalid_files.length} file không hợp lệ`),
      h("ul", { class: "autolist" }, ...r.invalid_files.map((f) => h("li", null, h("span", { class: "mono" }, f.rel), ` — ${f.problem}`)))));
  }

  async function saveMode(p, mode, el) {
    el.disabled = true;
    try { await api.put(`/api/image-pools/${encodeURIComponent(p.name)}`, { folder: p.folder, selection_mode: mode }); toast({ title: "Đã đổi cách chọn ảnh", message: "Áp dụng cho job tạo từ giờ; job đã chốt ảnh giữ nguyên.", tone: "done" }); }
    catch (e) { toastError(e, "Chưa lưu được"); el.value = p.selection_mode; }
    el.disabled = false;
    sig = ""; poller.poke();
  }

  async function poolDialog(p) {
    const edit = !!p;
    const nameIn = input({ placeholder: "anime_female", value: p?.name || "", disabled: edit });
    const dirIn = input({ placeholder: "D:\\anh\\anime_nu", value: p?.folder || "" });
    const pickBtn = btn({ label: "Chọn thư mục…", icon: "folder", onClick: (e) => busy(e.currentTarget, async () => {
      try {
        const r = await api.post("/api/pick", { kind: "folder", title: "Chọn thư mục ảnh" });
        if (r.unsupported) toast({ title: "Máy không có hộp thoại chọn thư mục", message: r.message, tone: "wait" });
        else if (r.path) dirIn.value = r.path;
      } catch (err) { toastError(err); }
    }) });
    const modeSel = select({ options: data.modes.map((m) => [m.id, m.label]), value: p?.selection_mode || data.default_mode });
    const help = h("div", { class: "hint", "aria-live": "polite" });
    const setHelp = () => { help.textContent = data.modes.find((m) => m.id === modeSel.value)?.help || ""; };
    modeSel.addEventListener("change", setHelp);
    setHelp();
    const nameF = field({ label: "Tên pool", control: nameIn, hint: edit ? "Không đổi được tên; xoá rồi tạo lại nếu cần." : "Chữ không dấu, số, _ và -.", required: !edit });
    const dirF = field({ label: "Thư mục ảnh", control: dirIn, hint: "Chỉ lấy file jpg/png/webp (kiểm theo nội dung), tối đa 2 cấp thư mục con; không theo symlink.", required: true });
    const content = h("div", { class: "stack" }, nameF, h("div", { class: "stack" }, dirF, h("div", null, pickBtn)), field({ label: "Cách chọn ảnh", control: modeSel }), help);
    const r = await openDialog({ title: edit ? `Sửa pool ảnh “${p.name}”` : "Thêm pool ảnh thumbnail", content, actions: [{ label: "Huỷ", value: null }, { label: "Lưu pool", kind: "primary", value: "ok", onClick: async () => {
      nameF.setError(null); dirF.setError(null);
      if (!/^[\w-]{1,40}$/.test(nameIn.value.trim())) { nameF.setError("Tên pool chỉ gồm chữ không dấu, số, _ và -, tối đa 40 ký tự."); return false; }
      if (!dirIn.value.trim()) { dirF.setError("Chọn hoặc dán đường dẫn thư mục ảnh."); return false; }
      try { await api.put(`/api/image-pools/${encodeURIComponent(nameIn.value.trim())}`, { folder: dirIn.value.trim(), selection_mode: modeSel.value }); return true; }
      catch (e) { (e.code === "INVALID_POOL_NAME" ? nameF : dirF).setError([e.message, e.hint].filter(Boolean).join(" ")); return false; }
    } }] });
    if (r !== "ok") return;
    toast({ title: "Đã lưu pool ảnh", message: "Chọn pool này trong cấu hình Kênh để job dùng nó.", tone: "done" });
    sig = ""; poller.poke();
  }

  async function removePool(p, button) {
    if (!(await confirmDialog({ title: `Xoá pool ảnh “${p.name}”?`, body: "Chỉ bỏ cấu hình pool; ảnh gốc trong thư mục KHÔNG bị xoá. Job đã chốt ảnh giữ nguyên ảnh của chúng.", confirmLabel: "Xoá pool", danger: true }))) return;
    await busy(button, async () => {
      try { await api.del(`/api/image-pools/${encodeURIComponent(p.name)}`); toast({ title: "Đã xoá pool ảnh", tone: "done" }); }
      catch (e) { toastError(e, "Không xoá được pool"); }
      sig = ""; poller.poke();
    });
  }

  poller.start();
  return { destroy() { poller.stop(); for (const urls of shown.values()) for (const u of urls) forgetBlob(u); shown.clear(); scans.clear(); } };
}
