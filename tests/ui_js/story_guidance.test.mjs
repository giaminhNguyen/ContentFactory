// Logic thuần của "Đề xuất truyện" (D-112). Chạy: node --test tests/ui_js/story_guidance.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "../../src/contentfactory/orchestrator/webui_static/js");
const load = (f) => import(pathToFileURL(path.join(root, f)).href);

test("story_guidance_logic: kiểm độ dài, không cắt âm thầm", async () => {
  const g = await load("story_guidance_logic.js");
  assert.equal(g.MAX_LEN, 8000);
  assert.equal(g.validateGuidance("inherit", ""), null);
  assert.equal(g.validateGuidance("none", "x".repeat(99999)), null);
  assert.match(g.validateGuidance("custom", "  \r\n "), /Nhập nội dung/);
  assert.equal(g.validateGuidance("custom", "Kết thúc mở"), null);
  assert.equal(g.validateGuidance("custom", "x".repeat(8000)), null);
  assert.match(g.validateGuidance("custom", "x".repeat(8001)), /8001.*8000/);
  assert.equal(g.validateDefault(""), null);                                   // mặc định trong Cài đặt: rỗng hợp lệ
  assert.match(g.validateDefault("x".repeat(8001)), /tối đa 8000/);
});

test("story_guidance_logic: payload tạo job / sửa job", async () => {
  const g = await load("story_guidance_logic.js");
  assert.equal(g.createPayload("inherit", "bị bỏ"), null);                     // inherit = không gửi gì → backend dùng Cài đặt
  assert.equal(g.createPayload("none", "x"), null);
  assert.deepEqual(g.createPayload("custom", " Cha không được chết.\r\nCó twist. "), { mode: "custom", text: "Cha không được chết.\nCó twist." });
  assert.deepEqual(g.savePayload("inherit", "bị bỏ"), { mode: "inherit", text: "" });
  assert.deepEqual(g.savePayload("none", ""), { mode: "none", text: "" });
  assert.deepEqual(g.savePayload("custom", " a "), { mode: "custom", text: "a" });
});

test("story_guidance_logic: bộ đếm, nhãn nguồn, xem trước mặc định", async () => {
  const g = await load("story_guidance_logic.js");
  assert.deepEqual(g.counter("ab\r\ncd"), { n: 5, over: false, label: "5/8000" });
  assert.equal(g.counter("x".repeat(8001)).over, true);
  assert.equal(g.currentLabel("settings"), "Đang dùng đề xuất từ Cài đặt");
  assert.equal(g.currentLabel("job"), "Đang dùng đề xuất riêng của job");
  assert.equal(g.currentLabel("none"), "Không dùng đề xuất");
  assert.deepEqual(g.previewDefault("  "), { text: "", empty: true });
  assert.deepEqual(g.previewDefault("Mở"), { text: "Mở", empty: false });
});
