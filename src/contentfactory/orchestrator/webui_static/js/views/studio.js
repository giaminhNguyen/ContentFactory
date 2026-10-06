// Template Studio: sửa MỘT template (cùng schema mà ContentFlow render). Lớp, canvas kéo/thả, thuộc tính, Hoàn tác/Làm lại,
// Lưu nháp, Kiểm tra, Xem trước, Render thử, Publish. Bản đã publish/có sẵn chỉ xem; muốn sửa thì tạo bản nháp mới (version mới) hoặc nhân bản.
import { api, forgetBlob } from "../api.js";
import { h, clear, loadCss, uid } from "../dom.js";
import { icon } from "../icons.js";
import { btn, busy, field, input, select, switchCtl, badge, alertBox, emptyState, errorState, skeleton, pageHead, toast, toastError, confirmDialog, openDialog, disclosure } from "../components.js";
import * as motion from "../motion.js";
import * as L from "../templates_logic.js";
import { assetPicker, openAssetLibrary, IMAGE_ASSET_TYPES } from "../asset_lib.js";
import { statusBadges } from "./templates.js";

const KIND = { image: "Hình ảnh", photo: "Ảnh nhân vật", source_video: "Video nguồn", text: "Chữ" };
const KIND_ICON = { image: "image", photo: "image", source_video: "film", text: "file" };
const SRC_LABEL = { "channel.name": "Tên kênh", "project.title": "Tiêu đề truyện" };
const SAMPLE_TEXT = { "channel.name": "Truyện Audio", "project.title": "Tiêu đề truyện mẫu" };
const HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
const FONTS = ["arialbd.ttf", "arial.ttf", "segoeuib.ttf", "tahomabd.ttf", "calibrib.ttf", "verdanab.ttf", "timesbd.ttf"];
const COLOR = /^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/;
const layerName = (e) => (e.type === "text" ? `${SRC_LABEL[e.source] || "Chữ"}` : KIND[e.type] || e.type);

