// Kênh: danh sách bên trái, form có cấu trúc bên phải (không bắt người dùng sửa JSON). Form giữ nguyên các khóa lạ của file (thumbnail, _doc…).
import { api } from "../api.js";
import { h, clear, loadCss } from "../dom.js";
import { icon } from "../icons.js";
import { btn, busy, field, input, select, alertBox, emptyState, errorState, skeleton, pageHead, toast, confirmDialog, openDialog, disclosure } from "../components.js";
import * as motion from "../motion.js";

// ---------- tiện ích đường dẫn trên model (xóa khóa rỗng, dọn object rỗng) ----------
const clone = (o) => JSON.parse(JSON.stringify(o ?? {}));
const canon = (v) => (Array.isArray(v) ? v.map(canon) : v && typeof v === "object" ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, canon(v[k])])) : v);
const canonStr = (o) => JSON.stringify(canon(o));
const getp = (o, p) => p.split(".").reduce((a, k) => (a && typeof a === "object" ? a[k] : undefined), o);
function setp(o, p, v) {
  const keys = p.split(".");
  const chain = [o];
  let cur = o;
  const empty = v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length);
  for (const k of keys.slice(0, -1)) {
    if (!cur[k] || typeof cur[k] !== "object") { if (empty) return; cur[k] = {}; }
    cur = cur[k];
    chain.push(cur);
  }
  const last = keys[keys.length - 1];
  if (empty) delete cur[last]; else cur[last] = v;
  for (let i = chain.length - 1; i > 0; i--) { if (Object.keys(chain[i]).length === 0) delete chain[i - 1][keys[i - 1]]; else break; }
}
const withCurrent = (opts, cur, tag = "") => (cur && !opts.some(([v]) => v === cur) ? [...opts, [cur, `${cur}${tag}`]] : opts);
const csv = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
const AUDIO_EXT = /\.(wav|mp3|m4a|flac|ogg|aac)$/i;
const YT_RES = [["", "Mặc định của hệ thống"], ["1920x1080", "1920×1080 (Full HD)"], ["1280x720", "1280×720 (HD)"]];
const TT_RES = [["", "Mặc định của hệ thống"], ["1080x1920", "1080×1920 (Full HD dọc)"], ["720x1280", "720×1280 (HD dọc)"]];

