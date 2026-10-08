// Logic thuần của Kho nhân vật. Chạy: node --test tests/ui_js/universe.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "../../src/contentfactory/orchestrator/webui_static/js");
const load = (f) => import(pathToFileURL(path.join(root, f)).href);
const fields = [{ key: "display_name", type: "name", limit: 80, core: false }, { key: "core_personality", type: "text", limit: 20, core: true },
                { key: "motivations", type: "list", limit: [2, 5], core: true }];

test("avatar giữ chỗ và danh sách <-> văn bản", async () => {
  const L = await load("universe_logic.js");
  assert.equal(L.initials("Lan Phương"), "LP");
  assert.equal(L.initials("Hùng"), "HÙ");
  assert.equal(L.initials("  "), "?");
  assert.deepEqual(L.listFromText(" a \r\n\nb  c\na"), ["a", "b c"]);
});

test("validate không cắt âm thầm; tên bắt buộc", async () => {
  const L = await load("universe_logic.js");
  assert.deepEqual(L.validate(fields, { display_name: "A", core_personality: "ok", motivations: ["a"] }), {});
  const e = L.validate(fields, { display_name: "", core_personality: "x".repeat(21), motivations: ["a", "b", "c"] });
  assert.deepEqual(Object.keys(e).sort(), ["core_personality", "display_name", "motivations"]);
  assert.match(L.validate(fields, { display_name: "A", core_personality: "", motivations: ["toolong"] }).motivations, /tối đa 5/);
});

test("changedKeys + editability theo trạng thái/khóa", async () => {
  const L = await load("universe_logic.js");
  const ch = { display_name: "A", core_personality: "p", motivations: ["m"], locked: false, status: "active" };
  const prof = L.profileFromForm(fields, L.formFromProfile(fields, ch));
  assert.deepEqual(L.changedKeys(fields, ch, prof), []);
  assert.deepEqual(L.changedKeys(fields, ch, { ...prof, motivations: ["m", "n"] }), ["motivations"]);
  assert.equal(L.editability(fields, ch).size, 0);
  assert.deepEqual([...L.editability(fields, { ...ch, locked: true })].sort(), ["core_personality", "motivations"]);   // khóa: chỉ chặn phần cốt lõi
  assert.equal(L.editability(fields, { ...ch, status: "archived" }).size, 3);
});

test("nhập Excel: tiêu đề + điều kiện áp dụng", async () => {
  const L = await load("universe_logic.js");
  const rep = (c) => ({ counts: { create: 0, update: 0, unchanged: 0, conflict: 0, error: 0, ...c } });
  assert.equal(L.importHeadline(rep({ error: 1, update: 2 })).tone, "fail");
  assert.equal(L.importHeadline(rep({ conflict: 1, update: 1 })).tone, "wait");
  assert.match(L.importHeadline(rep({})).text, /Không có gì/);
  assert.equal(L.canApply(rep({ error: 1, update: 1 }), true), false);
  assert.equal(L.canApply(rep({ conflict: 1, update: 1 }), false), false);
  assert.equal(L.canApply(rep({ conflict: 1, update: 1 }), true), true);
  assert.equal(L.canApply(rep({ unchanged: 3 }), true), false);
});

test("thống kê dùng lại / mới", async () => {
  const L = await load("universe_logic.js");
  assert.match(L.reuseSummary({ reused_appearances: 0, new_character_appearances: 0 }), /Chưa có/);
  assert.match(L.reuseSummary({ reused_appearances: 3, new_character_appearances: 2 }), /3 dùng lại \/ 2 nhân vật mới/);
});

test("sơ đồ quan hệ: node theo vòng tròn, không có cạnh mồ côi", async () => {
  const L = await load("universe_logic.js");
  const members = [{ character_id: "a" }, { character_id: "b" }, { character_id: "c" }];
  const g = L.graphLayout(members, [{ a_id: "a", b_id: "b", type: "enemy_of" }, { a_id: "a", b_id: "zzz", type: "x" }], 320);
  assert.equal(g.nodes.length, 3);
  assert.equal(g.edges.length, 1);                                    // cạnh tới node không có trong dàn bị bỏ
  assert.ok(g.nodes.every((n) => n.x >= 0 && n.x <= 320 && n.y >= 0 && n.y <= 320));
  assert.equal(L.graphLayout([{ character_id: "a" }], [], 320).nodes[0].x, 160);
  assert.equal(L.roleLabel("protagonist"), "Nhân vật chính");
  assert.equal(L.roleLabel("zzz"), "zzz");
  assert.equal(L.fitText({ fit: null }), "Nhân vật mới");
  assert.equal(L.fitText({ fit: 0.456 }), "Hợp 46%");
  assert.match(L.relText({ a_id: "a", b_id: "b", type: "enemy_of" }, (i) => i.toUpperCase()), /A — enemy of — B/);
});

test("nhật ký cập nhật kho: tóm tắt dễ hiểu và chỉ hoàn tác bản đang áp dụng", async () => {
  const L = await load("universe_logic.js");
  const c = { created: [{ display_name: "Kiều An" }, { display_name: "Võ Quyết" }], reused: 1, merged: 0, appearances: 3, relationships: 2, status: "applied" };
  assert.match(L.changeSummary(c), /thêm 2 nhân vật mới \(Kiều An, Võ Quyết\).*dùng lại 1.*3 lần xuất hiện.*2 quan hệ/);
  assert.match(L.changeSummary({ ...c, created: [], merged: 2 }), /không thêm nhân vật mới.*gộp 2/);
  assert.equal(L.canRevert(c), true);
  assert.equal(L.canRevert({ ...c, status: "reverted" }), false);
});
