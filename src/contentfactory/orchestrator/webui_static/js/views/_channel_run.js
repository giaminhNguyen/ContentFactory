// Luồng Channel Run ở màn Chạy: dán link kênh/playlist -> quét (chỉ metadata) -> chọn video -> tạo MỘT Channel Run với N job con độc lập.
// Việc quét, lọc "đã xử lý", chọn mặc định (10 video mới nhất chưa xử lý), trần an toàn và dedupe đều do backend; ở đây chỉ gom lựa chọn của người dùng và hiển thị.
import { api } from "../api.js";
import { h, clear } from "../dom.js";
import { icon } from "../icons.js";
import { btn, field, input, switchCtl, alertBox, skeleton, errorState, openDialog } from "../components.js";
import { externalLink } from "./_batch_ui.js";
import { duration } from "../format.js";

const MODES = [["newest", "Mới nhất"], ["oldest", "Cũ nhất"], ["range", "Theo vị trí"], ["dates", "Theo ngày"]];
const SKIP_TEXT = { processed: "Đã xử lý", livestream: "Livestream", upcoming: "Sắp công chiếu", short: "Shorts", unavailable: "Không khả dụng" };

export function channelRun({ getChannel, onChange }) {
  let sel = { mode: "newest", n: 10 };
  const filters = { skip_processed: true, skip_live: true, skip_upcoming: true, include_shorts: false };
  let rerun = false, value = "", data = null, error = null, loading = false, seq = 0, timer = null, manual = null;
  const el = h("section", { class: "card stack", hidden: true, "aria-labelledby": "chrun-h" });
  const summary = h("div", { "aria-live": "polite", class: "stack" });
  const entries = h("ul", { class: "joblist chrun-list", "aria-label": "Video của kênh/playlist" });
  const warn = h("div", { class: "stack" });

  // ---- điều khiển chọn ----
  const nIn = input({ type: "number", min: "1", max: "500", value: "10", "aria-label": "Số video" });
  const fromIn = input({ type: "number", min: "1", value: "1", "aria-label": "Từ vị trí" });
  const toIn = input({ type: "number", min: "1", value: "10", "aria-label": "Đến vị trí" });
  const d1 = input({ type: "date", "aria-label": "Từ ngày" });
  const d2 = input({ type: "date", "aria-label": "Đến ngày" });
  const modeBox = h("div", { class: "radio-row", role: "radiogroup", "aria-label": "Cách chọn video" });
  for (const [k, label] of MODES) {
    const r = h("input", { type: "radio", name: "chrun-mode", value: k, checked: k === "newest" });
    r.addEventListener("change", () => { sel = { mode: k }; manual = null; paintInputs(); discoverSoon(); });
    modeBox.append(h("label", null, r, label));
  }
  const params = h("div", { class: "row wrap" });
  function paintInputs() {
    clear(params);
    if (sel.mode === "newest" || sel.mode === "oldest") params.append(field({ label: "Số video", control: nIn }));
    if (sel.mode === "range") params.append(field({ label: "Từ vị trí", control: fromIn }), field({ label: "Đến vị trí", control: toIn }));
    if (sel.mode === "dates") params.append(field({ label: "Từ ngày", control: d1 }), field({ label: "Đến ngày", control: d2 }));
  }
  const readSel = () => {
    if (sel.mode === "newest" || sel.mode === "oldest") return { mode: sel.mode, n: Number(nIn.value) || 10 };
    if (sel.mode === "range") return { mode: "range", from: Number(fromIn.value) || 1, to: Number(toIn.value) || 10 };
    return { mode: "dates", date_from: d1.value || null, date_to: d2.value || null };
  };
  for (const c of [nIn, fromIn, toIn, d1, d2]) c.addEventListener("input", () => { manual = null; discoverSoon(); });
  const sw = (label, key) => switchCtl({ label, checked: filters[key], onChange: (v) => { filters[key] = v; if (key === "skip_processed") rerun = !v; manual = null; discoverSoon(); } });

  el.append(h("h2", { id: "chrun-h" }, "Chọn video từ kênh/playlist"), h("div", { class: "row spread" }, h("div", { id: "chrun-src" }), h("div", { id: "chrun-ext" })),
    h("fieldset", { class: "stack" }, h("legend", { class: "label" }, "Cách chọn"), modeBox, params),
    h("div", { class: "stack" }, h("div", { class: "label" }, "Bộ lọc"), sw("Bỏ video đã xử lý cho kênh xuất bản này (tắt = chạy lại có chủ đích)", "skip_processed"),
      sw("Bỏ livestream", "skip_live"), sw("Bỏ video sắp công chiếu", "skip_upcoming"), sw("Gồm Shorts", "include_shorts")),
    summary, warn, entries);
  paintInputs();

  // ---- khám phá ----
  function discoverSoon() { clearTimeout(timer); timer = setTimeout(discover, 350); }
  async function discover() {
    const my = ++seq;
    if (!value) return;
    loading = true; error = null;
    paint();
    try {
      const d = await api.post("/api/sources/youtube/discover", { url: value, output_channel: getChannel(), selection: readSel(), filters, skip_policy: rerun ? "rerun" : undefined });
      if (my !== seq) return;
      data = d;
    } catch (e) { if (my !== seq) return; data = null; error = e; }
    loading = false;
    paint();
    onChange?.();
  }

  const chosen = () => (manual ? data?.entries.filter((e) => manual.has(e.video_id)) : data?.entries.filter((e) => e.selected)) || [];

  function paint() {
    el.hidden = !value;
    clear(summary); clear(warn); clear(entries);
    const src = el.querySelector("#chrun-src"), ext = el.querySelector("#chrun-ext");
    clear(src); clear(ext);
    if (loading) { summary.append(skeleton(2)); return; }
    if (error) { summary.append(errorState(error, discover)); return; }
    if (!data) return;
    const s = data.source;
    src.append(h("div", null, h("strong", null, `${s.kind === "playlist" ? "Playlist" : "Kênh"}: ${s.title || s.canonical_url}`), s.kind === "playlist" && s.channel_title ? h("div", { class: "muted small" }, `Kênh: ${s.channel_title}`) : null));
    ext.append(externalLink(s.channel_url || s.canonical_url, "Mở trên YouTube"));
    const picked = chosen();
    const skipped = Object.entries(data.skipped).map(([k, n]) => `${n} ${(SKIP_TEXT[k] || k).toLowerCase()}`).join(", ");
    summary.append(h("p", null, h("strong", null, `Sẽ tạo ${picked.length} job`), ` (quét ${data.total} video${skipped ? `; bỏ qua: ${skipped}` : ""}).`,
      manual ? h("span", { class: "chip" }, "Chọn tay") : null, manual ? btn({ label: "Về lựa chọn tự động", size: "sm", kind: "ghost", onClick: () => { manual = null; paint(); onChange?.(); } }) : null),
      h("p", { class: "muted small" }, `Output vào kênh “${data.output_channel.name}”. Mỗi video là một job riêng (retry/checkpoint/output riêng); Channel Run chỉ gom và theo dõi.`));
    for (const w of data.warnings) warn.append(alertBox({ tone: "wait", title: w }));
    if (!data.entries.length) summary.append(alertBox({ tone: "info", title: "Kênh/playlist này chưa có video công khai nào.", body: "Kiểm tra lại link hoặc thử sau." }));
    else if (!picked.length) summary.append(alertBox({ tone: "wait", title: "Chưa có video nào được chọn", body: skipped ? `Tất cả video khớp đều bị bỏ qua (${skipped}). Đổi cách chọn hoặc bỏ bộ lọc.` : "Đổi cách chọn." }));
    if (data.requires_confirmation || picked.length > data.limits.confirm_above) summary.append(alertBox({ tone: "wait", title: `Sắp tạo hơn ${data.limits.confirm_above} job`, body: "Bạn sẽ được hỏi xác nhận trước khi tạo." }));
    for (const e of data.entries) {
      const on = manual ? manual.has(e.video_id) : e.selected;
      const cb = h("input", { type: "checkbox", checked: on, disabled: !!e.skip_reason, "aria-label": `Chọn video: ${e.title || e.video_id}` });
      cb.addEventListener("change", () => { manual = manual || new Set(data.entries.filter((x) => x.selected).map((x) => x.video_id)); if (cb.checked) manual.add(e.video_id); else manual.delete(e.video_id); paint(); onChange?.(); });
      const reason = e.skip_reason ? h("span", { class: "chip warn" }, SKIP_TEXT[e.skip_reason] || e.skip_reason, e.processed_job ? h("a", { href: `#/jobs/${e.processed_job}`, style: "margin-left: 6px" }, `job #${e.processed_job}`) : null) : null;
      entries.append(h("li", { class: "job child" }, h("div", { class: "row" }, cb), h("div", { class: "grow" }, h("div", { class: "title trunc" }, e.title || e.video_id),
        h("div", { class: "meta" }, h("span", { class: "mono" }, `#${e.position}`), e.published ? h("span", null, e.published) : null, e.duration ? h("span", null, duration(e.duration)) : null, reason, externalLink(e.url, "Mở video"))), h("div"), h("div")));
    }
  }

  async function confirmLarge(n) {
    return !!(await openDialog({ title: `Tạo ${n} job?`, describe: `Đây là số lượng lớn. Mỗi video là một job riêng; việc chạy vẫn tuần tự theo giới hạn tài nguyên (không chạy ${n} job song song), nhưng sẽ mất nhiều giờ và tốn quota/API.`,
      content: null, actions: [{ label: "Chưa tạo", value: null }, { label: `Tạo ${n} job`, kind: "primary", value: "ok" }] }));
  }

  return {
    el,
    // gọi khi giá trị ô nhập là link kênh/playlist (value) hoặc rỗng/khác (xóa)
    setValue(v) { if (v === value) return; value = v; data = null; error = null; manual = null; seq++; if (!v) { paint(); return; } paint(); discover(); },
    reload() { if (value) discoverSoon(); },
    firstUrl: () => chosen()[0]?.url || null,
    count: () => chosen().length,
    // “Hệ thống sẽ làm” cho Channel Run: nguồn, cách chọn, bao nhiêu bị bỏ qua và vì sao, kênh xuất bản, số job sẽ tạo
    summaryLines() {
      if (!data) return [];
      const sel = manual ? "chọn tay từng video" : ({ newest: `${data.selection?.n ?? ""} video mới nhất`, oldest: `${data.selection?.n ?? ""} video cũ nhất`, range: `video từ vị trí ${data.selection?.from} đến ${data.selection?.to}`, dates: "video theo khoảng ngày" })[data.selection?.mode] || "";
      const skipped = Object.entries(data.skipped).map(([k, n]) => `${n} ${(SKIP_TEXT[k] || k).toLowerCase()}`).join(", ");
      return [`Nguồn: ${data.source.title || data.source.canonical_url}`, `Chọn: ${sel}${skipped ? `; bỏ qua ${skipped}` : ""}`, `Kênh xuất bản: ${data.output_channel.name}`, `Sẽ tạo ${chosen().length} job độc lập`];
    },
    ready: () => !!data && !loading && chosen().length > 0,
    why: () => (loading ? "Đang quét kênh/playlist…" : error ? error.message : !data ? "" : !chosen().length ? "Chưa có video nào được chọn." : ""),
    // thân yêu cầu tạo batch (phần chạy/pipeline/kids do màn Chạy thêm vào)
    body() {
      const picked = chosen();
      return { url: value, output_channel: getChannel(), filters, skip_policy: rerun ? "rerun" : undefined,
               ...(manual ? { video_ids: picked.map((e) => e.video_id) } : { selection: data.selection }) };
    },
    async confirm() { const n = chosen().length; return n > (data?.limits.confirm_above ?? 100) ? { ok: await confirmLarge(n), large: true } : { ok: true, large: false }; },
  };
}
