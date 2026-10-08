// Logic thuần của "Chế độ truyện". Chạy: node --test tests/ui_js/story_mode.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "../../src/contentfactory/orchestrator/webui_static/js");
const load = (f) => import(pathToFileURL(path.join(root, f)).href);

const info = {
  default_mode: "story_branch",
  modes: [{ id: "story_branch", label: "Story hiện có", available: true }, { id: "story_remix", label: "Story Remix", available: false }],
  defaults: { story: { tone: "", quality_repair_max_passes: 1, budget_usd: null, blocked_themes: [], ending: "auto", auto_select_premise: true },
              character_universe: { auto_cast: true, reuse_strategy: "reuse" } },
  schema: {
    story: [{ key: "tone", type: "text", default: "", rule: { max_len: 80 } }, { key: "quality_repair_max_passes", type: "int", default: 1, rule: { min: 0, max: 3 } },
            { key: "budget_usd", type: "number_or_null", default: null, rule: { min: 1, max: 5000 } }, { key: "blocked_themes", type: "list", default: [], rule: { max_items: 2, max_len: 5 } },
            { key: "ending", type: "select", default: "auto", rule: { options: ["auto", "happy"] } }, { key: "auto_select_premise", type: "bool", default: true, rule: {} }],
    character_universe: [{ key: "auto_cast", type: "bool", default: true, rule: {} }, { key: "reuse_strategy", type: "select", default: "reuse", rule: { options: ["reuse", "create_new"] } }],
  },
};

test("mặc định là Story hiện có và không gửi payload", async () => {
  const m = await load("story_mode_logic.js");
  const st = m.initialState(info);
  assert.equal(st.mode, m.LEGACY);
  assert.equal(m.createPayload(info, st), null);                       // job cũ y như trước
  st.story.tone = "x".repeat(500);                                      // cấu hình Remix bị bỏ qua khi ở mode cũ
  assert.deepEqual(m.validateAll(info, st), {});
});

test("Remix: payload đủ cấu hình, chuyển qua lại không mất giá trị", async () => {
  const m = await load("story_mode_logic.js");
  const st = m.initialState(info);
  st.story.tone = "u ám";
  st.mode = m.REMIX;
  const p = m.createPayload(info, st);
  assert.equal(p.mode, "story_remix");
  assert.equal(p.story.tone, "u ám");
  assert.equal(p.character_universe.auto_cast, true);
  p.story.tone = "đổi";                                                  // payload là bản sao
  assert.equal(st.story.tone, "u ám");
  st.mode = m.LEGACY;
  assert.equal(st.story.tone, "u ám");
});

test("validate khớp luật server, không ép kiểu", async () => {
  const m = await load("story_mode_logic.js");
  const st = m.initialState(info);
  st.mode = m.REMIX;
  assert.deepEqual(m.validateAll(info, st), {});
  st.story.quality_repair_max_passes = 9;
  st.story.budget_usd = 0;
  st.story.blocked_themes = ["a", "b", "c"];
  st.story.ending = "sad";
  st.story.auto_select_premise = "yes";
  st.character_universe.reuse_strategy = "x";
  st.story.tone = "x".repeat(81);
  assert.deepEqual(Object.keys(m.validateAll(info, st)).sort(),
    ["character_universe.reuse_strategy", "story.auto_select_premise", "story.blocked_themes", "story.budget_usd", "story.ending", "story.quality_repair_max_passes", "story.tone"]);
  assert.equal(m.validateField(info.schema.story[1], 2), null);
  assert.ok(m.validateField(info.schema.story[1], 1.5));                // không phải số nguyên
  assert.equal(m.validateField(info.schema.story[2], null), null);       // ngân sách trống hợp lệ
});

test("list <-> text và đếm mục đã tuỳ chỉnh", async () => {
  const m = await load("story_mode_logic.js");
  assert.deepEqual(m.listFromText(" a \r\n\nb\na"), ["a", "b"]);
  assert.equal(m.textFromList(["a", "b"]), "a\nb");
  const st = m.initialState(info);
  assert.equal(m.changedCount(info, st), 0);
  st.story.tone = "x";
  st.character_universe.auto_cast = false;
  assert.equal(m.changedCount(info, st), 2);
});

test("mẫu cấu hình: nạp vào form, khôi phục mặc định giữ chế độ, tên mẫu hợp lệ", async () => {
  const m = await load("story_mode_logic.js");
  const preset = { name: "P", story: { tone: "u ám", ending: "happy" }, character_universe: { auto_cast: false } };
  const st = m.stateFromPreset(info, preset);
  assert.equal(st.mode, m.REMIX);
  assert.equal(st.story.tone, "u ám");
  assert.equal(st.story.quality_repair_max_passes, 1);                 // phần thiếu lấy mặc định hệ thống
  assert.equal(st.character_universe.auto_cast, false);
  assert.equal(m.stateFromPreset(info, null).mode, "story_branch");
  assert.equal(m.presetByName({ presets: [preset] }, "P"), preset);
  const reset = m.resetToDefaults(info, st);
  assert.equal(reset.story.tone, "");
  assert.equal(reset.mode, m.REMIX);                                    // giữ chế độ đang chọn
  assert.equal(m.validPresetName("Trinh thám u ám"), true);
  for (const bad of ["", "a/b", "x".repeat(41), "-x"]) assert.equal(m.validPresetName(bad), false);
});

test("ước tính + định dạng giá trị hiệu lực, không bịa USD", async () => {
  const m = await load("story_mode_logic.js");
  const est = { calls: { min: 19, max: 35 }, input_tokens: { min: 122000, max: 200000 }, output_tokens: { min: 30000, max: 60000 }, chapters: 14, usd: null };
  const l = m.estimateLines(est, null);
  assert.match(l[0], /19–35 lượt.*14 chương/);
  assert.match(l[1], /không rõ/);
  assert.equal(m.estimateLines(est, 10).length, 3);                      // có ngân sách mà không có giá: nói rõ giới hạn của việc chặn
  const priced = { ...est, usd: { min: 0.8, max: 1.4 } };
  assert.equal(m.estimateLines(priced, 5).length, 2);
  assert.match(m.estimateLines(priced, 1)[2], /cao hơn ngân sách/);
  assert.equal(m.formatValue({ type: "bool" }, true), "Bật");
  assert.equal(m.formatValue({ type: "list" }, []), "—");
  assert.equal(m.formatValue({ type: "select", key: "ending" }, "happy"), "Có hậu");
  assert.equal(m.formatValue({ type: "number_or_null" }, null), "—");
  assert.equal(m.SOURCE_LABEL.preset, "từ mẫu");
});
