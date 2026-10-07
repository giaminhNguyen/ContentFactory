// Watermark Library của một kênh (Kênh → Watermark): watermark đang dùng + thư viện (TTS / tải lên / file cũ), nghe thử, chọn, sửa, tạo lại, thay file, xóa/lưu trữ.
// Không có logic nghiệp vụ ở đây: danh sách, trạng thái, quy tắc xóa/lưu trữ và kết quả đều do backend trả; mọi thay đổi lưu NGAY bằng API riêng (không qua nút “Lưu thay đổi” của kênh).
// Tổng hợp giọng chạy ở tác vụ nền: dialog giữ trạng thái “Đang tạo watermark…” (thời gian thật) và báo lỗi nhà cung cấp ngay trong dialog để sửa rồi thử lại.
import { api, forgetBlob, newRequestId, ApiError } from "../api.js";
import { h, clear } from "../dom.js";
import { icon } from "../icons.js";
import { btn, busy, field, input, select, alertBox, emptyState, errorState, skeleton, toast, toastError, openDialog, confirmDialog, disclosure } from "../components.js";
import * as motion from "../motion.js";
import { relTime } from "../format.js";
import { sourceLabel, ttsSummary, durationText, deleteMode, editKind, EDIT_LABEL, createErrors, FIELD_OF_CODE } from "../watermark_logic.js";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function watermarkSection({ channelId, scope, onSync }) {
  const base = `/api/channels/${channelId}`;
  const root = h("div", { class: "stack wm" });
  const activeHost = h("div", null);
  const listHost = h("div", { class: "stack" });
  const head = h("div", { class: "row spread" });
  let lib = null, alive = true, showArchived = false;
  const urls = new Set();
  root.append(head, activeHost, listHost);
  listHost.append(skeleton(2));

  // ---------- dữ liệu ----------
  async function refresh({ animateId } = {}) {
    try { lib = await api.get(`${base}/watermarks${showArchived ? "?archived=1" : ""}`); }
    catch (e) { if (alive) { clear(listHost); listHost.append(errorState(e, () => { clear(listHost); listHost.append(skeleton(2)); refresh(); })); } return; }
    if (!alive) return;
    paint(animateId);
  }

  async function waitTask(tid, onTick) {
    const t0 = Date.now();
    for (;;) {
      const t = await api.get(`/api/tasks/${tid}`);
      if (t.state === "done") return t.result;
      if (t.state === "error") throw new ApiError(t.error?.message || "Không tạo được watermark.", { code: t.error?.code || "ERROR", hint: t.error?.hint || "" });
      onTick?.((Date.now() - t0) / 1000);
      await sleep(500);
    }
  }

  // ---------- vẽ ----------
  function paint(animateId) {
    const items = lib.items;
    const act = items.find((i) => i.active);
    head.replaceChildren(h("div", null, h("p", { class: "muted small" }, "Watermark là đoạn audio ngắn gắn vào bản YouTube (âm lượng tự cân với giọng đọc). Mỗi lần sửa tạo một bản mới; job đã tạo giữ nguyên bản cũ.")),
      h("div", { class: "row" },
        lib.archived || showArchived ? h("label", { class: "row small" }, h("input", { type: "checkbox", checked: showArchived, onChange: (e) => { showArchived = e.target.checked; refresh(); } }), `Hiện đã lưu trữ (${lib.archived})`) : null,
        btn({ label: "Tạo watermark", icon: "plus", kind: "primary", onClick: () => openCreate().catch(toastError) })));
    clear(activeHost);
    activeHost.append(act ? h("section", { class: "wm-active", "aria-label": "Watermark đang dùng" }, h("h3", null, "Đang dùng cho kênh"), card(act, true))
      : alertBox({ tone: "info", title: "Kênh chưa dùng watermark", body: "Video YouTube chỉ có giọng truyện. Tạo hoặc chọn một watermark bên dưới nếu muốn." }));
    if (!lib.tts.available) activeHost.append(alertBox({ tone: "wait", title: "Chưa có engine giọng đọc", body: "Vẫn tải được file audio lên. Mở trang Giọng đọc để chuẩn bị engine nếu muốn tạo bằng giọng." }));
    const others = items.filter((i) => !i.active);
    clear(listHost);
    if (!items.length) {
      listHost.append(emptyState({ icon: "mic", title: "Thư viện watermark còn trống", text: "Tạo watermark bằng giọng đọc hoặc tải file audio lên.", action: btn({ label: "Tạo watermark", icon: "plus", kind: "primary", onClick: () => openCreate().catch(toastError) }) }));
      return;
    }
    if (others.length) listHost.append(h("h3", { id: "wm-lib-h" }, "Thư viện"), h("div", { class: "wm-grid", "aria-labelledby": "wm-lib-h" }, ...others.map((i) => card(i, false))));
    if (animateId) { const el = root.querySelector(`[data-wm="${animateId}"]`); if (el) scope.add(() => motion.itemsEnter([el])); }
  }

  function card(item, isActive) {
    const chips = h("span", { class: "row" },
      h("span", { class: "chip" }, sourceLabel(item.source)),
      isActive ? h("span", { class: "chip ok" }, icon("check", { size: 12 }), " Đang dùng") : null,
      item.archived ? h("span", { class: "chip warn" }, "Đã lưu trữ") : null,
      !item.valid ? h("span", { class: "chip warn" }, icon("alert", { size: 12 }), " File thiếu/hỏng") : null);
    const meta = [durationText(item.duration_sec), item.revision_count > 1 ? `bản ${item.current_revision}` : null, item.updated_at ? `cập nhật ${relTime(item.updated_at)}` : null, item.in_use_by_jobs ? `${item.in_use_by_jobs} job đang dùng` : null].filter(Boolean).join(" · ");
    const player = h("div", { class: "wm-player" });
    const listen = btn({ label: "Nghe thử", icon: "play", size: "sm", disabled: !item.valid, onClick: () => playInto(player, item, item.current_revision) });
    player.append(listen);
    const actions = h("div", { class: "row" });
    if (!item.archived && !isActive && item.valid) actions.append(btn({ label: "Dùng cho kênh", icon: "check", size: "sm", kind: "primary", onClick: (e) => activate(item, e.currentTarget) }));
    if (isActive) actions.append(btn({ label: "Bỏ khỏi kênh", icon: "x", size: "sm", onClick: (e) => deactivate(e.currentTarget) }));
    if (item.archived) actions.append(btn({ label: "Khôi phục", icon: "undo", size: "sm", onClick: (e) => restore(item, e.currentTarget) }));
    else if (item.source !== "legacy") actions.append(btn({ label: "Sửa", icon: "settings", size: "sm", onClick: () => openEdit(item).catch(toastError) }));
    if (item.source === "tts" && !item.archived) actions.append(btn({ label: "Tạo lại", icon: "refresh", size: "sm", onClick: (e) => regenerate(item, e.currentTarget) }));
    if (item.source === "upload" && !item.archived) actions.append(replaceControl(item));
    actions.append(btn({ label: item.archived ? "Xóa hẳn…" : "Xóa…", icon: "trash", size: "sm", kind: "danger", onClick: () => confirmDelete(item).catch(toastError) }));
    const revHost = h("div", { class: "stack" });
    const el = h("article", { class: "wm-card", dataset: { wm: item.id, active: String(isActive), source: item.source }, "aria-label": item.name },
      h("div", { class: "row spread" }, h("h4", { class: "wm-name" }, item.name), chips),
      h("p", { class: "muted small" }, meta),
      item.text ? h("blockquote", { class: "wm-text" }, item.text) : null,
      item.tts ? h("p", { class: "small muted" }, "Giọng: " + ttsSummary(item.tts)) : null,
      player, actions,
      item.revision_count > 1 || item.source !== "legacy" ? disclosure({ label: `Các bản (${item.revision_count})`, content: revHost, onToggle: (open) => { if (open) loadRevisions(item, revHost); } }) : null);
    return el;
  }

  async function playInto(host, item, revision) {
    const path = `${base}/watermarks/${item.id}/audio${revision ? `?revision=${revision}` : ""}`;
    try {
      const url = await api.blobUrl(path, { fresh: true });
      urls.add(path);
      const audio = h("audio", { controls: true, src: url, preload: "metadata", "aria-label": `Nghe thử ${item.name}` });
      host.replaceChildren(audio);
      audio.play().catch(() => {});                                   // trình duyệt có thể chặn tự phát: người dùng vẫn bấm Phát được
    } catch (e) { toastError(e, "Không nghe thử được"); }
  }

  async function loadRevisions(item, host) {
    host.replaceChildren(skeleton(1));
    try {
      const d = await api.get(`${base}/watermarks/${item.id}`);
      host.replaceChildren(...d.revisions.slice().reverse().map((r) => {
        const pl = h("span", { class: "wm-player" }, btn({ label: "Nghe", icon: "play", size: "sm", kind: "ghost", onClick: () => playInto(pl, item, r.revision) }));
        return h("div", { class: "wm-rev row" }, h("strong", null, `Bản ${r.revision}`), h("span", { class: "muted small" }, `${durationText(r.duration_sec)} · ${relTime(r.created_at)}${r.jobs ? ` · ${r.jobs} job` : ""}`),
          r.active ? h("span", { class: "chip ok" }, "Đang dùng") : r.current ? h("span", { class: "chip" }, "Mới nhất") : null,
          r.text ? h("span", { class: "small muted wm-rev-text" }, r.text) : null, pl,
          !r.active && !item.archived ? btn({ label: "Dùng bản này", size: "sm", onClick: (e) => activate(item, e.currentTarget, r.revision) }) : null);
      }));
    } catch (e) { host.replaceChildren(errorState(e, () => loadRevisions(item, host))); }
  }

  // ---------- thao tác nhanh ----------
  async function activate(item, button, revision) {
    return busy(button, async () => {
      try {
        await api.post(`${base}/watermarks/${item.id}/activate`, revision ? { revision } : {});
        toast({ title: `Đã chọn “${item.name}”${revision ? ` (bản ${revision})` : ""} cho kênh`, message: "Job mới dùng watermark này; job đã tạo giữ nguyên bản cũ.", tone: "done" });
        await refresh({ animateId: item.id }); onSync?.();
      } catch (e) { toastError(e, "Chưa chọn được watermark"); }
    });
  }
  async function deactivate(button) {
    return busy(button, async () => {
      try { await api.post(`${base}/watermark/deactivate`, {}); toast({ title: "Đã bỏ watermark khỏi kênh", message: "File vẫn còn trong thư viện.", tone: "done" }); await refresh(); onSync?.(); }
      catch (e) { toastError(e, "Chưa bỏ được watermark"); }
    });
  }
  async function restore(item, button) {
    return busy(button, async () => { try { await api.post(`${base}/watermarks/${item.id}/restore`, {}); await refresh({ animateId: item.id }); } catch (e) { toastError(e, "Chưa khôi phục được"); } });
  }
  async function regenerate(item, button) {
    return busy(button, async () => {
      try {
        let r = await api.post(`${base}/watermarks/${item.id}/regenerate`, {});
        if (r.task) { toast({ title: "Đang tạo lại watermark…", tone: "info" }); r = await waitTask(r.task); }
        toast({ title: r.result === "unchanged" ? "Không có gì đổi: giữ bản hiện tại" : `Đã tạo bản ${r.item.current_revision}`, message: r.result === "unchanged" ? "Cùng nội dung và giọng nên không tốn thêm lượt giọng đọc." : "", tone: "done" });
        await refresh({ animateId: item.id }); onSync?.();
      } catch (e) { toastError(e, "Chưa tạo lại được"); }
    });
  }
  function replaceControl(item) {
    const f = h("input", { type: "file", accept: "audio/*,.wav,.mp3,.m4a,.flac,.ogg", hidden: true, "aria-label": `Chọn file thay cho ${item.name}` });
    const b = btn({ label: "Thay file", icon: "upload", size: "sm", onClick: () => f.click() });
    f.addEventListener("change", () => busy(b, async () => {
      const file = f.files[0]; f.value = "";
      if (!file) return;
      try {
        await api.upload(`${base}/watermarks/${item.id}/file?filename=${encodeURIComponent(file.name)}`, await file.arrayBuffer());
        toast({ title: "Đã thay file", message: "Tạo bản mới; bản cũ giữ nguyên cho job đã tạo.", tone: "done" });
        await refresh({ animateId: item.id }); onSync?.();
      } catch (e) { toastError(e, "Chưa thay được file"); }
    }));
    return h("span", null, b, f);
  }

  async function confirmDelete(item) {
    const m = deleteMode(item);
    const ok = await openDialog({ title: m.title, content: h("div", { class: "stack" }, h("p", null, m.body), m.note ? alertBox({ tone: "info", title: m.note }) : null),
      actions: [{ label: "Không", value: false }, { label: m.confirm, kind: "danger solid", value: true }] });
    if (ok !== true) return;
    try {
      const r = await api.del(`${base}/watermarks/${item.id}${m.unset ? "?unset=1" : ""}`);
      toast({ title: r.result === "archived" ? "Đã lưu trữ watermark" : "Đã xóa watermark", message: r.result === "archived" ? "Job cũ vẫn dùng được bản đã chốt." : "", tone: "done" });
      await refresh(); onSync?.();
    } catch (e) { toastError(e, "Chưa xóa được"); }
  }

  // ---------- dialog Tạo / Sửa ----------
  function ttsField(initial) {
    const t = lib.tts;
    const opts = [["auto", `Tự chọn (khuyên dùng)${t.auto.profile ? ` — ${t.auto.profile}` : ""}`], ...t.profiles.map((p) => [p.name, `${p.name} — ${p.engine || "?"}${p.voice ? ` · ${p.voice}` : ""}`])];
    const sel = select({ options: opts, value: opts.some(([v]) => v === initial) ? initial : "auto" });
    return { sel, el: field({ label: "Giọng đọc", hint: "Cùng danh sách giọng với truyện; “Tự chọn” dùng đúng cách chọn của truyện (profile ưa thích của kênh, rồi theo ngôn ngữ).", control: sel }) };
  }
  const progressBox = () => h("div", { class: "wm-progress", role: "status", hidden: true });
  function showProgress(box, text) { box.hidden = false; box.replaceChildren(icon("spinner", { size: 18, cls: "spin" }), h("span", null, text)); }

  // Dùng chung cho Tạo và Sửa: bật/tắt nút theo trạng thái, hiện lỗi cạnh ô nhập, giữ dialog khi lỗi nhà cung cấp, chuyển sang màn kết quả có nghe thử khi xong.
  async function openCreate() {
    const reqId = newRequestId();
    let source = lib.tts.available ? "tts" : "upload", working = false, ctl = null, ticker = null;
    const name = input({ placeholder: "Ví dụ: Intro truyện đêm", maxlength: 80 });
    const nameF = field({ label: "Tên watermark", control: name, required: true });
    const radios = [["tts", "Tạo bằng giọng đọc (TTS)"], ["upload", "Tải file audio"]].map(([v, l]) => h("label", { class: "row" }, h("input", { type: "radio", name: "wm-src", value: v, checked: source === v, onChange: () => { source = v; sync(); } }), h("span", null, l)));
    const text = h("textarea", { class: "textarea", rows: 4, maxlength: 1000, placeholder: "Bạn đang nghe truyện tại kênh ABC…", "aria-describedby": "wm-count" });
    const count = h("span", { class: "muted small", id: "wm-count" }, "0/1000");
    const textF = field({ label: "Nội dung watermark", hint: "Chỉ đọc đúng chữ bạn nhập — hệ thống không tự thêm câu nào.", control: text, required: true });
    const tts = ttsField(lib.tts.preset_profile || "auto");
    const file = h("input", { type: "file", accept: "audio/*,.wav,.mp3,.m4a,.flac,.ogg", class: "input", id: "wm-new-file" });
    const fileF = field({ label: "File audio", hint: "WAV, MP3, M4A, FLAC hoặc OGG, tối đa 50 MB, dài hơn 0,1 giây.", control: file });
    const act = h("label", { class: "row" }, h("input", { type: "checkbox", id: "wm-activate", checked: !lib.items.some((i) => i.active) }), h("span", null, "Dùng làm watermark của kênh sau khi tạo"));
    const prog = progressBox();
    const ttsBox = h("div", { class: "stack" }, textF, count, tts.el), upBox = h("div", { class: "stack" }, fileF);
    const form = h("div", { class: "stack" }, nameF, h("div", { class: "field", role: "radiogroup", "aria-label": "Nguồn" }, h("div", { class: "label" }, "Nguồn"), ...radios), ttsBox, upBox, act, prog, h("div", { "aria-live": "polite", class: "wm-err" }));
    const errBox = form.lastChild;
    const sync = () => { ttsBox.hidden = source !== "tts"; upBox.hidden = source !== "tts" ? false : true; if (ctl) ctl.buttons.create.disabled = working; };
    text.addEventListener("input", () => { count.textContent = `${text.value.length}/1000`; });
    const showErr = (code, e) => {
      errBox.replaceChildren(alertBox({ tone: "fail", title: e.message, body: e.hint || null }));
      motion.swap(errBox);
      const which = FIELD_OF_CODE[code];
      if (which === "name") nameF.setError(e.message); else if (which === "text") textF.setError(e.message); else if (which === "file") fileF.setError(e.message);
    };
    const finish = async (res) => {
      const item = res.item;
      working = false; clearInterval(ticker);
      const player = h("div", { class: "wm-player" });
      const ok = icon("check-circle", { size: 28 });
      form.replaceChildren(h("div", { class: "stack", role: "status" }, h("div", { class: "row" }, ok, h("strong", null, `Đã tạo “${item.name}” (${durationText(item.duration_sec)})`)), player,
        h("p", { class: "muted small" }, item.active ? "Watermark này đang được dùng cho kênh." : "Đã lưu vào thư viện. Bấm “Dùng cho kênh” ở thẻ của nó khi muốn dùng.")));
      scope.add(() => motion.successMark(ok));
      playInto(player, item, item.current_revision);
      ctl.buttons.create.hidden = true; ctl.buttons.cancel.querySelector("span").textContent = "Xong";
      await refresh({ animateId: item.id }); onSync?.();
    };
    const submit = async () => {
      if (working) return false;
      for (const f of [nameF, textF, fileF]) f.setError(null);
      errBox.replaceChildren();
      const v = { name: name.value, text: text.value, source, file: file.files[0] };
      const errs = createErrors(v);
      if (errs.name) nameF.setError(errs.name);
      if (errs.text) textF.setError(errs.text);
      if (errs.file) fileF.setError(errs.file);
      if (Object.keys(errs).length) { (errs.name ? name : errs.text ? text : file).focus(); return false; }
      working = true; sync(); ctl.buttons.create.setAttribute("aria-busy", "true");
      try {
        const activate = document.getElementById("wm-activate").checked;
        if (source === "tts") {
          const t0 = Date.now();
          showProgress(prog, "Đang tạo watermark…");
          ticker = setInterval(() => showProgress(prog, `Đang tạo watermark… ${Math.round((Date.now() - t0) / 1000)} giây`), 1000);
          const r = await api.post(`${base}/watermarks`, { name: name.value, text: text.value, tts: tts.sel.value, activate, request_id: reqId });
          await finish(await waitTask(r.task));
        } else {
          showProgress(prog, "Đang tải lên…");
          const f = v.file;
          if (f.size > 50 * 1024 * 1024) throw new ApiError("File quá lớn (tối đa 50 MB).", { code: "WATERMARK_AUDIO_INVALID" });
          await finish(await api.upload(`${base}/watermarks/upload?name=${encodeURIComponent(name.value)}&filename=${encodeURIComponent(f.name)}&activate=${activate ? 1 : 0}&request_id=${reqId}`, await f.arrayBuffer()));
        }
        return false;                                                // giữ dialog mở để hiện kết quả + nghe thử
      } catch (e) {
        working = false; clearInterval(ticker); prog.hidden = true;
        showErr(e.code, e);
        return false;
      } finally { ctl?.buttons.create.removeAttribute("aria-busy"); sync(); }
    };
    sync();
    await openDialog({ title: "Tạo watermark", wide: true, content: form, onOpen: (c) => { ctl = c; sync(); }, actions: [
      { id: "cancel", label: "Hủy", value: null }, { id: "create", label: "Tạo watermark", kind: "primary", value: "ok", onClick: submit }] });
    clearInterval(ticker);
  }

  async function openEdit(item) {
    let working = false, ctl = null;
    const name = input({ maxlength: 80 }); name.value = item.name;
    const nameF = field({ label: "Tên watermark", control: name, required: true });
    const text = h("textarea", { class: "textarea", rows: 4, maxlength: 1000 }); text.value = item.text || "";
    const textF = field({ label: "Nội dung watermark", hint: "Đổi nội dung hoặc giọng sẽ tạo BẢN MỚI; bản cũ giữ nguyên cho job đã tạo.", control: text, required: true });
    const tts = item.source === "tts" ? ttsField(item.tts?.selection || item.tts?.profile || "auto") : null;
    const prog = progressBox();
    const err = h("div", { "aria-live": "polite" });
    const form = h("div", { class: "stack" }, nameF, item.source === "tts" ? textF : h("p", { class: "muted small" }, "Watermark tải lên: đổi tên ở đây; dùng nút “Thay file” để thay audio (tạo bản mới)."), tts?.el || null, prog, err);
    const state = () => editKind(item, { name: name.value, text: text.value, tts: tts?.sel.value });
    const sync = () => {
      const k = state();
      if (ctl) { ctl.buttons.save.disabled = working || k === "none" || !name.value.trim(); ctl.buttons.save.querySelector("span").textContent = EDIT_LABEL[k]; }
    };
    for (const c of [name, text]) c.addEventListener("input", sync);
    tts?.sel.addEventListener("change", sync);
    const save = async () => {
      if (working) return false;
      nameF.setError(null); textF.setError(null); err.replaceChildren();
      working = true; sync();
      const k = state();
      try {
        const body = { name: name.value };
        if (k === "revision") { body.text = text.value; body.tts = tts.sel.value; }
        let r = await api.put(`${base}/watermarks/${item.id}`, body);
        if (r.task) { showProgress(prog, "Đang tạo bản mới…"); r = await waitTask(r.task); }
        toast({ title: r.result === "revised" ? `Đã tạo bản ${r.item.current_revision}` : "Đã lưu", tone: "done" });
        await refresh({ animateId: item.id }); onSync?.();
        return true;
      } catch (e) {
        prog.hidden = true; err.replaceChildren(alertBox({ tone: "fail", title: e.message, body: e.hint || null }));
        const which = FIELD_OF_CODE[e.code]; if (which === "name") nameF.setError(e.message); else if (which === "text") textF.setError(e.message);
        return false;
      } finally { working = false; sync(); }
    };
    await openDialog({ title: `Sửa “${item.name}”`, wide: true, content: form, onOpen: (c) => { ctl = c; sync(); }, actions: [
      { label: "Hủy", value: null }, { id: "save", label: "Lưu", kind: "primary", value: "ok", disabled: true, onClick: save }] });
  }

  refresh();
  return { el: root, refresh, destroy() { alive = false; for (const u of urls) forgetBlob(u); } };
}
