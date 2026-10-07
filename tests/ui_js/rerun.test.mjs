// Logic thuần của "Chạy lại" (D-113). Chạy: node --test tests/ui_js/rerun.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "../../src/contentfactory/orchestrator/webui_static/js");
const load = () => import(pathToFileURL(path.join(root, "rerun_logic.js")).href);

const options = { blocked: null, stages: [
  { id: "story", label: "Truyện", eligible: true }, { id: "tts", label: "Giọng đọc", eligible: true },
  { id: "audio", label: "Audio", eligible: false }, { id: "publish", label: "Đăng YouTube", eligible: true }] };

test("selectableIds/orderedSelection: thứ tự lấy từ backend, bước không chọn được bị loại", async () => {
  const r = await load();
  assert.deepEqual(r.selectableIds(options), ["story", "tts", "publish"]);
  assert.deepEqual(r.orderedSelection(options, ["publish", "story", "story"]), ["story", "publish"]);
});

test("requestIds: cùng lựa chọn cùng id (chống bấm đúp), đổi lựa chọn đổi id", async () => {
  const r = await load();
  let n = 0;
  const rid = r.requestIds(() => `id${++n}`);
  assert.equal(rid(["a", "b"]), rid(["b", "a"]));
  assert.notEqual(rid(["a"]), rid(["a", "b"]));
  assert.deepEqual(r.payload(["story"], "x"), { stages: ["story"], request_id: "x" });
});

test("latest: chỉ kết quả của lần gọi mới nhất được dùng", async () => {
  const r = await load();
  const l = r.latest();
  const a = l.next(), b = l.next();
  assert.equal(l.isCurrent(a), false);
  assert.equal(l.isCurrent(b), true);
});

test("summary/problems/suggestion: dùng đúng dữ liệu backend", async () => {
  const r = await load();
  assert.equal(r.summary(null, options).title, "Chưa chọn bước nào.");
  const plan = { ok: false, order: ["story", "publish"], publishes: true, suggested: ["tts"],
    stages: [{ id: "story", problems: [] }, { id: "publish", problems: [{ message: "Video không đồng bộ", hard: false }] }] };
  assert.deepEqual(r.summary(plan, options), { title: "Sẽ chạy lại 2 bước:", lines: ["1. Truyện", "2. Đăng YouTube"] });
  assert.deepEqual(r.problemsById(plan), { publish: ["Video không đồng bộ"] });
  assert.equal(r.hasHard(plan), false);
  assert.equal(r.suggestionText(plan, options), "Thêm: Giọng đọc");
  assert.deepEqual(r.withSuggested(["story", "publish"], plan), ["story", "publish", "tts"]);
});

test("canSubmit: cần chọn, backend ok, không bị chặn, Publish phải xác nhận", async () => {
  const r = await load();
  const ok = { ok: true, publishes: false, order: ["story"] };
  assert.equal(r.canSubmit({ selected: [], plan: ok, options }), false);
  assert.equal(r.canSubmit({ selected: ["story"], plan: null, options }), false);
  assert.equal(r.canSubmit({ selected: ["story"], plan: { ...ok, ok: false }, options }), false);
  assert.equal(r.canSubmit({ selected: ["story"], plan: ok, options: { ...options, blocked: { code: "JOB_BUSY" } } }), false);
  assert.equal(r.canSubmit({ selected: ["story"], plan: ok, options }), true);
  const pub = { ok: true, publishes: true, order: ["publish"] };
  assert.equal(r.canSubmit({ selected: ["publish"], plan: pub, options }), false);
  assert.equal(r.canSubmit({ selected: ["publish"], plan: pub, options, confirmed: true }), true);
});

test("nhãn: số lần chạy lại, phiên đang chạy, phiên vừa xong", async () => {
  const r = await load();
  assert.equal(r.countLabel(0), "Chưa chạy lại lần nào");
  assert.equal(r.countLabel(3), "Đã chạy lại 3 lần");
  assert.equal(r.countBadge(0), "");
  assert.equal(r.countBadge(2), "Rerun ×2");
  const s = { number: 3, state: "running", stages: [{ label: "Truyện", state: "succeeded" }, { label: "Giọng đọc", state: "running", progress: { done: 3, total: 12 } }, { label: "Audio", state: "pending" }] };
  assert.equal(r.activeLine(s), "Đang chạy lại #3: Truyện ✓ → Giọng đọc 3/12 → Audio");
  assert.equal(r.activeLine(null), "");
  assert.deepEqual(r.finishedSession({ id: "R1", number: 1 }, null), { id: "R1", number: 1 });
  assert.equal(r.finishedSession(null, null), null);
  assert.equal(r.finishedSession({ id: "R1" }, { id: "R1" }), null);
});
