// Chi tiết job: pipeline trực quan từng stage, vì sao đang dừng và hệ thống sẽ làm gì tiếp, nút hành động đúng ngữ cảnh, output, chi tiết kỹ thuật thu gọn.
import { api, forgetBlob } from "../api.js";
import { h, clear, patchList } from "../dom.js";
import { icon } from "../icons.js";
import { btn, alertBox, badge, jobBadge, updateBadge, stageBadge, progress, updateProgress, errorState, skeleton, switchCtl, disclosure, kv, toast, toastError, busy } from "../components.js";
import { createPoller } from "../poller.js";
import { jobStatus, stageState, timelineState, ACTION_LABEL, BRANCH_LABEL, PART_STATE } from "../status.js";
import { relTime, duration } from "../format.js";
import { openOutput, pauseJob, resumeJob, retryJob, setAutoResume } from "../actions.js";
import { openClone, confirmCancel, openRerollThumbnail } from "./_job_control.js";
import { openEditJob } from "./_job_edit.js";
import { openProsodyDialog } from "./_prosody.js";
import { storyGuidanceCard } from "./_story_guidance.js";
import { openRerunDialog, rerunBanner, rerunHistoryCard } from "./_rerun.js";
import { countBadge, countLabel, finishedSession, RESULT_LABEL } from "../rerun_logic.js";
import { externalLink } from "./_batch_ui.js";
import * as motion from "../motion.js";

const TONE = { waiting: "wait", attention: "attn", failed: "fail", paused: "wait" };

