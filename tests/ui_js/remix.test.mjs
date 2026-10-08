// Logic thuần của thẻ Kế hoạch Story Remix. Chạy: node --test tests/ui_js/remix.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "../../src/contentfactory/orchestrator/webui_static/js");
const load = (f) => import(pathToFileURL(path.join(root, f)).href);

test("quyết định cổng originality không bao giờ nói 'an toàn bản quyền'", async () => {
  const R = await load("remix_logic.js");
  for (const d of ["pass", "pass_with_note", "review", "block"]) assert.doesNotMatch(R.decisionView(d).label, /an toàn bản quyền|đã xác minh/i);
  assert.equal(R.decisionView("block").tone, "fail");
  assert.equal(R.decisionView("review").tone, "wait");
  assert.equal(R.decisionView("???").label, "???");
});

test("chi phí: số biết + lượt không rõ, không ước đoán", async () => {
  const R = await load("remix_logic.js");
  assert.match(R.costLine({ known_cost_usd: 1.5, calls: 4, calls_with_unknown_cost: 0 }), /\$1\.50/);
  assert.match(R.costLine({ known_cost_usd: 0, calls: 4, calls_with_unknown_cost: 4 }), /4\/4 lượt không rõ chi phí/);
  assert.match(R.costLine({ known_cost_usd: 2, calls: 3, calls_with_unknown_cost: 1, budget_usd: 10, retries: 2 }), /ngân sách \$10\.00.*2 lần thử lại/);
});

test("danh sách ý tưởng: được chọn lên đầu, điểm hiển thị 2 chữ số", async () => {
  const R = await load("remix_logic.js");
  const rows = R.premiseRows([{ id: "P2", logline: "b", total: 0.5, selected: false, reason: "yếu" }, { id: "P1", logline: "a", total: 0.91234, selected: true }, { id: "P3", logline: "c", total: null }]);
  assert.deepEqual(rows.map((r) => r.id), ["P1", "P2", "P3"]);
  assert.equal(rows[0].score, "0.91");
  assert.equal(rows[2].score, "—");
  assert.equal(R.qualityIssues({ issues: [{ message: "x" }] })[0], "x");
});
