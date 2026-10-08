// Màn hình chính: dán đầu vào -> (chọn kênh) -> RUN. Hệ thống nhận dạng đầu vào, chỉ đề xuất chế độ hợp lệ, hiện những gì sẽ tự chọn.
import { api, newRequestId } from "../api.js";
import { h, clear, patchList } from "../dom.js";
import { icon } from "../icons.js";
import { btn, busy, field, input, select, switchCtl, alertBox, pageHead, toast, toastError, openDialog } from "../components.js";
import { createPoller } from "../poller.js";
import { publishCounts } from "../router.js";
import { createSamples } from "../samples.js";
import { disclosure } from "../components.js";
import * as motion from "../motion.js";
import { makeRow, updateRow, rowKey } from "./_batch_ui.js";
import { channelRun } from "./_channel_run.js";
import { guidanceEditor } from "./_story_guidance.js";
import { createPayload } from "../story_guidance_logic.js";
import { storyModeEditor } from "./_story_mode.js";

const LS = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* bỏ qua */ } } };

export async function mount(root, ctx) {
  const { app, navigate, scope } = ctx;
  const boot = app.boot;
  const s = { value: "", kindOverride: null, title: "", channel: LS.get("cf-channel") || boot.default_channel, run: null, kids: null, remember: true,
              autoResume: boot.auto_resume_default, rid: null, seq: 0, preview: null, running: false,
              custom: false, stages: new Set(), isColl: false, collLabel: "" };                       // custom: pipeline tùy chỉnh; stages = các bước người dùng CHỌN (phần bắt buộc do backend tính)
  if (!boot.channels.some((c) => c.id === s.channel)) s.channel = boot.channels[0]?.id || boot.default_channel;
  let timer = null;

  // ---------- thành phần ----------
  const valueIn = input({ placeholder: "Dán link YouTube hoặc đường dẫn file", "aria-describedby": "detect-line", autofocus: true, inputmode: "url" });
  const pickBtn = btn({ label: "Chọn file…", icon: "file", onClick: async (e) => {
    await busy(e.currentTarget, async () => {
      try {
        const r = await api.post("/api/pick", { kind: "file", title: "Chọn file đầu vào" });
        if (r.unsupported) toast({ title: "Máy không có hộp thoại chọn file", message: r.message, tone: "wait" });
        else if (r.path) { valueIn.value = r.path; onValue(); }
      } catch (err) { toastError(err); }
    });
  } });
  const detectLine = h("div", { class: "detect", id: "detect-line", "aria-live": "polite" });
  const titleIn = input({ placeholder: "Tên truyện của bạn" });
  const titleField = field({ label: "Tên truyện", control: titleIn, hint: "Dùng cho thumbnail, tiêu đề YouTube và tên thư mục output." });
  const channelSel = select({ options: boot.channels.filter((c) => c.ok).map((c) => [c.id, c.name || c.id]), value: s.channel });
  const newChannelBtn = btn({ label: "Kênh mới", icon: "plus", size: "sm", kind: "ghost", onClick: () => createChannel() });
  const channelField = field({ label: "Kênh", control: channelSel });
  channelField.append(h("div", null, newChannelBtn));
  const modesBox = h("div", { class: "mode-list", role: "radiogroup", "aria-label": "Chạy đến đâu" });
  const modesField = h("div", { class: "field" }, h("div", { class: "label", id: "modes-label" }, "Chạy đến đâu"), modesBox);
  modesBox.setAttribute("aria-labelledby", "modes-label");
  const customSw = switchCtl({ label: "Tùy chỉnh các bước", checked: false, onChange: (v) => onCustom(v) });
  const stagesBox = h("div", { class: "stage-pick", role: "group", "aria-label": "Các bước muốn chạy", hidden: true });
  const customField = h("div", { class: "field", hidden: true }, customSw,
    h("p", { class: "muted small" }, "Chọn kết quả bạn muốn; bước cần thiết sẽ tự được thêm và khóa lại, bước đã có sẵn dùng lại."), stagesBox);
  const kidsBox = h("div", { class: "sub-card", hidden: true, role: "group", "aria-labelledby": "kids-label" });
  const autoSw = switchCtl({ label: "Auto Resume", checked: s.autoResume, onChange: (v) => { s.autoResume = v; } });
  const autoHint = h("p", { class: "muted small" }, "Khi mất mạng, hết quota… job tự chạy tiếp thay vì chờ bạn bấm Tiếp tục.");
  const previewBox = h("div", { class: "sub-card stack", hidden: true, "aria-live": "polite" });
  const guidance = guidanceEditor({ onChange: () => { s.rid = null; } });                       // Đề xuất truyện cho job này (mặc định: dùng đề xuất trong Cài đặt)
  const guidanceBox = h("div", { class: "sub-card", hidden: true }, guidance.el);
  const storyMode = storyModeEditor({ onChange: () => { s.rid = null; } });                       // Story hiện có | Story Remix (mặc định: Story hiện có)
  const storyModeBox = h("div", { class: "sub-card", hidden: true }, storyMode.el);
  api.get("/api/settings").then((st) => guidance.setDefault(st.items.find((i) => i.key === "story.guidance")?.value || "")).catch(() => { /* chỉ là phần xem trước */ });
  const problems = h("div", { class: "stack", "aria-live": "polite" });
  const runBtn = btn({ label: "RUN", icon: "play", kind: "primary lg", type: "button", disabled: true });
  const runNote = h("span", { class: "muted small", id: "run-note" });
  runBtn.setAttribute("aria-describedby", "run-note");

  const fake = boot.runtime.fake_adapters || [];
  const notice = fake.length
    ? alertBox({ tone: "info", title: "Đang ở chế độ thử nghiệm", body: `Các bước ${fake.join(", ")} đang dùng bản giả (video/giọng đọc chỉ để kiểm tra pipeline). Chạy "setup" và cấu hình để dùng thật; mở Cài đặt & Doctor để xem còn thiếu gì.` })
    : null;
  const noChannels = boot.channels.length === 0;

  const panel = channelRun({ getChannel: () => s.channel, onChange: () => { s.rid = null; schedule(0); } });
  const card = h("div", { class: "card run-card stack" },
    h("div", { class: "field" }, h("label", { for: "run-input" }, "Đầu vào"), h("div", { class: "input-row" }, valueIn, pickBtn), detectLine),
    panel.el, titleField, channelField, modesField, customField, storyModeBox, guidanceBox, kidsBox, previewBox, problems,
    h("div", { class: "stack" }, autoSw, autoHint),
    h("div", { class: "run-actions" }, runBtn, runNote));
  valueIn.id = "run-input";

  const recent = h("ul", { class: "joblist" });
  const recentCard = h("section", { class: "card flush", "aria-labelledby": "recent-h", hidden: true },
    h("div", { class: "card-title", style: "" }, h("h2", { id: "recent-h" }, "Gần đây"), btn({ label: "Xem tất cả", icon: "list", kind: "ghost", size: "sm", href: "#/jobs" })), recent);
  recentCard.firstChild.style.padding = "var(--s-4) var(--s-4) 0";

  // dữ liệu mẫu: cho người chưa có truyện/video để thử
  const samplesOut = h("div", { class: "row" });
  const makeSamples = btn({ label: "Tạo dữ liệu mẫu", icon: "film", onClick: async (e) => {
    const r = await createSamples(e.currentTarget);
    if (!r) return;
    clear(samplesOut);
    for (const [label, key] of [["Dùng truyện mẫu", "story"], ["Dùng phụ đề mẫu", "subtitle"], ["Dùng audio mẫu", "audio"]]) {
      samplesOut.append(btn({ label, size: "sm", onClick: () => { valueIn.value = r[key]; onValue(); valueIn.focus(); } }));
    }
    if (r.registered.length) app.boot.runtime = { ...app.boot.runtime };
  } });
  const samplesBlock = disclosure({ label: "Chưa có truyện hoặc video để thử?", content: h("div", { class: "stack", style: "padding-top: var(--s-2)" },
    h("p", { class: "muted small" }, "Tạo sẵn một truyện ngắn, phụ đề, audio và vài video nền (ngang + dọc) trong thư mục samples/ để chạy thử toàn bộ pipeline; pool video nền được đăng ký giúp bạn. Đây chỉ là dữ liệu tổng hợp, không phải nội dung thật."),
    h("div", { class: "row" }, makeSamples), samplesOut) });

  root.append(pageHead("Chạy", "Dán link, chọn kênh, bấm RUN. Phần còn lại hệ thống tự lo."),
    h("div", { class: "stack" }, notice, noChannels ? noChannelNotice() : null, card, samplesBlock, recentCard));

  function noChannelNotice() {
    return alertBox({ tone: "wait", title: "Chưa có kênh nào", body: "Tạo một kênh để hệ thống biết đặt tên, watermark, giọng đọc và video nền cho bạn.", actions: [btn({ label: "Tạo kênh", icon: "plus", size: "sm", onClick: () => createChannel() })] });
  }

  // ---------- nhập liệu ----------
  function onValue() {
    s.value = valueIn.value.trim();
    s.isColl = false;
    panel.setValue("");
    s.kindOverride = null;
    s.run = null;
    s.rid = null;
    schedule(250);
  }
  valueIn.addEventListener("input", onValue);
  valueIn.addEventListener("paste", () => setTimeout(onValue, 0));
  titleIn.addEventListener("input", () => { s.title = titleIn.value; s.rid = null; schedule(400); });
  channelSel.addEventListener("change", () => { s.channel = channelSel.value; LS.set("cf-channel", s.channel); s.rid = null; s.kids = null; clear(kidsBox); panel.reload(); schedule(0); });
  valueIn.addEventListener("keydown", (e) => { if (e.key === "Enter" && !runBtn.disabled) runBtn.click(); });

  function schedule(ms) { clearTimeout(timer); timer = setTimeout(refresh, ms); }

  async function refresh() {
    const my = ++s.seq;
    if (!s.value) { s.preview = null; paint(); return; }
    try {
      // Link kênh/playlist: kế hoạch + kiểm tra (made_for_kids, template…) tính trên video đầu tiên được chọn; nhận dạng kênh/playlist giữ nguyên (sticky) tới khi ô nhập đổi
      const value = (s.isColl && panel.firstUrl()) || s.value;
      const pv = await api.post("/api/preview", { input: { value, kind: s.kindOverride }, channel: s.channel, run: s.run, title: s.title, kids: s.kids, pipeline: pipelineBody() });
      if (my !== s.seq) return;                         // đã có lần nhập mới hơn
      if (pv.detect?.collection && !s.isColl) { s.isColl = true; s.collLabel = pv.detect.label; panel.setValue(s.value); }
      s.preview = pv;
      if (!s.run) s.run = pv.run;
    } catch (e) {
      if (my !== s.seq) return;
      s.preview = { error: e };
    }
    paint();
  }

  function paint() {
    const pv = s.preview;
    clear(detectLine);
    // nhận dạng
    if (!s.value) detectLine.append(h("span", { class: "muted" }, "Hệ thống tự nhận ra: link YouTube, phụ đề, truyện (story.txt), audio…"));
    else if (pv?.error) detectLine.append(icon("alert-circle", { size: 16 }), h("span", null, pv.error.message));
    else if (pv) {
      const d = pv.detect;
      if (d.ok) {
        detectLine.append(h("span", { class: "chip ok" }, icon("check", { size: 14 }), s.isColl ? s.collLabel : d.label));
        const det = s.isColl ? {} : (d.details || {});
        if (det.video_id) detectLine.append(h("span", { class: "muted mono" }, det.video_id));
        if (det.name) detectLine.append(h("span", { class: "muted" }, det.name));
        if (d.ambiguous) {
          const alt = d.alternatives[0];
          const txt = alt === "story_text" ? "Đây là truyện (story.txt)" : "Đây là phụ đề / transcript";
          detectLine.append(btn({ label: txt, kind: "ghost", size: "sm", onClick: () => { s.kindOverride = alt; s.run = null; refresh(); } }));
        }
        if (d.kind === "project") detectLine.append(h("span", { class: "muted" }, `Project "${det.title || ""}" đã có sẵn — mở thư mục để xem.`), btn({ label: "Mở job", size: "sm", kind: "ghost", href: det.job_id ? `#/jobs/${det.job_id}` : undefined, disabled: !det.job_id }));
      } else if (d.problem) detectLine.append(icon("alert-circle", { size: 16 }), h("span", null, d.problem));
    }
    // tên truyện
    const d = pv?.detect;
    titleField.hidden = !(d && d.ok && d.modes.length) || s.isColl;            // job con lấy tên từ tiêu đề video nguồn
    const needs = !!d?.needs_title;
    titleField.querySelector("label").lastChild?.nodeName === "SPAN" && titleField.querySelector("label").lastChild.remove();
    if (needs) titleField.querySelector("label").append(h("span", { class: "muted", "aria-hidden": "true" }, " *"));
    titleIn.required = needs;
    titleIn.placeholder = needs ? "Bắt buộc: tên truyện của bạn" : d?.auto_title ? "Không bắt buộc — để trống thì tự đặt theo tên file" : "Không bắt buộc — để trống thì dùng tiêu đề video nguồn đã làm sạch";
    // chế độ
    const hasModes = !!(d && d.ok && d.modes.length);
    modesField.hidden = !hasModes || s.custom;
    customField.hidden = !hasModes;
    if (!modesField.hidden) patchModes(d.modes);
    paintStages(pv);
    // đề xuất truyện: chỉ khi kế hoạch có chạy bước Truyện (Channel Run tạo job con theo Cài đặt nên không hiện)
    storyModeBox.hidden = guidanceBox.hidden = s.isColl || !(pv?.plan?.stages || []).some((x) => x.name === "story" && x.state === "run");
    // kids
    paintKids(pv);
    // preview kế hoạch + tự chọn
    paintPreview(pv);
    // vấn đề
    clear(problems);
    for (const p of pv?.problems || []) {
      if (p.field === "kids") continue;
      if (p.field === "title") { titleField.setError(p.hint ? `${p.message} ${p.hint}` : p.message); continue; }
      problems.append(alertBox({ tone: "wait", title: p.message, body: p.hint || null, actions: p.code === "INVALID_CHANNEL_TEMPLATE" ? [btn({ label: "Sửa template của kênh", size: "sm", href: `#/channels/${s.channel}` })] : [] }));
    }
    if (!(pv?.problems || []).some((p) => p.field === "title")) titleField.setError(null);
    const planOk = s.isColl ? !(pv?.problems?.length) && !!pv?.plan && !(pv?.preflight?.blocking?.length) : !!(pv && pv.can_run);
    const ok = planOk && (!s.isColl || panel.ready()) && !s.running;
    runBtn.disabled = !ok;
    runBtn.querySelector("span").textContent = s.isColl ? "Tạo Channel Run" : "RUN";
    runNote.textContent = !s.value ? "Nhập đầu vào để bắt đầu." : ok ? (s.isColl ? `Sẽ tạo ${panel.count()} job con độc lập.` : (pv.warnings?.[0] || "")) : (s.isColl && panel.why() ? panel.why() : (pv?.problems?.length ? "Hoàn thành các mục ở trên để chạy." : ""));
  }

  function patchModes(modes) {
    const added = patchList(modesBox, modes, (m) => m.id,
      (m) => {
        const r = h("input", { type: "radio", name: "run-mode", value: m.id });
        r.addEventListener("change", () => { s.run = m.id; s.rid = null; schedule(0); });
        const el = h("label", { class: "mode" }, r, h("div", null, h("div", { class: "m-label" }, m.label), h("div", { class: "m-desc" }, m.description)));
        el._r = r;
        return el;
      },
      (el) => { /* nội dung chế độ không đổi */ });
    for (const el of modesBox.children) el._r.checked = el._r.value === s.run;
    scope.add(() => motion.itemsEnter(added));
  }

  function pipelineBody() { return s.custom ? { mode: "custom", requested_stages: [...s.stages] } : null; }

  async function onCustom(on) {
    s.custom = on;
    s.rid = null;
    if (on && !s.stages.size) {
      try { s.stages = new Set((await api.get("/api/pipeline")).modes.FULL.requested_stages); }       // gợi ý ban đầu = Toàn bộ, lấy từ backend
      catch (e) { toastError(e); }
    }
    schedule(0);
  }

  // Danh sách bước: trạng thái (chọn / bắt buộc / dùng lại / không chạy) và lý do đều do backend tính; ở đây chỉ vẽ và bật/tắt phần người dùng chọn.
  function paintStages(pv) {
    const stages = s.custom && pv?.plan ? pv.plan.stages : [];
    stagesBox.hidden = !stages.length;
    const apply = (el, st) => {
      const role = st.role;
      const fixed = role === "locked" || role === "provided" || (role === "selected" && st.by.length > 0);
      el.dataset.role = role;
      el._cb.checked = role === "selected" || role === "locked";
      el._cb.disabled = fixed;
      const tag = el.querySelector(".s-tag");
      tag.replaceChildren(...({ locked: [icon("lock", { size: 14 }), "Bắt buộc"], provided: [icon("refresh", { size: 14 }), "Dùng lại"], selected: [icon("check", { size: 14 }), "Đã chọn"] }[role] || []));
      el.querySelector(".s-why").textContent = role === "not_requested" ? "" : st.reason + (role === "selected" && st.by.length ? " Bỏ chọn bước phía sau trước nếu muốn bỏ bước này." : "");
    };
    patchList(stagesBox, stages, (st) => st.name,
      (st) => {
        const cb = h("input", { type: "checkbox", id: `stage-${st.name}` });
        cb.addEventListener("change", () => { if (cb.checked) s.stages.add(st.name); else s.stages.delete(st.name); s.rid = null; schedule(0); });
        const el = h("label", { class: "pick-row", for: cb.id }, cb, h("span", { class: "s-label" }, st.label), h("span", { class: "s-tag" }), h("span", { class: "s-why" }));
        el._cb = cb;
        apply(el, st);
        return el;
      },
      apply);
  }

  function paintKids(pv) {
    // Khai báo COPPA: hiện khi kênh chưa khai; giữ hiện sau khi người dùng chọn (nếu không hộp sẽ biến mất ngay khi chọn)
    const show = !!pv?.needs_kids || (s.kids !== null && !!pv);
    kidsBox.hidden = !show;
    if (!show) { clear(kidsBox); return; }
    if (kidsBox.childElementCount) return;
    const mk = (v, text) => h("label", null, h("input", { type: "radio", name: "kids", value: String(v), onChange: () => { s.kids = v; s.rid = null; schedule(0); } }), text);
    kidsBox.append(h("div", { class: "label", id: "kids-label" }, "Video của kênh này có dành cho trẻ em không?"),
      h("p", { class: "muted small" }, "YouTube bắt buộc khai báo. Hệ thống không tự đoán."),
      h("div", { class: "radio-row" }, mk(false, "Không dành cho trẻ em"), mk(true, "Có, dành cho trẻ em")),
      h("label", { class: "switch" }, h("input", { type: "checkbox", checked: true, onChange: (e) => { s.remember = e.target.checked; } }), h("span", { class: "track", "aria-hidden": "true" }), h("span", null, "Ghi nhớ cho kênh này")));
  }

  function paintPreview(pv) {
    clear(previewBox);
    const show = !!(pv && pv.plan);
    previewBox.hidden = !show;
    if (!show) return;
    const plan = h("div", { class: "plan", role: "list", "aria-label": "Các bước sẽ chạy" });
    for (const st of pv.plan.stages) {
      plan.append(h("span", { class: "step", role: "listitem", dataset: { s: st.state }, title: { run: "Sẽ chạy", skip: "Đã có/bỏ qua", off: "Không chạy" }[st.state] },
        st.label, h("span", { class: "sr-only" }, ` — ${{ run: "sẽ chạy", skip: "bỏ qua", off: "không chạy" }[st.state]}`)));
    }
    previewBox.append(h("div", { class: "label" }, "Hệ thống sẽ làm"), plan);
    const lines = s.isColl ? panel.summaryLines() : [];
    lines.push(`Kênh “${pv.channel_name}”: tập kế tiếp là Full Audio ${pv.sequence_next}; chế độ đăng mặc định: ${pv.privacy}.`);
    if (pv.templates && Object.keys(pv.templates).length) {
      const L = { thumbnail: "Thumbnail", youtube: "YouTube", tiktok: "TikTok" };
      lines.push("Template: " + Object.entries(pv.templates).map(([k, t]) => `${L[k] || k}: ${t.name || t.id} (v${t.version})`).join(" · "));
    }
    for (const a of (pv.auto || []).filter((x) => !String(x.what).startsWith("template."))) lines.push(`Tự chọn ${a.what}: ${typeof a.value === "object" ? Array.isArray(a.value) ? a.value.join(", ") : "theo preset" : a.value} — ${a.why}`);
    previewBox.append(h("div", null, h("div", { class: "label small muted" }, "Đã tự nhận ra / tự chọn"), h("ul", { class: "autolist" }, ...lines.map((t) => h("li", null, t)))));
    if (pv.preflight) previewBox.append(preflightBlock(pv.preflight));
  }

  // Kiểm tra trước khi chạy: chỉ những gì kế hoạch này cần. Chỉ dòng "chặn" mới khoá nút RUN; còn lại là thông tin (job sẽ giữ lại và tự chạy tiếp).
  const PFI = { ok: ["check-circle", "Đạt"], warn: ["alert", "Lưu ý"], fail: ["x-circle", "Lỗi"] };
  function preflightBlock(pf) {
    const ul = h("ul", { class: "preflight", "aria-label": "Kết quả kiểm tra trước khi chạy" });
    for (const c of pf.checks) {
      const [ic, word] = PFI[c.status] || PFI.warn;
      ul.append(h("li", { dataset: { s: c.status } }, icon(ic, { size: 16 }), h("span", { class: "sr-only" }, word + ": "), h("strong", null, c.label), c.detail ? h("span", { class: "muted" }, ` — ${c.detail}`) : null,
        c.blocking ? h("span", { class: "chip warn" }, "Cần sửa trước khi chạy") : null, c.hint ? h("div", { class: "small muted" }, c.hint) : null));
    }
    const box = h("div", { class: "stack", style: "gap: var(--s-1)" }, h("div", { class: "label small muted" }, "Kiểm tra trước khi chạy"), ul);
    if (pf.skipped?.length) box.append(disclosure({ label: `Không kiểm tra ${pf.skipped.length} mục vì không cần`, content: h("ul", { class: "autolist" }, ...pf.skipped.map((t) => h("li", null, t))) }));
    return box;
  }

  // ---------- tạo kênh nhanh ----------
  async function createChannel() {
    const idIn = input({ placeholder: "kenh_a" });
    const nameIn = input({ placeholder: "Tên hiển thị" });
    const kids = h("div", { class: "radio-row" }, h("label", null, h("input", { type: "radio", name: "nk", value: "no", checked: true }), "Không dành cho trẻ em"), h("label", null, h("input", { type: "radio", name: "nk", value: "yes" }), "Dành cho trẻ em"));
    const idF = field({ label: "Mã kênh", control: idIn, hint: "Chữ không dấu, số, _ và -.", required: true });
    const content = h("div", { class: "stack" }, idF, field({ label: "Tên hiển thị", control: nameIn }), h("div", { class: "field" }, h("div", { class: "label" }, "Video dành cho trẻ em? (khai báo bắt buộc)"), kids));
    const r = await openDialog({ title: "Tạo kênh mới", content, actions: [{ label: "Huỷ", value: null }, { label: "Tạo kênh", kind: "primary", value: "ok", onClick: async () => {
      try {
        await api.post("/api/channels", { id: idIn.value.trim(), name: nameIn.value.trim() || idIn.value.trim(), kids: content.querySelector("input[name=nk]:checked").value === "yes" });
        return true;
      } catch (e) { idF.setError(e.message); return false; }
    } }] });
    if (r !== "ok") return;
    const list = await api.get("/api/channels");
    app.boot.channels = list.channels;
    s.channel = idIn.value.trim();
    LS.set("cf-channel", s.channel);
    channelSel.replaceChildren(...list.channels.filter((c) => c.ok).map((c) => h("option", { value: c.id }, c.name || c.id)));
    channelSel.value = s.channel;
    toast({ title: "Đã tạo kênh", message: "Chỉnh watermark, giọng đọc, video nền ở trang Kênh.", tone: "done" });
    schedule(0);
  }

  // ---------- RUN ----------
  runBtn.addEventListener("click", async () => {
    if (runBtn.disabled || s.running) return;
    if (!storyModeBox.hidden && !storyMode.validate()) return;                       // cấu hình Story Remix sai: báo tại trường, không gửi
    if (!guidanceBox.hidden && !guidance.validate()) return;                          // đề xuất riêng rỗng/quá dài: báo ngay tại ô nhập, không gửi
    s.running = true;
    s.rid = s.rid || newRequestId();
    await busy(runBtn, async () => {
      try {
        if (s.isColl) {                                            // Channel Run: một batch + N job con, không phải một job
          const c = await panel.confirm();
          if (!c.ok) { s.running = false; return; }
          const r = await api.post("/api/batches", { ...panel.body(), run: s.run, pipeline: pipelineBody(), kids: s.kids, auto_resume: s.autoResume, request_id: s.rid, confirm_large: c.large });
          toast(r.deduped ? { title: "Đã có Channel Run này", message: "Chuyển tới Channel Run đó thay vì tạo thêm.", tone: "info" } : { title: `Đã tạo Channel Run ${r.id}`, message: `${r.counts.total} job con đã xếp hàng; mỗi video chạy độc lập.`, tone: "done" });
          navigate(`/batches/${r.id}`);
          return;
        }
        const r = await api.post("/api/runs", { request_id: s.rid, input: { value: s.value, kind: s.kindOverride }, channel: s.channel, run: s.run, title: s.title, kids: s.kids,
                                                remember_kids: s.remember, auto_resume: s.autoResume, pipeline: pipelineBody(),
                                                ...(storyModeBox.hidden || !storyMode.payload() ? {} : { story_mode: storyMode.payload() }),
                                                ...(guidanceBox.hidden ? {} : ((g) => { const p = createPayload(g.mode, g.text); return p ? { story_guidance: p } : {}; })(guidance.get())) });
        toast(r.deduped ? { title: "Đã có job cùng nội dung đang chạy", message: "Chuyển tới job đó thay vì tạo thêm.", tone: "info" } : { title: `Đã xếp hàng job ${r.job_id}`, message: "Bạn có thể theo dõi tiến độ ở đây.", tone: "done" });
        navigate(`/jobs/${r.job_id}`);
      } catch (e) {
        toastError(e, "Chưa chạy được");
        s.rid = null;
        schedule(0);
      }
    });
    s.running = false;
    if (root.isConnected) paint();
  });

  // ---------- gần đây (poller) ----------
  let version = null;
  const poller = createPoller(async (signal) => {
    const d = await api.get("/api/jobs", { query: { status: "all", limit: 5, since: version }, signal });
    if (!d.changed) return (app.counts?.running || 0) > 0 ? "fast" : "idle";
    version = d.version;
    publishCounts(d.counts);
    recentCard.hidden = d.jobs.length === 0;
    const added = patchList(recent, d.jobs, rowKey, (j) => makeRow(j, () => poller.poke()), updateRow);
    scope.add(() => motion.itemsEnter(added));
    return d.jobs.some((j) => j.status === "running" || j.status === "queued") ? "fast" : "idle";
  });
  poller.start();

  paint();
  if (!s.value) valueIn.focus();
  return { destroy() { clearTimeout(timer); poller.stop(); s.seq++; } };
}
