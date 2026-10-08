// Kho nhân vật (Living Character Universe): bảng điều khiển, danh mục tìm/lọc, hồ sơ có revision + khóa + lưu trữ, nhập/xuất Excel có xem trước.
// Kho TỰ lớn lên từ các truyện đạt QA; người dùng không bắt buộc làm gì ở đây (mọi thao tác đều tuỳ chọn).
import { api, ApiError } from "../api.js";
import { h, clear, loadCss } from "../dom.js";
import { icon } from "../icons.js";
import { btn, busy, field, input, select, textarea, badge, alertBox, emptyState, errorState, skeleton, pageHead, toast, toastError, confirmDialog, openDialog, tabs, disclosure } from "../components.js";
import * as L from "../universe_logic.js";
import { mountStories } from "./_universe_stories.js";
import { mountChanges } from "./_universe_changes.js";

const STATUS_TONE = { active: { tone: "done", icon: "check-circle" }, archived: { tone: "off", icon: "folder" }, staged: { tone: "wait", icon: "hourglass" } };

export async function mount(root, ctx) {
  loadCss("/css/universe.css");
  const { scope, navigate } = ctx;
  let alive = true, sum = null, selected = ctx.params?.[0] || null, detail = null, tab = "profile";
  const flt = { q: "", status: "active", role: "", genre: "" };
  const dash = h("div", { class: "uv-dash", "aria-live": "polite" });
  const listHost = h("div", { class: "stack", "aria-live": "polite" });
  const detailHost = h("div", { class: "uv-detail" });
  let timer = null;

  const addBtn = btn({ label: "Thêm nhân vật", icon: "plus", kind: "primary", onClick: () => openCreate() });
  const expBtn = btn({ label: "Xuất Excel", icon: "arrow-down", onClick: (e) => busy(e.currentTarget, exportXlsx) });
  const impBtn = btn({ label: "Nhập Excel", icon: "upload", onClick: () => openImport() });
  const charsPane = h("div", { id: "panel-chars", role: "tabpanel", "aria-labelledby": "tab-chars" }, h("div", { class: "uv-layout" }, h("div", { class: "stack uv-left" }, filters(), listHost), detailHost));
  const storiesPane = h("div", { id: "panel-stories", role: "tabpanel", "aria-labelledby": "tab-stories", hidden: true });
  const changesPane = h("div", { id: "panel-changes", role: "tabpanel", "aria-labelledby": "tab-changes", hidden: true });
  let storiesInst = null, changesInst = null;
  function showTop(t) {
    charsPane.hidden = t !== "chars";
    storiesPane.hidden = t !== "stories";
    changesPane.hidden = t !== "changes";
    if (t === "changes") { if (changesInst) changesInst.reload(); else changesInst = mountChanges(changesPane, { onChanged: async () => { await loadSummary(); loadList(); } }); }
    if (t === "stories" && !storiesInst) storiesInst = mountStories(storiesPane, { onOpenCharacter: (id) => { setUrl(id); selected = id; document.querySelector("#tab-chars")?.click(); loadDetail(); loadList(); } });
  }

  root.append(pageHead("Kho nhân vật", "Nhân vật tự được tạo và dùng lại khi Story Remix viết truyện. Bạn không cần làm gì ở đây — chỉ xem, sửa hồ sơ hoặc lưu trữ khi muốn.",
    h("div", { class: "row" }, addBtn, expBtn, impBtn)), dash, h("div", { class: "uv-tabs" }, tabs({ items: [["chars", "Nhân vật"], ["stories", "Truyện & dàn nhân vật"], ["changes", "Nhật ký cập nhật"]], active: "chars", onSelect: (t) => showTop(t), label: "Kho nhân vật" })),
    charsPane, storiesPane, changesPane);

  // ---------------------------------------------------------------- bảng điều khiển
  function stat(label, value, hint) {
    return h("div", { class: "uv-stat" }, h("div", { class: "uv-stat-v" }, String(value)), h("div", { class: "uv-stat-l" }, label), hint ? h("div", { class: "muted small" }, hint) : null);
  }
  function paintDash() {
    clear(dash);
    if (!sum) return;
    dash.append(stat("Nhân vật đang dùng", sum.active), stat("Đã lưu trữ", sum.archived), stat("Chờ truyện đạt QA", sum.staged_candidates, "Chưa vào kho chính thức"),
      stat("Truyện đã dùng kho", sum.stories), stat("Dùng lại / mới", `${sum.reused_appearances} / ${sum.new_character_appearances}`, L.reuseSummary(sum)),
      stat("Cập nhật gần nhất", L.fmtTime(sum.last_publish_at)), stat("Xung đột ghi", sum.write_conflicts, "Lần ghi bị chặn vì hồ sơ đã đổi"));
  }

  // ---------------------------------------------------------------- bộ lọc + danh sách
  function filters() {
    const q = input({ type: "search", placeholder: "Tìm theo tên, ID, tính cách…", "aria-label": "Tìm nhân vật" });
    q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { flt.q = q.value.trim(); loadList(); }, 250); });
    const st = select({ options: [["active", "Đang dùng"], ["archived", "Lưu trữ"], ["", "Tất cả"]], value: flt.status, onChange: (v) => { flt.status = v; loadList(); } });
    st.setAttribute("aria-label", "Trạng thái");
    const role = select({ options: [["", "Mọi vai"]], value: "", onChange: (v) => { flt.role = v; loadList(); } });
    role.setAttribute("aria-label", "Vai từng đảm nhận");
    root.addEventListener("uv:roles", () => { role.replaceChildren(...[["", "Mọi vai"], ...sum.roles.map((r) => [r.role_code, r.label])].map(([v, t]) => h("option", { value: v }, t))); role.value = flt.role; });
    const genre = input({ placeholder: "Thể loại hợp", "aria-label": "Lọc theo thể loại" });
    genre.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { flt.genre = genre.value.trim(); loadList(); }, 250); });
    return h("div", { class: "uv-filters", role: "search" }, q, h("div", { class: "row" }, st, role, genre));
  }

  function card(c) {
    const el = h("button", { type: "button", class: "uv-card", "aria-current": c.character_id === selected ? "true" : null, onclick: () => select_(c.character_id) },
      h("span", { class: "uv-avatar", "aria-hidden": "true" }, L.initials(c.display_name)),
      h("span", { class: "uv-card-main" }, h("strong", null, c.display_name), h("span", { class: "mono small muted" }, c.character_id),
        h("span", { class: "uv-chips" }, badge({ ...STATUS_TONE[c.status], label: L.STATUS_LABEL[c.status] }), c.locked ? badge({ tone: "attn", icon: "lock", label: "Khóa cốt lõi" }) : null,
          ...c.genre_affinities.slice(0, 3).map((g) => h("span", { class: "chip" }, g))),
        h("span", { class: "muted small" }, c.appearance_count ? `${c.appearance_count} lần xuất hiện${c.roles.length ? " · " + c.roles.join(", ") : ""}` : "Chưa xuất hiện trong truyện nào")));
    return el;
  }

  async function loadList() {
    try {
      const r = await api.get("/api/universe/characters", { query: { ...flt, limit: 100 } });
      if (!alive) return;
      clear(listHost);
      if (!r.items.length) {
        const empty = !sum.active && !sum.archived && !flt.q && !flt.role && !flt.genre;
        listHost.append(empty
          ? emptyState({ icon: "database", title: "Kho nhân vật đang trống", text: "Đó là bình thường. Khi Story Remix viết truyện đầu tiên, nhân vật sẽ được tạo và lưu vào đây sau khi truyện đạt QA. Bạn cũng có thể thêm nhân vật thủ công hoặc nhập từ Excel.",
                              action: btn({ label: "Thêm nhân vật", icon: "plus", onClick: () => openCreate() }) })
          : emptyState({ icon: "search", title: "Không có nhân vật phù hợp", text: "Thử đổi bộ lọc hoặc từ khóa." }));
        return;
      }
      listHost.append(h("div", { class: "muted small" }, `${r.total} nhân vật`), h("div", { class: "uv-list" }, ...r.items.map(card)));
    } catch (e) { if (alive) { clear(listHost); listHost.append(errorState(e, loadList)); } }
  }

  async function loadSummary() {
    sum = await api.get("/api/universe/summary");
    paintDash();
    root.dispatchEvent(new Event("uv:roles"));
  }

  // ---------------------------------------------------------------- hồ sơ
  const setUrl = (id) => history.replaceState(null, "", "#/universe" + (id ? "/" + id : ""));
  function select_(id) { selected = id; setUrl(id); loadDetail(); loadList(); }

  async function loadDetail() {
    clear(detailHost);
    if (!selected) { detailHost.append(h("div", { class: "uv-hint muted" }, "Chọn một nhân vật để xem và sửa hồ sơ.")); return; }
    detailHost.append(skeleton(5));
    try {
      detail = await api.get(`/api/universe/characters/${selected}`);
      if (alive) paintDetail();
    } catch (e) { if (alive) { clear(detailHost); detailHost.append(errorState(e, loadDetail)); } }
  }

  function paintDetail(banner) {
    const ch = detail.character, fields = sum.fields, ro = L.editability(fields, ch);
    clear(detailHost);
    const ctrls = {};
    const form = h("div", { class: "stack" });
    for (const f of fields) {
      const long = f.type === "list" || (f.type === "text" && f.limit > 200);
      const ctl = long ? textarea({ rows: f.type === "list" ? 2 : 4, value: L.formFromProfile(fields, ch)[f.key], disabled: ro.has(f.key) })
                       : input({ value: ch[f.key] ?? "", disabled: ro.has(f.key) });
      const fl = field({ label: f.label + (f.core ? " (cốt lõi)" : ""), control: ctl, hint: f.type === "list" ? "Mỗi mục một dòng." : null, required: f.key === "display_name" });
      ctrls[f.key] = { ctl, fl };
      form.append(fl);
    }
    const values = () => Object.fromEntries(fields.map((f) => [f.key, ctrls[f.key].ctl.value]));
    const saveB = btn({ label: "Lưu thay đổi", icon: "save", kind: "primary", disabled: true });
    const sync = () => {
      const p = L.profileFromForm(fields, values()), errs = L.validate(fields, p), ck = L.changedKeys(fields, ch, p).filter((k) => !ro.has(k));
      for (const f of fields) ctrls[f.key].fl.setError(errs[f.key] || null);
      saveB.disabled = !ck.length || !!Object.keys(errs).length;
      saveB.title = ck.length ? `${ck.length} mục đã sửa` : "Chưa có thay đổi";
    };
    for (const f of fields) ctrls[f.key].ctl.addEventListener("input", sync);
    saveB.addEventListener("click", () => busy(saveB, async () => {
      const p = L.profileFromForm(fields, values()), fieldsChanged = Object.fromEntries(L.changedKeys(fields, ch, p).map((k) => [k, p[k]]));
      try {
        detail = { ...(await api.put(`/api/universe/characters/${ch.character_id}`, { revision: ch.revision, fields: fieldsChanged })), revision: detail.revision };
        detail.character = (await api.get(`/api/universe/characters/${ch.character_id}`)).character;
        toast({ title: "Đã lưu hồ sơ", tone: "done" });
        await Promise.all([loadSummary(), loadList()]);
        paintDetail();
      } catch (e) {
        if (e instanceof ApiError && e.code === "REVISION_CONFLICT") { await loadSummary(); paintDetail(alertBox({ tone: "wait", title: e.message, body: e.hint, actions: [btn({ label: "Tải lại bản mới", icon: "refresh", size: "sm", onClick: loadDetail })] })); }
        else toastError(e, "Chưa lưu được");
      }
    }));
    const lockB = btn({ label: ch.locked ? "Mở khóa cốt lõi" : "Khóa cốt lõi", icon: "lock", disabled: ch.status === "archived",
      title: "Khóa = không đổi tính cách/động cơ/ràng buộc cốt lõi (truyện mới luôn thấy cùng danh tính)", onClick: () => act(ch.locked ? "unlock" : "lock") });
    const arcB = btn({ label: ch.status === "archived" ? "Khôi phục" : "Lưu trữ", icon: ch.status === "archived" ? "undo" : "folder", kind: ch.status === "archived" ? "" : "ghost", onClick: async () => {
      if (ch.status !== "archived" && !(await confirmDialog({ title: `Lưu trữ “${ch.display_name}”?`, body: "Nhân vật lưu trữ sẽ không được chọn cho truyện mới. Lịch sử các truyện cũ giữ nguyên. Có thể khôi phục bất cứ lúc nào.", confirmLabel: "Lưu trữ" }))) return;
      act(ch.status === "archived" ? "restore" : "archive");
    } });
    async function act(a) {
      try {
        await api.post(`/api/universe/characters/${ch.character_id}/${a}`, { revision: ch.revision });
        toast({ title: { lock: "Đã khóa cốt lõi", unlock: "Đã mở khóa", archive: "Đã lưu trữ", restore: "Đã khôi phục" }[a], tone: "done" });
        await Promise.all([loadSummary(), loadList(), loadDetail()]);
      } catch (e) { toastError(e, "Chưa thực hiện được"); if (e.code === "REVISION_CONFLICT") loadDetail(); }
    }
    const head = h("div", { class: "uv-detail-head" }, h("span", { class: "uv-avatar lg", "aria-hidden": "true" }, L.initials(ch.display_name)),
      h("div", null, h("h2", null, ch.display_name), h("div", { class: "mono small muted" }, `${ch.character_id} · revision ${ch.revision}`),
        h("div", { class: "uv-chips" }, badge({ ...STATUS_TONE[ch.status], label: L.STATUS_LABEL[ch.status] }), ch.locked ? badge({ tone: "attn", icon: "lock", label: "Khóa cốt lõi" }) : null,
          h("span", { class: "chip" }, L.ORIGIN_LABEL[ch.origin] || ch.origin))));
    const notes = [];
    if (ch.status === "archived") notes.push(alertBox({ tone: "info", title: "Nhân vật đang lưu trữ", body: "Hồ sơ chỉ xem được. Khôi phục để sửa hoặc để AI dùng lại." }));
    else if (ch.locked) notes.push(alertBox({ tone: "info", title: "Phần cốt lõi đang bị khóa", body: "Vẫn sửa được tên, bí danh, thể loại và gợi ý hình ảnh/giọng." }));
    const body = h("div", { class: "stack" });
    const show = (t) => {
      tab = t; clear(body);
      body.id = "panel-" + t; body.setAttribute("role", "tabpanel"); body.setAttribute("aria-labelledby", "tab-" + t);
      if (t === "profile") body.append(form, h("div", { class: "row" }, saveB, lockB, arcB), h("p", { class: "muted small" }, "Sửa ở đây chỉ đổi hồ sơ toàn cục (danh tính ổn định). Sự kiện trong từng truyện (chết, yêu, nghề…) thuộc về truyện đó, không lan sang truyện khác."));
      else if (t === "stories") body.append(storiesTab(ch));
      else body.append(auditTab(ch));
    };
    detailHost.append(...[head, ...notes, banner, tabs({ items: [["profile", "Hồ sơ"], ["stories", `Xuất hiện (${ch.appearances.length})`], ["audit", "Nhật ký"]], active: tab, onSelect: show, label: "Chi tiết nhân vật" }), body].filter(Boolean));   // Node.append(null) sẽ chèn chữ "null"
    show(tab);
    sync();
  }

  function storiesTab(ch) {
    if (!ch.appearances.length) return h("p", { class: "muted" }, "Chưa xuất hiện trong truyện nào. Khi một truyện dùng nhân vật này và đạt QA, vai và kết cục sẽ hiện ở đây.");
    return h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", null, h("tr", null, ...["Truyện", "Vai", "Kết cục (chỉ trong truyện đó)", "Lúc"].map((t) => h("th", null, t)))),
      h("tbody", null, ...ch.appearances.map((a) => h("tr", null, h("td", { class: "mono" }, a.story_id), h("td", null, a.role_code), h("td", null, a.outcome || "—"), h("td", null, L.fmtTime(a.created_at)))))));
  }

  function auditTab(ch) {
    const names = { create: "Tạo", update: "Sửa", archive: "Lưu trữ", restore: "Khôi phục", lock: "Khóa", unlock: "Mở khóa", conflict: "Xung đột ghi", publish: "Publish", revert: "Hoàn tác" };
    if (!ch.audit.length) return h("p", { class: "muted" }, "Chưa có thay đổi.");
    return h("ol", { class: "uv-audit" }, ...ch.audit.map((a) => h("li", null, h("strong", null, names[a.action] || a.action), " ", h("span", { class: "muted small" }, `${L.fmtTime(a.ts)} · ${a.actor}${a.job_id ? " · job " + a.job_id : ""}`),
      a.before || a.after ? h("div", { class: "small uv-diff" }, Object.keys({ ...(a.before || {}), ...(a.after || {}) }).filter((k) => k !== "origin" && k !== "status" || a.action !== "create").slice(0, 6).map((k) => `${k}: ${JSON.stringify(a.before?.[k] ?? null)} → ${JSON.stringify(a.after?.[k] ?? null)}`).join("\n")) : null)));
  }

  // ---------------------------------------------------------------- tạo / xuất / nhập
  async function openCreate() {
    const name = input({ placeholder: "Ví dụ: Lan Phương", maxlength: 80 });
    const pers = textarea({ rows: 3, placeholder: "Tính cách cốt lõi: điều khiến nhân vật này luôn là chính họ." });
    const mot = textarea({ rows: 2, placeholder: "Mỗi động cơ một dòng" });
    const gen = textarea({ rows: 2, placeholder: "Thể loại hợp (mỗi dòng một thể loại)" });
    const nf = field({ label: "Tên hiển thị", control: name, required: true });
    const content = h("div", { class: "stack" }, nf, field({ label: "Tính cách cốt lõi", control: pers }), field({ label: "Động cơ", control: mot }), field({ label: "Thể loại hợp", control: gen }));
    const r = await openDialog({ title: "Thêm nhân vật", describe: "Các trường còn lại sửa sau trong hồ sơ.", content, actions: [{ label: "Huỷ", value: null }, { label: "Thêm", kind: "primary", value: "ok", onClick: async () => {
      if (!name.value.trim()) { nf.setError("Tên hiển thị là bắt buộc."); return false; }
      try {
        const res = await api.post("/api/universe/characters", { display_name: name.value, core_personality: pers.value, motivations: L.listFromText(mot.value), genre_affinities: L.listFromText(gen.value) });
        selected = res.character.character_id;
        return true;
      } catch (e) { nf.setError(e.message + (e.hint ? " " + e.hint : "")); return false; }
    } }] });
    if (r !== "ok") return;
    toast({ title: "Đã thêm nhân vật", tone: "done" });
    await loadSummary(); setUrl(selected); await Promise.all([loadList(), loadDetail()]);
  }

  async function exportXlsx() {
    try {
      const url = await api.blobUrl("/api/universe/export.xlsx", { fresh: true });
      const a = h("a", { href: url, download: "kho-nhan-vat.xlsx" });
      document.body.append(a); a.click(); a.remove();
      toast({ title: "Đã xuất Excel", message: "File phản ánh kho tại thời điểm này. Sửa xong hãy nhập lại; dòng nào đã đổi trong app sẽ báo xung đột.", tone: "done" });
    } catch (e) { toastError(e, "Chưa xuất được"); }
  }

  async function openImport() {
    const file = h("input", { type: "file", accept: ".xlsx", "aria-label": "Chọn file Excel (.xlsx)" });
    const out = h("div", { class: "stack", "aria-live": "polite" });
    const skip = h("input", { type: "checkbox" });
    const skipRow = h("label", { class: "row", hidden: true }, skip, "Bỏ qua các dòng xung đột và nhập phần còn lại");
    let bytes = null, rep = null;
    const applyAct = { label: "Nhập", kind: "primary", value: "ok", onClick: async () => {
      if (!L.canApply(rep, skip.checked)) { toast({ title: "Chưa nhập được", message: L.importHeadline(rep).text, tone: "wait" }); return false; }
      try { const r = await api.upload("/api/universe/import/apply", bytes, { query: { skip_conflicts: skip.checked ? 1 : 0 } }); toast({ title: "Đã nhập", message: `Tạo ${r.created.length}, cập nhật ${r.updated.length}.`, tone: "done" }); return true; }
      catch (e) { toastError(e, "Chưa nhập được"); return false; }
    } };
    file.addEventListener("change", async () => {
      clear(out); rep = null;
      if (!file.files[0]) return;
      bytes = await file.files[0].arrayBuffer();
      try {
        rep = await api.upload("/api/universe/import/preview", bytes);
        const hl = L.importHeadline(rep);
        skipRow.hidden = !rep.counts.conflict;
        out.append(alertBox({ tone: hl.tone, title: hl.text, body: rep.stale ? "File được xuất từ revision cũ hơn kho hiện tại." : null }),
          h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", null, h("tr", null, ...["Dòng", "Việc", "Nhân vật", "Ghi chú"].map((t) => h("th", null, t)))),
            h("tbody", null, ...rep.rows.filter((r) => r.action !== "unchanged").map((r) => h("tr", null, h("td", null, String(r.row)), h("td", null, badge({ tone: L.ACTION_TONE[r.action], icon: r.action === "error" ? "x-circle" : r.action === "conflict" ? "alert" : "check", label: L.ACTION_LABEL[r.action] })),
              h("td", null, r.name || r.id), h("td", { class: "small" }, r.message || (r.changes?.length ? "Đổi: " + r.changes.join(", ") : ""))))))));
      } catch (e) { out.append(errorState(e)); }
    });
    const r = await openDialog({ title: "Nhập từ Excel", wide: true, describe: "Chọn file .xlsx do ứng dụng xuất. Bạn sẽ xem trước trước khi ghi; có lỗi thì không nhập gì cả.",
      content: h("div", { class: "stack" }, field({ label: "File Excel", control: file }), skipRow, out), actions: [{ label: "Huỷ", value: null }, applyAct] });
    if (r === "ok") { await loadSummary(); await Promise.all([loadList(), selected ? loadDetail() : null]); }
  }

  // ---------------------------------------------------------------- khởi động
  try {
    await loadSummary();
    await Promise.all([loadList(), loadDetail()]);
  } catch (e) { clear(dash); dash.append(errorState(e, () => navigate("/universe", { replace: true }))); }
  return { destroy() { alive = false; clearTimeout(timer); } };
}
