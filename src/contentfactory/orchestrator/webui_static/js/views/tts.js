// Giọng đọc (TTS): engine đang dùng, profile (kèm bằng chứng/độ tin cậy), tự chọn giọng, thêm engine mới chỉ bằng repo/docs (Analyzer tự phân tích).
import { api } from "../api.js";
import { h, loadCss } from "../dom.js";
import { icon } from "../icons.js";
import { badge, btn, busy, field, input, alertBox, errorState, skeleton, pageHead, toastError, openDialog } from "../components.js";
import { createPoller } from "../poller.js";

const REASON = {
  required_reference_voice: "cần file giọng mẫu", credential: "cần API key", required_parameter: "cần nhập tham số", credential_optional: "API key (tuỳ chọn)",
};
const STATUS = {
  ready: { label: "Sẵn sàng", icon: "check-circle", tone: "done" },
  candidate: { label: "Ứng viên — cần kiểm chứng", icon: "hourglass", tone: "wait" },
};
const CONF = { high: { label: "cao", tone: "done" }, medium: { label: "vừa", tone: "wait" }, low: { label: "thấp", tone: "off" } };
const FACT_CAP = 60;

const short = (v, n = 90) => { const s = typeof v === "string" ? v : JSON.stringify(v); return s && s.length > n ? s.slice(0, n - 1) + "…" : s ?? ""; };