export async function mount(root, ctx) {
  loadCss("/css/channels.css");
  const { app, navigate, scope, params } = ctx;
  let channels = [], editor = null, selected = params[0] || null, listAnimated = false;

  const listHost = h("div", { class: "stack" });
  const editorHost = h("div", { class: "stack chan-editor" });
  const newBtn = btn({ label: "Kênh mới", icon: "plus", onClick: () => openCreate() });
  root.append(pageHead("Kênh", "Mỗi kênh nhớ tên, watermark, giọng đọc, video nền và cách đăng. Chỉnh một lần, dùng mãi.", newBtn),
    h("div", { class: "split" }, h("nav", { "aria-label": "Danh sách kênh" }, listHost), editorHost));
  listHost.append(skeleton(3));

  // ---- chặn rời trang khi còn thay đổi chưa lưu (liên kết trong ứng dụng + đóng tab) ----
  const guardClick = (e) => {
    if (!editor?.dirty()) return;
    const a = e.target.closest?.('a[href^="#"]');
    if (!a || a.getAttribute("href") === location.hash) return;
    e.preventDefault(); e.stopImmediatePropagation();
    confirmDialog({ title: "Bỏ thay đổi chưa lưu?", body: "Bạn có thay đổi chưa lưu ở kênh này. Rời đi sẽ mất chúng.", confirmLabel: "Rời đi, bỏ thay đổi", danger: true })
      .then((ok) => { if (ok) { editor.discard(); location.hash = a.getAttribute("href").slice(1); } });
  };
  const guardUnload = (e) => { if (editor?.dirty()) { e.preventDefault(); e.returnValue = ""; } };
  document.addEventListener("click", guardClick, true);
  window.addEventListener("beforeunload", guardUnload);

  async function load() {
    try {
      const r = await api.get("/api/channels");
      channels = r.channels;
      app.boot.channels = r.channels;
      if (!selected || !channels.some((c) => c.id === selected)) selected = (channels.find((c) => c.id === r.default) || channels[0])?.id || null;
      paintList();
      if (selected) {
        if (!params[0] || params[0] !== selected) history.replaceState(null, "", `#/channels/${selected}`);
        openEditor(selected);
      } else paintEmpty();
    } catch (e) { clear(listHost); listHost.append(errorState(e, load)); }
  }

  function paintList() {
    clear(listHost);
    if (!channels.length) { listHost.append(h("p", { class: "muted small" }, "Chưa có kênh nào.")); return; }
    const ul = h("ul", { class: "list-pick" });
    for (const c of channels) {
      const b = h("button", { type: "button", "aria-current": String(c.id === selected) },
        h("span", { class: "chan-name" }, c.name || c.id),
        h("span", { class: "muted small" }, c.ok ? `${c.id} · ${c.privacy || "private"}` : `${c.id} · lỗi cấu hình`));
      b.addEventListener("click", () => select(c.id));
      ul.append(h("li", null, b));
    }
    listHost.append(ul);
    if (!listAnimated) { listAnimated = true; scope.add(() => motion.itemsEnter([...ul.children])); }
  }

  async function select(id) {
    if (id === selected) return;
    if (editor?.dirty() && !(await confirmDialog({ title: "Bỏ thay đổi chưa lưu?", body: "Chuyển kênh sẽ mất các thay đổi chưa lưu.", confirmLabel: "Chuyển, bỏ thay đổi", danger: true }))) return;
    selected = id;
    history.replaceState(null, "", `#/channels/${id}`);
    paintList();
    openEditor(id);
  }

  function paintEmpty() {
    clear(editorHost);
    editorHost.append(emptyState({ icon: "tv", title: "Chưa có kênh nào", text: "Tạo kênh để hệ thống biết đặt tên, watermark, giọng đọc và video nền cho bạn.", action: btn({ label: "Tạo kênh", icon: "plus", kind: "primary", onClick: () => openCreate() }) }));
  }

  function openEditor(id) {
    editor?.destroy();
    clear(editorHost);
    editor = createEditor(editorHost, id, {
      scope,
      onSaved: (name) => { const c = channels.find((x) => x.id === id); if (c) { c.name = name || id; paintList(); } },
    });
  }

  async function openCreate() {
    const idIn = input({ placeholder: "kenh_a" });
    const nameIn = input({ placeholder: "Tên hiển thị" });
    const kids = h("div", { class: "radio-row" },
      h("label", null, h("input", { type: "radio", name: "nk", value: "no", checked: true }), "Không dành cho trẻ em"),
      h("label", null, h("input", { type: "radio", name: "nk", value: "yes" }), "Dành cho trẻ em"));
    const idF = field({ label: "Mã kênh", control: idIn, hint: "Chữ không dấu, số, _ và -.", required: true });
    const content = h("div", { class: "stack" }, idF, field({ label: "Tên hiển thị", control: nameIn }),
      h("div", { class: "field" }, h("div", { class: "label" }, "Video dành cho trẻ em? (khai báo bắt buộc)"), kids));
    if (editor?.dirty() && !(await confirmDialog({ title: "Bỏ thay đổi chưa lưu?", body: "Tạo kênh mới sẽ chuyển khỏi kênh đang sửa.", confirmLabel: "Tiếp tục", danger: true }))) return;
    const r = await openDialog({ title: "Tạo kênh mới", content, actions: [{ label: "Huỷ", value: null }, { label: "Tạo kênh", kind: "primary", value: "ok", onClick: async () => {
      try {
        await api.post("/api/channels", { id: idIn.value.trim(), name: nameIn.value.trim() || idIn.value.trim(), kids: content.querySelector("input[name=nk]:checked").value === "yes" });
        return true;
      } catch (e) { idF.setError(e.message); return false; }
    } }] });
    if (r !== "ok") return;
    editor?.discard();
    toast({ title: "Đã tạo kênh", message: "Chỉnh watermark, giọng đọc, video nền ở form bên cạnh.", tone: "done" });
    selected = idIn.value.trim();
    history.replaceState(null, "", `#/channels/${selected}`);
    await load();
  }

  await load();
  return { destroy() { document.removeEventListener("click", guardClick, true); window.removeEventListener("beforeunload", guardUnload); editor?.destroy(); } };
}