export async function mount(root, ctx) {
  loadCss("/css/templates.css");
  const { params, query, navigate, scope, app } = ctx;
  const tid = params[0];
  let doc = null, base = null, meta = null, alive = true, selected = null, zoom = "fit", scale = 1, saving = false;
  let validation = null, issueIds = new Map(), previewUrl = null, previewKey = null, showPreview = false, testResult = null, testRunning = false;
  const history = new L.History(100, 700);
  const stageNodes = new Map(), binders = [];
  const urlsToForget = new Set();

  const readOnly = () => !doc || doc.status !== "draft" || meta?.scope === "builtin";
  const dirty = () => !!doc && L.isDirty(doc, base);
  const canvasOf = () => doc.canvas;

  // ================================================================================ khung trang
  const titleHost = h("div");
  const toolbar = h("div", { class: "st-toolbar", role: "toolbar", "aria-label": "Công cụ Template Studio" });
  const banner = h("div", { "aria-live": "polite" });
  const layersHost = h("section", { class: "st-panel st-layers", "aria-label": "Lớp" });
  const stageWrap = h("div", { class: "st-stage-wrap" });
  const stageNote = h("p", { class: "muted small st-hint" }, "Kéo lớp để di chuyển, kéo chốt để đổi cỡ. Chọn lớp rồi dùng phím mũi tên (Shift = 10 px). Ctrl+Z hoàn tác, Ctrl+Shift+Z làm lại.");
  const propsHost = h("section", { class: "st-panel st-props", "aria-label": "Thuộc tính" });
  const resultsHost = h("div", { class: "stack", "aria-live": "polite" });
  const body = h("div", { class: "st-grid" }, layersHost, h("div", { class: "st-center" }, stageWrap, stageNote), propsHost);
  const live = h("div", { class: "sr-only", role: "status", "aria-live": "polite" });
  root.append(titleHost, toolbar, banner, body, resultsHost, live);
  titleHost.append(skeleton(3));

  // ---- chặn rời trang khi còn thay đổi chưa lưu ----
  const guardClick = (e) => {
    if (!dirty() || readOnly()) return;
    const a = e.target.closest?.('a[href^="#"]');
    if (!a || a.getAttribute("href") === location.hash) return;
    e.preventDefault(); e.stopImmediatePropagation();
    confirmDialog({ title: "Bỏ thay đổi chưa lưu?", body: "Template này có thay đổi chưa lưu. Rời đi sẽ mất chúng.", confirmLabel: "Rời đi, bỏ thay đổi", danger: true })
      .then((ok) => { if (ok) { base = L.clone(doc); location.hash = a.getAttribute("href").slice(1); } });
  };
  const guardUnload = (e) => { if (dirty() && !readOnly()) { e.preventDefault(); e.returnValue = ""; } };
  const confirmLeave = async () => !dirty() || readOnly() || confirmDialog({ title: "Bỏ thay đổi chưa lưu?", body: "Thay đổi chưa lưu của template này sẽ mất.", confirmLabel: "Bỏ thay đổi", danger: true });
  document.addEventListener("click", guardClick, true);
  window.addEventListener("beforeunload", guardUnload);

  // ================================================================================ nạp
  async function load(version, { keepSelection = false } = {}) {
    try {
      const r = await api.get(`/api/templates/${tid}`, { query: { version } });
      if (!alive) return;
      doc = L.clone(r.template);
      base = L.clone(r.template);
      meta = { scope: r.scope, versions: r.versions || [], used_by: r.used_by || [], checksum: r.checksum };
      validation = r.validation || null;
      testResult = null; previewUrl = null; previewKey = null; showPreview = false;
      history.reset(JSON.stringify(doc));
      if (!keepSelection || !L.byId(doc, selected)) selected = null;
      const want = `#/templates/${tid}?v=${doc.version}`;
      if (location.hash !== want) window.history.replaceState(null, "", want);
      document.title = `${doc.name || tid} · Template Studio · ContentFactory`;
      renderAll();
      collectIssues();
    } catch (e) {
      if (!alive) return;
      clear(titleHost); clear(toolbar); clear(body); clear(banner);
      titleHost.append(pageHead("Template Studio", null, btn({ label: "Về danh sách", icon: "layout", href: "#/templates" })),
        e.status === 404 ? emptyState({ icon: "layout", title: "Không tìm thấy template", text: [e.message, e.hint].filter(Boolean).join(" "), action: btn({ label: "Về danh sách template", href: "#/templates", kind: "primary" }) })
          : errorState(e, () => { clear(titleHost); titleHost.append(skeleton(3)); load(query.get("v") || undefined); }));
    }
  }

  // ================================================================================ chỉnh sửa tài liệu
  // Mọi sửa đổi đi qua commit(): ghi lịch sử (gom theo key), cập nhật các phần hiển thị.
  function commit(key, { structural = false } = {}) {
    history.record(JSON.stringify(doc), key);
    if (structural) { renderStage(); renderLayers(); } else { updateStageNodes(); }
    refreshBinders();
    updateToolbar();
    markStale();
  }
  const el = () => (selected && selected !== "__canvas" ? L.byId(doc, selected) : null);
  function applyBox(e, box, key) { Object.assign(e, box); commit(key); }
  function undoRedo(dirn) {
    if (readOnly()) return;
    const s = dirn < 0 ? history.undo() : history.redo();
    if (s == null) return;
    doc = JSON.parse(s);
    if (selected && selected !== "__canvas" && !L.byId(doc, selected)) selected = null;
    renderAll();
    live.textContent = dirn < 0 ? "Đã hoàn tác" : "Đã làm lại";                    // vùng đọc cho trình đọc màn hình; không bật toast cho mỗi lần Ctrl+Z
  }
  const onKey = (e) => {
    if (!alive || readOnly()) return;
    const k = e.key.toLowerCase();
    if ((e.ctrlKey || e.metaKey) && (k === "z" || k === "y")) {
      if (e.target.tagName === "TEXTAREA") return;
      e.preventDefault();
      undoRedo(k === "y" || e.shiftKey ? +1 : -1);
    }
  };
  document.addEventListener("keydown", onKey);

  // ================================================================================ thanh công cụ
  let saveB, undoB, redoB, valB, prevB, testB, pubB, zoomSel, prevSw, moreB, delB;
  function buildToolbar() {
    clear(toolbar);
    saveB = btn({ label: "Lưu nháp", icon: "save", kind: "primary", onClick: () => save() });
    undoB = btn({ icon: "undo", label: "Hoàn tác", onClick: () => undoRedo(-1), title: "Hoàn tác (Ctrl+Z)" });
    redoB = btn({ icon: "redo", label: "Làm lại", onClick: () => undoRedo(+1), title: "Làm lại (Ctrl+Shift+Z)" });
    valB = btn({ icon: "check-circle", label: "Kiểm tra", onClick: () => runValidate() });
    prevB = btn({ icon: "eye", label: "Xem trước", onClick: () => runPreview(), title: "Dựng ảnh xem trước bằng bộ render thật của ContentFlow" });
    testB = btn({ icon: "play", label: "Render thử", onClick: () => runTestRender(), title: "Render thật một mẫu ngắn (có thể mất vài giây)" });
    pubB = btn({ icon: "upload", label: "Publish", kind: "primary", onClick: () => publish() });
    zoomSel = select({ options: [["fit", "Vừa khung"], ["0.5", "50%"], ["1", "100%"], ["2", "200%"]], value: zoom, onChange: (v) => { zoom = v; layoutStage(); } });
    zoomSel.setAttribute("aria-label", "Thu phóng canvas (chỉ là hiển thị, không đổi kích thước thật)");
    prevSw = switchCtl({ label: "Hiện ảnh xem trước", checked: showPreview, onChange: (v) => { showPreview = v; renderStage(); } });
    prevSw.input.disabled = true;
    moreB = btn({ icon: "dot", label: "Thêm", onClick: () => openMore() });
    delB = meta.scope === "user" && doc.status === "draft" ? btn({ icon: "trash", label: "Xoá bản nháp", kind: "danger", onClick: () => deleteDraft(), title: "Xoá bản nháp này (có xác nhận); bản đã publish không bị ảnh hưởng" }) : null;
    toolbar.append(h("div", { class: "row" }, saveB, undoB, redoB), h("span", { class: "st-sep", "aria-hidden": "true" }), h("div", { class: "row" }, valB, prevB, testB),
      h("span", { class: "st-sep", "aria-hidden": "true" }), pubB, delB, moreB, h("div", { class: "row st-zoom" }, h("label", { class: "small muted", for: "st-zoom" }, "Thu phóng"), zoomSel, prevSw));
    zoomSel.id = "st-zoom";
  }
  function updateToolbar() {
    if (!saveB) return;
    const ro = readOnly();
    saveB.disabled = ro || !dirty() || saving;
    saveB.title = ro ? "Bản này không sửa được." : dirty() ? "" : "Chưa có thay đổi.";
    undoB.disabled = ro || !history.canUndo();
    redoB.disabled = ro || !history.canRedo();
    pubB.disabled = ro || saving;
    prevSw.input.disabled = !previewUrl;
    prevSw.input.checked = showPreview && !!previewUrl;
    const d = dirty() && !ro;
    const dot = titleHost.querySelector(".st-dirty");
    if (dot) dot.hidden = !d;
  }
  function markStale() {
    const stale = previewUrl && previewKey !== L.canonStr(L.content(doc));
    const note = toolbar.querySelector(".st-stale");
    if (stale && !note) prevSw.after(h("span", { class: "chip warn st-stale", role: "status" }, "Ảnh xem trước đã cũ"));
    if (!stale && note) note.remove();
  }

  // ================================================================================ tiêu đề + banner
  function renderTitle() {
    clear(titleHost);
    const vsel = select({ options: meta.versions.map((v) => [String(v.version), `v${v.version} · ${{ draft: "bản nháp", published: "đã publish", archived: "đã lưu trữ" }[v.status] || v.status}`]), value: String(doc.version) });
    vsel.setAttribute("aria-label", "Chọn version");
    vsel.addEventListener("change", async () => { if (await confirmLeave()) load(Number(vsel.value)); else vsel.value = String(doc.version); });
    const row = { id: tid, scope: meta.scope, latest_published: meta.versions.filter((v) => v.status === "published").at(-1)?.version, draft: meta.versions.find((v) => v.status === "draft")?.version };
    titleHost.append(pageHead(doc.name || tid, null, h("div", { class: "row" }, btn({ label: "Danh sách", icon: "layout", href: "#/templates", kind: "ghost" }), vsel)));
    const sub = h("div", { class: "row st-sub" }, h("span", { class: "mono small muted" }, tid), h("span", { class: "chip" }, doc.type === "thumbnail" ? "Thumbnail" : "Video"),
      badge({ tone: { draft: "wait", published: "done", archived: "queue" }[doc.status] || "queue", icon: doc.status === "draft" ? "file" : doc.status === "published" ? "check-circle" : "folder", label: { draft: "Bản nháp", published: "Đã publish", archived: "Đã lưu trữ" }[doc.status] || doc.status }),
      meta.scope === "builtin" ? badge({ tone: "off", icon: "lock", label: "Có sẵn" }) : null,
      h("span", { class: "chip warn st-dirty", hidden: true }, "Chưa lưu"),
      meta.used_by?.length ? h("span", { class: "small muted" }, `Kênh đang dùng: ${meta.used_by.map((u) => u.channel).join(", ")}`) : null);
    titleHost.append(sub);
    titleHost.querySelector("h1").id = "page-title";
  }
  function renderBanner() {
    clear(banner);
    if (!readOnly()) return;
    const openDraft = meta.versions.find((v) => v.status === "draft");
    if (meta.scope === "builtin") {
      banner.append(alertBox({ tone: "info", title: "Template có sẵn của ContentFlow — chỉ xem", body: "Nhân bản để có bản của bạn rồi chỉnh sửa tự do; bản gốc không bao giờ bị đổi.",
        actions: [btn({ label: "Nhân bản để sửa", icon: "copy", kind: "primary", size: "sm", onClick: () => openDuplicate() })] }));
    } else if (doc.status === "draft") {
      banner.append(alertBox({ tone: "info", title: "Chỉ xem", body: "Không sửa được bản nháp này." }));
    } else {
      banner.append(alertBox({ tone: "info", title: `Version ${doc.version} đã ${doc.status === "archived" ? "lưu trữ" : "publish"} — chỉ xem`,
        body: "Version đã publish không bao giờ bị sửa để các job đã tạo luôn dựng lại đúng như cũ. Muốn đổi, hãy tạo bản nháp mới (version kế tiếp).",
        actions: [openDraft ? btn({ label: `Mở bản nháp v${openDraft.version}`, icon: "file", kind: "primary", size: "sm", onClick: () => confirmLeave().then((ok) => ok && load(openDraft.version)) })
          : btn({ label: "Tạo bản nháp mới để sửa", icon: "plus", kind: "primary", size: "sm", onClick: (e) => newDraft(e.currentTarget) })] }));
    }
  }

  // ================================================================================ lớp
  function renderLayers() {
    const had = layersHost.contains(document.activeElement) ? (document.activeElement.closest("[data-id]")?.dataset.id || (document.activeElement.classList.contains("st-canvas-btn") ? "__canvas" : null)) : null;
    clear(layersHost);
    const ro = readOnly();
    const add = btn({ label: "Thêm lớp", icon: "plus", size: "sm", disabled: ro, onClick: () => openAdd() });
    layersHost.append(h("div", { class: "row spread st-panel-head" }, h("h2", { id: "st-layers-h" }, "Lớp"), add));
    layersHost.append(h("p", { class: "muted small" }, "Trên cùng = vẽ sau cùng. Thứ tự lớp là z xác định (10, 20, 30…)."));
    const ul = h("ul", { class: "st-layer-list", "aria-labelledby": "st-layers-h" });
    const top = L.elementsTopDown(doc);
    top.forEach((e, i) => {
      const on = selected === e.id;
      const bad = issueIds.get(e.id);
      const selBtn = h("button", { type: "button", class: "st-layer-main", "aria-pressed": String(on), onClick: () => selectLayer(e.id) },
        icon(KIND_ICON[e.type] || "dot", { size: 16 }), h("span", { class: "st-layer-name" }, h("strong", null, layerName(e)), h("span", { class: "mono small muted" }, e.id)),
        bad ? h("span", { class: "st-bad", title: bad.join("; ") }, icon("alert-circle", { size: 14 }), h("span", { class: "sr-only" }, "Có lỗi")) : null);
      const eye = btn({ icon: e.enabled === false ? "eye-off" : "eye", kind: "ghost", size: "sm", disabled: ro, ariaLabel: `${e.enabled === false ? "Hiện" : "Ẩn"} lớp ${e.id}`, title: e.enabled === false ? "Đang ẩn: không được vẽ" : "Đang hiện",
        onClick: () => { if (e.enabled === false) delete e.enabled; else e.enabled = false; commit("enable:" + e.id, { structural: true }); } });
      const up = btn({ icon: "arrow-up", kind: "ghost", size: "sm", disabled: ro || i === 0, ariaLabel: `Đưa lớp ${e.id} lên trên`, onClick: () => { L.moveLayer(doc, e.id, +1); commit(null, { structural: true }); } });
      const down = btn({ icon: "arrow-down", kind: "ghost", size: "sm", disabled: ro || i === top.length - 1, ariaLabel: `Đưa lớp ${e.id} xuống dưới`, onClick: () => { L.moveLayer(doc, e.id, -1); commit(null, { structural: true }); } });
      ul.append(h("li", { class: "st-layer", "aria-current": String(on), dataset: { id: e.id } }, selBtn, h("span", { class: "chip st-z", title: "Thứ tự lớp z" }, `z ${e.z ?? "?"}`), h("span", { class: "row st-layer-act" }, eye, up, down)));
    });
    layersHost.append(ul);
    const cv = h("button", { type: "button", class: "btn sm st-canvas-btn", "aria-pressed": String(selected === "__canvas"), onClick: () => selectLayer("__canvas") }, icon("settings", { size: 16 }), h("span", null, "Template & canvas"));
    layersHost.append(cv);
    if (had) (had === "__canvas" ? cv : ul.querySelector(`[data-id="${had}"] .st-layer-main`))?.focus({ preventScroll: true });
  }
  function selectLayer(id) { selected = id; renderLayers(); renderProps(); updateSelectionOnStage(); }

  async function openAdd() {
    const types = [];
    types.push(["image", "Hình ảnh (frame, nền, logo…)"]);
    const srcs = (doc.elements || []).filter((e) => e.type === "text").map((e) => e.source);
    if (doc.type === "thumbnail") {
      if (!srcs.includes("channel.name")) types.push(["text:channel.name", "Chữ: tên kênh"]);
      if (!srcs.includes("project.title")) types.push(["text:project.title", "Chữ: tiêu đề truyện"]);
      if (!doc.elements.some((e) => e.type === "photo")) types.push(["photo", "Ảnh nhân vật"]);
    } else if (!doc.elements.some((e) => e.type === "source_video")) types.push(["source_video", "Video nguồn"]);
    const sel = select({ options: types });
    const ok = await openDialog({ title: "Thêm lớp", content: field({ label: "Loại lớp", control: sel, hint: "Thumbnail: tối đa một lớp chữ cho mỗi nguồn (tên kênh, tiêu đề) và một ảnh nhân vật." }), actions: [{ label: "Huỷ", value: false }, { label: "Thêm", kind: "primary", value: true }] });
    if (!ok) return;
    const [t, src] = sel.value.split(":");
    let e;
    if (t === "image") {
      const aid = await openAssetLibrary({ types: IMAGE_ASSET_TYPES, title: "Chọn hình cho lớp mới" });
      if (!aid) return;
      e = L.newElement("image", doc, { assetId: aid });
      const info = (await api.get("/api/assets").catch(() => ({ assets: [] }))).assets.find((a) => a.id === aid);
      if (info?.metadata?.width) { e.width = Math.min(info.metadata.width, doc.canvas.width); e.height = Math.round(e.width * info.metadata.height / info.metadata.width); e.x = Math.round((doc.canvas.width - e.width) / 2); e.y = Math.round((doc.canvas.height - e.height) / 2); }
    } else e = L.newElement(t, doc, { source: src });
    doc.elements.push(e);
    selected = e.id;
    commit(null, { structural: true });
    renderProps();
    updateSelectionOnStage();
  }
  function removeSelected() {
    const e = el();
    if (!e || readOnly()) return;
    doc.elements = doc.elements.filter((x) => x.id !== e.id);
    selected = null;
    commit(null, { structural: true });
    renderProps();
  }

  // ================================================================================ canvas
  const assetUrls = new Map();
  function renderStage() {
    clear(stageWrap);
    stageNodes.clear();
    if (!L.canvasOk(doc.canvas)) { stageWrap.append(alertBox({ tone: "wait", title: "Kích thước canvas chưa hợp lệ", body: "Nhập rộng/cao là số nguyên dương ở “Template & canvas”." })); return; }
    const stage = h("div", { class: "st-stage", tabindex: "-1", role: "group", "aria-label": `Canvas ${doc.canvas.width}×${doc.canvas.height}` });
    stage.style.background = doc.canvas.background_color && doc.type === "thumbnail" ? doc.canvas.background_color : "";
    stage.dataset.type = doc.type;
    if (previewUrl && showPreview) { stage.append(h("img", { class: "st-preview-img", src: previewUrl, alt: "Ảnh xem trước do ContentFlow dựng" })); stage.classList.add("has-preview"); }
    for (const e of [...doc.elements].sort((a, b) => (a.z ?? 0) - (b.z ?? 0))) {
      if (!L.hasBox(e) || e.enabled === false) continue;
      const node = h("div", { class: "st-el", dataset: { id: e.id, type: e.type }, role: "button", tabindex: "-1" });
      buildNodeBody(node, e);
      node.addEventListener("pointerdown", (ev) => onDown(ev, e.id, "move"));
      node.addEventListener("click", (ev) => ev.stopPropagation());
      node.addEventListener("focus", () => { if (selected !== e.id) selectLayer(e.id); });
      stage.append(node);
      stageNodes.set(e.id, node);
      place(node, e);
    }
    stage.addEventListener("pointerdown", (ev) => { if (ev.target === stage || ev.target.classList.contains("st-preview-img")) { selectLayer(null); } });
    stage.addEventListener("keydown", onStageKey);
    stageWrap.append(stage);
    layoutStage();
    updateSelectionOnStage();
  }
  function buildNodeBody(node, e) {
    clear(node);
    if (e.type === "image") {
      const img = h("img", { alt: "", draggable: false });
      img.style.objectFit = { stretch: "fill", cover: "cover", contain: "contain" }[e.fit || "stretch"];
      img.style.opacity = String(e.opacity ?? 1);
      node.append(img);
      if (e.asset_id) api.blobUrl(`/api/assets/${e.asset_id}/file`).then((u) => { img.src = u; }).catch(() => { node.classList.add("st-missing"); node.append(h("span", { class: "st-label" }, `Không thấy asset ${e.asset_id}`)); });
      else { node.classList.add("st-missing"); node.append(h("span", { class: "st-label" }, "Chưa chọn asset")); }
    } else if (e.type === "photo") node.append(h("span", { class: "st-label" }, "Ảnh nhân vật"));
    else if (e.type === "source_video") node.append(h("span", { class: "st-label" }, `Video nguồn (${Math.round(e.width)}×${Math.round(e.height)})`));
    else if (e.type === "text") node.append(h("span", { class: "st-text" }, e.uppercase ? SAMPLE_TEXT[e.source]?.toUpperCase() : SAMPLE_TEXT[e.source] || "Chữ"));
    else node.append(h("span", { class: "st-label" }, e.type));
  }
  function place(node, e) {
    node.style.left = `${e.x * scale}px`; node.style.top = `${e.y * scale}px`; node.style.width = `${e.width * scale}px`; node.style.height = `${e.height * scale}px`;
    node.setAttribute("aria-label", `${layerName(e)} ${e.id}: x ${e.x}, y ${e.y}, ${e.width} × ${e.height}`);
    if (e.type === "text") {
      const t = node.firstChild;
      const size = Math.max(7, Math.min(e.font_size_max ?? 48, (e.height / Math.max(1, e.max_lines ?? 1)) * 0.9) * scale);
      t.style.fontSize = `${size}px`; t.style.color = e.fill || "#fff";
      t.style.alignItems = { top: "flex-start", bottom: "flex-end" }[e.vertical_align] || "center";
      if (e.stroke?.color) t.style.webkitTextStroke = `${Math.max(1, (e.stroke.width || 0) * scale * 0.5)}px ${e.stroke.color}`;
      else t.style.webkitTextStroke = "";
    }
    if (e.type === "image") { const i = node.firstChild; if (i) { i.style.opacity = String(e.opacity ?? 1); i.style.objectFit = { stretch: "fill", cover: "cover", contain: "contain" }[e.fit || "stretch"]; } }
  }
  function updateStageNodes() { for (const [id, node] of stageNodes) { const e = L.byId(doc, id); if (e) { place(node, e); } } }
  function updateSelectionOnStage() {
    for (const [id, node] of stageNodes) {
      const on = id === selected;
      node.classList.toggle("sel", on);
      node.tabIndex = on ? 0 : -1;
      node.querySelectorAll(".st-handle").forEach((x) => x.remove());
      if (on && !readOnly()) for (const hd of HANDLES) {
        const n = h("span", { class: "st-handle", dataset: { h: hd }, "aria-hidden": "true" });
        n.addEventListener("pointerdown", (ev) => { ev.stopPropagation(); onDown(ev, id, hd); });
        node.append(n);
      }
    }
  }
  function layoutStage() {
    const stage = stageWrap.querySelector(".st-stage");
    if (!stage || !L.canvasOk(doc.canvas)) return;
    const avail = Math.max(120, stageWrap.clientWidth - 2);
    scale = L.viewScale(zoom, avail, doc.canvas.width);
    stage.style.width = `${Math.round(doc.canvas.width * scale)}px`;
    stage.style.height = `${Math.round(doc.canvas.height * scale)}px`;
    updateStageNodes();
  }
  const ro = new ResizeObserver(() => { if (zoom === "fit") layoutStage(); });
  ro.observe(stageWrap);

  // ---- kéo / đổi cỡ ----
  let drag = null;
  function onDown(ev, id, mode) {
    if (ev.button !== 0) return;
    const e = L.byId(doc, id);
    if (!e) return;
    if (selected !== id) selectLayer(id);
    if (readOnly()) return;
    ev.preventDefault();
    stageNodes.get(id)?.focus({ preventScroll: true });
    drag = { id, mode, x0: ev.clientX, y0: ev.clientY, box: { x: e.x, y: e.y, width: e.width, height: e.height }, moved: false };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp, { once: true });
  }
  function onMove(ev) {
    if (!drag) return;
    const e = L.byId(doc, drag.id);
    const dx = L.toCanvas(ev.clientX - drag.x0, scale), dy = L.toCanvas(ev.clientY - drag.y0, scale);
    if (!drag.moved && Math.hypot(ev.clientX - drag.x0, ev.clientY - drag.y0) < 3) return;
    drag.moved = true;
    const mode = L.boxMode(e);
    const box = drag.mode === "move" ? L.moveBox(drag.box, dx, dy, canvasOf(), mode) : L.resizeBox(drag.box, drag.mode, dx, dy, canvasOf(), mode, { keepRatio: ev.shiftKey && e.type === "image" });
    applyBox(e, box, `drag:${drag.id}`);
  }
  function onUp() { window.removeEventListener("pointermove", onMove); drag = null; }
  function onStageKey(ev) {
    const e = el();
    if (!e || readOnly()) return;
    if (ev.key === "Delete" || ev.key === "Backspace") { ev.preventDefault(); removeSelected(); return; }
    if (ev.key === "Escape") { selectLayer(null); return; }
    const box = L.nudge(e, ev.key, ev.shiftKey, canvasOf(), L.boxMode(e));
    if (box) { ev.preventDefault(); applyBox(e, box, `nudge:${e.id}`); }
  }

  // ================================================================================ thuộc tính
  function refreshBinders() { for (const b of binders) b(); }
  function renderProps() {
    clear(propsHost);
    binders.length = 0;
    const ro2 = readOnly();
    const e = el();
    propsHost.append(h("div", { class: "st-panel-head" }, h("h2", null, e ? `Lớp: ${layerName(e)}` : "Thuộc tính")));
    if (e) {
      propsHost.append(h("p", { class: "mono small muted" }, e.id), ...elementProps(e, ro2));
      if (!ro2) propsHost.append(h("div", null, btn({ label: "Xoá lớp", icon: "trash", kind: "danger", size: "sm", onClick: () => removeSelected() })));
    } else propsHost.append(h("p", { class: "muted small" }, "Chọn một lớp ở bên trái hoặc trên canvas để chỉnh. Toạ độ tính theo pixel của canvas."));
    propsHost.append(disclosure({ label: "Template & canvas", open: selected === "__canvas" || !e, content: h("div", { class: "stack" }, ...templateProps(ro2)) }));
  }

  // -- ô nhập gắn tài liệu --
  const num = (label, get, set, { min, max, step = 1, int = true, hint, disabled = false, key }) => {
    const ctl = input({ type: "number", min, max, step: String(step), inputmode: "decimal", disabled });
    const f = field({ label, control: ctl, hint });
    const sync = () => { if (document.activeElement !== ctl) { const v = get(); ctl.value = v == null ? "" : String(v); } };
    ctl.addEventListener("input", () => {
      const n = Number(ctl.value);
      const bad = ctl.value === "" || !Number.isFinite(n) || (int && !Number.isInteger(n)) || (min != null && n < min) || (max != null && n > max);
      if (bad) { f.setError(`Nhập ${int ? "số nguyên" : "số"}${min != null ? ` từ ${min}` : ""}${max != null ? ` đến ${max}` : ""}.`); return; }
      f.setError(null); set(n); commit(key || `num:${label}`);
    });
    ctl.addEventListener("change", () => { f.setError(null); ctl.value = get() == null ? "" : String(get()); });
    binders.push(sync); sync();
    return f;
  };
  const txt = (label, get, set, { disabled = false, hint, list, key, placeholder } = {}) => {
    const ctl = input({ disabled, placeholder });
    if (list) ctl.setAttribute("list", list);
    const f = field({ label, control: ctl, hint });
    const sync = () => { if (document.activeElement !== ctl) ctl.value = get() ?? ""; };
    ctl.addEventListener("input", () => { set(ctl.value); commit(key || `txt:${label}`, { structural: false }); });
    binders.push(sync); sync();
    return f;
  };
  const pickOne = (label, options, get, set, { disabled = false, hint } = {}) => {
    const ctl = select({ options, value: get(), disabled });
    const f = field({ label, control: ctl, hint });
    ctl.addEventListener("change", () => { set(ctl.value); commit(null, { structural: true }); renderPropsKeep(); });
    binders.push(() => { if (document.activeElement !== ctl) ctl.value = get(); });
    return f;
  };
  const color = (label, get, set, { disabled = false, optional = false, key } = {}) => {
    const t = input({ placeholder: "#RRGGBB", disabled, maxlength: 9 });
    t.id = uid("c");
    const c = h("input", { type: "color", class: "st-color", disabled, "aria-label": `${label}: chọn màu` });
    const err = h("div", { class: "error", role: "alert" });
    const setErr = (m) => { err.textContent = m || ""; if (m) t.setAttribute("aria-invalid", "true"); else t.removeAttribute("aria-invalid"); };
    const sync = () => { const v = get() || ""; if (document.activeElement !== t) t.value = v; c.value = COLOR.test(v) ? v.slice(0, 7) : "#000000"; };
    t.addEventListener("input", () => {
      const v = t.value.trim();
      if (v === "" && optional) { setErr(null); set(undefined); commit(key || `color:${label}`); return; }
      if (!COLOR.test(v)) { setErr("Màu dạng #RRGGBB (hoặc #RRGGBBAA)."); return; }
      setErr(null); set(v.toUpperCase()); commit(key || `color:${label}`); c.value = v.slice(0, 7);
    });
    t.addEventListener("change", () => { setErr(null); t.value = get() || ""; });
    c.addEventListener("input", () => { t.value = c.value.toUpperCase(); setErr(null); set(c.value.toUpperCase()); commit(key || `color:${label}`); });
    binders.push(sync); sync();
    return h("div", { class: "field" }, h("label", { for: t.id }, label), h("div", { class: "input-row" }, t, c), err);
  };
  const check = (label, get, set, { disabled = false } = {}) => {
    const cb = h("input", { type: "checkbox", disabled, checked: !!get() });
    cb.addEventListener("change", () => { set(cb.checked); commit(null, { structural: true }); renderPropsKeep(); });
    binders.push(() => { cb.checked = !!get(); });
    return h("label", { class: "st-check" }, cb, h("span", null, label));
  };
  const renderPropsKeep = () => { const sc = propsHost.scrollTop; renderProps(); propsHost.scrollTop = sc; };

  function boxProps(e, ro2) {
    const mode = L.boxMode(e);
    const setBox = (k) => (v) => { const box = L.clampBox({ x: e.x, y: e.y, width: e.width, height: e.height, [k]: v }, canvasOf(), mode); Object.assign(e, box); };
    return h("div", { class: "st-grid-4" }, ...["x", "y", "width", "height"].map((k) => num({ x: "X", y: "Y", width: "Rộng", height: "Cao" }[k], () => e[k], setBox(k), { int: true, disabled: ro2, key: `box:${e.id}.${k}` })));
  }
  const zProp = (e, ro2) => num("Thứ tự lớp (z)", () => e.z, (v) => { e.z = v; }, { min: 0, max: 10000, disabled: ro2, key: `z:${e.id}`, hint: "Phải duy nhất. Dùng nút lên/xuống ở danh sách lớp để đánh số lại tự động." });
  function elementProps(e, ro2) {
    const out = [];
    if (!L.hasBox(e)) return [h("p", { class: "muted small" }, `Loại “${e.type}” chưa có bộ chỉnh trong giao diện.`)];
    out.push(boxProps(e, ro2), zProp(e, ro2));
    if (e.type === "image") {
      out.push(h("div", { class: "field" }, h("div", { class: "label" }, "Asset"), assetPicker({ types: IMAGE_ASSET_TYPES, value: e.asset_id, disabled: ro2, label: "Hình của lớp", onPick: (id) => { e.asset_id = id; commit(null, { structural: true }); } })),
        pickOne("Cách vừa khung", [["stretch", "Kéo giãn (stretch)"], ["cover", "Phủ kín, cắt phần thừa (cover)"], ["contain", "Vừa trọn (contain)"]], () => e.fit || "stretch", (v) => { e.fit = v; }, { disabled: ro2 }),
        num("Độ mờ (0–1)", () => e.opacity ?? 1, (v) => { if (v === 1) delete e.opacity; else e.opacity = v; }, { min: 0, max: 1, step: 0.05, int: false, disabled: ro2, key: `op:${e.id}` }));
    } else if (e.type === "photo") {
      out.push(num("Bo góc (px)", () => e.corner_radius ?? 0, (v) => { e.corner_radius = v; }, { min: 0, disabled: ro2, key: `r:${e.id}` }),
        h("div", { class: "grid-2" }, num("Tiêu điểm ngang (0–1)", () => e.focus_x ?? 0.5, (v) => { e.focus_x = v; }, { min: 0, max: 1, step: 0.05, int: false, disabled: ro2, key: `fx:${e.id}` }),
          num("Tiêu điểm dọc (0–1)", () => e.focus_y ?? 0.5, (v) => { e.focus_y = v; }, { min: 0, max: 1, step: 0.05, int: false, disabled: ro2, key: `fy:${e.id}` })),
        h("div", { class: "grid-2" }, num("Zoom nhỏ nhất", () => e.zoom_min ?? 1, (v) => { e.zoom_min = v; }, { min: 0.1, max: 10, step: 0.1, int: false, disabled: ro2, key: `zn:${e.id}` }),
          num("Zoom lớn nhất", () => e.zoom_max ?? 3, (v) => { e.zoom_max = v; }, { min: 0.1, max: 10, step: 0.1, int: false, disabled: ro2, key: `zx:${e.id}` })),
        color("Màu nền ảnh (tuỳ chọn)", () => e.background_color, (v) => { if (v) e.background_color = v; else delete e.background_color; }, { disabled: ro2, optional: true, key: `pb:${e.id}` }),
        h("div", { class: "field" }, h("div", { class: "label" }, "Mặt nạ (tuỳ chọn)"), assetPicker({ types: ["mask"], value: e.mask_asset_id || null, optional: true, disabled: ro2, label: "Mặt nạ", onPick: (id) => { if (id) e.mask_asset_id = id; else delete e.mask_asset_id; commit(null); } })));
    } else if (e.type === "source_video") {
      out.push(pickOne("Cách vừa khung", [["cover", "Phủ kín, cắt phần thừa (cover)"]], () => e.fit || "cover", (v) => { e.fit = v; }, { disabled: true, hint: "Bộ render hiện chỉ hỗ trợ cover." }),
        h("div", { class: "grid-2" }, num("Tiêu điểm ngang (0–1)", () => e.focus_x ?? 0.5, (v) => { e.focus_x = v; }, { min: 0, max: 1, step: 0.05, int: false, disabled: ro2, key: `fx:${e.id}` }),
          num("Tiêu điểm dọc (0–1)", () => e.focus_y ?? 0.5, (v) => { e.focus_y = v; }, { min: 0, max: 1, step: 0.05, int: false, disabled: ro2, key: `fy:${e.id}` })));
    } else if (e.type === "text") out.push(...textProps(e, ro2));
    return out;
  }
  function textProps(e, ro2) {
    const srcOpts = Object.entries(SRC_LABEL).filter(([k]) => k === e.source || !doc.elements.some((x) => x.type === "text" && x.source === k));
    const fontMode = e.font?.asset_id ? "asset" : e.font?.family ? "family" : "default";
    const out = [pickOne("Nội dung lấy từ", srcOpts, () => e.source, (v) => { e.source = v; }, { disabled: ro2 }),
      pickOne("Font", [["default", "Mặc định của ContentFlow"], ["family", "Font cài trên máy (tên file)"], ["asset", "Font trong thư viện asset"]], () => (e.font?.asset_id ? "asset" : e.font?.family ? "family" : "default"),
        (v) => { if (v === "default") delete e.font; else if (v === "family") e.font = { family: FONTS[0] }; else e.font = { asset_id: "" }; }, { disabled: ro2 })];
    if (fontMode === "family") out.push(txt("Tên file font", () => e.font?.family, (v) => { e.font = { family: v }; }, { disabled: ro2, list: "st-fonts", key: `ff:${e.id}`, hint: "Ví dụ arialbd.ttf (không có thư mục)." }), h("datalist", { id: "st-fonts" }, ...FONTS.map((f) => h("option", { value: f }))));
    if (fontMode === "asset") out.push(h("div", { class: "field" }, h("div", { class: "label" }, "Font asset"), assetPicker({ types: ["font"], value: e.font?.asset_id || null, disabled: ro2, label: "Font", onPick: (id) => { e.font = { asset_id: id }; commit(null); } })));
    out.push(pickOne("Căn dọc", [["top", "Trên"], ["center", "Giữa"], ["bottom", "Dưới"]], () => e.vertical_align || "center", (v) => { e.vertical_align = v; }, { disabled: ro2, hint: "Căn ngang luôn ở giữa (bộ render hiện chỉ hỗ trợ giữa)." }),
      h("div", { class: "grid-2" }, num("Cỡ chữ nhỏ nhất", () => e.font_size_min ?? 28, (v) => { e.font_size_min = v; }, { min: 6, max: 400, disabled: ro2, key: `fmin:${e.id}` }), num("Cỡ chữ lớn nhất", () => e.font_size_max ?? 52, (v) => { e.font_size_max = v; }, { min: 6, max: 400, disabled: ro2, key: `fmax:${e.id}` })),
      h("div", { class: "grid-2" }, num("Số dòng tối đa", () => e.max_lines ?? 2, (v) => { e.max_lines = v; }, { min: 1, max: 10, disabled: ro2, key: `ml:${e.id}` }), num("Giãn dòng", () => e.line_spacing ?? 1, (v) => { e.line_spacing = v; }, { min: 0.5, max: 3, step: 0.1, int: false, disabled: ro2, key: `ls:${e.id}` })),
      check("Viết HOA toàn bộ", () => e.uppercase, (v) => { if (v) e.uppercase = true; else delete e.uppercase; }, { disabled: ro2 }),
      h("div", { class: "grid-2" }, color("Màu chữ", () => e.fill, (v) => { e.fill = v; }, { disabled: ro2, key: `fill:${e.id}` }), color("Màu nhấn dòng nổi bật", () => e.highlight_fill, (v) => { if (v) e.highlight_fill = v; else delete e.highlight_fill; }, { disabled: ro2, optional: true, key: `hl:${e.id}` })));
    const fx = (key, label, defaults, fields) => {
      out.push(check(label, () => !!e[key], (v) => { if (v) e[key] = { ...defaults }; else delete e[key]; }, { disabled: ro2 }));
      if (e[key]) out.push(h("div", { class: "st-sub-group" }, ...fields(e[key])));
    };
    fx("stroke", "Viền chữ", { color: "#000000", width: 4 }, (o) => [h("div", { class: "grid-2" }, color("Màu viền", () => o.color, (v) => { o.color = v; }, { disabled: ro2, key: `sc:${e.id}` }), num("Dày (px)", () => o.width, (v) => { o.width = v; }, { min: 0, int: false, step: 0.5, disabled: ro2, key: `sw:${e.id}` }))]);
    fx("outer_stroke", "Viền ngoài", { color: "#FFFFFF", width: 4 }, (o) => [h("div", { class: "grid-2" }, color("Màu viền ngoài", () => o.color, (v) => { o.color = v; }, { disabled: ro2, key: `oc:${e.id}` }), num("Dày (px)", () => o.width, (v) => { o.width = v; }, { min: 0, int: false, step: 0.5, disabled: ro2, key: `ow:${e.id}` }))]);
    fx("shadow", "Bóng đổ", { color: "#000000", offset: [4, 5], blur: 2, spread: 0, opacity: 1 }, (o) => [color("Màu bóng", () => o.color, (v) => { o.color = v; }, { disabled: ro2, key: `shc:${e.id}` }),
      h("div", { class: "grid-2" }, num("Lệch ngang", () => o.offset?.[0] ?? 0, (v) => { o.offset = [v, o.offset?.[1] ?? 0]; }, { int: false, step: 1, disabled: ro2, key: `sx:${e.id}` }), num("Lệch dọc", () => o.offset?.[1] ?? 0, (v) => { o.offset = [o.offset?.[0] ?? 0, v]; }, { int: false, step: 1, disabled: ro2, key: `sy:${e.id}` })),
      h("div", { class: "grid-2" }, num("Làm mờ", () => o.blur ?? 0, (v) => { o.blur = v; }, { min: 0, int: false, step: 0.5, disabled: ro2, key: `sb:${e.id}` }), num("Lan rộng", () => o.spread ?? 0, (v) => { o.spread = v; }, { min: 0, int: false, step: 0.5, disabled: ro2, key: `ss:${e.id}` })),
      num("Độ đậm bóng (0–1)", () => o.opacity ?? 1, (v) => { o.opacity = v; }, { min: 0, max: 1, step: 0.05, int: false, disabled: ro2, key: `so:${e.id}` })]);
    return out;
  }
  function templateProps(ro2) {
    const c = doc.canvas;
    const out = [txt("Tên hiển thị", () => doc.name, (v) => { doc.name = v; }, { disabled: ro2, key: "name" }),
      h("div", { class: "field" }, h("label", { for: "st-desc" }, "Mô tả"), (() => { const t = h("textarea", { class: "textarea", id: "st-desc", rows: 2, disabled: ro2 }); t.value = doc.description || ""; t.addEventListener("input", () => { doc.description = t.value; commit("desc"); }); binders.push(() => { if (document.activeElement !== t) t.value = doc.description || ""; }); return t; })()),
      h("div", { class: "grid-2" }, num("Canvas rộng (px)", () => c.width, (v) => { c.width = v; layoutAfterCanvas(); }, { min: 16, max: 8192, disabled: ro2, key: "cw" }), num("Canvas cao (px)", () => c.height, (v) => { c.height = v; layoutAfterCanvas(); }, { min: 16, max: 8192, disabled: ro2, key: "ch" })),
      h("p", { class: "muted small" }, "Đổi cỡ canvas không tự co giãn các lớp: kiểm tra lại vị trí từng lớp.")];
    if (doc.type === "video") out.push(num("FPS (tuỳ chọn)", () => c.fps ?? "", (v) => { c.fps = v; }, { min: 1, max: 120, disabled: ro2, key: "fps", hint: "Để trống = theo cấu hình render của kênh." }),
      btn({ label: "Bỏ FPS riêng", kind: "ghost", size: "sm", disabled: ro2 || c.fps == null, onClick: () => { delete c.fps; commit("fps"); renderPropsKeep(); } }));
    else out.push(color("Màu nền canvas", () => c.background_color, (v) => { c.background_color = v; }, { disabled: ro2, key: "cbg", optional: false }));
    return out;
  }
  function layoutAfterCanvas() { renderStage(); }

  // ================================================================================ lưu / kiểm tra / xem trước / render thử / publish
  function collectIssues() {
    issueIds = new Map();
    for (const it of [...(validation?.errors || []), ...(validation?.warnings || [])]) {
      const p = L.parseIssuePath(it.path);
      const e = p.id ? L.byId(doc, p.id) : p.index != null ? doc.elements?.[p.index] : null;
      if (e) issueIds.set(e.id, [...(issueIds.get(e.id) || []), it.message.split(": ").slice(2).join(": ") || it.message]);
    }
    renderLayers();
    renderResults();
  }
  const errText = (e) => [e.message, e.hint].filter(Boolean).join(" ");

  async function save({ quiet = false } = {}) {
    if (readOnly() || saving) return false;
    if (!dirty()) return true;
    saving = true; updateToolbar();
    let ok = false;
    await busy(saveB, async () => {
      try {
        const { scope: _s, ...payload } = doc;
        const r = await api.put(`/api/templates/${tid}/${doc.version}`, { template: payload });
        base = L.clone(r.template);
        validation = r.validation;
        meta.checksum = r.checksum;
        if (!quiet) toast({ title: "Đã lưu bản nháp", message: r.validation?.ok ? "Bố cục hợp lệ." : `Còn ${r.validation?.errors?.length || 0} lỗi cần sửa trước khi publish.`, tone: r.validation?.ok ? "done" : "wait" });
        ok = true;
      } catch (e) {
        toastError(e, "Chưa lưu được");
        if (e.status === 404 || e.code === "TEMPLATE_IMMUTABLE") await load(doc.version);   // trạng thái phía máy chủ đã đổi: nạp lại
      }
    });
    saving = false;
    updateToolbar();
    if (ok) collectIssues();
    return ok;
  }

  async function runValidate() {
    await busy(valB, async () => {
      try {
        validation = await api.post(`/api/templates/${tid}/validate`, { template: (({ scope, ...r }) => r)(doc) });
        collectIssues();
        toast(validation.ok ? { title: "Bố cục hợp lệ", message: validation.warnings.length ? `${validation.warnings.length} cảnh báo.` : undefined, tone: "done" } : { title: `Có ${validation.errors.length} lỗi`, message: "Xem danh sách bên dưới.", tone: "wait" });
        resultsHost.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
      } catch (e) { toastError(e, "Chưa kiểm tra được"); }
    });
  }

  async function runPreview() {
    await busy(prevB, async () => {
      try {
        const key = L.canonStr(L.content(doc));
        const r = await api.post(`/api/templates/${tid}/preview`, { template: (({ scope, ...rr }) => rr)(doc) });
        previewUrl = await api.blobUrl(r.url);
        urlsToForget.add(r.url);
        previewKey = key;
        showPreview = true;
        testResult = null;
        renderStage(); updateToolbar(); markStale();
        if (r.warnings?.length) toast({ title: "Xem trước có cảnh báo", message: r.warnings[0], tone: "wait" });
      } catch (e) { toastError(e, "Chưa xem trước được"); }
    });
  }

  async function runTestRender() {
    if (testRunning) return;
    testRunning = true;
    testResult = { running: true };
    renderResults();
    await busy(testB, async () => {
      try {
        const r = await api.post(`/api/templates/${tid}/test-render`, { template: (({ scope, ...rr }) => rr)(doc) });
        const url = await api.blobUrl(r.url);
        urlsToForget.add(r.url);
        testResult = { ...r, blob: url, key: L.canonStr(L.content(doc)) };
      } catch (e) { testResult = { error: e }; toastError(e, "Render thử lỗi"); }
    });
    testRunning = false;
    renderResults();
  }

  function renderResults() {
    clear(resultsHost);
    // --- kiểm tra ---
    const v = validation;
    if (v) {
      const items = [...v.errors, ...v.warnings];
      const card = h("section", { class: "card stack", "aria-labelledby": "st-val-h" }, h("div", { class: "row spread" }, h("h2", { id: "st-val-h" }, "Kiểm tra bố cục"),
        v.ok ? badge({ tone: "done", icon: "check-circle", label: v.warnings.length ? `Hợp lệ · ${v.warnings.length} cảnh báo` : "Hợp lệ" }) : badge({ tone: "fail", icon: "x-circle", label: `${v.errors.length} lỗi` })));
      if (!items.length) card.append(h("p", { class: "muted small" }, "Không có lỗi hay cảnh báo."));
      else {
        const ul = h("ul", { class: "st-issues" });
        for (const it of items) {
          const p = L.parseIssuePath(it.path);
          const target = p.id ? L.byId(doc, p.id) : p.index != null ? doc.elements?.[p.index] : null;
          const go = () => { if (target) selectLayer(target.id); else if (p.canvas) selectLayer("__canvas"); };
          ul.append(h("li", { class: "st-issue", dataset: { level: it.level } }, icon(it.level === "error" ? "x-circle" : "alert", { size: 16 }), h("span", null, it.message.replace(/^template '[^']*' v\d+: /, "")),
            target || p.canvas ? btn({ label: target ? `Chọn ${target.id}` : "Mở canvas", kind: "ghost", size: "sm", onClick: go }) : null));
        }
        card.append(ul);
      }
      resultsHost.append(card);
    }
    // --- render thử ---
    const t = testResult;
    if (t) {
      const card = h("section", { class: "card stack", "aria-labelledby": "st-test-h" }, h("h2", { id: "st-test-h" }, "Render thử"));
      if (t.running) card.append(h("p", { class: "row" }, icon("spinner", { size: 18, cls: "spin" }), "Đang render thật bằng ContentFlow… video có thể mất vài chục giây."));
      else if (t.error) card.append(alertBox({ tone: "fail", title: t.error.message, body: t.error.hint || null }));
      else {
        const stale = t.key !== L.canonStr(L.content(doc));
        const size = t.kind === "video" ? (t.probe_size ? `${t.probe_size[0]}×${t.probe_size[1]}` : "?") : `${t.canvas[0]}×${t.canvas[1]}`;
        card.append(h("p", { class: "small" }, t.kind === "video" ? `Video mẫu ~2 giây, kích thước ${size}${t.ok ? " — đúng canvas." : " — KHÔNG khớp canvas!"}` : `Thumbnail ${size}.`), stale ? h("p", { class: "chip warn" }, "Đã cũ so với bố cục hiện tại") : null,
          t.kind === "video" ? h("video", { class: "st-test-media", src: t.blob, controls: true, muted: true, preload: "metadata", "aria-label": "Video render thử" }) : h("img", { class: "st-test-media", src: t.blob, alt: "Thumbnail render thử" }));
        for (const w of t.warnings || []) card.append(h("p", { class: "small muted" }, `Cảnh báo: ${w}`));
      }
      resultsHost.append(card);
    }
  }

  async function publish() {
    if (readOnly() || saving) return;
    if (!(await save({ quiet: true }))) return;
    if (validation && !validation.ok) { toast({ title: "Chưa publish được", message: `Còn ${validation.errors.length} lỗi bố cục — sửa rồi thử lại.`, tone: "wait" }); collectIssues(); return; }
    const ok = await confirmDialog({ title: `Publish version ${doc.version}?`, body: "Sau khi publish, version này KHÔNG sửa được nữa (job đã tạo luôn dựng lại đúng như cũ) và có thể được kênh chọn. Muốn đổi, bạn tạo bản nháp mới.", confirmLabel: "Publish" });
    if (!ok) return;
    await busy(pubB, async () => {
      try {
        const r = await api.post(`/api/templates/${tid}/${doc.version}/publish`, {});
        toast({ title: `Đã publish v${r.version}`, tone: "done" });
        await load(r.version);
        offerChannels();
      } catch (e) { toastError(e, "Chưa publish được"); if (e.code === "TEMPLATE_INVALID") { await runValidate(); } }
    });
  }

  async function offerChannels() {
    const chans = (app.boot?.channels || []).filter((c) => c.ok);
    if (!chans.length) return;
    const keys = doc.type === "thumbnail" ? [["thumbnail", "Thumbnail"]] : [["youtube_video", "Video YouTube"], ["tiktok_video", "Video TikTok"]];
    const defKey = doc.type === "video" && doc.canvas.height > doc.canvas.width ? "tiktok_video" : keys[0][0];
    const chSel = select({ options: chans.map((c) => [c.id, c.name || c.id]) });
    const kSel = select({ options: keys, value: defKey });
    const r = await openDialog({ title: "Chọn template này cho một kênh?", describe: "Kênh luôn dùng bản publish mới nhất; job đã tạo giữ đúng version lúc tạo.", content: h("div", { class: "stack" }, field({ label: "Kênh", control: chSel }), field({ label: "Dùng làm", control: kSel })),
      actions: [{ label: "Để sau", value: false }, { label: "Chọn cho kênh", kind: "primary", value: true, onClick: async () => {
        try { await api.put(`/api/channels/${chSel.value}/templates`, { key: kSel.value, template_id: tid, version_policy: "latest_published" }); } catch (e) { toastError(e, "Chưa chọn được"); return false; }
      } }] });
    if (r) { toast({ title: "Đã chọn cho kênh", message: `${chSel.options[chSel.selectedIndex].text}`, tone: "done" }); meta.used_by = [...(meta.used_by || []), { channel: chSel.value, key: kSel.value }]; renderTitle(); updateToolbar(); }
  }

  async function newDraft(button) {
    await busy(button, async () => {
      try { const r = await api.post(`/api/templates/${tid}/new-draft`, { from_version: doc.version }); toast({ title: `Đã tạo bản nháp v${r.template.version}`, tone: "done" }); await load(r.template.version); }
      catch (e) { toastError(e, "Chưa tạo được bản nháp"); if (e.code === "DRAFT_EXISTS") await load(); }
    });
  }

  async function openDuplicate() {
    const nameIn = input({ value: `${doc.name || tid} Copy` });
    const idIn = input({ value: L.slug(`${tid}_copy`) });
    let touched = false;
    const nameF = field({ label: "Tên bản sao", control: nameIn }), idF = field({ label: "Mã template mới", control: idIn });
    nameIn.addEventListener("input", () => { if (!touched) idIn.value = L.slug(nameIn.value); });
    idIn.addEventListener("input", () => { touched = true; idF.setError(null); });
    const r = await openDialog({ title: "Nhân bản template", content: h("div", { class: "stack" }, nameF, idF), actions: [{ label: "Huỷ", value: false }, { label: "Nhân bản và mở", kind: "primary", value: true, onClick: async () => {
      const id = idIn.value.trim();
      if (!L.validTemplateId(id)) { idF.setError("Mã không hợp lệ: chữ thường không dấu, số, _ (2–48 ký tự)."); return false; }
      try { await api.post(`/api/templates/${tid}/duplicate`, { new_id: id, name: nameIn.value.trim() || null, version: doc.version }); } catch (e) { idF.setError(errText(e)); return false; }
      dupTarget = id;
    } }] });
    if (r && dupTarget) { base = L.clone(doc); toast({ title: "Đã nhân bản", tone: "done" }); navigate(`/templates/${dupTarget}`); }
  }
  let dupTarget = null;

  async function openMore() {
    const ro3 = readOnly();
    const content = h("div", { class: "stack st-more" },
      btn({ label: "Nhân bản template này", icon: "copy", onClick: () => { dlg.close(); openDuplicate(); } }),
      btn({ label: "Thư viện asset (xem, tải lên, xoá)", icon: "image", onClick: () => { dlg.close(); openAssetLibrary({ title: "Thư viện asset" }); } }),
      ro3 && meta.scope !== "builtin" && doc.status !== "draft" && !meta.versions.some((v) => v.status === "draft") ? btn({ label: "Tạo bản nháp mới (version kế tiếp)", icon: "plus", onClick: (e) => { dlg.close(); newDraft(e.currentTarget); } }) : null,
      meta.scope === "user" && doc.status === "draft" ? btn({ label: "Xoá bản nháp này", icon: "trash", kind: "danger", onClick: () => { dlg.close(); deleteDraft(); } }) : null,
      meta.scope === "user" && meta.versions.some((v) => v.status === "published") ? btn({ label: "Lưu trữ template (ẩn khỏi kênh)", icon: "folder", kind: "danger", onClick: () => { dlg.close(); archive(); } }) : null,
      h("p", { class: "muted small" }, `Checksum nội dung: ${meta.checksum ? meta.checksum.slice(0, 12) : "—"}`));
    const dlg = h("dialog", { class: "dlg", "aria-label": "Thêm thao tác" }, h("h2", null, "Thao tác khác"), content, h("div", { class: "actions" }, btn({ label: "Đóng", onClick: () => dlg.close() })));
    dlg.addEventListener("close", () => dlg.remove());
    document.getElementById("dialogs").append(dlg);
    dlg.showModal();
    dlg.querySelector("button")?.focus();
  }

  async function archive() {
    const used = meta.used_by?.length ? ` ${meta.used_by.length} kênh đang chọn template này: job mới của họ sẽ báo lỗi cho tới khi chọn template khác.` : "";
    if (!(await confirmDialog({ title: "Lưu trữ template?", body: `Template không còn được đề xuất cho kênh; version cũ vẫn dùng được cho job đã tạo.${used}`, confirmLabel: "Lưu trữ", danger: true }))) return;
    try { const r = await api.post(`/api/templates/${tid}/archive`, {}); toast({ title: "Đã lưu trữ", message: r.warning || undefined, tone: r.warning ? "wait" : "done", sticky: !!r.warning }); await load(doc.version); } catch (e) { toastError(e, "Chưa lưu trữ được"); }
  }
  async function deleteDraft() {
    if (!(await confirmDialog({ title: "Xoá bản nháp?", body: `Xoá bản nháp v${doc.version}. Không thể khôi phục.`, confirmLabel: "Xoá", danger: true }))) return;
    try {
      await api.del(`/api/templates/${tid}/${doc.version}`);
      base = L.clone(doc);
      toast({ title: "Đã xoá bản nháp", tone: "done" });
      const left = meta.versions.filter((v) => v.version !== doc.version);
      if (left.length) await load(left.at(-1).version); else navigate("/templates");
    } catch (e) { toastError(e, "Chưa xoá được"); }
  }

  // ================================================================================ dựng
  function renderAll() {
    renderTitle(); buildToolbar(); renderBanner(); renderLayers(); renderStage(); renderProps(); renderResults(); updateToolbar();
    scope.add(() => motion.itemsEnter([layersHost, propsHost]));
  }

  await load(query.get("v") || undefined);
  return {
    destroy() {
      alive = false; ro.disconnect();
      document.removeEventListener("click", guardClick, true); document.removeEventListener("keydown", onKey);
      window.removeEventListener("beforeunload", guardUnload); window.removeEventListener("pointermove", onMove);
      for (const u of urlsToForget) forgetBlob(u);
    },
  };
}