export async function mount(root, ctx) {
  loadCss("/css/tts.css");
  let destroyed = false, analyzing = false, onboardPoller = null;

  const dynamic = h("div", { class: "stack" });
  const resultHost = h("div", { "aria-live": "polite" });
  root.append(pageHead("Giọng đọc (TTS)", "Engine đang dùng, các profile giọng và cách thêm engine mới."), dynamic);
  dynamic.append(skeleton(4));
  const onboard = onboardCard();

  async function load() {
    try {
      const data = await api.get("/api/tts");
      if (destroyed) return;
      dynamic.replaceChildren(engineCard(data.engine), autoCard(data), profilesCard(data), onboard);
    } catch (e) {
      if (destroyed || e.name === "AbortError") return;
      dynamic.replaceChildren(errorState(e, load));
    }
  }

  // ---------- 1. engine ----------
  function engineCard(en) {
    const meta = en.ok ? { label: "Đang hoạt động", icon: "check-circle", tone: "done" } : { label: "Cần xử lý", icon: "alert", tone: "attn" };
    const card = h("section", { class: "card stack", "aria-labelledby": "tts-engine-h" },
      h("div", { class: "card-title" }, h("h2", { id: "tts-engine-h" }, "Engine đang dùng"), badge(meta)),
      h("dl", { class: "kv" }, h("dt", null, "Adapter"), h("dd", null, en.adapter || "—"), h("dt", null, "Loại"), h("dd", null, en.kind || "—"), h("dt", null, "Mã engine"), h("dd", null, en.engine_id || "—")),
      h("p", { class: "muted small" }, en.ok ? "Engine trả lời bình thường nên job có thể dùng ngay." : `Engine chưa sẵn sàng${en.health?.error ? `: ${en.health.error}` : "."} Job dùng giọng đọc sẽ bị giữ cho tới khi engine chạy được; mở Cài đặt & Doctor để xem cách sửa.`));
    if (en.is_fake) card.append(alertBox({ tone: "info", title: "Đang dùng giọng đọc giả", body: "Đang dùng giọng đọc giả (âm tổng hợp), chỉ để thử pipeline. Thêm một engine TTS thật bên dưới." }));
    return card;
  }

  // ---------- 2. tự chọn ----------
  function autoCard({ auto }) {
    const body = auto.selected
      ? h("div", { class: "stack" }, h("div", { class: "tts-auto" }, icon("zap", { size: 20 }), h("div", null, h("div", { class: "big" }, `TTS: Auto — ${auto.selected}`), h("div", { class: "muted small" }, auto.why || ""))))
      : alertBox({ tone: "wait", title: `Chưa có profile phù hợp với ngôn ngữ “${auto.language}”`, body: "Hệ thống sẽ dùng giọng mặc định của engine. Thêm hoặc kiểm chứng một profile bên dưới để Auto chọn được." });
    return h("section", { class: "card stack", "aria-labelledby": "tts-auto-h" }, h("h2", { id: "tts-auto-h" }, "Tự chọn giọng đọc"), body,
      h("p", { class: "muted small" }, `Đây là giọng sẽ chạy khi một kênh không chọn profile riêng (ngôn ngữ mặc định: ${auto.language}). Chọn profile cố định cho từng kênh ở trang Kênh.`));
  }

  // ---------- 3. bảng profile ----------
  function profilesCard({ profiles, profiles_dir }) {
    const card = h("section", { class: "card flush", "aria-labelledby": "tts-prof-h" }, h("div", { class: "card-title", style: "padding: var(--s-4) var(--s-4) 0" }, h("h2", { id: "tts-prof-h" }, "Profile giọng đọc")));
    if (!profiles.length) {
      card.append(h("div", { style: "padding: var(--s-4)" }, alertBox({ tone: "info", title: "Chưa có profile nào", body: `Profile nằm trong ${profiles_dir}. Thêm engine ở mục bên dưới để hệ thống tự tạo profile ứng viên.` })));
      return card;
    }
    const head = ["Profile", "Engine", "Trạng thái", "Ngôn ngữ / giọng", "Auto Tune", "Độ tin cậy", "Cần từ bạn", ""];
    const rows = profiles.map(profileRow);
    card.append(h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("caption", { class: "sr-only" }, "Danh sách profile TTS"),
      h("thead", null, h("tr", null, ...head.map((t) => h("th", { scope: "col" }, t || h("span", { class: "sr-only" }, "Hành động"))))), h("tbody", null, ...rows))));
    return card;
  }

  function profileRow(p) {
    const st = STATUS[p.status] || { label: p.status || "?", icon: "info", tone: "off" };
    const c = p.confidence || {};
    const needs = p.needs_user || [];
    const tuned = p.autotune && p.autotune.works;
    const det = btn({ label: "Chi tiết", size: "sm", ariaLabel: `Chi tiết profile ${p.name}`, onClick: () => showDetail(p.name, det) });
    return h("tr", null,
      h("td", null, h("strong", null, p.name), p.selected_by_auto ? h("div", null, h("span", { class: "chip ok" }, icon("zap", { size: 12 }), "Đang được Auto chọn")) : null),
      h("td", null, p.engine || "—"),
      h("td", null, badge(st)),
      h("td", null, (p.languages || []).join(", ") || "—", p.voice ? h("div", { class: "muted small" }, `Giọng: ${short(p.voice, 40)}`) : null),
      h("td", null, tuned ? h("span", { class: "chip ok" }, icon("check", { size: 12 }), "Đã chạy thử") : h("span", { class: "muted" }, "Chưa")),
      h("td", { class: "nowrap" }, `cao ${c.high || 0} · vừa ${c.medium || 0} · thấp ${c.low || 0}`),
      h("td", null, needs.length ? h("ul", { class: "tts-needs" }, ...needs.map((n) => h("li", null, REASON[n.reason] || n.reason, h("span", { class: "muted small" }, ` (${n.key})`)))) : h("span", { class: "muted" }, "Không"),
        p.credentials?.length ? h("div", { class: "tts-cred" }, ...p.credentials.map((cr) => h("span", { class: "chip " + (cr.ready ? "ok" : "warn") }, icon(cr.ready ? "check" : "x", { size: 12 }), cr.name, h("span", { class: "sr-only" }, cr.ready ? " đã có" : " chưa có")))) : null),
      h("td", null, det));
  }

  async function showDetail(name, button) {
    let d = null;
    await busy(button, async () => {
      try { d = await api.get(`/api/tts/profiles/${encodeURIComponent(name)}`); } catch (e) { toastError(e, "Không đọc được profile"); }
    });
    if (!d || destroyed) return;                          // mở dialog SAU khi nút đã hết trạng thái bận (dialog trả focus về nút này)
    await openDialog({ title: `Profile ${name}`, wide: true, describe: `Engine ${d.engine || "?"} · ${d.status || ""}. Mỗi giá trị đều có nguồn và độ tin cậy.`, content: factsView(d), actions: [{ label: "Đóng", kind: "primary", value: true }] });
  }

  function factsView(d) {
    const box = h("div", { class: "stack" });
    if (d.needs_user?.length) box.append(alertBox({ tone: "attn", title: "Cần từ bạn", body: h("ul", { class: "tts-needs" }, ...d.needs_user.map((n) => h("li", null, `${REASON[n.reason] || n.reason} (${n.key})`))) }));
    if (!d.facts.length) { box.append(h("p", { class: "muted" }, "Profile này chưa có giá trị kèm bằng chứng.")); return box; }
    const tbody = h("tbody", null);
    let shown = 0;
    const more = btn({ label: "Hiện thêm", size: "sm" });
    const addRows = () => {
      for (const f of d.facts.slice(shown, shown + FACT_CAP)) {
        const cf = CONF[f.confidence] || { label: f.confidence, tone: "off" };
        const ev = (f.evidence || []).length ? h("details", null, h("summary", null, `${f.evidence.length} bằng chứng`), h("ul", null, ...f.evidence.map((e) => h("li", null, h("span", { class: "mono" }, e.ref || ""), e.quote ? ` — “${short(e.quote, 140)}”` : "")))) : null;
        tbody.append(h("tr", null, h("td", null, f.path), h("td", null, short(f.value)), h("td", null, f.source), h("td", null, h("span", { class: "badge", dataset: { tone: cf.tone } }, cf.label), f.note ? h("div", { class: "muted small" }, f.note) : null), h("td", null, ev || h("span", { class: "muted" }, "—"))));
      }
      shown = Math.min(d.facts.length, shown + FACT_CAP);
      more.hidden = shown >= d.facts.length;
      more.textContent = `Hiện thêm (${d.facts.length - shown})`;
    };
    more.addEventListener("click", addRows);
    addRows();
    box.append(h("div", { class: "table-wrap", style: "max-height: 50vh; overflow: auto" }, h("table", { class: "table tts-facts" }, h("caption", { class: "sr-only" }, "Giá trị, nguồn và bằng chứng"),
      h("thead", null, h("tr", null, ...["Thuộc tính", "Giá trị", "Nguồn", "Độ tin cậy", "Bằng chứng"].map((t) => h("th", { scope: "col" }, t)))), tbody)), more);
    return box;
  }

  // ---------- 4. thêm engine ----------
  function onboardCard() {
    const refIn = input({ placeholder: "https://github.com/…  hoặc  D:\\tts\\engine", "aria-describedby": "tts-ref-hint" });
    const pick = btn({ label: "Chọn thư mục…", icon: "folder", onClick: async (e) => {
      await busy(e.currentTarget, async () => {
        try {
          const r = await api.post("/api/pick", { kind: "folder", title: "Chọn thư mục repo/docs của engine" });
          if (r.path) refIn.value = r.path; else if (r.unsupported) toastError(new Error(r.message), "Không mở được hộp thoại");
        } catch (err) { toastError(err); }
      });
    } });
    const f = field({ label: "Repo / thư mục / link docs của engine", control: refIn, hint: "Bạn chỉ cần đưa repo/docs; hệ thống tự phân tích khả năng, giới hạn và tạo profile ứng viên — không phải điền tham số kỹ thuật.", required: true });
    const go = btn({ label: "Phân tích", icon: "search", kind: "primary", type: "button" });
    go.addEventListener("click", () => start(f, refIn, go));
    refIn.addEventListener("keydown", (e) => { if (e.key === "Enter" && !go.disabled) go.click(); });
    return h("section", { class: "card stack", "aria-labelledby": "tts-add-h" }, h("h2", { id: "tts-add-h" }, "Thêm engine TTS mới"), f, h("div", { class: "row" }, pick, go), resultHost);
  }

  async function start(f, refIn, go) {
    if (analyzing) return;
    const ref = refIn.value.trim();
    if (!ref) { f.setError("Nhập đường dẫn hoặc link của engine."); refIn.focus(); return; }
    f.setError(null);
    analyzing = true;
    resultHost.replaceChildren(h("div", { class: "row" }, icon("spinner", { size: 18, cls: "spin" }), h("span", null, "Đang phân tích repo/docs… có thể mất vài chục giây.")));
    let task;
    await busy(go, async () => {
      try { task = await api.post("/api/tts/onboard", { reference: ref }); }
      catch (e) { toastError(e, "Chưa phân tích được"); resultHost.replaceChildren(); analyzing = false; }
    });
    if (!task) return;
    go.disabled = true;
    onboardPoller = createPoller(async (signal) => {
      let t;
      try { t = await api.get(`/api/tasks/${task.task_id}`, { signal }); }
      catch (e) {
        if (e.status !== 404) throw e;                       // lỗi mạng: poller tự thử lại; 404 = tác vụ đã mất
        t = { state: "error", error: { message: e.message, hint: e.hint } };
      }
      if (t.state === "running") return "fast";
      onboardPoller.stop();
      analyzing = false;
      go.disabled = false;
      if (destroyed) return;
      resultHost.replaceChildren(t.state === "done" ? resultView(t.result) : alertBox({ tone: "fail", title: t.error?.message || "Phân tích thất bại", body: t.error?.hint || t.error?.code || null }));
      if (t.state === "done") load();
    }, { fast: 1000, idle: 2000 });
    onboardPoller.start();
  }

  function resultView(r) {
    const caps = r.capabilities || {};
    const lines = [["Engine", r.engine], ["Adapter", `${r.adapter_kind}${r.adapter_ready ? " (đã sẵn sàng)" : " (chưa sẵn sàng, cần bổ sung)"}`], ["Số file đã đọc", r.files],
      ["Giới hạn ký tự", caps.max_chars ?? "chưa rõ"], ["Ngôn ngữ", (caps.languages || []).join(", ") || "chưa rõ"], ["Sample rate", caps.sample_rate ?? "chưa rõ"],
      ["Tăng tốc", caps.speed ? "có" : "không"], ["Nhân bản giọng", caps.voice_cloning ? "có" : "không"], ["Thư mục kết quả", r.out_dir], ["File đã ghi", `${(r.written || []).length}`]];
    const dl = h("dl", { class: "kv" });
    for (const [k, v] of lines) dl.append(h("dt", null, k), h("dd", null, String(v)));
    return h("div", { class: "tts-result" }, alertBox({ tone: "done", title: `Đã phân tích engine “${r.engine}”`, body: "Profile ứng viên đã được tạo; cần kiểm chứng bằng chạy thật trước khi Auto dùng." }), dl,
      r.needs_user?.length ? alertBox({ tone: "attn", title: "Cần từ bạn", body: h("ul", { class: "tts-needs" }, ...r.needs_user.map((n) => h("li", null, `${REASON[n.reason] || n.reason} (${n.key})`))) }) : h("p", { class: "muted small" }, "Không cần bạn nhập thêm gì."),
      h("div", null, h("div", { class: "small muted" }, "Bước tiếp theo"), h("pre", { class: "tts-mono" }, r.next || "")));
  }

  await load();
  return { destroy() { destroyed = true; onboardPoller?.stop(); } };
}
