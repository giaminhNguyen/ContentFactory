// Nhịp đọc (Prosody): thẻ nghe thử A/B ở trang Giọng đọc và dialog chỉnh khoảng nghỉ theo job.
// Mọi quyết định (tách câu, loại ranh giới, mili-giây) do backend tính; ở đây chỉ chọn profile/giá trị, gọi API, phát audio và vẽ kết quả.
import { api } from "../api.js";
import { h, clear } from "../dom.js";
import { icon } from "../icons.js";
import { btn, busy, field, input, select, alertBox, openDialog, toast, toastError } from "../components.js";

const CUSTOM_KINDS = ["sentence", "paragraph", "dialogue", "ellipsis", "scene"];

// ---------------------------------------------------------------- thẻ nghe thử (trang Giọng đọc)
export async function prosodyCard({ engineOk, profiles = [], isDestroyed = () => false }) {
  const info = await api.get("/api/tts/prosody");
  const labelOf = Object.fromEntries(info.kinds.map((k) => [k.id, k.label]));
  const variants = [makeVariant("A", info.default_profile || "natural"), makeVariant("B", "dramatic")];
  let compare = false;
  const voiceSel = select({ options: [["", "Tự chọn giọng (như job)"], ...profiles.map((p) => [p.name, `${p.name} — ${p.engine || "?"}`])], value: "" });
  const cmp = h("input", { type: "checkbox", id: "pro-cmp" });
  const results = h("div", { class: "stack", "aria-live": "polite" });
  const go = btn({ label: "Nghe thử (~25 giây)", icon: "play", kind: "primary", disabled: !engineOk, title: engineOk ? "" : "Engine TTS chưa sẵn sàng", onClick: () => run(go) });
  const cols = h("div", { class: "grid-2" }, ...variants.map((v) => v.el));

  function makeVariant(tag, profile) {
    const sel = select({ options: info.profiles.map((p) => [p.id, p.label]), value: profile });
    const scale = input({ type: "number", min: "0", max: "5", step: "0.1", placeholder: "1" });
    const customBox = h("div", { class: "stack", hidden: true });
    const nums = {};
    for (const k of CUSTOM_KINDS) {
      nums[k] = input({ type: "number", min: "0", max: String(info.max_pause_ms), step: "10", placeholder: String(info.profiles[0].pauses[k]) });
      customBox.append(field({ label: `${labelOf[k]} (ms)`, control: nums[k] }));
    }
    sel.addEventListener("change", () => { customBox.hidden = sel.value !== "custom"; });
    const el = h("div", { class: "stack", role: "group", "aria-label": `Biến thể ${tag}` }, h("h3", null, tag === "A" ? "Nhịp đọc A" : "Nhịp đọc B"),
      field({ label: "Profile nhịp đọc", control: sel }), field({ label: "Thang khoảng nghỉ chung", hint: "1 = giữ nguyên; 0.8 = ngắn hơn 20%.", control: scale }), customBox);
    return { tag, el, sel, scale, nums,
      body() {
        const out = { profile: sel.value };
        if (scale.value !== "") out.scale = Number(scale.value);
        if (sel.value === "custom") out.custom = Object.fromEntries(CUSTOM_KINDS.filter((k) => nums[k].value !== "").map((k) => [k, Number(nums[k].value)]));
        return out;
      } };
  }

  function sync() {
    variants[1].el.hidden = !compare;
    go.querySelector("span").textContent = compare ? "Nghe thử A và B" : "Nghe thử (~25 giây)";
  }
  cmp.addEventListener("change", () => { compare = cmp.checked; sync(); });
  sync();

  async function run(button) {
    clear(results);
    await busy(button, async () => {
      try {
        const body = { variants: variants.slice(0, compare ? 2 : 1).map((v) => v.body()), tts_profile: voiceSel.value || undefined };
        const { task } = await api.post("/api/tts/prosody/preview", body);
        results.append(h("p", { class: "muted" }, "Đang tổng hợp đoạn mẫu… (lần đầu có thể mất vài giây tới vài chục giây tùy engine)"));
        let t;
        do {
          await new Promise((r) => setTimeout(r, 700));
          if (isDestroyed()) return;
          t = await api.get(`/api/tasks/${task}`);
        } while (t.state === "running");
        clear(results);
        if (t.state === "error") { results.append(alertBox({ tone: "fail", title: t.error.message, body: t.error.hint })); return; }
        for (const [i, v] of t.result.variants.entries()) results.append(await variantResult(v, i === 0 ? "A" : "B"));
      } catch (e) { clear(results); toastError(e, "Không nghe thử được"); }
    });
  }

  async function variantResult(v, tag) {
    const src = await api.blobUrl(v.url);
    const audio = h("audio", { controls: true, src, preload: "auto", "aria-label": `Bản nghe thử ${tag}` });
    const pauses = v.pauses_ms.length ? `${Math.min(...v.pauses_ms)}–${Math.max(...v.pauses_ms)} ms` : "—";
    const warns = (v.warnings || []).map((w) => h("li", null, w.message));
    return h("div", { class: "card stack" }, h("h3", null, `${tag} — ${info.profiles.find((p) => p.id === v.profile)?.label || v.profile}`), audio,
      h("p", { class: "muted small" }, `Dài ${v.duration_sec.toFixed(1)} giây · ${v.groups} lần gọi TTS · khoảng nghỉ chèn: ${pauses}`),
      warns.length ? h("ul", { class: "small" }, ...warns) : null);
  }

  return h("section", { class: "card stack", "aria-labelledby": "tts-pro-h" },
    h("h2", { id: "tts-pro-h" }, "Nhịp đọc (Prosody)"),
    h("p", { class: "muted small" }, "Giọng do TTS profile quyết định; NHỊP đọc (nghỉ bao lâu ở hết câu, hết đoạn, đổi lượt thoại, chuyển cảnh…) do ContentFactory tính tất định, không dùng AI. Nghe thử không cần chạy job; chọn nhịp cho từng kênh ở trang Kênh."),
    h("div", { class: "grid-2" }, field({ label: "Giọng đọc để thử", control: voiceSel }),
      h("label", { class: "switch", for: "pro-cmp" }, cmp, h("span", { class: "track", "aria-hidden": "true" }), h("span", null, "So sánh A/B"))),
    cols, h("div", { class: "row" }, go), results);
}