// ================================================================================== editor
function createEditor(host, id, { scope, onSaved }) {
  let model = {}, base = "", data = null, alive = true, previewTitle = "Tên truyện mẫu", previewTimer = null, previewSeq = 0;
  const invalid = new Set();
  let formHost, saveBtn, savedNote, errHost, previewBox, rawArea, rawErr;

  host.append(skeleton(5));
  load();

  async function load() {
    try {
      data = await api.get(`/api/channels/${id}`);
      if (!alive) return;
      model = clone(data.raw);
      base = canonStr(model);
      build();
    } catch (e) {
      if (!alive) return;
      clear(host);
      host.append(errorState(e, () => { clear(host); host.append(skeleton(5)); load(); }));
    }
  }

  const dirty = () => !!data && canonStr(model) !== base;
  function changed() {
    savedNote.textContent = "";
    updateDirty();
    if (rawArea && document.activeElement !== rawArea) rawArea.value = JSON.stringify(model, null, 2);
  }
  function updateDirty() {
    const d = dirty();
    saveBtn.disabled = !d || invalid.size > 0;
    saveBtn.title = invalid.size ? "Sửa các ô đang báo lỗi trước khi lưu." : d ? "" : "Chưa có thay đổi.";
    host.dataset.dirty = d ? "1" : "";
  }

  // ---- ô nhập gắn với model ----
  function text(path, { label, hint, textarea = false, placeholder, required = false }) {
    const ctl = textarea ? h("textarea", { class: "textarea chan-text", rows: 3 }) : input({ placeholder });
    ctl.value = getp(model, path) ?? "";
    const f = field({ label, hint, control: ctl, required });
    ctl.addEventListener("input", () => {
      const v = ctl.value;
      if (required && !v.trim()) { f.setError("Không được để trống."); invalid.add(path); } else { f.setError(null); invalid.delete(path); setp(model, path, v); }
      changed();
    });
    return f;
  }
  function num(path, { label, hint, min, max, step = "1", int = false, scale = 1, placeholder }) {
    const ctl = input({ type: "number", min, max, step, inputmode: "decimal", placeholder });
    const cur = getp(model, path);
    ctl.value = cur == null ? "" : String(cur / scale);
    const f = field({ label, hint, control: ctl });
    ctl.addEventListener("input", () => {
      if (ctl.value === "") { f.setError(null); invalid.delete(path); setp(model, path, undefined); return changed(); }
      const n = Number(ctl.value);
      const bad = !Number.isFinite(n) || (min != null && n < min) || (max != null && n > max) || (int && !Number.isInteger(n));
      if (bad) { f.setError(`Nhập ${int ? "số nguyên" : "số"}${min != null ? ` từ ${min}` : ""}${max != null ? ` đến ${max}` : ""}.`); invalid.add(path); }
      else { f.setError(null); invalid.delete(path); setp(model, path, Math.round(n * scale * 1000) / 1000); }
      changed();
    });
    return f;
  }
  function pick(path, { label, hint, options, empty, id: cid }) {
    const cur = getp(model, path) ?? "";
    const sel = select({ options: empty != null ? [["", empty], ...options] : options, value: cur, id: cid });
    sel.addEventListener("change", () => { setp(model, path, sel.value); changed(); });
    return field({ label, hint, control: sel });
  }
  function list(path, { label, hint, placeholder }) {
    const ctl = input({ placeholder });
    ctl.value = (getp(model, path) || []).join(", ");
    ctl.addEventListener("input", () => { setp(model, path, csv(ctl.value)); changed(); });
    return field({ label, hint, control: ctl });
  }

  const sec = (title, ...kids) => h("section", { class: "form-section", "aria-label": title }, h("h3", null, title), ...kids);

  function build() {
    clear(host);
    invalid.clear();
    const o = data.options;
    errHost = h("div", { "aria-live": "polite" });
    savedNote = h("span", { class: "muted small", "aria-live": "polite" });
    saveBtn = btn({ label: "Lưu thay đổi", icon: "check", kind: "primary", onClick: () => save() });
    formHost = h("div", { class: "stack" });
    previewBox = h("div", { class: "preview-box", "aria-live": "polite" });
    rawArea = h("textarea", { class: "textarea", rows: 14, spellcheck: false, "aria-label": "JSON đầy đủ của kênh" });
    rawErr = h("div", { class: "field" }, h("div", { class: "error", role: "alert" }));
    rawArea.value = JSON.stringify(model, null, 2);

    const pools = o.pools.map((p) => [p, p]);
    const ttsOpts = withCurrent(o.tts_profiles.map((p) => [p.name, `${p.name} — ${p.engine || "?"} (${p.status || "?"})`]), getp(model, "preset.tts_profile"), " (không còn trong tts_profiles/)");
    const poolOpts = (path) => withCurrent(pools, getp(model, path), " (không còn trong cấu hình)");
    const priv = o.privacy.map((p) => [p, { private: "Riêng tư (private)", unlisted: "Không công khai (unlisted)", public: "Công khai (public)" }[p] || p]);

    formHost.append(
      sec("Cơ bản", text("name", { label: "Tên kênh", hint: "Hiện trên thumbnail và trong mô tả.", required: true }),
        h("div", { class: "grid-2" },
          text("title_template", { label: "Mẫu tiêu đề YouTube", hint: "Biến: {sequence} {project_title} {channel_name}. Để trống = dùng mẫu mặc định.", textarea: false, placeholder: data.channel.title_template }),
          num("sequence.last_used", { label: "Số Full Audio đã đăng", placeholder: "0", hint: "Số tập đã đăng trước đó; job kế tiếp lấy số +1.", min: 0, int: true })),
        text("description_template", { label: "Mẫu mô tả YouTube", hint: "Cùng các biến như trên. Để trống = dùng mẫu mặc định.", textarea: true, placeholder: data.channel.description_template })),
      sec("Watermark", watermarkBlock()),
      sec("Giọng đọc & render",
        pick("preset.tts_profile", { label: "Giọng đọc (TTS profile)", empty: "Tự chọn (khuyên dùng)", options: ttsOpts, hint: "Tự chọn: hệ thống chọn profile hợp ngôn ngữ nhất." }),
        h("div", { class: "grid-2" },
          pick("preset.pools.youtube", { label: "Video nền cho YouTube", empty: "Tự chọn theo hướng khung hình", options: poolOpts("preset.pools.youtube") }),
          pick("preset.pools.tiktok", { label: "Video nền cho TikTok", empty: "Tự chọn theo hướng khung hình", options: poolOpts("preset.pools.tiktok") })),
        h("div", { class: "grid-2" },
          pick("preset.render.youtube.resolution", { label: "Độ phân giải video YouTube", options: withCurrent(YT_RES, getp(model, "preset.render.youtube.resolution")) }),
          num("preset.render.youtube.fps", { label: "FPS video YouTube (tuỳ chọn)", hint: "Để trống = mặc định.", min: 1, max: 120, int: true })),
        h("div", { class: "grid-2" },
          pick("preset.render.tiktok.resolution", { label: "Độ phân giải video TikTok", options: withCurrent(TT_RES, getp(model, "preset.render.tiktok.resolution")) }),
          num("preset.render.tiktok.fps", { label: "FPS video TikTok (tuỳ chọn)", hint: "Để trống = mặc định.", min: 1, max: 120, int: true })),
        h("div", { class: "grid-2" },
          num("preset.tiktok.speed", { label: "Tốc độ audio TikTok", hint: "1–3, giữ nguyên cao độ. Trống = theo cài đặt chung.", min: 1, max: 3, step: "0.1" }),
          num("preset.tiktok.target_part_sec", { label: "Độ dài mỗi part TikTok (phút)", hint: "Hệ thống cắt ở ranh giới câu/đoạn gần nhất. Trống = theo cài đặt chung.", min: 0.25, max: 30, step: "0.25", scale: 60 }))),
      sec("Đăng YouTube",
        h("div", { class: "grid-2" },
          pick("publishing.privacy", { label: "Chế độ đăng", options: priv, empty: "Mặc định (private)", hint: "Nên để private cho tới khi bạn tin tưởng pipeline." }),
          text("publishing.account_id", { label: "Tài khoản upload (account_id)", hint: "Để trống = tài khoản mặc định của yt-uploader." })),
        h("div", { class: "grid-2" }, list("publishing.tags", { label: "Tags", hint: "Cách nhau bằng dấu phẩy." }), list("publishing.playlists", { label: "Playlist", hint: "Cách nhau bằng dấu phẩy." })),
        kidsBlock(),
        h("p", { class: "muted small" }, "Tự đăng và Auto Resume hiện đặt theo từng job / cài đặt chung, chưa đặt riêng cho kênh.")),
      sec("Xem trước", previewBlock()),
      sec("Nâng cao", disclosure({ label: "Sửa JSON thô của kênh", content: rawBlock() })));

    const bar = h("div", { class: "row chan-savebar" }, saveBtn, savedNote);
    host.append(h("div", { class: "card stack" }, h("div", { class: "card-title" }, h("h2", { id: "page-title-ch" }, model.name || id), h("span", { class: "muted small mono" }, id)), errHost, formHost, bar));
    updateDirty();
    scope.add(() => motion.itemsEnter([...formHost.children]));
    refreshPreview(0);
  }

  // ---- watermark ----
  function watermarkBlock() {
    const cur = h("p", null);
    const paint = () => {
      const w = model.watermark;
      cur.replaceChildren(icon("mic", { size: 16 }), " ", w ? h("strong", null, String(w).split(/[\\/]/).pop()) : h("span", { class: "muted" }, "Chưa có watermark"));
    };
    paint();
    const audio = (data.assets || []).filter((a) => AUDIO_EXT.test(a));
    const sel = select({ options: [["", "— chọn file có sẵn trong thư mục kênh —"], ...audio.map((a) => [a, a])], value: audio.includes(model.watermark) ? model.watermark : "" });
    const refillSel = (name) => { if (name && !audio.includes(name)) { audio.push(name); sel.append(h("option", { value: name }, name)); } sel.value = name || ""; };
    sel.addEventListener("change", () => { if (sel.value) { model.watermark = sel.value; paint(); changed(); } });
    const fileIn = h("input", { type: "file", accept: "audio/*,.wav,.mp3,.m4a,.flac", class: "input", id: "wm-file" });
    const upErr = h("div", { class: "error", role: "alert" });
    const upBtn = btn({ label: "Tải lên", icon: "upload", disabled: true });
    fileIn.addEventListener("change", () => { upErr.textContent = ""; upBtn.disabled = !fileIn.files.length; });
    upBtn.addEventListener("click", () => busy(upBtn, async () => {
      const f = fileIn.files[0];
      if (!f) return;
      if (f.size > 60 * 1024 * 1024) { upErr.textContent = "File quá lớn (tối đa 60 MB)."; return; }
      try {
        const r = await api.upload(`/api/channels/${id}/asset?name=${encodeURIComponent(f.name)}`, await f.arrayBuffer());
        model.watermark = r.name;
        refillSel(r.name);
        fileIn.value = "";
        paint();
        changed();
        toast({ title: "Đã tải watermark lên", message: "Bấm Lưu để áp dụng cho kênh.", tone: "done" });
      } catch (e) { upErr.textContent = [e.message, e.hint].filter(Boolean).join(" "); }
    }));
    const clearBtn = btn({ label: "Bỏ watermark", icon: "x", kind: "ghost", size: "sm", onClick: () => { delete model.watermark; sel.value = ""; paint(); changed(); } });
    return h("div", { class: "stack" }, cur,
      field({ label: "Chọn từ thư mục kênh", control: sel }),
      h("div", { class: "field" }, h("label", { for: "wm-file" }, "Hoặc tải file audio mới"), h("div", { class: "input-row" }, fileIn, upBtn), upErr,
        h("div", { class: "hint" }, "Watermark là audio ngắn gắn vào cuối bản YouTube; âm lượng tự cân với giọng đọc.")),
      h("div", null, clearBtn));
  }

  // ---- khai báo trẻ em ----
  function kidsBlock() {
    const name = `kids-${id}`;
    const cur = getp(model, "publishing.made_for_kids");
    const mk = (v, label) => h("label", null, h("input", { type: "radio", name, value: v, checked: (cur === undefined && v === "") || String(cur) === v, onChange: () => { setp(model, "publishing.made_for_kids", v === "" ? undefined : v === "true"); changed(); } }), label);
    return h("div", { class: "field", role: "radiogroup", "aria-labelledby": "kids-l" },
      h("div", { class: "label", id: "kids-l" }, "Video có dành cho trẻ em không?"),
      h("div", { class: "radio-row" }, mk("", "Chưa khai"), mk("false", "Không dành cho trẻ em"), mk("true", "Dành cho trẻ em")),
      h("div", { class: "hint" }, "Đây là khai báo pháp lý của YouTube (COPPA), hệ thống không bao giờ tự đoán. Chưa khai thì job tới bước đăng sẽ hỏi lại."));
  }

  // ---- xem trước ----
  function previewBlock() {
    const tIn = input({ value: previewTitle, placeholder: "Tên truyện mẫu" });
    tIn.addEventListener("input", () => { previewTitle = tIn.value; refreshPreview(400); });
    return h("div", { class: "stack" }, field({ label: "Tên truyện để xem thử", control: tIn }), previewBox,
      h("p", { class: "muted small" }, "Xem trước dùng cấu hình đã LƯU của kênh; lưu để thấy thay đổi mới."));
  }
  function refreshPreview(ms) {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      const my = ++previewSeq;
      try {
        const p = await api.get(`/api/channels/${id}/preview`, { query: { title: previewTitle } });
        if (my !== previewSeq || !alive) return;
        previewBox.replaceChildren(
          h("div", { class: "small muted" }, `Full Audio ${p.sequence} (tập kế tiếp) · chế độ đăng: ${p.privacy}`),
          h("div", { class: "small muted" }, "Tiêu đề YouTube"), h("div", { class: "yt-title" }, p.youtube_title),
          h("div", { class: "small muted" }, "Mô tả"), h("div", { class: "small chan-desc" }, p.description),
          h("div", { class: "small muted" }, "Chữ trên thumbnail"), h("div", { class: "chan-thumb" }, h("strong", null, p.thumbnail.channel_name), h("span", null, p.thumbnail.title)));
      } catch (e) {
        if (my !== previewSeq || !alive) return;
        previewBox.replaceChildren(alertBox({ tone: "wait", title: "Chưa xem trước được", body: [e.message, e.hint].filter(Boolean).join(" ") }));
      }
    }, ms);
  }

  // ---- JSON thô ----
  function rawBlock() {
    const apply = btn({ label: "Áp dụng vào form", icon: "check", size: "sm", onClick: () => {
      const err = rawErr.firstChild;
      try {
        const v = JSON.parse(rawArea.value);
        if (!v || typeof v !== "object" || Array.isArray(v)) throw new Error("Gốc phải là một object JSON.");
        err.textContent = "";
        model = v;
        build();
        changed();
        toast({ title: "Đã áp dụng JSON vào form", message: "Bấm Lưu để ghi vào kênh.", tone: "info" });
      } catch (e) { err.replaceChildren(icon("alert-circle", { size: 14 }), h("span", null, `JSON chưa hợp lệ: ${e.message}`)); }
    } });
    return h("div", { class: "stack" }, h("p", { class: "muted small" }, "Chỉ dành cho người rành cấu hình. Khi lưu, hệ thống luôn gửi dữ liệu của form; bấm “Áp dụng” để đưa JSON này vào form trước."), rawArea, rawErr, h("div", null, apply));
  }

  // ---- lưu ----
  async function save() {
    clear(errHost);
    await busy(saveBtn, async () => {
      try {
        await api.put(`/api/channels/${id}`, { raw: model });
        base = canonStr(model);
        savedNote.textContent = "Đã lưu.";
        toast({ title: "Đã lưu kênh", tone: "done" });
        onSaved(model.name);
        refreshPreview(0);
      } catch (e) {
        errHost.append(alertBox({ tone: "fail", title: e.code === "INVALID_CHANNEL_CONFIG" ? "Cấu hình kênh chưa hợp lệ" : e.message, body: e.code === "INVALID_CHANNEL_CONFIG" ? e.message : e.hint }));
        errHost.scrollIntoView?.({ block: "nearest" });
      }
    });
    updateDirty();
  }

  return {
    dirty,
    discard() { base = canonStr(model); if (saveBtn) updateDirty(); },
    destroy() { alive = false; clearTimeout(previewTimer); previewSeq++; },
  };
}
