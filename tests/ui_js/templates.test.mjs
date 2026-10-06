// Logic thuần của Template Studio. Chạy: node --test tests/ui_js/templates.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "../../src/contentfactory/orchestrator/webui_static/js");
const L = await import(pathToFileURL(path.join(root, "templates_logic.js")).href);
const canvas = { width: 1000, height: 500 };

test("slug: bỏ dấu tiếng Việt, đúng regex id template", () => {
  assert.equal(L.slug("Truyện Audio – Vàng!"), "truyen_audio_vang");
  assert.equal(L.slug("Đường  Đời"), "duong_doi");
  assert.equal(L.slug("  ---  "), "");
  assert.equal(L.slug("x".repeat(80)).length, 48);
  assert.ok(L.validTemplateId(L.slug("Story Frame 2")));
  assert.ok(!L.validTemplateId("a"));
  assert.ok(!L.validTemplateId("Bad Id"));
  assert.equal(L.assetSlug("Gold Frame 01.PNG"), "gold_frame_01");
  assert.ok(L.validAssetId("gold-frame_1"));
  assert.equal(L.uniqueId("image", ["image", "image_2"]), "image_3");
  assert.equal(L.uniqueId("a", []), "a");
});

test("renumberZ / moveLayer: z luôn duy nhất, 10,20,30… theo thứ tự từ dưới lên", () => {
  const doc = { elements: [{ id: "a", z: 5 }, { id: "b", z: 5 }, { id: "c", z: 99 }] };     // trùng z (tài liệu hỏng) vẫn sửa được xác định
  assert.ok(L.moveLayer(doc, "a", +1));
  const order = [...doc.elements].sort((p, q) => p.z - q.z).map((e) => e.id);
  assert.deepEqual(order, ["b", "a", "c"]);
  assert.deepEqual(doc.elements.map((e) => e.z).sort((p, q) => p - q), [10, 20, 30]);
  assert.equal(L.moveLayer(doc, "c", +1), false);               // đã ở trên cùng
  assert.equal(L.moveLayer(doc, "b", -1), false);               // đã ở dưới cùng
  assert.ok(L.moveLayer(doc, "c", -1));
  assert.deepEqual(L.elementsTopDown(doc).map((e) => e.id), ["a", "c", "b"]);
  assert.equal(L.nextZ(doc), 40);
});

test("clampBox: text/photo nằm trong canvas, ảnh/video tràn nhưng còn phần nằm trong", () => {
  assert.deepEqual(L.clampBox({ x: -50, y: 480, width: 200, height: 100 }, canvas, "inside"), { x: 0, y: 400, width: 200, height: 100 });
  assert.deepEqual(L.clampBox({ x: 0, y: 0, width: 5000, height: 5000 }, canvas, "inside"), { x: 0, y: 0, width: 1000, height: 500 });
  const t = L.clampBox({ x: -5000, y: 9999, width: 300, height: 200 }, canvas, "touch");
  assert.equal(t.x, -(300 - L.TOUCH)); assert.equal(t.y, 500 - L.TOUCH);
  assert.deepEqual(L.clampBox({ x: 10.6, y: 3.2, width: 0, height: 2 }, canvas, "inside"), { x: 11, y: 3, width: L.MIN_SIZE, height: L.MIN_SIZE });
});

test("moveBox / nudge: số nguyên, Shift = 10px, phím lạ bị bỏ qua", () => {
  const b = { x: 100, y: 100, width: 200, height: 100 };
  assert.deepEqual(L.nudge(b, "ArrowRight", false, canvas, "inside"), { x: 101, y: 100, width: 200, height: 100 });
  assert.deepEqual(L.nudge(b, "ArrowUp", true, canvas, "inside"), { x: 100, y: 90, width: 200, height: 100 });
  assert.equal(L.nudge(b, "a", false, canvas, "inside"), null);
  assert.deepEqual(L.moveBox(b, 5000, 0, canvas, "inside"), { x: 800, y: 100, width: 200, height: 100 });
});

test("resizeBox: cạnh/góc, kích thước tối thiểu, giữ tỉ lệ, không vượt canvas", () => {
  const b = { x: 100, y: 100, width: 200, height: 100 };
  assert.deepEqual(L.resizeBox(b, "e", 50, 0, canvas, "inside"), { x: 100, y: 100, width: 250, height: 100 });
  assert.deepEqual(L.resizeBox(b, "w", 50, 0, canvas, "inside"), { x: 150, y: 100, width: 150, height: 100 });
  assert.deepEqual(L.resizeBox(b, "se", 30, 20, canvas, "inside"), { x: 100, y: 100, width: 230, height: 120 });
  const small = L.resizeBox(b, "e", -9999, 0, canvas, "inside");
  assert.equal(small.width, L.MIN_SIZE); assert.equal(small.x, 100);              // đầu kia đứng yên
  const north = L.resizeBox(b, "n", 0, 9999, canvas, "inside");
  assert.equal(north.height, L.MIN_SIZE); assert.equal(north.y + north.height, 200);
  const r = L.resizeBox(b, "se", 100, 0, canvas, "inside", { keepRatio: true });
  assert.equal(r.width / r.height, 2);
  assert.deepEqual(L.resizeBox(b, "se", 9999, 9999, canvas, "inside"), { x: 100, y: 100, width: 900, height: 400 });
});

