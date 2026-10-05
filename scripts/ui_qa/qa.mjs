// Kiểm tra giao diện bằng Chrome thật: không lỗi console, không tràn ngang, axe (a11y), luồng chính, bàn phím, reduced-motion, rò rỉ poller/listener, ảnh chụp.
//   node scripts/ui_qa/qa.mjs <base-url> <fixture.json> [--shots out-dir] [--only name,name]
import { chromium } from "playwright-core";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const axeSource = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const base = process.argv[2].replace(/\/$/, "");
const fx = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const shotsDir = process.argv.includes("--shots") ? process.argv[process.argv.indexOf("--shots") + 1] : null;
const only = process.argv.includes("--only") ? process.argv[process.argv.indexOf("--only") + 1].split(",") : null;
if (shotsDir) fs.mkdirSync(shotsDir, { recursive: true });
const CHROME = [process.env.CHROME_PATH, "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"].find((p) => p && fs.existsSync(p));

const results = [];
let failures = 0;
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  if (!ok) failures++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${name}${detail && !ok ? "  -> " + detail : ""}`);
}
const wanted = (n) => !only || only.includes(n);

const browser = await chromium.launch({ executablePath: CHROME, headless: true });

async function newPage({ width = 1280, height = 860, scheme = "light", reduced = "no-preference" } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: scheme, reducedMotion: reduced, locale: "vi-VN" });
  const page = await ctx.newPage();
  const problems = [];
  page.on("console", (m) => { if (m.type() === "error") problems.push("console: " + m.text()); });
  page.on("pageerror", (e) => problems.push("pageerror: " + e.message));
  page.on("requestfailed", (r) => { if (!r.failure()?.errorText.includes("ERR_ABORTED")) problems.push("requestfailed: " + r.url() + " " + r.failure()?.errorText); });
  page.on("response", (r) => { if (r.status() >= 500) problems.push(`http ${r.status()}: ${r.url()}`); });
  page.problems = problems;
  page.requests = [];
  page.on("request", (r) => { if (r.url().includes("/api/")) page.requests.push(r.url().replace(base, "")); });
  return page;
}
const settle = async (page, ms = 500) => { await page.waitForLoadState("networkidle").catch(() => {}); await page.waitForTimeout(ms); };
const go = async (page, hash) => { await page.goto(base + "/#" + hash); await page.waitForSelector("#page-title", { timeout: 15000 }); await settle(page, 400); };
const shot = async (page, name) => { await page.waitForTimeout(700); if (shotsDir) await page.screenshot({ path: path.join(shotsDir, name + ".png"), fullPage: true }); };

async function axe(page, label) {
  await page.evaluate(axeSource);
  const r = await page.evaluate(async () => await window.axe.run(document, { runOnly: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"] }));
  const bad = r.violations.filter((v) => ["serious", "critical"].includes(v.impact));
  check(`a11y ${label}`, bad.length === 0, bad.map((v) => `${v.id}(${v.nodes.length}): ${v.nodes[0].target.join(" ")}`).join(" | "));
  const minor = r.violations.filter((v) => !["serious", "critical"].includes(v.impact));
  if (minor.length) console.log(`        (nhẹ: ${minor.map((v) => v.id).join(", ")})`);
}

async function noOverflow(page, label) {
  const o = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth }));
  check(`không tràn ngang ${label}`, o.sw <= o.iw + 1, `scrollWidth=${o.sw} > ${o.iw}`);
}

const ROUTES = [["/", "run"], ["/jobs", "jobs"], ["/channels", "channels"], ["/tts", "tts"], ["/pools", "pools"], ["/settings", "settings"], ["/settings/storage", "settings-storage"]];

// ===================================================================== 1. mọi trang: sạch lỗi, a11y, tràn ngang, ảnh chụp ở nhiều cỡ + 2 theme
if (wanted("pages")) {
  console.log("\n# Trang & kích thước");
  for (const scheme of ["light", "dark"]) {
    for (const [w, h] of [[1440, 900], [1024, 768], [768, 1024], [390, 844]]) {
      const page = await newPage({ width: w, height: h, scheme });
      for (const [hash, name] of ROUTES) {
        await go(page, hash);
        if (name === "settings") await page.waitForSelector(".health-group", { timeout: 60000 }).catch(() => {});
        await noOverflow(page, `${name} ${w}x${h} ${scheme}`);
        if (w === 1440 || w === 390) await shot(page, `${name}_${w}_${scheme}`);
        if (w === 1440 || w === 390) await axe(page, `${name} ${w} ${scheme}`);
      }
      check(`không lỗi console/mạng (${w}x${h} ${scheme})`, page.problems.length === 0, page.problems.slice(0, 3).join(" | "));
      await page.context().close();
    }
  }
}

// ===================================================================== 2. luồng hằng ngày: dán link -> RUN -> theo dõi -> mở output
if (wanted("daily")) {
  console.log("\n# Luồng hằng ngày");
  const page = await newPage();
  await go(page, "/");
  check("nút RUN tắt khi chưa nhập", await page.locator("button:has-text('RUN')").isDisabled());
  await page.fill("#run-input", fx.youtube);
  await page.waitForSelector(".chip.ok:has-text('Link YouTube')");
  await page.waitForSelector(".mode");
  check("chỉ hiện chế độ hợp lệ cho link", (await page.locator(".mode").count()) === 4);
  check("hiện kế hoạch + tự chọn", await page.locator(".plan .step").count() === 8 && await page.locator(".autolist li").count() >= 1);
  await page.selectOption("select >> nth=0", "kenh_a").catch(() => {});
  const run = page.locator("button:has-text('RUN')");
  await page.waitForFunction(() => !document.querySelector("button.btn.primary.lg")?.disabled, null, { timeout: 10000 });
  await shot(page, "run_ready");
  const tokenNow = await page.locator("meta[name=cf-token]").getAttribute("content");
  const countJobs = async () => (await (await page.request.get(base + "/api/jobs?status=all&limit=1", { headers: { "X-CF-Token": tokenNow } })).json()).counts.all;
  const jobsBefore = await countJobs();
  await run.dblclick();                                    // bấm đúp: chỉ được tạo MỘT job
  await page.waitForURL(/#\/jobs\/\d+/, { timeout: 15000 });
  const jobId = page.url().match(/jobs\/(\d+)/)[1];
  await page.waitForSelector(".stage-row");
  check("chi tiết job hiện 8 bước", (await page.locator(".stage-row").count()) === 8);
  await page.waitForSelector("text=Job đã hoàn tất", { timeout: 60000 });
  await shot(page, "job_completed");
  check("job hoàn tất có nút mở output", await page.locator("button:has-text('Mở thư mục output')").first().isVisible());
  await page.locator("button:has-text('Mở thư mục output')").first().click();
  await page.waitForSelector(".toast:has-text('Đã mở thư mục output')");
  const opened = fs.readFileSync(path.join(fx.root, "opened.log"), "utf8").trim().split("\n");
  check("opener được gọi đúng một lần với thư mục output", opened.length >= 1 && opened.at(-1).includes("output"));
  const created = (await countJobs()) - jobsBefore;
  check("bấm đúp RUN không tạo job thứ hai", created === 1, `tạo ${created} job`);
  check("không lỗi console/mạng", page.problems.length === 0, page.problems.slice(0, 3).join(" | "));
  await page.context().close();
}

// ===================================================================== 3. chế độ một phần + xác thực + khai báo trẻ em
if (wanted("partial")) {
  console.log("\n# Chế độ một phần");
  const page = await newPage();
  await go(page, "/");
  await page.fill("#run-input", fx.story);
  await page.waitForSelector(".chip.ok:has-text('Truyện')");
  check("story.txt: hiện chế độ TTS/Video/Full còn lại", (await page.locator(".mode").count()) === 3);
  check("story.txt: yêu cầu tên truyện", await page.locator("#run-input >> xpath=ancestor::div[contains(@class,'run-card')]//input[@required]").count() >= 1);
  const rb = page.locator("button:has-text('RUN')");
  check("thiếu tên truyện thì RUN tắt", await rb.isDisabled());
  await page.locator(".mode:has-text('Chỉ đọc truyện')").click();
  await page.fill("input[placeholder^='Bắt buộc']", "Truyện thử TTS");
  await page.waitForFunction(() => !document.querySelector("button.btn.primary.lg")?.disabled, null, { timeout: 10000 });
  await rb.click();
  await page.waitForURL(/#\/jobs\/\d+/);
  await page.waitForSelector("text=Job đã hoàn tất", { timeout: 60000 });
  const states = await page.locator(".stage-row").evaluateAll((els) => els.map((e) => e.dataset.state));
  check("TTS-only: các bước khác 'Không chạy'", states.filter((s) => s === "not_planned").length >= 5, states.join());
  // file không phải audio/phụ đề
  await go(page, "/");
  await page.fill("#run-input", fx.root + "\\khong-co.txt");
  await page.waitForSelector("text=Không tìm thấy file");
  check("đường dẫn sai: nói rõ phải làm gì", true);
  // kênh chưa khai made_for_kids
  await page.fill("#run-input", fx.youtube);
  await page.waitForSelector(".mode");
  await page.selectOption("select >> nth=0", "chua_khai");
  await page.waitForSelector("text=có dành cho trẻ em không");
  check("kênh chưa khai: RUN tắt tới khi chọn", await page.locator("button:has-text('RUN')").isDisabled());
  await page.locator("input[name=kids][value=false]").check();
  await page.waitForFunction(() => !document.querySelector("button.btn.primary.lg")?.disabled, null, { timeout: 10000 });
  check("chọn xong thì RUN bật", true);
  check("không lỗi console/mạng", page.problems.length === 0, page.problems.slice(0, 3).join(" | "));
  await page.context().close();
}

// ===================================================================== 4. job bị giữ / lỗi / cần xử lý
if (wanted("paused")) {
  console.log("\n# Trạng thái giữ / lỗi");
  const page = await newPage();
  await go(page, "/jobs?status=attention");
  await page.waitForSelector(".job");
  const titles = await page.locator(".job .title").allTextContents();
  check("nhóm 'Cần xử lý' có job lỗi + cần đăng nhập", titles.some((t) => t.includes("lỗi giọng đọc")) && titles.some((t) => t.includes("cần đăng nhập")), titles.join(" | "));
  await shot(page, "jobs_attention");
  await go(page, "/jobs?status=waiting");
  await page.waitForSelector(".job");
  await page.locator(".job a:has-text('chờ mạng')").click();
  await page.waitForSelector(".alert[data-tone=wait]");
  check("job chờ mạng: giải thích + Auto Resume bật", (await page.locator(".alert").first().innerText()).includes("Đang chờ mạng") && await page.locator("input[role=switch]").first().isChecked());
  check("job chờ mạng: có Thử lại ngay + Tắt Auto Resume", await page.locator("button:has-text('Thử lại ngay')").isVisible() && await page.locator("button:has-text('Tắt Auto Resume')").isVisible());
  await shot(page, "job_waiting");
  await page.locator("button:has-text('Thử lại ngay')").click();
  await page.waitForSelector(".toast:has-text('Nguyên nhân vẫn còn')");
  check("Resume khi nguyên nhân còn: báo rõ, không đổi trạng thái", await page.locator(".alert[data-tone=wait]").count() === 1);
  await page.locator("button:has-text('Tắt Auto Resume')").click();
  await page.waitForSelector(".toast:has-text('Đã tắt Auto Resume')");
  await page.waitForSelector("button:has-text('Bật Auto Resume')", { timeout: 10000 });
  check("tắt Auto Resume cập nhật nút và công tắc", !(await page.locator("input[role=switch]").first().isChecked()));
  await page.locator("button:has-text('Bật Auto Resume')").click();                       // trả lại trạng thái ban đầu để QA chạy lại được
  await page.waitForSelector("button:has-text('Tắt Auto Resume')", { timeout: 10000 });
  await go(page, "/jobs?status=attention");
  await page.locator(".job a:has-text('lỗi giọng đọc')").click();
  await page.waitForSelector(".alert[data-tone=fail]");
  check("job lỗi: nói bước nào, có nút chạy lại đúng stage", await page.locator("button:has-text('Chạy lại stage lỗi')").isVisible() && (await page.locator(".alert").first().innerText()).includes("Giọng đọc"));
  check("job lỗi: không lộ stack trace", !(await page.locator("main").innerText()).includes("Traceback"));
  await shot(page, "job_failed");
  await go(page, "/jobs");
  const rows = await page.locator(".job .badge").allTextContents();
  check("danh sách có đủ nhóm trạng thái (chữ, không chỉ màu)", ["Hoàn tất", "Lỗi", "Đang chờ", "Cần bạn xử lý"].every((t) => rows.some((r) => r.includes(t))), rows.join(","));
  check("không lỗi console/mạng", page.problems.length === 0, page.problems.slice(0, 3).join(" | "));
  await page.context().close();
}

// ===================================================================== 5. kênh / TTS / cài đặt
if (wanted("config")) {
  console.log("\n# Kênh, TTS, cài đặt");
  const page = await newPage();
  await go(page, "/channels/kenh_a");
  const nameIn = page.getByLabel("Tên kênh", { exact: false }).first();
  await nameIn.waitFor();
  await page.waitForFunction(() => [...document.querySelectorAll("input")].some((i) => i.value.startsWith("Kênh Truyện A")));
  const newName = `Kênh Truyện A ${Date.now() % 10000}`;
  await nameIn.fill(newName);
  await page.waitForTimeout(300);
  const save = page.locator("button:has-text('Lưu thay đổi')").first();
  check("sửa kênh bật nút Lưu", await save.isEnabled());
  await save.click();
  await page.waitForSelector("text=Đã lưu", { timeout: 10000 });
  check("lưu kênh thành công", true);
  await page.reload();
  await page.waitForFunction((n) => [...document.querySelectorAll("input")].some((i) => i.value === n), newName, { timeout: 15000 });
  check("giá trị kênh được giữ sau khi tải lại", true);
  await shot(page, "channel_editor");
  await go(page, "/tts");
  await page.waitForSelector("text=giong_vi");
  check("TTS: cảnh báo giọng đọc giả + profile + Auto", (await page.locator("main").innerText()).includes("giả") && (await page.locator("main").innerText()).includes("giong_vi"));
  check("TTS: chỉ hiện tên biến credential, không giá trị", (await page.locator("main").innerText()).includes("QA_TTS_KEY"));
  await shot(page, "tts");
  await go(page, "/settings/general");
  await page.waitForSelector(".setting");
  const sw = page.locator(".setting:has-text('Auto Resume') input[role=switch]");
  const before = await sw.isChecked();
  const track = page.locator(".setting:has-text('Auto Resume') .track");
  await track.click();
  await page.waitForSelector(".setting:has-text('Auto Resume') .saved:has-text('Đã lưu')", { timeout: 10000 });
  check("cài đặt tự lưu và báo đã lưu", (await sw.isChecked()) !== before);
  await track.click();
  await go(page, "/settings");
  await page.waitForSelector(".health-group", { timeout: 60000 });
  const groups = await page.locator(".health-group > summary").allInnerTexts();
  check("Doctor: nhóm theo hạng mục, có chữ trạng thái", groups.length >= 8 && groups.every((g) => /Ổn|Cần xem|Cần xử lý|Chưa dùng/.test(g)), groups.slice(0, 3).join(" | "));
  await shot(page, "doctor");
  check("không lỗi console/mạng", page.problems.length === 0, page.problems.slice(0, 3).join(" | "));
  await page.context().close();
}

// ===================================================================== 6. bàn phím + focus + dialog
if (wanted("keyboard")) {
  console.log("\n# Bàn phím");
  const page = await newPage();
  await go(page, "/jobs");
  await page.keyboard.press("Tab");
  check("Tab đầu tiên là liên kết 'Bỏ qua điều hướng'", (await page.evaluate(() => document.activeElement?.className)).includes("skip-link"));
  await page.keyboard.press("Enter");
  check("Skip link chuyển focus vào nội dung", await page.evaluate(() => document.activeElement?.id === "view" || !!document.activeElement?.closest("#view")));
  await go(page, "/");
  check("trang Chạy tự đặt focus vào ô nhập", await page.evaluate(() => document.activeElement?.id === "run-input"));
  const focusStyle = await page.evaluate(() => { const el = document.querySelector("#run-input"); el.focus(); const s = getComputedStyle(el); return s.outlineStyle !== "none" || s.boxShadow !== "none"; });
  check("ô nhập có focus nhìn thấy được", focusStyle);
  // dialog tạo kênh: Esc đóng, focus trả về nút gọi
  await page.locator("button:has-text('Kênh mới')").click();
  await page.waitForSelector("dialog[open]");
  check("dialog mở và focus nằm trong dialog", await page.evaluate(() => !!document.activeElement?.closest("dialog")));
  await page.keyboard.press("Escape");
  await page.waitForSelector("dialog[open]", { state: "detached", timeout: 3000 });
  check("Esc đóng dialog, focus về nút 'Kênh mới'", await page.evaluate(() => document.activeElement?.textContent?.includes("Kênh mới")));
  // chuyển trang: tiêu đề + thông báo cho trình đọc màn hình
  await page.locator("nav a:has-text('Job')").click();
  await page.waitForSelector("h1:has-text('Job')");
  check("đổi trang cập nhật document.title và vùng thông báo", (await page.title()).startsWith("Job") && (await page.locator("#route-live").innerText()).includes("Job"));
  await page.context().close();
}

// ===================================================================== 7. reduced motion
if (wanted("motion")) {
  console.log("\n# Chuyển động");
  const calm = await newPage({ reduced: "reduce" });
  await go(calm, "/");
  const active = await calm.evaluate(() => window.gsap.globalTimeline.getChildren(true, true, false).length);
  check("reduced-motion: không tween GSAP nào chạy", active === 0, String(active));
  await go(calm, "/jobs");
  await calm.waitForSelector(".job");
  const tr = await calm.evaluate(() => [...document.querySelectorAll(".job")].every((e) => getComputedStyle(e).transform === "none" && getComputedStyle(e).opacity === "1"));
  check("reduced-motion: phần tử ở trạng thái cuối ngay (không ẩn/lệch)", tr);
  await calm.context().close();
  const page = await newPage();
  await go(page, "/jobs");
  await page.waitForSelector(".job");
  await page.waitForTimeout(800);
  const left = await page.evaluate(() => [...document.querySelectorAll(".job, .card")].filter((e) => e.style.opacity && e.style.opacity !== "1").length);
  check("sau hoạt họa không còn style opacity/transform treo", left === 0, String(left));
  const tweensAfterLeave = await (async () => { await go(page, "/settings/general"); await page.waitForTimeout(800); return page.evaluate(() => window.gsap.globalTimeline.getChildren(true, true, false).length); })();
  check("rời view: không còn tween mồ côi", tweensAfterLeave === 0, String(tweensAfterLeave));
  await page.context().close();
}

// ===================================================================== 8. polling + rò rỉ
if (wanted("perf")) {
  console.log("\n# Polling & rò rỉ");
  const page = await newPage();
  await go(page, "/settings/general");
  await page.waitForTimeout(500);
  page.requests.length = 0;
  await page.waitForTimeout(6000);
  const leaked = page.requests.filter((u) => u.startsWith("/api/jobs?") && u.includes("limit=30")).length;
  check("rời trang Job: không còn poller danh sách job", leaked === 0, String(leaked));
  const idleCalls = page.requests.length;
  check("trang tĩnh: yêu cầu nền ít (≤ 6 trong 6s)", idleCalls <= 6, `${idleCalls}: ${page.requests.join(",")}`);
  await go(page, "/jobs");
  await page.waitForSelector(".job");
  page.requests.length = 0;
  await page.evaluate(() => { Object.defineProperty(document, "hidden", { configurable: true, get: () => true }); document.dispatchEvent(new Event("visibilitychange")); });
  await page.waitForTimeout(500);
  page.requests.length = 0;
  await page.waitForTimeout(5000);
  check("tab ẩn: không gọi API", page.requests.length === 0, page.requests.join(","));
  await page.evaluate(() => { Object.defineProperty(document, "hidden", { configurable: true, get: () => false }); document.dispatchEvent(new Event("visibilitychange")); });
  await page.waitForTimeout(800);
  check("tab hiện lại: poll lại ngay", page.requests.length > 0);
  // rò rỉ listener/node qua nhiều lần chuyển trang
  const cdp = await page.context().newCDPSession(page);
  const counters = async () => { await cdp.send("HeapProfiler.collectGarbage"); return (await cdp.send("Memory.getDOMCounters")); };
  await go(page, "/");
  const a = await counters();
  for (let i = 0; i < 12; i++) for (const h of ["/jobs", "/channels", "/tts", "/settings/general", "/"]) { await page.evaluate((x) => { location.hash = x; }, h); await page.waitForTimeout(120); }
  await settle(page, 600);
  const b = await counters();
  check("12 vòng chuyển trang: DOM nodes không phình", b.nodes - a.nodes < 400, `${a.nodes} -> ${b.nodes}`);
  check("12 vòng chuyển trang: listener không phình", b.jsEventListeners - a.jsEventListeners < 120, `${a.jsEventListeners} -> ${b.jsEventListeners}`);
  check("không lỗi console/mạng", page.problems.length === 0, page.problems.slice(0, 3).join(" | "));
  await page.context().close();
}

// ===================================================================== 9. quy mô + số liệu tải (dùng fixture --many)
if (wanted("scale")) {
  console.log("\n# Quy mô & số liệu");
  const page = await newPage({ width: 1280, height: 860 });
  await page.addInitScript(() => {
    window.__long = []; try { new PerformanceObserver((l) => l.getEntries().forEach((e) => window.__long.push(e.duration))).observe({ entryTypes: ["longtask"] }); } catch { /* không hỗ trợ */ }
  });
  const sizes = new Map();
  page.on("response", async (r) => { const u = r.url(); if (u.startsWith(base) && !u.includes("/api/")) { try { sizes.set(u, (await r.body()).length); } catch { /* bỏ qua */ } } });
  await page.goto(base + "/#/");
  await page.waitForSelector("#page-title");
  await page.waitForLoadState("networkidle");
  const nav = await page.evaluate(() => { const n = performance.getEntriesByType("navigation")[0]; const fcp = performance.getEntriesByName("first-contentful-paint")[0]; return { dcl: Math.round(n.domContentLoadedEventEnd), load: Math.round(n.loadEventEnd), fcp: Math.round(fcp?.startTime || 0) }; });
  const total = [...sizes.values()].reduce((a, b) => a + b, 0);
  const js = [...sizes.entries()].filter(([u]) => u.endsWith(".js")).reduce((a, [, b]) => a + b, 0);
  console.log(`        man hinh chinh: DCL ${nav.dcl}ms, load ${nav.load}ms, FCP ${nav.fcp}ms, ${sizes.size} file tinh, ${(total / 1024).toFixed(0)} KB (JS ${(js / 1024).toFixed(0)} KB, gom GSAP ~72 KB)`);
  check("màn hình chính nạp < 2s và < 250 KB tài nguyên tĩnh", nav.load < 2000 && total < 250 * 1024, `${nav.load}ms ${(total / 1024).toFixed(0)}KB`);
  check("màn hình chính chỉ nạp module cần thiết (view phụ nạp lười)", ![...sizes.keys()].some((u) => /views\/(channels|tts|pools|settings)\.js/.test(u)), [...sizes.keys()].filter((u) => /views\//.test(u)).join(","));
  await page.goto(base + "/#/jobs");
  await page.waitForSelector(".job");
  await page.waitForTimeout(800);
  const m1 = await page.evaluate(() => ({ rows: document.querySelectorAll(".job").length, nodes: document.querySelectorAll("*").length }));
  console.log(`        /jobs: ${m1.rows} dong, ${m1.nodes} node DOM`);
  check("danh sách lớn chỉ dựng trang đầu (<= 30 dòng)", m1.rows <= 30 && m1.rows > 0, String(m1.rows));
  await page.locator("button:has-text('Tải thêm')").click();
  await page.waitForFunction((n) => document.querySelectorAll(".job").length > n, m1.rows);
  const m2 = await page.evaluate(() => document.querySelectorAll(".job").length);
  check("Tải thêm nạp thêm đúng một trang", m2 > m1.rows && m2 <= 60, `${m1.rows} -> ${m2}`);
  page.requests.length = 0;
  await page.waitForTimeout(6000);
  const polls = page.requests.filter((u) => u.startsWith("/api/jobs?")).length;
  check("poll danh sách lớn dùng `since` (ít yêu cầu)", polls <= 8, String(polls));
  const long = await page.evaluate(() => window.__long);
  const maxLong = long.length ? Math.max(...long) : 0;
  console.log(`        long task: ${long.length} lan, dai nhat ${Math.round(maxLong)}ms`);
  check("không tác vụ chính > 200ms khi nạp/cuộn danh sách", maxLong < 200, String(maxLong));
  const id = await page.locator(".job a").first().getAttribute("href");
  await page.goto(base + "/" + id);
  await page.waitForSelector(".stage-row");
  await page.locator("button:has-text('Chi tiết kỹ thuật')").click();
  await page.waitForSelector(".log div");
  const logRows = await page.locator(".log div").count();
  check("log job bị chặn trần số dòng trong DOM", logRows <= 130, String(logRows));
  check("không lỗi console/mạng", page.problems.length === 0, page.problems.slice(0, 3).join(" | "));
  await page.context().close();
}

// ===================================================================== 10. ỨNG DỤNG THẬT: dữ liệu mẫu -> RUN -> ffmpeg + ContentFlow thật -> output (cần real_root.py)
if (wanted("real")) {
  console.log("\n# Ứng dụng thật (ffmpeg + ContentFlow thật)");
  const { execFileSync } = await import("node:child_process");
  const probe = (f) => JSON.parse(execFileSync("ffprobe", ["-v", "error", "-print_format", "json", "-show_streams", "-show_format", f], { encoding: "utf8" }));
  const page = await newPage();
  await go(page, "/");
  await page.locator("button:has-text('Chưa có truyện hoặc video để thử?')").click();
  await page.locator("button:has-text('Tạo dữ liệu mẫu')").click();
  await page.waitForSelector(".toast:has-text('Đã tạo dữ liệu mẫu')", { timeout: 120000 });
  check("tạo dữ liệu mẫu từ giao diện", fs.existsSync(path.join(fx.root, "samples", "truyen_mau.txt")) && fs.readdirSync(path.join(fx.root, "samples", "video_ngang")).length >= 1);
  await page.locator("button:has-text('Dùng truyện mẫu')").click();
  await page.waitForSelector(".chip.ok:has-text('Truyện')");
  await page.locator(".mode:has-text('Đọc + dựng video + đóng gói + đăng')").click();
  await page.fill("input[placeholder^='Bắt buộc']", "Ngôi nhà cuối ngõ");
  await page.waitForFunction(() => !document.querySelector("button.btn.primary.lg")?.disabled, null, { timeout: 15000 });
  await shot(page, "real_run_ready");
  await page.locator("button:has-text('RUN')").click();
  await page.waitForURL(/#\/jobs\/\d+/);
  await page.waitForSelector("text=Job đã hoàn tất", { timeout: 300000 });
  await shot(page, "real_job_done");
  const jobId = page.url().match(/jobs\/(\d+)/)[1];
  const tok = await page.locator("meta[name=cf-token]").getAttribute("content");
  const d = await (await page.request.get(`${base}/api/jobs/${jobId}`, { headers: { "X-CF-Token": tok } })).json();
  const dir = d.output.project_dir;
  const yv = probe(path.join(dir, "youtube", "video.mp4")).streams.find((s) => s.codec_type === "video");
  check("YouTube thật: 1920x1080, h264", yv.width === 1920 && yv.height === 1080 && yv.codec_name === "h264", `${yv.width}x${yv.height}`);
  const parts = fs.readdirSync(path.join(dir, "tiktok")).filter((f) => f.endsWith(".mp4")).sort();
  const pv = probe(path.join(dir, "tiktok", parts[0])).streams.find((s) => s.codec_type === "video");
  check("TikTok thật: 1080x1920", pv.width === 1080 && pv.height === 1920 && parts.length >= 1, `${pv.width}x${pv.height} x${parts.length}`);
  check("thumbnail thật có trong gói", fs.statSync(path.join(dir, "youtube", "thumbnail.jpg")).size > 5000);
  check("tiêu đề YouTube đúng mẫu kênh", d.output.youtube_title === "[Full Audio 1] | Ngôi nhà cuối ngõ", d.output.youtube_title);
  check("không lỗi console/mạng", page.problems.length === 0, page.problems.slice(0, 3).join(" | "));
  // phần video nền: trang pool hiện đã đồng bộ
  await go(page, "/pools");
  await page.waitForSelector("text=gameplay");
  await page.waitForFunction(() => document.body.innerText.includes("Sẵn sàng"), null, { timeout: 60000 });
  check("trang Video nguồn: pool mẫu ở trạng thái Sẵn sàng", true);
  await shot(page, "real_pools");
  await page.context().close();
}

await browser.close();
console.log(`\n${results.length - failures}/${results.length} đạt`);
fs.writeFileSync(path.join(shotsDir || ".", "qa-results.json"), JSON.stringify(results, null, 1));
process.exit(failures ? 1 : 0);
