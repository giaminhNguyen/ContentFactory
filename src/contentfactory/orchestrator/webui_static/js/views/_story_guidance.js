// Đề xuất truyện của MỘT job (D-112): bộ chọn inherit/custom(/none) dùng chung cho màn Chạy và Chi tiết job + thẻ hiển thị/sửa trên Chi tiết job.
// Backend quyết định đề xuất hiệu lực; ở đây chỉ vẽ lựa chọn và gửi {mode, text}.
import { api } from "../api.js";
import { h, uid } from "../dom.js";
import { btn, busy, textarea, toast, toastError, disclosure } from "../components.js";
import { MAX_LEN, counter, currentLabel, previewDefault, savePayload, validateGuidance, SOURCE_LABEL } from "../story_guidance_logic.js";
import { relTime } from "../format.js";

const COPY = {
  inherit: "Dùng đề xuất trong Cài đặt",
  custom: "Dùng đề xuất riêng",
  none: "Không dùng đề xuất",
};

/** {el, get(), validate(), setDefault(text)}: el là fieldset. `onChange` được gọi mỗi lần đổi chế độ/nội dung. */
export function guidanceEditor({ mode = "inherit", text = "", defaultText = "", max = MAX_LEN, allowNone = false, onChange } = {}) {
  const name = uid("sg-mode"), tid = uid("sg-text");
  let cur = mode, dflt = defaultText;
  const radios = {};
  const row = h("div", { class: "radio-row sg-modes" });
  for (const m of allowNone ? ["inherit", "custom", "none"] : ["inherit", "custom"]) {
    radios[m] = h("input", { type: "radio", name, value: m, checked: m === cur });
    radios[m].addEventListener("change", () => { cur = m; sync(); onChange?.(); });
    row.append(h("label", null, radios[m], COPY[m]));
  }
  const preview = h("div", { class: "sg-preview", "aria-live": "polite" });
  const ta = textarea({ id: tid, rows: 5, value: text, maxlength: undefined, placeholder: "Ví dụ: Truyện tập trung vào mối quan hệ cha con. Nhân vật người cha không được chết. Cuối truyện có một cú đảo ngược hợp logic.", "aria-describedby": tid + "-c " + tid + "-e" });
  const count = h("div", { class: "sg-count small muted", id: tid + "-c" });
  const err = h("div", { class: "error", id: tid + "-e", role: "alert" });
  const custom = h("div", { class: "stack", hidden: true }, h("label", { class: "sr-only", for: tid }, "Nội dung đề xuất truyện riêng của job"), ta, count, err);

  function showErr(msg) { err.textContent = msg || ""; ta.toggleAttribute("aria-invalid", !!msg); }
  function sync() {
    custom.hidden = cur !== "custom";
    preview.hidden = cur !== "inherit";
    const p = previewDefault(dflt);
    preview.replaceChildren(p.empty ? h("span", { class: "muted small" }, "Chưa có đề xuất mặc định.")
      : h("div", null, h("div", { class: "small muted" }, "Đề xuất mặc định hiện tại (từ Cài đặt):"), h("div", { class: "sg-quote" }, p.text)));
    const c = counter(ta.value, max);
    count.textContent = c.label;
    count.dataset.over = c.over ? "1" : "";
    showErr(cur === "custom" && c.over ? validateGuidance("custom", ta.value, max) : "");
  }
  ta.addEventListener("input", () => { sync(); onChange?.(); });
  const el = h("fieldset", { class: "sg-editor" }, h("legend", { class: "label" }, "Đề xuất truyện cho job này"), row, preview, custom);
  sync();
  return {
    el,
    get: () => ({ mode: cur, text: ta.value }),
    validate() { const m = validateGuidance(cur, ta.value, max); showErr(m); if (m && cur === "custom") ta.focus(); return !m; },
    setDefault(t) { dflt = t || ""; sync(); },
    set(m, t) { cur = m; radios[m].checked = true; ta.value = t || ""; sync(); },
  };
}

/** Thẻ "Đề xuất truyện" trên Chi tiết job: nguồn đang dùng, sửa + lưu, snapshot của lần chạy Truyện gần nhất. `d` = job detail; `after` làm mới trang. */
export function storyGuidanceCard(d, { after } = {}) {
  const g = d.story_guidance;
  if (!g) return null;
  const ed = guidanceEditor({ mode: g.mode, text: g.text, defaultText: g.default_text, max: g.max_len, allowNone: true });
  const save = btn({ label: "Lưu đề xuất", icon: "check", size: "sm", kind: "primary" });
  save.addEventListener("click", () => {
    if (!ed.validate()) return;
    busy(save, async () => {
      try {
        const v = ed.get();
        const r = await api.put(`/api/jobs/${d.id}/story-guidance`, savePayload(v.mode, v.text));
        toast({ title: r.message || "Đã lưu đề xuất truyện", tone: "done" });
        after?.();
      } catch (e) { toastError(e, "Chưa lưu được đề xuất"); }
    });
  });
  const lr = g.last_run;
  const body = h("div", { class: "stack" },
    h("p", { class: "small", role: "status" }, h("strong", null, currentLabel(g.effective.source)), ".",
      " Thay đổi chỉ áp dụng cho lần chạy Truyện kế tiếp; truyện đã có không bị đổi."),
    ed.el,
    h("div", { class: "row" }, save),
    lr ? h("div", { class: "stack" },
      h("p", { class: "small" }, `Đề xuất hiệu lực lần chạy Truyện gần nhất: ${SOURCE_LABEL[lr.source] || lr.source}${lr.at ? ` · ${relTime(lr.at)}` : ""}.`),
      g.drift ? h("p", { class: "small sg-drift", role: "note" }, "Đề xuất hiện tại khác với đề xuất mà truyện hiện có được viết theo. Chạy lại bước Truyện để áp dụng.") : null,
      disclosure({ label: "Chi tiết kỹ thuật", content: h("div", { class: "stack", style: "padding-top: var(--s-2)" },
        h("div", { class: "small muted" }, `Nguồn: ${lr.source} · mã băm ${lr.hash || "—"} · trạng thái ${lr.status}`),
        lr.text ? h("pre", { class: "cfg-pre", tabindex: "0", "aria-label": "Nội dung đề xuất đã dùng ở lần chạy Truyện gần nhất" }, lr.text) : h("div", { class: "small muted" }, "Lần chạy đó không có đề xuất.")) }))
      : h("p", { class: "small muted" }, "Bước Truyện chưa chạy lần nào nên chưa có đề xuất được chốt."));
  return h("section", { class: "card stack", "aria-labelledby": "sg-h" }, h("div", { class: "card-title" }, h("h2", { id: "sg-h" }, "Đề xuất truyện")), body);
}