test("viewScale: thu phóng chỉ là hệ số hiển thị", () => {
  assert.equal(L.viewScale("fit", 500, 1000), 0.5);
  assert.equal(L.viewScale("fit", 50000, 1000), 2);
  assert.equal(L.viewScale("1", 500, 1000), 1);
  assert.equal(L.viewScale("0.5", 500, 1000), 0.5);
  assert.equal(L.viewScale("lạ", 500, 1000), 1);
  const doc = { canvas, elements: [{ id: "a", x: 10, y: 10, width: 100, height: 50 }] };
  const before = L.canonStr(doc);
  L.viewScale("2", 500, 1000);
  assert.equal(L.canonStr(doc), before);                         // không đụng tài liệu
  assert.equal(L.toCanvas(100, 0.5), 200);
});

test("History: gom thao tác liên tiếp, Hoàn tác/Làm lại, giới hạn, sửa mới xoá phần Làm lại", () => {
  const h = new L.History(3, 700);
  h.reset("s0");
  assert.ok(h.record("s1", "drag", 1000));
  assert.ok(h.record("s2", "drag", 1100));                       // cùng khóa, trong 700ms: gộp thành 1 bước
  assert.equal(h.states.length, 2);
  assert.equal(h.undo(), "s0");                                  // một lần Hoàn tác về hẳn trước khi kéo
  assert.equal(h.undo(), null);
  assert.ok(h.canRedo());
  assert.equal(h.redo(), "s2");
  assert.ok(h.record("s3", "type", 2000));                       // khóa khác: bước mới
  assert.ok(h.record("s4", "type", 3000));                       // quá 700ms: bước mới
  assert.equal(h.undo(), "s3");
  assert.ok(h.record("s5"));                                     // sửa mới xoá redo
  assert.ok(!h.canRedo());
  assert.equal(h.record("s5"), false);                           // không đổi => không ghi
  for (let i = 0; i < 10; i++) h.record("x" + i);
  assert.equal(h.states.length, 4);                              // giới hạn limit+1
  let n = 0; while (h.undo()) n++;
  assert.equal(n, 3);
  assert.ok(!h.canUndo());
});

test("isDirty bỏ qua trường máy chủ đóng dấu; newElement hợp lệ và z cao nhất", () => {
  const doc = { schema: 1, id: "t", type: "thumbnail", canvas: { width: 1648, height: 928 }, elements: [{ id: "image", type: "image", z: 10, x: 0, y: 0, width: 1, height: 1 }], status: "draft" };
  const saved = { ...clone(doc), created_at: 5, scope: "user" };
  assert.ok(!L.isDirty(doc, saved));
  const d2 = clone(doc); d2.elements[0].x = 3;
  assert.ok(L.isDirty(d2, saved));
  const t = L.newElement("text", doc, { source: "channel.name" });
  assert.equal(t.id, "channel_name"); assert.equal(t.z, 20); assert.ok(t.x >= 0 && t.x + t.width <= 1648);
  assert.equal(L.newElement("image", doc, { assetId: "a" }).id, "image_2");
  assert.equal(L.newElement("source_video", { canvas: { width: 100, height: 50 }, elements: [] }).width, 100);
  assert.throws(() => L.newElement("hologram", doc));
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
});

test("parseIssuePath: nối lỗi validator với phần tử", () => {
  assert.deepEqual(L.parseIssuePath("elements[2] (project_title).width"), { index: 2, id: "project_title", field: "width" });
  assert.deepEqual(L.parseIssuePath("elements[0]"), { index: 0, id: null, field: null });
  assert.deepEqual(L.parseIssuePath("canvas.width"), { canvas: true, field: "width" });
  assert.deepEqual(L.parseIssuePath("elements"), { other: true });
});

test("LatestGate: chỉ yêu cầu mới nhất được dùng, kết quả cũ về muộn bị bỏ", () => {
  const g = new L.LatestGate();
  const a = g.next(), b = g.next();
  assert.equal(g.isLatest(a), false);
  assert.equal(g.isLatest(b), true);
  g.next();                                   // đổi version / rời trang cũng vô hiệu hoá yêu cầu đang bay
  assert.equal(g.isLatest(b), false);
});

test("nextSample: đi hết ảnh của một mẫu chữ rồi sang mẫu chữ kế, quay vòng", () => {
  const samples = [{ id: "s1" }, { id: "s2" }, { id: "s3" }], images = [{ id: "builtin" }, { id: "frame:p:0" }];
  let s = { id: "s1", image: "builtin", channel: "k" };
  s = L.nextSample(s, samples, images); assert.deepEqual([s.id, s.image, s.channel], ["s1", "frame:p:0", "k"]);
  s = L.nextSample(s, samples, images); assert.deepEqual([s.id, s.image], ["s2", "builtin"]);
  assert.deepEqual(L.nextSample({ id: "s3", image: "frame:p:0" }, samples, images), { id: "s1", image: "builtin" });
  assert.equal(L.nextSample({ id: "s3", image: "builtin" }, samples, []).id, "s1");                // template video: không có ảnh, chỉ xoay mẫu chữ
});
