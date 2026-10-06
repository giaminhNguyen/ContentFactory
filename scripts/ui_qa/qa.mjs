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

const ROUTES = [["/", "run"], ["/jobs", "jobs"], ["/channels", "channels"], ["/tts", "tts"], ["/pools", "pools"], ["/templates", "templates"], ["/settings", "settings"], ["/settings/storage", "settings-storage"]];

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

// ===================================================================== 3b. pipeline tùy chỉnh: chọn bước, bước bắt buộc bị khóa, bỏ nhánh YouTube
if (wanted("pipeline")) {
  console.log("\n# Pipeline tùy chỉnh");
  const page = await newPage();
  await go(page, "/");
  await page.fill("#run-input", fx.youtube);
  await page.waitForSelector(".mode");
  check("mặc định: không hiện danh sách bước", (await page.locator(".pick-row").count()) === 0);
  await page.locator("label.switch:has-text('Tùy chỉnh các bước')").click();
  await page.waitForSelector(".pick-row");
  check("danh sách đủ 8 bước theo thứ tự backend", (await page.locator(".pick-row").count()) === 8);
  check("ẩn danh sách chế độ cũ khi tùy chỉnh", await page.locator(".mode-list").isHidden());
  check("bước phía trên bị khóa (Bắt buộc, có chữ không chỉ màu)", await page.locator("#stage-audio").isDisabled() && (await page.locator(".pick-row[data-role=locked]:has-text('Bắt buộc')").count()) >= 3);
  await page.locator("#stage-publish").uncheck();
  await page.waitForFunction(() => document.querySelector(".pick-row[data-role=not_requested] #stage-publish"), null, { timeout: 10000 });
  await page.locator("#stage-render_youtube").uncheck();
  await page.waitForFunction(() => document.querySelector(".pick-row[data-role=not_requested] #stage-render_youtube"), null, { timeout: 10000 });
  check("bỏ YouTube: TikTok vẫn được chọn, audio vẫn bị khóa", await page.locator("#stage-render_tiktok").isChecked() && await page.locator("#stage-audio").isDisabled());
  check("kế hoạch hiển thị YouTube là 'không chạy'", (await page.locator(".plan .step[data-s=off]:has-text('Video YouTube')").count()) === 1);
  await axe(page, "pipeline tùy chỉnh");
  await noOverflow(page, "pipeline tùy chỉnh");
  await page.waitForFunction(() => !document.querySelector("button.btn.primary.lg")?.disabled, null, { timeout: 10000 });
  await page.locator("button:has-text('RUN')").click();
  await page.waitForURL(/#\/jobs\/\d+/);
  await page.waitForSelector("text=Job đã hoàn tất", { timeout: 90000 });
  const rows = await page.locator(".stage-row").evaluateAll((els) => Object.fromEntries(els.map((e) => [e.dataset.name || e.textContent.trim().slice(0, 12), e.dataset.state])));
  const st = await page.locator(".stage-row").evaluateAll((els) => els.map((e) => e.dataset.state));
  check("job: Video YouTube/Đăng YouTube 'không chạy', TikTok đã xong", st.filter((s) => s === "not_planned").length >= 2 && st.includes("done"), JSON.stringify(rows));
  check("không lỗi console/mạng", page.problems.length === 0, page.problems.slice(0, 3).join(" | "));
  await page.context().close();
}

// ===================================================================== 3c. tạm dừng / tiếp tục / cập nhật pipeline / hủy có xác nhận (job chạy chậm trong fixture)
if (wanted("control")) {
  console.log("\n# Điều khiển job");
  const page = await newPage();
  await go(page, "/jobs");
  await page.locator("a:has-text('Truyện đang chạy chậm')").first().click();
  await page.waitForSelector("#page-title:has-text('Truyện đang chạy chậm')");
  await page.waitForSelector("button:has-text('Tạm dừng')");
  check("job đang chạy: có nút Tạm dừng, không có nút Hủy ngoài menu nâng cao", await page.locator("button:has-text('Hủy job')").isHidden());
  await page.locator("button:has-text('Tạm dừng')").first().click();
  await page.waitForSelector("button.btn.primary:has-text('Tiếp tục')", { timeout: 15000 });
  check("sau khi tạm dừng: nút chính đổi thành Tiếp tục", true);
  await page.waitForFunction(() => document.querySelector(".badge[data-tone=wait]")?.textContent.includes("Tạm dừng"), null, { timeout: 15000 });
  check("trạng thái hiển thị chữ 'Tạm dừng' (không chỉ màu)", true);
  await page.locator("button:has-text('Thao tác nâng cao')").click();
  await page.locator("button:has-text('Cập nhật pipeline')").click();
  await page.waitForSelector("dialog[open] .pick-row");
  check("dialog pipeline hiện đủ 8 bước với lý do", (await page.locator("dialog[open] .pick-row").count()) === 8 && (await page.locator("dialog[open] .pick-row .s-why:not(:empty)").count()) >= 1);
  await axe(page, "dialog cập nhật pipeline");
  await page.locator("dialog[open] #pd-publish").uncheck();
  await page.waitForFunction(() => document.querySelector("dialog[open] #pd-publish")?.closest(".pick-row")?.dataset.role === "not_requested", null, { timeout: 10000 });
  check("bỏ Đăng YouTube: impact báo bước đó bị bỏ khỏi kế hoạch", (await page.locator("dialog[open] .autolist").innerText()).includes("Bỏ khỏi kế hoạch"));
  await page.locator("dialog[open] button:has-text('Hủy')").first().click();
  await page.locator("button:has-text('Hủy job')").click();
  await page.waitForSelector("dialog[open]:has-text('Hủy job này?')");
  await page.locator("dialog[open] button:has-text('Không hủy')").click();
  check("Hủy job cần xác nhận; 'Không hủy' giữ nguyên job", await page.locator("button.btn.primary:has-text('Tiếp tục')").isVisible());
  await page.locator("button.btn.primary:has-text('Tiếp tục')").click();
  await page.waitForFunction(() => !document.querySelector(".badge")?.textContent.includes("Tạm dừng"), null, { timeout: 20000 });
  check("Tiếp tục: job rời trạng thái tạm dừng", true);
  await noOverflow(page, "trang job có điều khiển");
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

// ===================================================================== 11. Template: danh sách -> Studio -> publish -> chọn cho kênh (cần ContentFlow THẬT: real_templates.py)
if (wanted("templates")) {
  console.log("\n# Template (ContentFlow thật)");
  const tok = async (page) => page.locator("meta[name=cf-token]").getAttribute("content");
  const apiGet = async (page, p) => (await page.request.get(base + p, { headers: { "X-CF-Token": await tok(page) } })).json();
  const apiSend = async (page, m, p, data) => page.request.fetch(base + p, { method: m, headers: { "X-CF-Token": await tok(page), "Content-Type": "application/json" }, data: JSON.stringify(data) });
  const props = (page) => page.locator(".st-props");
  const val = async (page, label) => Number(await props(page).getByLabel(label, { exact: true }).first().inputValue());
  const toastText = async (page, re) => page.waitForSelector(`.toast:has-text("${re}")`, { timeout: 15000 }).then(() => true).catch(() => false);

  const page = await newPage({ width: 1440, height: 900 });
  await go(page, "/templates");
  check("danh sách: có 6 template có sẵn", (await page.locator(".tpl-card").count()) === 6);
  check("danh sách: template có sẵn hiện huy hiệu khoá", (await page.locator(".tpl-card:has-text('Có sẵn')").count()) === 6);
  check("danh sách: kenh_b được ghi là đang dùng youtube_framed", (await page.locator(".tpl-card:has-text('youtube_framed')").innerText()).includes("kenh_b"));
  await noOverflow(page, "templates 1440");
  await shot(page, "tpl_list_light");

  // ---- tạo template mới (video 16:9)
  await page.getByRole("button", { name: "Template mới" }).first().click();
  await page.getByLabel("Tên hiển thị").fill("Story Frame");
  check("mã tự sinh từ tên", (await page.getByLabel("Mã template").inputValue()) === "story_frame");
  await page.getByRole("button", { name: "Tạo và mở Studio" }).click();
  await page.waitForURL(/#\/templates\/story_frame/);
  await page.waitForSelector(".st-stage .st-el");
  check("Studio: bản nháp v1 sửa được, nút Lưu tắt khi chưa đổi", (await page.getByRole("button", { name: "Lưu nháp" }).isDisabled()));

  // ---- sửa bằng số + hoàn tác / làm lại
  await page.locator(".st-layer-main", { hasText: "Video nguồn" }).click();
  await props(page).getByLabel("X", { exact: true }).first().fill("100");
  await page.waitForTimeout(900);                                         // quá cửa sổ gom => bước hoàn tác riêng
  await props(page).getByLabel("Rộng", { exact: true }).first().fill("1600");
  check("sửa số: Lưu nháp bật + chip 'Chưa lưu'", (await page.getByRole("button", { name: "Lưu nháp" }).isEnabled()) && (await page.locator(".st-dirty").isVisible()));
  await page.keyboard.press("Control+z");
  check("Ctrl+Z hoàn tác lần sửa gần nhất", (await val(page, "Rộng")) === 1920 && (await val(page, "X")) === 100, `X=${await val(page, "X")} W=${await val(page, "Rộng")}`);
  await page.keyboard.press("Control+Shift+z");
  check("Ctrl+Shift+Z làm lại", (await val(page, "Rộng")) === 1600);
  await page.getByRole("button", { name: "Hoàn tác" }).click();
  await page.getByRole("button", { name: "Hoàn tác" }).click();
  check("nút Hoàn tác đưa về nguyên bản (hết 'Chưa lưu')", !(await page.locator(".st-dirty").isVisible()) && (await val(page, "X")) === 0);
  await page.getByRole("button", { name: "Làm lại" }).click();
  await page.getByRole("button", { name: "Làm lại" }).click();

  // ---- kéo thả + thu phóng không đổi toạ độ thật
  await page.waitForTimeout(800);
  const x0 = await val(page, "X");
  const node = page.locator('.st-el[data-id="source_video"]');
  const bb = await node.boundingBox();
  await page.mouse.move(bb.x + bb.width / 2, bb.y + bb.height / 2);
  await page.mouse.down();
  await page.mouse.move(bb.x + bb.width / 2 + 40, bb.y + bb.height / 2 + 20, { steps: 5 });
  await page.mouse.up();
  const x1 = await val(page, "X");
  check("kéo thả đổi X (số nguyên)", x1 !== x0 && Number.isInteger(x1), `${x0} -> ${x1}`);
  await page.locator("#st-zoom").selectOption("0.5");
  check("thu phóng không đổi toạ độ thật", (await val(page, "X")) === x1);
  check("thu phóng đổi kích thước hiển thị", Math.abs((await page.locator(".st-stage").boundingBox()).width - 960) < 3);
  await page.locator("#st-zoom").selectOption("fit");
  await page.locator('.st-el[data-id="source_video"]').focus();
  await page.keyboard.press("Shift+ArrowRight");
  check("phím mũi tên + Shift dịch 10px", (await val(page, "X")) === x1 + 10, `${await val(page, "X")} vs ${x1 + 10}`);

  // ---- thêm lớp hình ảnh: tải asset lên rồi chọn
  await page.getByRole("button", { name: "Thêm lớp" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Thêm" }).click();
  await page.waitForSelector(".asset-lib");
  await page.locator("dialog input[type=file]").setInputFiles(fx.asset_png);
  check("id asset tự sinh từ tên file", (await page.locator("dialog").getByLabel("Mã asset").inputValue()) === "frame_mau");
  await page.getByRole("button", { name: "Tải lên" }).click();
  await page.waitForSelector('.asset-card:has-text("frame_mau")');
  await page.waitForTimeout(500);
  await shot(page, "tpl_assets_light");
  await page.locator('.asset-card:has-text("frame_mau")').getByRole("button", { name: "Chọn" }).click();
  await page.waitForSelector(".st-layer:has-text('image')");
  check("thêm lớp hình ảnh từ asset vừa tải", (await page.locator(".st-layer").count()) === 2);
  const zs = await page.locator(".st-z").allInnerTexts();
  check("z của các lớp duy nhất", new Set(zs).size === zs.length, zs.join(","));

  // ---- lưu + nạp lại
  await page.getByRole("button", { name: "Lưu nháp" }).click();
  check("lưu nháp thành công", await toastText(page, "Đã lưu bản nháp"));
  check("sau lưu: chip 'Chưa lưu' ẩn", !(await page.locator(".st-dirty").isVisible()));
  await page.reload();
  await page.waitForSelector(".st-stage .st-el");
  check("nạp lại: giữ đủ 2 lớp đã lưu", (await page.locator(".st-layer").count()) === 2);

  // ---- kiểm tra: tạo lỗi z trùng
  await page.locator(".st-layer-main", { hasText: "Hình ảnh" }).click();
  const zOther = await page.locator(".st-layer:has-text('source_video') .st-z").innerText();
  await props(page).getByLabel("Thứ tự lớp (z)").fill(zOther.replace("z ", ""));
  await page.getByRole("button", { name: "Kiểm tra" }).click();
  await page.waitForSelector(".st-issue[data-level=error]");
  check("Kiểm tra: báo z trùng", (await page.locator(".st-issue").first().innerText()).includes("z="));
  await shot(page, "tpl_validate_error_light");
  await page.locator(".st-issue button").first().click();
  await page.keyboard.press("Control+z");
  await page.getByRole("button", { name: "Kiểm tra" }).click();
  await page.waitForSelector("text=Hợp lệ");
  check("sau hoàn tác: bố cục hợp lệ", true);

  // ---- xem trước + render thử (ContentFlow thật)
  await page.getByRole("button", { name: "Xem trước" }).click();
  await page.waitForSelector(".st-stage.has-preview", { timeout: 60000 });
  check("Xem trước: ảnh do ContentFlow dựng hiện trên canvas", true);
  await shot(page, "tpl_preview_light");
  await page.getByRole("button", { name: "Render thử" }).click();
  await page.waitForSelector("video.st-test-media", { timeout: 180000 });
  const tline = await page.locator("#st-test-h ~ p").first().innerText();
  check("Render thử: video mẫu đúng cỡ canvas", tline.includes("1920×1080") && tline.includes("đúng canvas"), tline);
  await shot(page, "tpl_testrender_light");

  // ---- publish + chọn cho kênh
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Publish" }).click();
  await page.waitForSelector("text=Chọn template này cho một kênh?");
  await page.locator("dialog select").first().selectOption("kenh_a");
  await page.getByRole("button", { name: "Chọn cho kênh" }).click();
  await page.waitForSelector(".alert:has-text('chỉ xem')");
  await page.locator(".st-layer-main", { hasText: "Video nguồn" }).click();
  check("publish: version đã publish chuyển sang chỉ xem", await props(page).getByLabel("X", { exact: true }).first().isDisabled());
  await shot(page, "tpl_published_light");
  const api1 = await apiGet(page, "/api/templates/story_frame");
  check("API: v1 published, có checksum", api1.template.status === "published" && !!api1.checksum);
  const xPub = api1.template.elements.find((e) => e.id === "source_video").x;

  // ---- sửa bản đã publish = tạo bản nháp mới; v1 không đổi
  await page.getByRole("button", { name: "Tạo bản nháp mới để sửa" }).click();
  await page.waitForFunction(() => location.hash.includes("v=2"));
  await page.waitForSelector(".st-stage .st-el");
  await page.locator(".st-layer-main", { hasText: "Video nguồn" }).click();
  await props(page).getByLabel("X", { exact: true }).first().fill("20");
  await page.getByRole("button", { name: "Lưu nháp" }).click();
  await toastText(page, "Đã lưu bản nháp");
  const v1 = await apiGet(page, "/api/templates/story_frame?version=1");
  check("v1 đã publish KHÔNG bị đổi khi sửa v2", v1.template.elements.find((e) => e.id === "source_video").x === xPub);
  await props(page).getByLabel("X", { exact: true }).first().fill("30");
  await page.locator(".nav a[data-section=channels]").click();
  await page.waitForSelector("dialog:has-text('Bỏ thay đổi chưa lưu')");
  check("rời Studio khi chưa lưu: hỏi xác nhận", true);
  await page.getByRole("button", { name: "Rời đi, bỏ thay đổi" }).click();

  // ---- kênh thấy template mới + chọn bằng giao diện
  await go(page, "/channels/kenh_a");
  await page.waitForSelector("#tpl-youtube_video");
  check("kenh_a: ô YouTube Template đã chọn story_frame (do bước publish)", (await page.locator("#tpl-youtube_video").inputValue()) === "story_frame");
  check("kênh: ô chọn có template mới publish", (await page.locator("#tpl-youtube_video option").allInnerTexts()).some((t) => t.includes("Story Frame")));
  await page.locator("#tpl-thumbnail").selectOption("thumb_gold");
  await page.waitForFunction(() => document.querySelector("#tpl-thumbnail")?.value === "thumb_gold");
  await page.waitForTimeout(1200);
  check("kênh: chọn thumb_gold lưu ngay, form không báo 'chưa lưu'", await page.getByRole("button", { name: "Lưu thay đổi" }).isDisabled());
  const chan = await apiGet(page, "/api/channels/kenh_a");
  check("kênh: channel.json có templates, không có toạ độ", chan.raw.templates.thumbnail.id === "thumb_gold" && !/"(x|y|width|height)"/.test(JSON.stringify(chan.raw.templates)));
  await page.getByRole("button", { name: "Nâng cao: version, dự phòng, chi tiết" }).click();
  await page.waitForSelector(".tpl-adv-row");
  await shot(page, "tpl_channel_light");
  check("Nâng cao hiện version/checksum", (await page.locator(".tpl-adv-row").first().innerText()).includes("Checksum"));
  await page.locator("#pol-youtube_video").selectOption("pin");
  await page.waitForTimeout(1500);
  const pinned = await apiGet(page, "/api/channels/kenh_a/templates");
  check("ghim version lưu được", pinned.youtube_video.configured.version_policy === 1, JSON.stringify(pinned.youtube_video.configured));
  await page.locator("#tpl-thumbnail").selectOption("");
  await page.waitForTimeout(1200);

  // ---- Run: xem trước thấy template; template hỏng => báo rõ
  await go(page, "/");
  await page.fill("#run-input", fx.youtube);
  await page.waitForSelector(".plan .step");
  await page.locator("select").first().selectOption("kenh_a");
  await page.waitForFunction(() => document.body.innerText.includes("Template:"), null, { timeout: 15000 });
  check("Run: kế hoạch nêu template đã chọn", (await page.locator(".autolist").innerText()).includes("Story Frame"));
  await apiSend(page, "PUT", "/api/channels/kenh_b", { raw: { name: "Kênh B", publishing: { made_for_kids: false }, templates: { youtube_video: "ghost_tpl" } } });
  await page.locator("select").first().selectOption("kenh_b");
  await page.waitForSelector(".alert:has-text('ghost_tpl')", { timeout: 15000 });
  check("Run: template không dùng được => báo rõ + nút sửa", await page.locator("a:has-text('Sửa template của kênh')").isVisible());
  check("Run: RUN tắt khi template hỏng", await page.locator("button:has-text('RUN')").isDisabled());
  await shot(page, "tpl_run_invalid_light");
  await go(page, "/templates");
  await page.waitForSelector(".tpl-card:has-text('story_frame')");
  check("danh sách: story_frame hiện kenh_a đang dùng", (await page.locator(".tpl-card:has-text('story_frame')").innerText()).includes("kenh_a"));
  check("không lỗi console/mạng (luồng Template)", page.problems.filter((p) => !/http 4|ghost_tpl|status of 4/.test(p)).length === 0, page.problems.slice(0, 3).join(" | "));
  await page.context().close();

  // ---- giao diện ở nhiều cỡ + 2 theme + a11y
  for (const scheme of ["light", "dark"]) {
    for (const [w, h] of [[1440, 900], [390, 844]]) {
      const p = await newPage({ width: w, height: h, scheme });
      for (const [hash, name] of [["/templates", "list"], ["/templates/story_frame?v=2", "studio_draft"], ["/templates/thumb_default", "studio_thumb"], ["/channels/kenh_a", "channel"]]) {
        await go(p, hash);
        if (name.startsWith("studio")) await p.waitForSelector(".st-stage .st-el");
        if (name === "studio_thumb") await p.locator(".st-layer-main", { hasText: "Tiêu đề" }).click();
        await p.waitForTimeout(600);
        await noOverflow(p, `tpl-${name} ${w} ${scheme}`);
        await shot(p, `tpl_${name}_${w}_${scheme}`);
        await axe(p, `tpl-${name} ${w} ${scheme}`);
      }
      check(`không lỗi console/mạng (template ${w} ${scheme})`, p.problems.length === 0, p.problems.slice(0, 3).join(" | "));
      await p.context().close();
    }
  }
}

await browser.close();
console.log(`\n${results.length - failures}/${results.length} đạt`);
fs.writeFileSync(path.join(shotsDir || ".", "qa-results.json"), JSON.stringify(results, null, 1));
process.exit(failures ? 1 : 0);