export async function mount(root, ctx) {
  const [id] = ctx.params;
  const { scope } = ctx;
  let data = null, sigs = {}, logOpen = false, logState = { lines: [], start: 0, more: false };

  const head = h("div", null);
  const alertHost = h("div", { "aria-live": "polite" });
  const autoHost = h("div", null);
  const controlHost = h("div", null);
  const thumbHost = h("div", null);
  const guidanceHost = h("div", null);
  const rerunHost = h("div", { "aria-live": "polite" });
  const history = rerunHistoryCard(id);
  let lastActive = null;
  let thumbUrl = null;
  const stagesList = h("ul", { class: "stages", "aria-label": "Các bước của pipeline" });
  const stagesCard = h("section", { class: "card", "aria-labelledby": "pipe-h" }, h("div", { class: "card-title" }, h("h2", { id: "pipe-h" }, "Pipeline")), stagesList);
  const outputHost = h("div", null);
  const techHost = h("div", null);
  root.append(h("p", null, btn({ label: "Tất cả job", icon: "list", kind: "ghost", size: "sm", href: "#/jobs" })), head, h("div", { class: "stack" }, alertHost, autoHost, rerunHost, controlHost, thumbHost, guidanceHost, stagesCard, history.el, outputHost, techHost));
  head.append(skeleton(2));

  // ---------- tải ----------
  const poller = createPoller(async (signal) => {
    let d;
    try { d = await api.get(`/api/jobs/${id}`, { signal }); }
    catch (e) {
      if (e.name === "AbortError") throw e;
      if (e.status === 404 || e.code === "JOB_NOT_FOUND") { root.replaceChildren(h("h1", { id: "page-title" }, "Không tìm thấy job"), alertBox({ tone: "fail", title: e.message, body: e.hint, actions: [btn({ label: "Về danh sách job", href: "#/jobs", size: "sm" })] })); poller.stop(); return; }
      if (!data) { head.replaceChildren(errorState(e, () => poller.poke())); }
      throw e;
    }
    data = d;
    paint(d);
    if (logOpen && (d.status === "running" || d.status === "queued")) await loadLog(true, signal);
    return d.status === "running" || d.status === "queued" || d.control?.pausing || d.pending_revision || d.rerun?.active ? "fast" : "idle";
  });

  const after = () => poller.poke();

  // ---------- vẽ ----------
  function paint(d) {
    document.title = `${d.title} · ContentFactory`;
    sig("head", [d.title, d.status, d.channel, d.created_at, JSON.stringify(d.actions), d.control?.pausing, JSON.stringify(d.links), JSON.stringify(d.batch), d.edit?.awaiting_run], () => paintHead(d));
    sig("alert", [d.status, d.edit?.awaiting_run, d.edit?.awaiting_text, JSON.stringify(d.diagnosis.hold), d.diagnosis.human, d.diagnosis.resume.actions.join(), d.diagnosis.attempts, d.diagnosis.resume.text], () => paintAlert(d));
    sig("auto", [d.auto_resume, d.status], () => paintAuto(d));
    sig("control", [JSON.stringify(d.actions), JSON.stringify(d.pending_revision), d.pipeline_revision, d.requested_stages.join()], () => paintControl(d));
    sig("thumb", [JSON.stringify(d.thumbnail), d.pending_revision?.revision], () => paintThumb(d));
    sig("guidance", [JSON.stringify(d.story_guidance)], () => { clear(guidanceHost); const c = storyGuidanceCard(d, { after }); if (c) guidanceHost.append(c); });
    sig("rerun", [JSON.stringify(d.rerun?.active)], () => { clear(rerunHost); const b = rerunBanner(d); if (b) rerunHost.append(b); });
    const done = finishedSession(lastActive, d.rerun?.active);                       // phiên vừa kết thúc: báo kết quả + làm mới lịch sử
    lastActive = d.rerun?.active || null;
    if (done) { history.reload(); api.get(`/api/jobs/${id}/reruns`).then((r) => { const s = r.sessions.find((x) => x.id === done.id); if (s) toast({ title: `Chạy lại #${s.number}: ${RESULT_LABEL[s.result] || s.result}`, tone: s.result === "succeeded" ? "done" : "wait" }); }).catch(() => {}); }
    paintStages(withRerun(d));
    sig("output", [JSON.stringify(d.output)], () => paintOutput(d));
    sig("tech", [d.decisions.length, d.attempts.length, d.mode.start, d.mode.target], () => paintTech(d));
  }
  // Gắn số lần chạy lại + cờ "không đồng bộ" (backend tính) vào từng hàng của pipeline để updateStage vẽ badge.
  function withRerun(d) {
    const r = d.rerun || {}, lab = Object.fromEntries(d.pipeline.map((s) => [s.name, s.label])), run = (r.active?.stages || []).find((x) => x.state === "running")?.id;
    return d.pipeline.map((s) => ({ ...s, rerun_count: r.counts?.[s.name] || 0, stale: !!r.stale?.[s.name], stale_by: (r.stale_by?.[s.name] || []).map((x) => lab[x] || x), rerunning: run === s.name }));
  }
  function sig(key, parts, fn) { const s = JSON.stringify(parts); if (sigs[key] !== s) { sigs[key] = s; fn(); } }

  function paintHead(d) {
    const live = h("span", { class: "sr-only", "aria-live": "polite" }, `Trạng thái: ${jobStatus(d.status).label}`);
    const bd = jobBadge(d.status);
    head.replaceChildren(h("div", { class: "page-head" },
      h("div", { class: "grow" }, h("h1", { id: "page-title" }, d.title), h("p", { class: "muted" }, `Job #${d.id} · Kênh ${d.channel} · tạo ${relTime(d.created_at)}`),
        d.batch ? h("p", { class: "small" }, icon("tv", { size: 14 }), " Thuộc ", h("a", { href: `#/batches/${d.batch.id}` }, `Channel Run ${d.batch.title}`), d.batch.position ? ` (video #${d.batch.position})` : "") : null,
        linkRow(d.links)),
      h("div", { class: "row" }, bd, live, controlBtn(d), rerunBtn(d), editBtn(d), d.output?.project_dir ? openBtn(d) : null)));
  }
  // Nút điều khiển chính theo ngữ cảnh: Tạm dừng (job đang sống) hoặc Tiếp tục (đã tạm dừng). Hủy nằm ở "Thao tác nâng cao" vì không hoàn tác được.
  function controlBtn(d) {
    if (d.actions?.unpause) {
      const b = btn({ label: d.edit?.awaiting_run ? "Chạy tiếp" : ACTION_LABEL.resume, icon: "play", kind: "primary" });
      b.addEventListener("click", () => resumeJob(d.id, b, { after }));
      return b;
    }
    if (d.actions?.pause) {
      const b = btn({ label: ACTION_LABEL.pause, icon: "pause", title: "Hoàn tất đơn vị đang chạy rồi dừng; kết quả đã xong được giữ nguyên" });
      b.addEventListener("click", () => pauseJob(d.id, b, { after }));
      return b;
    }
    return null;
  }

  // “Chạy lại”: chọn các bước muốn chạy lại (kể cả job đã xong/đã đăng); mọi quyết định do backend.
  function rerunBtn(d) {
    if (!d.actions?.rerun) return null;
    const b = btn({ label: "Chạy lại", icon: "refresh", title: "Chọn các bước muốn chạy lại; bước khác giữ nguyên" });
    b.addEventListener("click", () => openRerunDialog(d, { after }).catch((e) => toastError(e)));
    return b;
  }

  // “Sửa job”: Cập nhật pipeline (đổi đích theo progress floor) + Xóa job. Một lối vào duy nhất — không còn nút “Cập nhật pipeline” riêng.
  function editBtn(d) {
    if (!d.actions?.edit) return null;
    const b = btn({ label: "Sửa job", icon: "settings", title: "Cập nhật pipeline hoặc xóa job" });
    b.addEventListener("click", () => openEditJob(d, { after, navigate: ctx.navigate }).catch((e) => toastError(e)));
    return b;
  }

  // Ảnh thumbnail đã chốt từ Image Pool (nếu kênh dùng pool): ảnh + nguồn + lý do + “Đổi ảnh” (tắt kèm lý do khi job đã kết thúc).
  function paintThumb(d) {
    clear(thumbHost);
    if (thumbUrl) { forgetBlob(thumbUrl); thumbUrl = null; }
    const t = d.thumbnail;
    if (!t) return;
    const why = (d.decisions || []).find((x) => x.what === "thumbnail.image");
    const img = h("div", { class: "ip-img skeleton", style: "aspect-ratio:16/10" });
    thumbUrl = t.image_url;
    api.blobUrl(t.image_url, { fresh: true }).then((u) => img.replaceWith(h("img", { src: u, alt: `Ảnh thumbnail đã chốt: ${t.source_relpath}` }))).catch(() => img.replaceWith(h("p", { class: "muted small" }, "Không tải được ảnh (file trong workspace đã mất?).")));
    const reroll = btn({ label: "Đổi ảnh thumbnail…", icon: "refresh", size: "sm", disabled: !t.can_reroll, onClick: () => openRerollThumbnail(d, { after }).catch((e) => toastError(e)) });
    thumbHost.append(h("section", { class: "card stack", "aria-labelledby": "thumb-h" }, h("div", { class: "card-title" }, h("h2", { id: "thumb-h" }, "Ảnh thumbnail"), reroll),
      h("div", { class: "job-thumb" }, img, h("div", { class: "stack" },
        kv([["Pool", t.pool], ["File", t.source_relpath], ["Kích thước", t.width ? `${t.width}×${t.height}` : "—"], ["Cách chọn", t.selection_mode], ["Đã đổi", `${t.rerolls} lần`]]),
        why ? h("p", { class: "small muted" }, why.why) : null,
        t.can_reroll ? null : h("p", { class: "small", role: "note" }, t.reroll_blocked),
        d.pending_revision ? h("p", { class: "small muted" }, "Có thay đổi đang chờ áp dụng ở điểm an toàn.") : null))));
  }

  function paintControl(d) {
    clear(controlHost);
    const a = d.actions || {};
    const box = h("div", { class: "stack" });
    if (d.pending_revision) {
      const s = d.pending_revision.summary;
      box.append(alertBox({ tone: "info", title: `Có thay đổi pipeline đang chờ áp dụng (bản ${d.pending_revision.revision})`,
        body: h("div", null, d.pending_revision.apply_policy === "pause_and_apply" ? "Job sẽ dừng ở ranh giới an toàn kế tiếp rồi tự chạy tiếp." : "Sẽ áp dụng sau điểm an toàn, không làm hỏng đơn vị đang chạy.",
          s.will_run.length ? h("div", { class: "small" }, "Sẽ chạy: " + s.will_run.join(", ")) : null) }));
    }
    const tools = h("div", { class: "row" });
    if (a.prosody) tools.append(btn({ label: "Nhịp đọc…", icon: "mic", size: "sm", onClick: () => openProsodyDialog(d, { after, navigate: ctx.navigate }).catch((e) => toastError(e)) }));
    if (a.clone) tools.append(btn({ label: "Chạy lại với thay đổi…", icon: "refresh", size: "sm", onClick: () => openClone(d, { navigate: ctx.navigate }).catch((e) => toastError(e)) }));
    if (a.cancel) tools.append(btn({ label: "Hủy job…", icon: "x", size: "sm", kind: "danger", onClick: () => confirmCancel(d, { after }) }));
    if (tools.childElementCount) box.append(disclosure({ label: "Thao tác nâng cao", content: h("div", { class: "stack", style: "padding-top: var(--s-2)" }, h("p", { class: "muted small" }, d.status === "completed" || d.status === "cancelled" || d.status === "failed" ? "Job đã kết thúc: kết quả cũ không bị thay đổi tại chỗ." : "Thay đổi chỉ áp dụng ở điểm an toàn và chỉ chạy lại phần bị ảnh hưởng."), tools) }));
    if (box.childElementCount) controlHost.append(h("section", { class: "card stack" }, box));
  }
  // Link YouTube do backend trả (đã kiểm https + host); mở tab mới với noopener.
  function linkRow(l) {
    const ls = [["source_video_url", "Mở video nguồn"], ["source_channel_url", "Mở kênh nguồn"], ["published_video_url", "Mở video đã đăng"]].filter(([k]) => l?.[k]);
    return ls.length ? h("div", { class: "row wrap" }, ...ls.map(([k, label]) => externalLink(l[k], label))) : null;
  }
  function openBtn(d) {
    const b = btn({ label: "Mở thư mục output", icon: "folder-open", kind: "primary" });
    b.addEventListener("click", () => openOutput(d.id, b));
    return b;
  }

  function paintAlert(d) {
    clear(alertHost);
    const dg = d.diagnosis;
    if (d.status === "completed") {
      const ok = icon("check-circle", { size: 22 });
      alertHost.append(h("div", { class: "alert", dataset: { tone: "done" } }, ok, h("div", { class: "body" }, h("div", { class: "title" }, "Job đã hoàn tất"), h("div", null, d.output?.project_dir ? "Mọi thứ đã sẵn sàng trong thư mục output." : "Các bước đã chọn đã chạy xong."))));
      scope.add(() => motion.successMark(ok));
      return;
    }
    if (d.edit?.awaiting_run) {                                                  // đã Lưu pipeline mới cho job đã xong: KHÔNG tự chạy, chờ người dùng
      const go = btn({ label: "Chạy tiếp", icon: "play", size: "sm", kind: "primary" });
      go.addEventListener("click", () => resumeJob(d.id, go, { after }));
      alertHost.append(alertBox({ tone: "info", title: "Có bước mới chưa chạy", body: d.edit.awaiting_text, actions: [go] }));
      return;
    }
    if (!TONE[d.status]) return;
    const actions = [];
    if (d.status !== "paused") for (const a of dg.resume.actions) actions.push(actionBtn(a, d));      // tạm dừng: nút Tiếp tục đã ở đầu trang
    const body = h("div", { class: "stack" });
    if (dg.hold) body.append(h("div", null, dg.hold.why));
    else if (dg.human) body.append(h("div", null, dg.human));
    if (dg.reason && !dg.hold) body.append(h("div", { class: "small mono" }, dg.reason));
    if (dg.hold?.detail) body.append(h("details", null, h("summary", { class: "small" }, "Chi tiết kỹ thuật"), h("p", { class: "small mono" }, dg.hold.detail)));
    body.append(h("div", { class: "small" }, h("strong", null, "Hệ thống sẽ: "), dg.resume.text || "—"));
    if (dg.attempts) body.append(h("div", { class: "small" }, `Đã thử ${dg.attempts} lần ở bước “${dg.stage_label}”` + (dg.failed_attempts ? ` (${dg.failed_attempts} lần lỗi/giữ)` : "") + "."));
    alertHost.append(alertBox({ tone: TONE[d.status], title: dg.hold?.title || dg.human || "Cần xử lý", body, actions }));
  }

  function actionBtn(a, d) {
    const b = btn({ label: ACTION_LABEL[a], icon: { resume: "play", resume_now: "refresh", retry: "refresh", enable_auto_resume: "zap", disable_auto_resume: "pause" }[a], size: "sm", kind: a === "resume" || a === "retry" ? "primary" : "" });
    b.addEventListener("click", () => {
      if (a === "resume") return resumeJob(d.id, b, { after });
      if (a === "resume_now") return resumeJob(d.id, b, { now: true, after });
      if (a === "retry") return retryJob(d.id, b, { after });
      return busy(b, async () => { await setAutoResume(d.id, a === "enable_auto_resume", { after }); });
    });
    return b;
  }

  function paintAuto(d) {
    clear(autoHost);
    if (d.status === "completed" || d.status === "failed" || d.status === "cancelled") return;
    const hid = "auto-hint";
    const sw = switchCtl({ label: "Auto Resume", checked: !!d.auto_resume, describedBy: hid, onChange: async (v) => { const ok = await setAutoResume(d.id, v, { after }); if (!ok) { sw.input.checked = !v; sw.querySelector(".state").textContent = !v ? "Bật" : "Tắt"; } } });
    autoHost.append(h("div", { class: "card row spread" }, sw, h("p", { class: "muted small", id: hid }, d.auto_resume ? "Khi gặp sự cố tạm thời (mạng, quota…), hệ thống tự chạy tiếp từ chỗ dừng." : "Khi gặp sự cố tạm thời, job chờ bạn bấm Tiếp tục.")));
  }

  function paintStages(rows) {
    const added = patchList(stagesList, rows, (s) => s.name, (s) => {
      const li = h("li", { class: "stage-row" });
      const why = h("p", { class: "small muted", hidden: true, id: `why-${s.name}` });
      const whyBtn = h("button", { type: "button", class: "btn ghost sm", "aria-expanded": "false", "aria-controls": `why-${s.name}` }, "Vì sao?");
      whyBtn.addEventListener("click", () => { why.hidden = !why.hidden; whyBtn.setAttribute("aria-expanded", String(!why.hidden)); });
      li._p = { ico: h("div", { class: "ico" }), name: h("div", { class: "name" }), detail: h("div", { class: "small muted" }), items: h("div", { class: "items" }), bar: progress(0, "running", "Tiến độ bước"), right: h("div", { class: "row" }), why, whyBtn };
      li.append(li._p.ico, h("div", { class: "grow" }, li._p.name, li._p.detail, li._p.bar, li._p.items, whyBtn, why), li._p.right);
      updateStage(li, s);
      return li;
    }, updateStage);
    scope.add(() => motion.itemsEnter(added));
  }

  function updateStage(li, s) {
    const key = JSON.stringify(s);
    if (li._k === key) return;
    li._k = key;
    const p = li._p, meta = s.timeline ? timelineState(s.timeline) : stageState(s.state);
    li.dataset.state = s.state;
    li.dataset.name = s.name;
    li.dataset.branch = s.branch || "shared";
    li.dataset.timeline = s.timeline || "";
    p.ico.replaceChildren(icon(meta.icon, { size: 20, cls: meta.spin ? "spin" : "" }));
    p.ico.style.color = `var(--st-${{ done: "done", running: "running", wait: "wait", fail: "fail", queue: "queue", off: "queue" }[meta.tone] || "queue"}-fg)`;
    p.name.replaceChildren(...[s.label, BRANCH_LABEL[s.branch] && s.name !== "publish" ? h("span", { class: "chip branch-chip" }, BRANCH_LABEL[s.branch]) : null,
      s.rerun_count > 0 ? h("span", { class: "chip branch-chip", title: countLabel(s.rerun_count) }, countBadge(s.rerun_count)) : null,
      s.rerunning ? h("span", { class: "chip branch-chip" }, "Đang chạy lại") : null,
      s.stale ? h("span", { class: "chip warn branch-chip", title: s.stale_by?.length ? `Không còn đồng bộ với: ${s.stale_by.join(", ")}` : "Kết quả không còn đồng bộ với đầu vào/tham số hiện tại" }, "Không đồng bộ") : null].filter(Boolean));
    p.why.textContent = s.why || "";
    p.whyBtn.hidden = !s.why;
    const bits = [];
    if (s.detail && s.state !== "waiting") bits.push(s.detail);
    if (s.total && (s.state === "running" || s.state === "held" || s.state === "failed")) bits.unshift(`${s.done ?? 0}/${s.total}`);
    if (s.seconds >= 1) bits.push(duration(s.seconds));
    if (s.attempts > 1) bits.push(`${s.attempts} lần chạy`);
    p.detail.textContent = bits.join(" · ");
    const showBar = s.total && (s.state === "running" || s.state === "held" || s.state === "failed");
    p.bar.hidden = !showBar;
    if (showBar) updateProgress(p.bar, (s.done || 0) / s.total, { running: "running", held: "wait", failed: "fail" }[s.state] || "running");
    p.items.replaceChildren(...s.items.map((it) => h("span", { class: "badge", dataset: { tone: { done: "done", failed: "fail", running: "running" }[it.state] || "queue" }, title: it.error || "" }, `${it.name} · ${PART_STATE[it.state] || it.state || ""}`)));
    p.right.replaceChildren(s.timeline ? badge(meta) : stageBadge(s.state));
  }

  function paintOutput(d) {
    clear(outputHost);
    const o = d.output;
    if (!o) return;
    const card = h("section", { class: "card stack", "aria-labelledby": "out-h" });
    card.append(h("div", { class: "card-title" }, h("h2", { id: "out-h" }, "Kết quả"), o.project_dir ? openBtn(d) : null));
    if (o.project_dir) card.append(o.exists === false ? alertBox({ tone: "wait", iconName: "alert", title: "Thư mục output đã bị di chuyển hoặc xoá", body: "Nó thuộc về bạn nên hệ thống không tạo lại." }) : h("p", { class: "muted small mono" }, o.project_dir));
    if (o.youtube_url) card.append(h("p", null, icon("link", { size: 16 }), " ", h("a", { href: o.youtube_url, target: "_blank", rel: "noopener noreferrer" }, o.youtube_url)));
    const files = (o.files || []).filter((f) => f !== "project.json").sort();
    if (files.length) card.append(h("div", null, h("div", { class: "small muted", style: "margin-bottom: var(--s-1)" }, `${files.length} file trong gói output`), h("ul", { class: "files" }, ...files.map((f) => h("li", null, f)))));
    if (o.youtube_title) card.append(h("div", { class: "preview-box" }, h("div", { class: "small muted" }, "Tiêu đề YouTube sẽ dùng"), h("div", { class: "yt-title" }, o.youtube_title), h("div", { class: "small muted" }, "Mô tả"), h("div", { class: "small", style: "white-space: pre-wrap" }, o.description || "")));
    for (const w of o.warnings || []) card.append(alertBox({ tone: "wait", iconName: "alert", title: "Lưu ý", body: w }));
    outputHost.append(card);
  }

  function paintTech(d) {
    clear(techHost);
    const content = h("div", { class: "stack" });
    content.append(kv([["Chế độ", `${d.mode.start || "tự động"} → ${d.mode.target || "publish"}`], ["Trạng thái nội bộ", d.state], ["Đầu vào", d.params_public.input?.value || d.input_kind]]));
    if (d.decisions.length) content.append(h("div", null, h("h3", null, "Hệ thống đã tự chọn"), h("ul", { class: "autolist" }, ...d.decisions.map((x) => h("li", null, `${x.what}: ${typeof x.value === "object" ? "theo preset" : x.value} — ${x.why}`)))));
    const rows = d.attempts.slice().reverse();
    content.append(h("div", null, h("h3", null, "Các lần chạy"), h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", null, h("tr", null, ...["Bước", "Lần", "Kết quả", "Bắt đầu"].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", null, ...rows.map((r) => h("tr", null, h("td", null, r.stage), h("td", null, String(r.attempt)), h("td", null, r.status), h("td", null, relTime(r.started_at)))))))));
    const logBox = h("div", { class: "log", role: "log", "aria-label": "Nhật ký job", tabindex: "0" });
    const refresh = btn({ label: "Làm mới log", icon: "refresh", size: "sm", onClick: () => loadLog(true) });
    const older = btn({ label: "Xem cũ hơn", size: "sm", onClick: () => loadLog(false) });
    content.append(h("div", { class: "stack" }, h("div", { class: "row spread" }, h("h3", null, "Nhật ký (sự kiện mới nhất)"), h("div", { class: "row" }, older, refresh)), logBox));
    techHost.append(h("section", { class: "card" }, disclosure({ label: "Chi tiết kỹ thuật", content, open: logOpen, onToggle: (v) => { logOpen = v; if (v) loadLog(true); } })));
    techHost._log = logBox; techHost._older = older;
    if (logOpen) renderLog();
  }

  async function loadLog(fresh, signal) {
    try {
      const r = await api.get(`/api/jobs/${id}/log`, { query: { tail: 120, before: fresh ? null : logState.start }, signal });
      logState = fresh ? { lines: r.lines, start: r.start, more: r.has_more } : { lines: [...r.lines, ...logState.lines], start: r.start, more: r.has_more };
      renderLog();
    } catch (e) { if (e.name !== "AbortError") toastError(e, "Không đọc được log"); }
  }
  function renderLog() {
    const box = techHost._log;
    if (!box) return;
    const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
    box.replaceChildren(...logState.lines.map((l) => h("div", { class: `lv-${l.level}` }, `${(l.ts || "").slice(11, 19)} ${l.stage ? `[${l.stage}] ` : ""}${l.event || ""} ${l.text || ""}`.trim())));
    if (!logState.lines.length) box.append(h("div", { class: "muted" }, "Chưa có sự kiện."));
    if (techHost._older) techHost._older.hidden = !logState.more;
    if (stick) box.scrollTop = box.scrollHeight;
  }

  poller.start();
  return { destroy() { poller.stop(); if (thumbUrl) forgetBlob(thumbUrl); } };
}
