// Kiểm tra giao diện Story Remix / Kho nhân vật bằng Chrome thật (cùng hạ tầng với qa.mjs): không lỗi console, axe, luồng chính, ảnh chụp.
//   node scripts/ui_qa/remix_qa.mjs <base-url> <fixture.json> [--shots out-dir] [--only run,universe,...]
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
let failures = 0;
const check = (name, ok, detail = "") => { if (!ok) failures++; console.log(`${ok ? "  ok  " : " FAIL "} ${name}${detail && !ok ? "  -> " + detail : ""}`); };
const wanted = (n) => !only || only.includes(n);
const browser = await chromium.launch({ executablePath: CHROME, headless: true });

async function newPage({ width = 1280, height = 900, scheme = "light" } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: scheme, locale: "vi-VN" });
  const page = await ctx.newPage();
  page.problems = [];
  page.on("console", (m) => { if (m.type() === "error") page.problems.push("console: " + m.text()); });
  page.on("pageerror", (e) => page.problems.push("pageerror: " + e.message));
  page.on("response", (r) => { if (r.status() >= 500) page.problems.push(`http ${r.status()}: ${r.url()}`); });
  return page;
}
const settle = async (page, ms = 500) => { await page.waitForLoadState("networkidle").catch(() => {}); await page.waitForTimeout(ms); };
const go = async (page, hash) => { await page.goto(base + "/#" + hash); await page.waitForSelector("#page-title", { timeout: 15000 }); await settle(page, 400); };
const shot = async (page, name) => { await page.waitForTimeout(500); if (shotsDir) await page.screenshot({ path: path.join(shotsDir, name + ".png"), fullPage: true }); };
async function axe(page, label) {
  await page.evaluate(axeSource);
  const r = await page.evaluate(async () => await window.axe.run(document, { runOnly: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"] }));
  const bad = r.violations.filter((v) => ["serious", "critical"].includes(v.impact));
  check(`a11y ${label}`, bad.length === 0, bad.map((v) => `${v.id}(${v.nodes.length}): ${v.nodes[0].target.join(" ")}`).join(" | "));
}
const noOverflow = async (page, label) => check(`không tràn ngang ${label}`, await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
const clean = (page, label) => check(`không lỗi console ${label}`, page.problems.length === 0, page.problems.join(" | "));

// ---------------------------------------------------------------------------------------------------- Phase 1: màn Chạy
if (wanted("run")) {
  for (const [label, opts] of [["desktop", {}], ["mobile", { width: 390, height: 844 }], ["dark", { scheme: "dark" }]]) {
    const page = await newPage(opts);
    await go(page, "/");
    await page.fill("#run-input", fx.youtube);
    await page.waitForSelector(".sm-editor:not([hidden])", { timeout: 15000 });
    await page.selectOption("select >> nth=0", "kenh_a").catch(() => {});
    const remix = page.locator('.sm-editor input[type=radio][value=story_remix]');
    const legacy = page.locator('.sm-editor input[type=radio][value=story_branch]');
    check(`run/${label}: Story hiện có được chọn sẵn`, await legacy.isChecked());
    check(`run/${label}: Story Remix bị khóa kèm lý do`, (await remix.isDisabled()) && /chưa khả dụng|đang được phát triển/i.test(await page.locator(".sm-editor").innerText()));
    await page.locator(".sm-editor button[aria-expanded]").first().click();
    await settle(page, 300);
    check(`run/${label}: xem được cấu hình mặc định (Kho nhân vật bật)`, /Tự chọn nhân vật/.test(await page.locator(".sm-editor").innerText()));
    check(`run/${label}: form chỉ-đọc khi Remix chưa khả dụng`, await page.locator(".sm-editor select, .sm-editor textarea").first().isDisabled());
    await shot(page, `run-${label}`);
    check(`run/${label}: khối Chế độ truyện nằm trong khung nhìn`, await page.evaluate(() => document.querySelector(".sm-editor").getBoundingClientRect().right <= window.innerWidth + 1));
    if (label !== "mobile") await axe(page, `run/${label}`);
    clean(page, `run/${label}`);
  }
  // RUN thật với Story hiện có: không gửi story_mode
  const page = await newPage();
  const sent = [];
  page.on("request", (r) => { if (r.method() === "POST" && r.url().endsWith("/api/runs")) sent.push(JSON.parse(r.postData() || "{}")); });
  await go(page, "/");
  await page.fill("#run-input", fx.story);
  await page.waitForSelector(".chip.ok:has-text('Truyện')");
  await page.locator(".mode:has-text('Chỉ đọc truyện')").click();
  const req = page.locator("input[placeholder^='Bắt buộc']");
  if (await req.count()) await req.fill("Truyện thử story mode");
  await page.waitForFunction(() => !document.querySelector("button.btn.primary.lg")?.disabled, null, { timeout: 10000 });
  await page.click("button:has-text('RUN')");
  await page.waitForURL(/#\/jobs\//, { timeout: 15000 });
  check("run: job Story hiện có được tạo và không gửi story_mode", sent.length === 1 && !("story_mode" in sent[0]));
  clean(page, "run/submit");
}

await browser.close();
console.log(failures ? `\n${failures} kiểm tra thất bại` : "\nTất cả đạt");
process.exit(failures ? 1 : 0);