// ---------------------------------------------------------------- chỉnh nhịp của một job
export async function openProsodyDialog(job, { after, navigate } = {}) {
  const sp = await api.get(`/api/jobs/${job.id}/speech-plan`);
  if (!sp.available) { toast({ title: "Chưa có nhịp đọc", message: sp.reason, tone: "wait" }); return; }
  const edits = {};                                              // key -> ms | null (null = trả về Tự động)
  const body = h("div", { class: "stack" });
  const impactBox = h("div", { "aria-live": "polite", class: "small" });
  let impact = null, seq = 0, timer = null, showAll = false, shown = 80;
  const qc = sp.qc;

  const head = h("div", { class: "stack" },
    h("p", { class: "muted small" }, sp.editable ? `Profile ${sp.profile}. ${qc.groups} lần gọi TTS; khoảng nghỉ chèn P50/P95/lớn nhất: ${qc.pause_ms.p50}/${qc.pause_ms.p95}/${qc.pause_ms.max} ms. Chỉnh một ô để đặt khoảng nghỉ chính xác; “Tự động” trả về giá trị của profile.`
      : "Job này chạy theo planner cũ (không có Prosody) nên không chỉnh từng ranh giới được."),
    ...(qc.warnings || []).map((w) => alertBox({ tone: "wait", title: w.message })));
  const allToggle = h("input", { type: "checkbox", id: "pd-all" });
  allToggle.addEventListener("change", async () => {
    showAll = allToggle.checked;
    const r = await api.get(`/api/jobs/${job.id}/speech-plan`, { query: { scope: showAll ? "all" : "external" } });
    sp.boundaries = r.boundaries;
    shown = 80;
    paintRows();
  });
  const table = h("div", { class: "stack" });
  const more = btn({ label: "Hiện thêm", size: "sm", onClick: () => { shown += 120; paintRows(); } });

  function paintRows() {
    clear(table);
    for (const b of sp.boundaries.slice(0, shown)) {
      const cur = b.key in edits ? edits[b.key] : null;
      const ms = input({ type: "number", min: "0", max: "10000", step: "10", placeholder: String(b.pause_ms), "aria-label": `Khoảng nghỉ sau: ${b.text}`, disabled: !sp.editable });
      if (cur != null) ms.value = String(cur);
      else if (b.manual) ms.value = String(b.pause_ms);
      const reset = btn({ label: "Tự động", size: "sm", kind: "ghost", disabled: !sp.editable, onClick: () => { ms.value = ""; edits[b.key] = null; schedule(); } });
      ms.addEventListener("input", () => { edits[b.key] = ms.value === "" ? null : Number(ms.value); schedule(); });
      table.append(h("div", { class: "pick-row" }, h("span", { class: "s-label" }, `${b.kind_label}`), h("span", { class: "s-why" }, `…${b.text}`),
        h("span", { class: "s-tag" }, b.manual ? "Chỉnh tay" : b.external ? "Chèn khoảng lặng" : "Engine tự xử lý"), ms, h("span", { class: "muted small" }, "ms"), reset));
    }
    more.hidden = shown >= sp.boundaries.length;
  }

  function patch() {
    const o = Object.fromEntries(Object.entries(edits).map(([k, v]) => [k, { pause_ms: v }]));
    return { params_patch: { prosody: { overrides: o } } };
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(refresh, 300); }
  async function refresh() {
    const my = ++seq;
    if (!Object.keys(edits).length) { impact = null; clear(impactBox); return; }
    try { impact = await api.post(`/api/jobs/${job.id}/pipeline-impact`, patch()); } catch (e) { impact = null; return; }
    if (my !== seq) return;
    clear(impactBox);
    if (!impact.ok) { impactBox.append(alertBox({ tone: "info", title: impact.errors[0] || "Không áp dụng được tại chỗ.", body: impact.clone_suggested ? "Bấm “Chạy lại với nhịp này” để tạo job mới từ kết quả còn hợp lệ." : null })); return; }
    const t = impact.summary_text;
    impactBox.append(h("ul", { class: "autolist" }, ...[["Sẽ chạy", t.will_run], ["Giữ nguyên", t.kept]].filter(([, a]) => a.length).map(([l, a]) => h("li", null, h("strong", null, l + ": "), a.join(", ")))));
  }

  body.append(head, sp.editable ? h("label", { class: "switch", for: "pd-all" }, allToggle, h("span", { class: "track", "aria-hidden": "true" }), h("span", null, "Hiện cả ranh giới trong nhóm (engine tự xử lý; chỉnh sẽ tách nhóm)")) : null, table, more, impactBox);
  paintRows();
  await openDialog({ title: "Nhịp đọc của job", wide: true, content: body, actions: [{ label: "Đóng", value: null },
    ...(sp.editable ? [{ label: "Áp dụng", kind: "primary", value: "ok", onClick: async () => {
      if (!Object.keys(edits).length) { toast({ title: "Chưa chỉnh gì", tone: "wait" }); return false; }
      try {
        if (impact && !impact.ok && impact.clone_suggested) {
          const r = await api.post(`/api/jobs/${job.id}/clone`, { rerun_from: "tts", ...patch() });
          toast({ title: r.message, tone: "done" });
          navigate?.(`/jobs/${r.job_id}`);
        } else {
          const r = await api.post(`/api/jobs/${job.id}/pipeline-revisions`, patch());
          toast({ title: r.status === "applied" ? "Đã áp dụng" : "Đã ghi thay đổi", message: r.message, tone: r.status === "applied" ? "done" : "wait" });
          after?.();
        }
        return true;
      } catch (e) { toastError(e, "Chưa áp dụng được"); return false; }
    } }] : [])] });
}
