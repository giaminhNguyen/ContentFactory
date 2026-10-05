// Test thuần của frontend (không DOM): định dạng, ánh xạ trạng thái, poller. Chạy: node --test tests/ui_js/logic.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "../../src/contentfactory/orchestrator/webui_static/js");
const load = (f) => import(pathToFileURL(path.join(root, f)).href);

test("format: relTime, bytes, duration, pct, shortPath", async () => {
  const f = await load("format.js");
  const now = 1_000_000;
  assert.equal(f.relTime(null, now), "—");
  assert.equal(f.relTime(now - 3, now), "vừa xong");
  assert.equal(f.relTime(now - 45, now), "45 giây trước");
  assert.equal(f.relTime(now - 600, now), "10 phút trước");
  assert.equal(f.relTime(now - 7200, now), "2 giờ trước");
  assert.equal(f.relTime(now - 86400 * 3, now), "3 ngày trước");
  assert.equal(f.relTime(now + 50, now), "vừa xong");                      // đồng hồ lệch: không ra số âm
  assert.equal(f.bytes(null), "—");
  assert.equal(f.bytes(512), "512 B");
  assert.equal(f.bytes(1536), "1.5 KB");
  assert.equal(f.bytes(5 * 1024 ** 3), "5.0 GB");
  assert.equal(f.duration(42), "42 giây");
  assert.equal(f.duration(125), "2 phút 05 giây");
  assert.equal(f.duration(3 * 3600 + 120), "3 giờ 2 phút");
  assert.equal(f.pct(0.456), "46%");
  assert.equal(f.pct(undefined), "0%");
  const long = "C:/Users/abc/Videos/" + "x".repeat(80) + "/clip.mp4";
  assert.ok(f.shortPath(long, 40).length <= 40 && f.shortPath(long, 40).endsWith("clip.mp4"));
  assert.equal(f.shortPath("ngan"), "ngan");
});

test("status: mọi nhóm trạng thái đều có nhãn + biểu tượng + tone (không chỉ màu)", async () => {
  const s = await load("status.js");
  for (const k of ["running", "queued", "waiting", "attention", "failed", "completed"]) {
    const m = s.jobStatus(k);
    assert.ok(m.label && m.icon && m.tone, k);
  }
  for (const k of ["done", "reused", "provided", "running", "waiting", "held", "failed", "not_planned"]) {
    const m = s.stageState(k);
    assert.ok(m.label && m.icon && m.tone, k);
  }
  assert.equal(s.jobStatus("khong-biet").label, s.JOB_STATUS.queued.label);          // trạng thái lạ rơi về mặc định, không sập
  assert.equal(s.stageState("khong-biet").label, s.STAGE_STATE.waiting.label);
  assert.deepEqual(s.FILTERS.map(([k]) => k), ["all", "running", "waiting", "attention", "completed"]);
  assert.ok(s.isActive("running") && s.isActive("queued") && !s.isActive("completed") && !s.isActive("waiting"));
  for (const a of ["resume", "resume_now", "retry", "enable_auto_resume", "disable_auto_resume"]) assert.ok(s.ACTION_LABEL[a], a);
});

// ---- poller: dựng document giả + timer giả ----
function fakeEnv() {
  const listeners = new Map();
  globalThis.document = {
    hidden: false,
    addEventListener: (t, fn) => { listeners.set(t, [...(listeners.get(t) || []), fn]); },
    removeEventListener: (t, fn) => { listeners.set(t, (listeners.get(t) || []).filter((x) => x !== fn)); },
  };
  return { listeners, fire: (t) => (listeners.get(t) || []).forEach((fn) => fn()) };
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test("poller: không chồng yêu cầu, nhịp fast/idle, stop dọn sạch listener", async () => {
  const env = fakeEnv();
  const { createPoller } = await load("poller.js");
  let calls = 0, concurrent = 0, maxConcurrent = 0;
  const p = createPoller(async () => { calls++; concurrent++; maxConcurrent = Math.max(maxConcurrent, concurrent); await sleep(30); concurrent--; return "fast"; }, { fast: 20, idle: 500 });
  p.start();
  await sleep(300);
  assert.ok(calls >= 4, `fast phải poll dày (${calls})`);
  assert.equal(maxConcurrent, 1, "không bao giờ chồng yêu cầu");
  assert.equal((env.listeners.get("visibilitychange") || []).length, 1);
  p.stop();
  const after = calls;
  await sleep(150);
  assert.equal(calls, after, "stop rồi không poll nữa");
  assert.equal((env.listeners.get("visibilitychange") || []).length, 0, "stop gỡ listener");
  p.stop();                                                                  // gọi lại an toàn
});

test("poller: tab ẩn không gọi API; hiện lại thì poll ngay", async () => {
  const env = fakeEnv();
  const { createPoller } = await load("poller.js");
  document.hidden = true;
  let calls = 0;
  const p = createPoller(async () => { calls++; return "idle"; }, { fast: 10, idle: 20 });
  p.start();
  await sleep(120);
  assert.equal(calls, 0, "ẩn: không gọi");
  document.hidden = false;
  env.fire("visibilitychange");
  await sleep(60);
  assert.ok(calls >= 1, "hiện lại: gọi ngay");
  p.stop();
});

test("poller: lỗi => lùi dần (backoff), lỗi mạng đổi trạng thái online, hết lỗi thì hồi phục", async () => {
  fakeEnv();
  const { createPoller, net, isOnline } = await load("poller.js");
  let calls = 0, fail = true;
  const events = [];
  net.addEventListener("change", () => events.push(isOnline()));
  const p = createPoller(async () => { calls++; if (fail) { const e = new Error("net"); e.network = true; throw e; } return "idle"; }, { fast: 10, idle: 15, max: 400 });
  p.start();
  await sleep(250);
  assert.ok(calls <= 5, `lỗi liên tiếp phải lùi dần, không dồn dập (${calls})`);
  assert.deepEqual(events, [false]);
  fail = false;
  p.poke();
  await sleep(60);
  assert.deepEqual(events, [false, true]);
  p.stop();
});

test("poller: huỷ yêu cầu đang bay khi stop (AbortError không bị coi là lỗi)", async () => {
  fakeEnv();
  const { createPoller } = await load("poller.js");
  let aborted = false;
  const p = createPoller((signal) => new Promise((res, rej) => { signal.addEventListener("abort", () => { aborted = true; const e = new Error("abort"); e.name = "AbortError"; rej(e); }); }), { fast: 10, idle: 10 });
  p.start();
  await sleep(30);
  p.stop();
  await sleep(30);
  assert.ok(aborted);
});
