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

async function newPage({ width = 1280, height = 900, scheme = "light", acceptDownloads = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: scheme, locale: "vi-VN", acceptDownloads });
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
const noOverflowEl = async (page, sel, label) => check(`khối ${sel} nằm trong khung nhìn ${label}`, await page.evaluate((s) => document.querySelector(s).getBoundingClientRect().right <= window.innerWidth + 1, sel));
const clean = (page, label) => { const real = page.problems.filter((p) => !/status of 400/.test(p)); check(`không lỗi console ${label}`, real.length === 0, real.join(" | ")); };   // 400 do cố ý thử tên trùng/xung đột

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

// ---------------------------------------------------------------------------------------------------- Phase 2: Kho nhân vật
if (wanted("universe")) {
  const names = ["Lan Phương", "Hùng Sói"];
  for (const [label, opts] of [["desktop", {}], ["mobile", { width: 390, height: 844 }], ["dark", { scheme: "dark" }]]) {
    const page = await newPage({ ...opts, acceptDownloads: true });
    await go(page, "/universe");
    if (label === "desktop") {
      check("universe: trạng thái trống giải thích rõ (kho không có nhân vật cài sẵn)", /Kho nhân vật đang trống/.test(await page.locator("#view").innerText()));
      await shot(page, "universe-empty");
      await page.click("button:has-text('Thêm nhân vật') >> nth=0");
      await page.fill("dialog input >> nth=0", names[0]);
      await page.fill("dialog textarea >> nth=0", "Điềm tĩnh, quan sát tinh tế, hay nghi ngờ lời hứa.");
      await page.click("dialog button:has-text('Thêm')");
      await page.waitForSelector(".uv-card");
      check("universe: thêm nhân vật thủ công → hiện trong danh sách + mở hồ sơ", (await page.locator(".uv-card").count()) === 1 && /ch_[0-9a-f]{12}/.test(await page.locator(".uv-detail").innerText()));
      // sửa + lưu
      const temp = page.locator(".uv-detail label:has-text('Khí chất') + input");
      await temp.fill("Lạnh lùng");
      await page.click("button:has-text('Lưu thay đổi')");
      await page.waitForSelector("text=Đã lưu hồ sơ");
      check("universe: sửa hồ sơ được lưu (revision tăng)", /revision 2/.test(await page.locator(".uv-detail").innerText()));
      // trùng tên bị chặn
      await page.click("button:has-text('Thêm nhân vật') >> nth=0");
      await page.fill("dialog input >> nth=0", "lan phuong");
      await page.click("dialog button:has-text('Thêm')");
      await page.waitForSelector("dialog .error:has-text('Đã có nhân vật giống')");
      check("universe: tên trùng bị chặn với thông báo rõ", true);
      await page.click("dialog button:has-text('Huỷ')");
      // khóa cốt lõi
      await page.click("button:has-text('Khóa cốt lõi')");
      await page.waitForSelector("text=Đã khóa cốt lõi");
      check("universe: khóa cốt lõi vô hiệu hoá trường cốt lõi nhưng vẫn sửa được tên", await page.locator(".uv-detail label:has-text('Tính cách cốt lõi') + textarea").isDisabled() && await page.locator(".uv-detail label:has-text('Tên hiển thị') + input").isEnabled());
      check("universe: không lọt chữ 'null'/'undefined' ra giao diện", !/(null|undefined)/.test(await page.locator("#view").innerText()));
      await shot(page, "universe-detail");
      // xuất Excel
      const [dl] = await Promise.all([page.waitForEvent("download"), page.click("button:has-text('Xuất Excel')")]);
      const xlsx = path.join(fx.root, "kho-nhan-vat.xlsx");
      await dl.saveAs(xlsx);
      check("universe: xuất Excel ra file .xlsx", fs.statSync(xlsx).size > 2000 && dl.suggestedFilename().endsWith(".xlsx"));
      // sửa trong app SAU khi xuất → nhập file cũ phải báo xung đột
      await page.locator(".uv-detail label:has-text('Gợi ý hình ảnh') + textarea, .uv-detail label:has-text('Gợi ý hình ảnh') + input").fill("Áo khoác xám");
      await page.click("button:has-text('Lưu thay đổi')");
      await page.waitForSelector("text=Đã lưu hồ sơ");
      await page.click("button:has-text('Nhập Excel')");
      await page.setInputFiles("dialog input[type=file]", xlsx);
      await page.waitForSelector("dialog >> text=xung đột");
      check("universe: nhập file cũ báo XUNG ĐỘT, không cho ghi đè", /xung đột/i.test(await page.locator("dialog").innerText()));
      await shot(page, "universe-import-conflict");
      await page.click("dialog button:has-text('Nhập')");
      check("universe: nút Nhập bị chặn khi còn xung đột chưa chọn bỏ qua", await page.locator("dialog").count() === 1);
      await page.click("dialog button:has-text('Huỷ')");
      // lưu trữ
      await page.click("button:has-text('Lưu trữ')");
      await page.click("dialog button:has-text('Lưu trữ')");
      await page.waitForSelector(".uv-detail >> text=Nhân vật đang lưu trữ");
      check("universe: lưu trữ hiển thị badge + hồ sơ chỉ xem", await page.locator(".uv-detail label:has-text('Tên hiển thị') + input").isDisabled());
    } else {
      await page.waitForSelector(".uv-card, .uv-hint");
    }
    await shot(page, `universe-${label}`);
    await noOverflowEl(page, ".uv-layout", `universe/${label}`);
    await axe(page, `universe/${label}`);
    clean(page, `universe/${label}`);
  }
}

await browser.close();
console.log(failures ? `\n${failures} kiểm tra thất bại` : "\nTất cả đạt");
process.exit(failures ? 1 : 0);
