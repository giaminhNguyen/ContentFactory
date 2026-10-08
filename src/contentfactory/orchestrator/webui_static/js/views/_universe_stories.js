// Tab "Truyện & dàn nhân vật" của Kho nhân vật: xem dàn nhân vật AI đã tự chọn cho từng truyện (vai, vì sao, quan hệ) và — TUỲ CHỌN — thay một nhân vật.
import { api } from "../api.js";
import { h, clear } from "../dom.js";
import { btn, badge, alertBox, emptyState, errorState, skeleton, toast, toastError, openDialog } from "../components.js";
import * as L from "../universe_logic.js";

const SVGNS = "http://www.w3.org/2000/svg";
const svg = (tag, attrs = {}) => { const el = document.createElementNS(SVGNS, tag); for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v); return el; };
const stateBadge = (state) => badge({ tone: state === "published" ? "done" : "wait", icon: state === "published" ? "check-circle" : "hourglass", label: L.STORY_STATE[state] || state });

export function mountStories(host, { onOpenCharacter }) {
  let alive = true, selected = null;
  const listBox = h("div", { class: "uv-list", role: "list", "aria-label": "Các truyện" });
  const detailBox = h("div", { class: "uv-detail" });
  host.append(h("div", { class: "uv-layout" }, h("div", { class: "stack uv-left" }, listBox), detailBox));

  async function loadList() {
    clear(listBox);
    listBox.append(skeleton(3));
    try {
      const { stories } = await api.get("/api/universe/stories");
      if (!alive) return;
      clear(listBox);
      if (!stories.length) { listBox.append(emptyState({ icon: "layers", title: "Chưa có truyện nào dùng Kho nhân vật", text: "Khi Story Remix tự chọn nhân vật cho một truyện, dàn nhân vật và lý do chọn sẽ hiện ở đây." })); return; }
      for (const s of stories) {
        listBox.append(h("button", { type: "button", class: "uv-card", role: "listitem", "aria-current": s.story_id === selected ? "true" : null, onclick: () => { selected = s.story_id; loadList(); loadDetail(); } },
          h("span", { class: "uv-card-main" }, h("strong", null, s.story_id), h("span", { class: "uv-chips" }, stateBadge(s.state), h("span", { class: "chip" }, s.genre || "chưa rõ thể loại")),
            h("span", { class: "muted small" }, `${s.members} nhân vật · ${s.reused} dùng lại · ${s.new} mới`))));
      }
      if (!selected) { selected = stories[0].story_id; loadList(); loadDetail(); }
    } catch (e) { if (alive) { clear(listBox); listBox.append(errorState(e, loadList)); } }
  }

  async function loadDetail() {
    clear(detailBox);
    if (!selected) { detailBox.append(h("div", { class: "uv-hint muted" }, "Chọn một truyện để xem dàn nhân vật.")); return; }
    detailBox.append(skeleton(4));
    try {
      const d = await api.get(`/api/universe/stories/${selected}`);
      if (alive) paint(d);
    } catch (e) { if (alive) { clear(detailBox); detailBox.append(errorState(e, loadDetail)); } }
  }

  function paint({ cast, orphans, can_replace }) {
    clear(detailBox);
    const nameOf = (id) => cast.members.find((m) => m.character_id === id)?.display_name || id;
    const head = h("div", null, h("h2", null, cast.story_id),
      h("div", { class: "uv-chips" }, stateBadge(cast.state), h("span", { class: "chip" }, `Dòng thời gian riêng: ${cast.world_id}`), h("span", { class: "chip" }, cast.strategy === "create_new" ? "Ưu tiên tạo mới" : "Ưu tiên dùng lại")),
      h("p", { class: "muted small" }, "Dàn nhân vật được chốt trước khi viết. Sự kiện trong truyện này (chết, yêu, phản bội…) chỉ thuộc truyện này, không lan sang truyện khác."));
    const notes = [];
    if (orphans.length) notes.push(alertBox({ tone: "fail", title: "Có quan hệ trỏ tới nhân vật không còn trong dàn", body: orphans.join(", ") }));
    for (const i of cast.issues.filter((x) => x.action === "kept")) notes.push(alertBox({ tone: "wait", title: L.ISSUE_LABEL[i.code] || i.code, body: i.message }));
    if (cast.state === "staged" && cast.members.some((m) => m.origin === "created")) notes.push(alertBox({ tone: "info", title: "Nhân vật mới đang chờ", body: "Chỉ được thêm vào Kho chính thức sau khi truyện đạt QA." }));

    const cards = h("div", { class: "uv-cast" }, ...cast.members.map((m) => {
      const fit = m.fit == null ? null : h("div", { class: "uv-fit" }, h("progress", { max: 100, value: Math.round(m.fit * 100), "aria-label": `Độ hợp vai ${Math.round(m.fit * 100)}%` }), h("span", { class: "small" }, L.fitText(m)));
      return h("article", { class: "uv-member" }, h("div", { class: "row spread" }, h("strong", null, m.display_name), h("span", { class: "chip" }, L.roleLabel(m.role_code))),
        h("div", { class: "uv-chips" }, badge({ tone: m.origin === "created" ? "wait" : "done", icon: m.origin === "created" ? "plus" : "refresh", label: m.origin === "created" ? "Mới (chờ QA)" : "Dùng lại từ kho" }),
          m.lock_status === "frozen" ? badge({ tone: "off", icon: "lock", label: "Đã chốt" }) : null),
        fit, h("p", { class: "small" }, m.rationale), m.goal ? h("p", { class: "small" }, h("strong", null, "Mục tiêu trong truyện: "), m.goal) : null,
        h("div", { class: "row" }, m.origin === "reused" ? btn({ label: "Xem hồ sơ", size: "sm", kind: "ghost", onClick: () => onOpenCharacter(m.character_id) }) : null,
          can_replace && m.slot_id ? btn({ label: "Thay nhân vật", size: "sm", onClick: () => openReplace(cast, m) }) : null));
    }));

    const lay = L.graphLayout(cast.members, cast.relationships, 320);
    const g = svg("svg", { viewBox: `0 0 ${lay.size} ${lay.size}`, class: "uv-graph", role: "img", "aria-label": `Sơ đồ quan hệ của ${cast.members.length} nhân vật` });
    const text = (x, y, cls, s) => { const t = svg("text", { x, y, class: cls, "text-anchor": "middle" }); t.textContent = s; return t; };
    for (const e of lay.edges) {
      g.append(svg("line", { x1: e.from.x, y1: e.from.y, x2: e.to.x, y2: e.to.y, class: "uv-edge" }), text((e.from.x + e.to.x) / 2, (e.from.y + e.to.y) / 2 - 4, "uv-edge-l", e.type.replaceAll("_", " ")));
    }
    for (const n of lay.nodes) {
      const m = cast.members.find((x) => x.character_id === n.id);
      g.append(svg("circle", { cx: n.x, cy: n.y, r: 22, class: "uv-node" + (m.origin === "created" ? " new" : "") }), text(n.x, n.y + 5, "uv-node-t", L.initials(m.display_name)), text(n.x, n.y + 40, "uv-edge-l", m.display_name));
    }
    const relList = cast.relationships.length ? h("ul", { class: "small" }, ...cast.relationships.map((r) => h("li", null, L.relText(r, nameOf)))) : h("p", { class: "muted small" }, "Chưa có quan hệ nào được lên kế hoạch.");
    detailBox.append(head, ...notes, cards, h("h3", null, "Quan hệ trong truyện"), h("div", { class: "uv-rel" }, g, relList));
  }

  async function openReplace(cast, m) {
    const body = h("div", { class: "stack" }, skeleton(3));
    let chosen = null;
    const act = { label: "Thay", kind: "primary", value: "ok", onClick: async () => {
      if (!chosen) { toast({ title: "Chọn một nhân vật", tone: "wait" }); return false; }
      try { await api.post(`/api/universe/stories/${cast.story_id}/replace`, { slot_id: m.slot_id, character_id: chosen }); return true; } catch (e) { toastError(e, "Chưa thay được"); return false; }
    } };
    const p = openDialog({ title: `Thay nhân vật vai ${L.roleLabel(m.role_code)}`, describe: "Chỉ làm được MỘT lần cho mỗi truyện, trước khi truyện đạt QA. AI vẫn tự chọn nếu bạn không thay.", content: body, actions: [{ label: "Huỷ", value: null }, act] });
    try {
      const { alternatives } = await api.get(`/api/universe/stories/${cast.story_id}/alternatives`, { query: { slot: m.slot_id } });
      clear(body);
      const usable = alternatives.filter((a) => a.character_id !== m.character_id);
      if (!usable.length) body.append(h("p", { class: "muted" }, "Kho chưa có nhân vật nào khác để thay."));
      for (const a of usable) {
        const r = h("input", { type: "radio", name: "alt", value: a.character_id, disabled: !!a.blocked || a.in_cast });
        r.addEventListener("change", () => { chosen = a.character_id; });
        body.append(h("label", { class: "sm-opt" + (r.disabled ? " disabled" : "") }, r,
          h("span", null, h("strong", null, a.display_name), " ", h("span", { class: "muted small" }, a.blocked ? a.blocked : a.in_cast ? "đã có trong dàn" : `độ hợp ${Math.round(a.score * 100)}%${a.ok ? "" : " (thấp)"}`))));
      }
    } catch (e) { clear(body); body.append(errorState(e)); }
    if ((await p) === "ok") { toast({ title: "Đã thay nhân vật", message: "Dàn nhân vật được lập lại; quan hệ được cập nhật theo.", tone: "done" }); loadList(); loadDetail(); }
  }

  loadList();
  return { destroy() { alive = false; } };
}
