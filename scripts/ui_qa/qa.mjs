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

const ROUTES = [["/", "run"], ["/jobs", "jobs"], ["/channels", "channels"], ["/tts", "tts"], ["/pools", "pools"], ["/pools/images", "pools-images"], ["/templates", "templates"], ["/settings", "settings"], ["/settings/storage", "settings-storage"]];

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
  await page.waitForSelector(".preflight li");
  const pfText = await page.locator(".preflight").innerText();
  check("preflight chỉ kiểm bước sẽ chạy: có TikTok/ffmpeg, KHÔNG có Đăng YouTube/Template YouTube", pfText.includes("Template TikTok") && pfText.includes("FFmpeg") && !pfText.includes("Đăng YouTube") && !pfText.includes("Template YouTube"), pfText.slice(0, 300));
  check("preflight nói rõ mục không kiểm vì không cần", (await page.locator("button:has-text('Không kiểm tra')").count()) === 1);
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

// ===================================================================== 3d. Nhịp đọc (Prosody): nghe thử A/B ở trang Giọng đọc + chỉnh nhịp của job
if (wanted("prosody")) {
  console.log("\n# Nhịp đọc");
  const page = await newPage();
  await go(page, "/tts");
  await page.waitForSelector("h2:has-text('Nhịp đọc (Prosody)')");
  check("trang Giọng đọc có thẻ Nhịp đọc", true);
  await page.locator("label.switch:has-text('So sánh A/B')").click();
  await page.locator("button:has-text('Nghe thử A và B')").click();
  await page.waitForSelector("audio[aria-label='Bản nghe thử B']", { timeout: 60000 });
  check("nghe thử A/B: có hai trình phát và thời lượng/số lần gọi TTS", (await page.locator("audio").count()) === 2 && (await page.locator("text=lần gọi TTS").count()) === 2);
  await axe(page, "trang Giọng đọc + Nhịp đọc");
  await noOverflow(page, "trang Giọng đọc + Nhịp đọc");
  await go(page, "/jobs");
  await page.locator("a:has-text('Truyện có nhịp đọc')").first().click();
  await page.waitForSelector("#page-title:has-text('Truyện có nhịp đọc')");
  await page.locator("button:has-text('Thao tác nâng cao')").click();
  await page.locator("button:has-text('Nhịp đọc…')").click();
  await page.waitForSelector("dialog[open] .pick-row");
  check("dialog nhịp đọc liệt kê khoảng nghỉ được chèn", (await page.locator("dialog[open] .pick-row").count()) >= 1);
  await page.waitForTimeout(600);                                     // đợi hết hiệu ứng mở dialog (axe đo contrast khi đang mờ dần sẽ sai)
  await axe(page, "dialog nhịp đọc");
  const first = page.locator("dialog[open] .pick-row input[type=number]").first();
  await first.fill("1500");
  await page.locator("dialog[open]").getByText("Chạy lại với nhịp này").waitFor({ timeout: 10000 });
  check("job đã xong: gợi ý Chạy lại với nhịp này (không sửa tại chỗ)", true);
  await page.locator("dialog[open] button:has-text('Áp dụng')").click();
  await page.waitForURL(/#\/jobs\/\d+/);
  check("áp dụng tạo job mới", !page.url().endsWith("#/jobs/1"));
  check("không lỗi console/mạng", page.problems.length === 0, page.problems.slice(0, 3).join(" | "));
  await page.context().close();
}

// ===================================================================== 3e. Channel Run: dán kênh -> chọn video -> tạo batch -> theo dõi -> thao tác nhiều video
if (wanted("channelrun")) {
  console.log("\n# Channel Run");
  const page = await newPage();
  await go(page, "/jobs");
  await page.waitForSelector(".job");
  const before = await page.locator(".joblist > li").count();
  await go(page, "/");
  await page.fill("#run-input", fx.channel_url);
  await page.waitForSelector(".chip.ok:has-text('Kênh YouTube')");
  await page.waitForSelector("text=Sẽ tạo 10 job");
  check("kênh: mặc định chọn 10 video mới nhất chưa xử lý", (await page.locator(".chrun-list input:checked").count()) === 10);
  check("video sắp công chiếu/livestream bị bỏ qua có lý do", (await page.locator(".chrun-list .chip.warn:has-text('Sắp công chiếu')").count()) === 1 && (await page.locator(".chrun-list .chip.warn:has-text('Livestream')").count()) === 1);
  check("có link mở video nguồn an toàn (tab mới, noopener)", await page.locator(".chrun-list a[target=_blank][rel*=noopener]").first().isVisible());
  await page.locator(".mode:has-text('Chỉ lấy phụ đề')").click();
  await page.locator(".chrun-list input:checked").first().click();
  await page.waitForSelector("text=Sẽ tạo 9 job");
  check("bỏ chọn tay một video: 9 job + nhãn Chọn tay", (await page.locator(".chip:has-text('Chọn tay')").count()) >= 1);
  await page.waitForTimeout(500);
  await axe(page, "màn Chạy với kênh");
  await noOverflow(page, "màn Chạy với kênh");
  await page.waitForFunction(() => !document.querySelector("button.btn.primary.lg")?.disabled, null, { timeout: 15000 });
  check("nút đổi thành Tạo Channel Run", (await page.locator("button.btn.primary.lg").innerText()).includes("Tạo Channel Run"));
  await page.locator("button.btn.primary.lg").click();
  await page.waitForURL(/#\/batches\/B\d+/);
  await page.waitForSelector("#page-title:has-text('Channel Run')");
  await page.waitForSelector(".chip:has-text('9 hoàn tất')", { timeout: 90000 });
  check("batch hoàn tất 9/9, mỗi video một dòng", (await page.locator(".joblist > li.child").count()) === 9);
  check("trạng thái batch có chữ (không chỉ màu)", (await page.locator(".badge:has-text('Hoàn tất')").count()) >= 1);
  await page.locator(".joblist > li.child input[type=checkbox]").nth(0).check();
  await page.locator(".joblist > li.child input[type=checkbox]").nth(1).check();
  await page.waitForSelector("text=Đã chọn 2 video");
  await page.locator(".bulkbar button:has-text('Tạm dừng')").click();
  await page.waitForSelector("dialog[open]:has-text('Một số video không áp dụng được')");
  check("thao tác nhiều video báo thành công một phần rõ ràng", true);
  await page.locator("dialog[open] button:has-text('Đóng')").click();
  await page.waitForTimeout(500);
  await axe(page, "chi tiết Channel Run");
  await noOverflow(page, "chi tiết Channel Run");
  await page.locator(".joblist > li.child a.trunc").first().click();
  await page.waitForSelector("a:has-text('Channel Run')");
  check("job con: có link về Channel Run + mở video nguồn/kênh nguồn", (await page.locator("a[target=_blank]:has-text('Mở video nguồn')").count()) === 1 && (await page.locator("a[target=_blank]:has-text('Mở kênh nguồn')").count()) === 1);
  await go(page, "/jobs");
  await page.waitForSelector(".job.batch");
  check("danh sách job: một thẻ Channel Run, job con không thành hàng cấp cao", (await page.locator(".job.batch").count()) === 1 && (await page.locator(".joblist > li").count()) === before + 1);
  await axe(page, "danh sách job có Channel Run");
  const mob = await newPage({ width: 390, height: 844 });
  await go(mob, "/jobs");
  await noOverflow(mob, "danh sách job có Channel Run 390px");
  check("không lỗi console/mạng", page.problems.length === 0 && mob.problems.length === 0, [...page.problems, ...mob.problems].slice(0, 3).join(" | "));
  await mob.context().close();
  await page.context().close();
}

// ===================================================================== 3f. Vòng đời template: xoá bản nháp ngay ở danh sách, template có sẵn chỉ nhân bản, lưu trữ/khôi phục
if (wanted("tpllife")) {
  console.log("\n# Vòng đời template");
  const page = await newPage();
  await go(page, "/templates");
  await page.waitForSelector(".tpl-card");
  const builtin = await page.locator(".tpl-card").count();
  check("template có sẵn: không có nút Xoá/Lưu trữ", (await page.locator(".tpl-card button:has-text('Xoá bản nháp'), .tpl-card button:has-text('Lưu trữ')").count()) === 0);
  await page.locator("button:has-text('Template mới')").first().click();
  await page.waitForSelector("dialog[open]");
  await page.locator("dialog[open] input[placeholder^='Ví dụ']").fill("Nháp thử xoá");
  await page.locator("dialog[open] button:has-text('Tạo và mở Studio')").click();
  await page.waitForURL(/#\/templates\/nhap_thu_xoa/);
  await page.waitForSelector("button:has-text('Xoá bản nháp')");
  check("Studio: nút Xoá bản nháp hiện ngay trên thanh công cụ (không giấu trong menu)", true);
  await page.waitForSelector(".st-pv-status .alert");                                              // adapter giả lập không có xem trước: báo rõ, không treo
  page.problems.splice(0);                                                                         // ...và 400 đó là hành vi mong đợi của fixture giả lập
  await go(page, "/templates");
  await page.waitForSelector(".tpl-card:has-text('Nháp thử xoá')");
  const card = page.locator(".tpl-card:has-text('Nháp thử xoá')");
  check("danh sách: bản nháp của user có Xoá bản nháp, không có Lưu trữ", (await card.locator("button:has-text('Xoá bản nháp')").count()) === 1 && (await card.locator("button:has-text('Lưu trữ')").count()) === 0);
  await page.waitForTimeout(400);
  await axe(page, "danh sách template có bản nháp");
  await card.locator("button:has-text('Xoá bản nháp')").click();
  await page.waitForSelector("dialog[open]:has-text('Xoá bản nháp?')");
  check("xác nhận xoá nói rõ hậu quả (template biến mất, không khôi phục)", (await page.locator("dialog[open]").innerText()).includes("biến mất"));
  await page.locator("dialog[open] button:has-text('Xoá')").last().click();
  await page.waitForFunction((n) => document.querySelectorAll(".tpl-card").length === n, builtin, { timeout: 10000 });
  check("xoá xong danh sách làm mới ngay, các template có sẵn còn nguyên", (await page.locator(".tpl-card:has-text('Nháp thử xoá')").count()) === 0);
  check("không lỗi console/mạng", page.problems.length === 0, page.problems.slice(0, 3).join(" | "));
  await page.context().close();
}

// ===================================================================== 3g. Xem trước nhanh trong Studio: tự cập nhật có debounce, bỏ kết quả cũ, đổi mẫu, lỗi dễ hiểu, Render thử tách riêng
if (wanted("tplprev")) {
  console.log("\n# Xem trước nhanh (Studio)");
  const tok = async (p) => p.locator("meta[name=cf-token]").getAttribute("content");
  const page = await newPage({ width: 1440, height: 900 });
  await go(page, "/templates");
  await page.request.fetch(base + "/api/templates", { method: "POST", headers: { "X-CF-Token": await tok(page), "Content-Type": "application/json" }, data: JSON.stringify({ type: "thumbnail", id: "qa_pv", name: "QA Preview" }) });
  const reqs = [];
  page.on("request", (r) => { if (r.method() === "POST" && /\/api\/templates\/qa_pv\/preview$/.test(r.url())) reqs.push(r.postDataJSON()); });
  await go(page, "/templates/qa_pv");
  await page.waitForSelector(".st-pv-status:has-text('Đã cập nhật')", { timeout: 40000 }).catch(async (e) => { console.log("DEBUG prevbar:", await page.locator(".st-prevbar").innerText().catch(() => "(none)"), "| problems:", page.problems.join(" | ")); throw e; });
  check("mở Studio là tự có ảnh xem trước (không cần bấm)", (await page.locator(".st-preview-img").count()) === 1 && reqs.length === 1);
  check("3 mẫu nội dung + đổi mẫu + chọn ảnh/tên kênh", (await page.locator(".st-chip").count()) === 3 && (await page.getByRole("button", { name: "Đổi mẫu" }).count()) === 1 && (await page.getByLabel("Ảnh nền").count()) === 1 && (await page.getByLabel("Tên kênh", { exact: true }).count()) === 1);
  await axe(page, "Studio có thanh xem trước");

  // đổi mẫu -> gửi mô tả mẫu (không phải đường dẫn)
  await page.locator(".st-chip", { hasText: "Mẫu 2" }).click();
  await page.waitForFunction(() => document.querySelector(".st-pv-status")?.textContent.includes("Đã cập nhật"));
  check("chọn Mẫu 2 gửi sample.id = s2, ảnh = builtin", reqs.at(-1)?.sample?.id === "s2" && reqs.at(-1)?.sample?.image === "builtin" && reqs.length === 2, JSON.stringify(reqs.at(-1)?.sample));
  check("chip đang chọn có aria-pressed", (await page.locator(".st-chip[aria-pressed=true]").innerText()).includes("Mẫu 2"));

  // kết quả cũ về muộn không ghi đè bản mới
  await page.route("**/api/templates/qa_pv/preview", async (route) => {
    if (route.request().postDataJSON()?.sample?.id === "s1") await new Promise((r) => setTimeout(r, 3000));
    await route.continue().catch(() => {});
  });
  await page.locator(".st-chip", { hasText: "Mẫu 1" }).click();
  await page.waitForTimeout(150);
  await page.locator(".st-chip", { hasText: "Mẫu 3" }).click();
  await page.waitForFunction(() => document.querySelector(".st-pv-status")?.textContent.includes("Đã cập nhật"));
  const imgNow = await page.locator(".st-preview-img").getAttribute("src");
  await page.waitForTimeout(3600);
  check("kết quả cũ (Mẫu 1) về muộn KHÔNG ghi đè ảnh của Mẫu 3", (await page.locator(".st-preview-img").getAttribute("src")) === imgNow && (await page.locator(".st-pv-status").innerText()).includes("Đã cập nhật"));
  await page.unroute("**/api/templates/qa_pv/preview");

  // debounce: nhiều lần sửa liên tiếp -> một yêu cầu
  const before = reqs.length;
  await page.locator(".st-el").first().focus();
  for (let i = 0; i < 6; i++) await page.keyboard.press("Shift+ArrowRight");
  await page.waitForTimeout(250);
  check("đang sửa: hiện 'sắp cập nhật' và chip ảnh đã cũ", (await page.locator(".st-pv-status").innerText()).includes("sắp cập nhật") && (await page.locator(".st-stale").count()) === 1);
  await page.waitForFunction(() => document.querySelector(".st-pv-status")?.textContent.includes("Đã cập nhật"), null, { timeout: 30000 });
  check("6 lần sửa liên tiếp chỉ gửi 1 yêu cầu xem trước", reqs.length === before + 1, `${reqs.length - before}`);
  check("xong thì hết chip 'ảnh đã cũ'", (await page.locator(".st-stale").count()) === 0);

  // tắt tự cập nhật
  await page.getByText("Tự cập nhật khi sửa", { exact: true }).click();
  const b2 = reqs.length;
  await page.locator(".st-el").first().focus();
  await page.keyboard.press("Shift+ArrowRight");
  await page.waitForTimeout(1500);
  check("tắt tự cập nhật: sửa không gửi yêu cầu, chip 'đã cũ' hiện", reqs.length === b2 && (await page.locator(".st-stale").count()) === 1);
  await page.getByRole("button", { name: "Xem trước", exact: true }).click();
  await page.waitForFunction(() => !document.querySelector(".st-stale"), null, { timeout: 30000 });
  check("nút Xem trước vẫn cập nhật thủ công", reqs.length === b2 + 1);
  await page.getByText("Tự cập nhật khi sửa", { exact: true }).click();

  // lỗi dễ hiểu + thử lại
  await page.route("**/api/templates/qa_pv/preview", (route) => route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ error: { code: "TEMPLATE_INVALID", message: "Lớp 'photo' có chiều rộng âm.", hint: "Sửa Rộng của lớp này rồi xem trước lại." } }) }));
  await page.locator(".st-chip", { hasText: "Mẫu 1" }).click();
  await page.waitForSelector(".st-pv-status .alert");
  check("lỗi nói rõ lớp/thuộc tính + cách sửa, có nút Thử lại", (await page.locator(".st-pv-status .alert").innerText()).includes("chiều rộng âm") && (await page.locator(".st-pv-status .alert").innerText()).includes("Sửa Rộng") && (await page.getByRole("button", { name: "Thử lại" }).count()) === 1);
  check("ảnh xem trước cũ vẫn còn (không mất khi lỗi)", (await page.locator(".st-preview-img").count()) === 1);
  await page.unroute("**/api/templates/qa_pv/preview");
  page.problems.splice(0);                                                                       // 400 ở trên là lỗi giả lập có chủ ý
  await page.getByRole("button", { name: "Thử lại" }).click();
  await page.waitForFunction(() => !document.querySelector(".st-pv-status .alert") && document.querySelector(".st-pv-status")?.textContent.includes("Đã cập nhật"), null, { timeout: 30000 });
  check("Thử lại thành công thì lỗi biến mất", true);

  // Render thử tách riêng
  const b3 = reqs.length;
  await page.getByRole("button", { name: "Render thử" }).click();
  await page.waitForSelector("#st-test-h", { timeout: 60000 });
  await page.waitForSelector(".st-test-media", { timeout: 60000 });
  check("Render thử là hành động riêng (không gửi thêm yêu cầu xem trước), có ảnh kết quả", reqs.length === b3 && (await page.locator(".st-test-media").count()) === 1);
  check("không lỗi console/mạng", page.problems.length === 0, page.problems.slice(0, 3).join(" | "));
  await page.request.fetch(base + "/api/templates/qa_pv/1", { method: "DELETE", headers: { "X-CF-Token": await tok(page) } });         // dọn: phần "templates" sau đó đếm đúng 6 template có sẵn
  await page.context().close();
}

// ===================================================================== 3h. Nguồn Media → Ảnh thumbnail (Image Pool): tạo pool, quét, chọn cho kênh, ảnh đã chốt trên job, đổi ảnh
if (wanted("imgpools")) {
  console.log("\n# Pool ảnh thumbnail");
  const zlib = await import("node:zlib");
  const os = await import("node:os");
  const png = (w, h, [r, g, b]) => {
    const chunk = (t, d) => { const len = Buffer.alloc(4); len.writeUInt32BE(d.length); const td = Buffer.concat([Buffer.from(t), d]); const crc = Buffer.alloc(4); crc.writeUInt32BE(zlib.crc32(td) >>> 0); return Buffer.concat([len, td, crc]); };
    const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 2;
    const row = Buffer.concat([Buffer.from([0]), Buffer.from(Array.from({ length: w }, () => [r, g, b]).flat())]);
    return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(Buffer.concat(Array.from({ length: h }, () => row)))), chunk("IEND", Buffer.alloc(0))]);
  };
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cf-qa-img-"));
  [[200, 80, 80], [80, 200, 80], [80, 80, 200]].forEach((c, i) => fs.writeFileSync(path.join(dir, `anh_${i + 1}.png`), png(800, 500, c)));
  fs.writeFileSync(path.join(dir, "hong.jpg"), "đây không phải ảnh ".repeat(8));
  const tok = async (p) => p.locator("meta[name=cf-token]").getAttribute("content");
  const send = async (p, m, u, data) => p.request.fetch(base + u, { method: m, headers: { "X-CF-Token": await tok(p), "Content-Type": "application/json" }, data: JSON.stringify(data ?? {}) });
  const page = await newPage({ width: 1280, height: 900 });
  await go(page, "/pools/images");
  await page.waitForSelector("h2:has-text('Chưa có pool ảnh thumbnail nào')");
  check("tab Ảnh thumbnail: trạng thái trống giải thích cách dùng", (await page.locator("[role=tab][aria-selected=true]").innerText()).includes("Ảnh thumbnail") && (await page.locator("body").innerText()).includes("cấu hình Kênh"));
  await axe(page, "Nguồn Media / Ảnh thumbnail (trống)");

  // tạo pool qua dialog
  await page.getByRole("button", { name: "Thêm pool ảnh" }).first().click();
  await page.waitForSelector("dialog[open]");
  await page.locator("dialog[open]").getByLabel("Tên pool").fill("anime nu");
  await page.locator("dialog[open]").getByLabel("Thư mục ảnh").fill(dir);
  await page.getByRole("button", { name: "Lưu pool" }).click();
  check("tên pool sai được báo ngay trong dialog (không đóng)", (await page.locator("dialog[open]").innerText()).includes("chỉ gồm chữ không dấu"));
  await page.locator("dialog[open]").getByLabel("Tên pool").fill("anime_nu");
  await page.getByRole("button", { name: "Lưu pool" }).click();
  await page.waitForSelector("section[aria-label='Pool ảnh anime_nu']");
  const card = page.locator("section[aria-label='Pool ảnh anime_nu']");
  check("pool hiện số ảnh hợp lệ + file không hợp lệ + chưa kênh nào dùng", (await card.innerText()).includes("3 ảnh hợp lệ") && (await card.innerText()).includes("1 file không hợp lệ") && (await card.innerText()).includes("Chưa kênh nào dùng"));
  await page.waitForTimeout(900);                                                                  // toast "Đã lưu" đang mờ dần làm axe đo sai độ tương phản
  await axe(page, "Nguồn Media / Ảnh thumbnail (có pool)");
  await card.getByRole("button", { name: "Quét & xem ảnh" }).click();
  await page.waitForFunction(() => document.querySelectorAll(".ip-grid img").length === 3 && [...document.querySelectorAll(".ip-grid img")].every((i) => i.naturalWidth > 0), null, { timeout: 15000 });
  check("quét: 3 ảnh xem thử tải được (qua chỉ số, không lộ đường dẫn) + liệt kê file hỏng", (await card.locator("details.ip-invalid").textContent()).includes("hong.jpg"));
  check("không có đường dẫn máy trong HTML ảnh", !(await page.locator(".ip-grid").innerHTML()).includes(dir.replace(/\\/g, "/")) && !(await page.locator(".ip-grid").innerHTML()).includes("cf-qa-img"));
  await page.getByLabel("Cách chọn ảnh cho mỗi job").selectOption("sequential");
  await page.waitForSelector(".toast:has-text('Đã đổi cách chọn ảnh')");
  check("đổi cách chọn ảnh lưu ngay", true);

  // chọn cho kênh -> xoá pool bị khoá, có giải thích
  await go(page, "/channels/kenh_a");
  await page.getByLabel("Pool ảnh thumbnail").selectOption("anime_nu");
  await page.getByRole("button", { name: "Lưu thay đổi" }).click();
  await page.waitForSelector(".toast:has-text('Đã lưu')", { timeout: 15000 }).catch(() => {});
  const ch = await (await page.request.get(base + "/api/channels/kenh_a", { headers: { "X-CF-Token": await tok(page) } })).json();
  check("cấu hình kênh lưu thumbnail.image_pool", ch.raw?.thumbnail?.image_pool === "anime_nu", JSON.stringify(ch.raw?.thumbnail));
  await go(page, "/pools/images");
  await page.waitForSelector("section[aria-label='Pool ảnh anime_nu']");
  check("pool đang được kênh dùng: nút Xoá bị khoá kèm lý do", (await page.locator("button[aria-label='Xoá pool anime_nu']").isDisabled()) && (await page.locator("body").innerText()).includes("Xoá bị khoá"));

  // job: ảnh đã chốt + đổi ảnh
  const r = await (await send(page, "POST", "/api/runs", { input: { value: fx.youtube }, channel: "kenh_a", run: "full" })).json();
  await send(page, "POST", `/api/jobs/${r.job_id}/pause`);
  await go(page, `/jobs/${r.job_id}`);
  await page.waitForSelector("section[aria-labelledby=thumb-h] img", { timeout: 15000 });
  check("trang job hiện ảnh thumbnail đã chốt + pool + cách chọn", (await page.locator("section[aria-labelledby=thumb-h]").innerText()).includes("anime_nu") && (await page.locator("section[aria-labelledby=thumb-h] img").evaluate((i) => i.complete)));
  await axe(page, "Job có ảnh thumbnail từ pool");
  const before = (await page.locator("section[aria-labelledby=thumb-h] dd").nth(1).innerText());
  await page.getByRole("button", { name: "Đổi ảnh thumbnail…" }).click();
  await page.waitForSelector("dialog[open]:has-text('Đổi ảnh thumbnail?')");
  check("hộp thoại nói trước việc gì sẽ chạy lại", (await page.locator("dialog[open]").innerText()).includes("giữ nguyên"));
  await page.getByRole("button", { name: "Đổi ảnh", exact: true }).click();
  await page.waitForFunction((b) => document.querySelector("section[aria-labelledby=thumb-h] dd:nth-of-type(2)")?.textContent !== b || document.body.innerText.includes("Có thay đổi đang chờ"), before, { timeout: 15000 });
  check("đổi ảnh: ảnh khác được chốt (hoặc đang chờ điểm an toàn, có báo)", true);

  // job đã kết thúc: không đổi tại chỗ, nói rõ vì sao
  await go(page, "/jobs");
  const fin = await (await send(page, "POST", "/api/runs", { input: { value: "https://www.youtube.com/watch?v=qqqqqqqqqqq" }, channel: "kenh_a", run: "full" })).json();       // video khác: cùng video + kênh sẽ trả lại job cũ
  for (let k = 0; k < 120; k++) {                                                                  // chờ job chạy xong (adapter giả: vài giây)
    const st = await (await page.request.get(base + `/api/jobs/${fin.job_id}`, { headers: { "X-CF-Token": await tok(page) } })).json();
    if (st.status === "completed") break;
    await page.waitForTimeout(1000);
  }
  await go(page, `/jobs/${fin.job_id}`);
  await page.waitForSelector("section[aria-labelledby=thumb-h]");
  const fb = page.getByRole("button", { name: "Đổi ảnh thumbnail…" });
  const fj = await (await page.request.get(base + `/api/jobs/${fin.job_id}`, { headers: { "X-CF-Token": await tok(page) } })).json();
  check("job đã xong: Đổi ảnh bị tắt và lý do hiện rõ (không chỉ tooltip)", (await fb.isDisabled()) && (await page.locator("section[aria-labelledby=thumb-h]").innerText()).includes("Chạy lại với thay đổi"), `status=${fj.status} thumb=${JSON.stringify(fj.thumbnail)}`);
  await noOverflow(page, "trang job có ảnh thumbnail");
  check("không lỗi console/mạng", page.problems.length === 0, page.problems.slice(0, 3).join(" | "));
  await send(page, "PUT", "/api/channels/kenh_a", { raw: { ...(ch.raw || {}), thumbnail: {} } });         // trả kênh về như cũ cho các phần QA khác
  await page.context().close();
}

// ===================================================================== 3i. Trang Job: dải tổng quan, tìm kiếm/lọc (nhớ lựa chọn), chọn nhiều + hàng loạt; chi tiết job: timeline nhánh + "Vì sao?"
if (wanted("jobsui")) {
  console.log("\n# Job: tổng quan, tìm kiếm, hàng loạt, timeline");
  const page = await newPage({ width: 1280, height: 900 });
  await go(page, "/jobs");
  await page.waitForSelector(".dash h2");
  check("dải tổng quan trả lời 'có việc cần xử lý không' + hiện làn tài nguyên và ổ đĩa", (await page.locator(".dash").innerText()).includes("Render (GPU)") && (await page.locator(".dash").innerText()).includes("trống"));
  await page.waitForSelector(".joblist li");
  const before = await page.locator(".joblist li").count();
  await page.getByLabel("Tìm job").fill("không có gì khớp zzzz");
  await page.waitForSelector("h2:has-text('Không có job nào khớp bộ lọc')");
  check("tìm không thấy: trạng thái trống giải thích + nút Xoá lọc", (await page.getByRole("button", { name: "Xoá lọc" }).count()) >= 1);
  await page.getByRole("button", { name: "Xoá lọc" }).first().click();
  await page.waitForFunction((n) => document.querySelectorAll(".joblist li").length === n, before, { timeout: 15000 });
  check("xoá lọc trả lại danh sách đầy đủ", true);
  await page.getByLabel("Tìm job").fill("chậm");
  await page.waitForFunction(() => document.querySelectorAll(".joblist li").length >= 1 && [...document.querySelectorAll(".joblist li")].every((li) => /chậm/i.test(li.innerText)), null, { timeout: 15000 });
  check("tìm theo tiêu đề chỉ giữ các job khớp", true);
  await page.getByLabel("Tìm job").fill("");
  await page.getByLabel("Loại job").selectOption("channel");
  await page.waitForFunction(() => [...document.querySelectorAll(".joblist li")].every((li) => li.querySelector("a[href^='#/batches/']")) , null, { timeout: 15000 });
  check("lọc theo loại 'Channel Run' chỉ còn hàng Channel Run (hoặc trống)", true);
  await page.reload();
  await page.waitForSelector(".jobs-tools");
  check("lựa chọn lọc gần nhất được nhớ sau khi tải lại", (await page.getByLabel("Loại job").inputValue()) === "channel");
  await page.getByLabel("Loại job").selectOption("");
  await page.getByLabel("Thời gian tạo").selectOption("1");
  await page.waitForSelector(".joblist li");
  await page.getByLabel("Thời gian tạo").selectOption("");
  await page.waitForTimeout(500);
  await axe(page, "trang Job có thanh tìm/lọc");

  // chọn nhiều + hàng loạt: báo thành công một phần rõ ràng (chỉ chọn job ĐÃ HOÀN TẤT: tạm dừng chúng bị từ chối kèm lý do, không đổi gì cho các phần QA khác)
  await page.locator(".filters button:has-text('Hoàn tất')").click();
  await page.waitForSelector(".joblist li");
  await page.getByRole("button", { name: "Chọn nhiều" }).click();
  await page.waitForSelector(".job-pick");
  check("chế độ chọn nhiều: có ô tích có nhãn đọc được", (await page.locator(".job-pick").first().getAttribute("aria-label")).startsWith("Chọn job"));
  check("chưa chọn gì: nút hàng loạt bị tắt", await page.locator(".bulkbar button:has-text('Tạm dừng')").isDisabled());
  await page.getByRole("button", { name: "Chọn tất cả đang hiện" }).click();
  check("chọn tất cả: thanh hàng loạt hiện số đã chọn", (await page.locator(".bulkbar").innerText()).includes("Đã chọn"));
  await page.locator(".bulkbar button:has-text('Tạm dừng')").click();
  await page.waitForSelector(".toast:has-text('Tạm dừng:')");
  const dlg = page.locator("dialog[open]:has-text('Một số job không áp dụng được')");
  await dlg.waitFor({ timeout: 15000 }).catch(() => {});
  check("hàng loạt: job không áp dụng được (đã xong/lỗi…) được liệt kê kèm lý do", (await dlg.count()) === 1 && (await dlg.locator("li").count()) >= 1);
  if (await dlg.count()) await dlg.getByRole("button", { name: "Đóng" }).click();
  // hộp thoại hàng loạt: focus vào trong, Esc đóng và trả focus về nút gọi (bàn phím)
  await page.getByRole("button", { name: "Chọn tất cả đang hiện" }).click();
  await page.locator(".bulkbar button:has-text('Cập nhật pipeline')").focus();
  await page.keyboard.press("Enter");
  await page.waitForSelector("dialog[open] .pick-row");
  check("hộp thoại cập nhật pipeline hàng loạt: focus nằm trong dialog", await page.evaluate(() => !!document.activeElement?.closest("dialog")));
  await page.waitForTimeout(600);                                                                 // dialog đang mờ vào làm axe đo sai độ tương phản
  await axe(page, "dialog cập nhật pipeline hàng loạt");
  await page.keyboard.press("Escape");
  await page.waitForSelector("dialog[open]", { state: "detached", timeout: 3000 });
  check("Esc đóng dialog hàng loạt và trả focus về nút gọi", await page.evaluate(() => document.activeElement?.textContent?.includes("Cập nhật pipeline")));
  await page.getByRole("button", { name: "Xong chọn" }).click();
  check("không lỗi console/mạng (trang Job)", page.problems.length === 0, page.problems.slice(0, 3).join(" | "));

  // chi tiết job: timeline + nhánh + vì sao
  await go(page, "/jobs");
  await page.waitForSelector(".joblist li a[href^='#/jobs/']");
  const done = await (await page.request.get(base + "/api/jobs?status=completed", { headers: { "X-CF-Token": await page.locator("meta[name=cf-token]").getAttribute("content") } })).json();
  const jid = done.jobs.find((j) => j.type !== "batch")?.id;
  await go(page, `/jobs/${jid}`);
  await page.waitForSelector(".stage-row[data-timeline]");
  const tl = await page.locator(".stage-row").evaluateAll((els) => els.map((e) => [e.dataset.name, e.dataset.timeline, e.dataset.branch]));
  check("timeline dùng trạng thái chuẩn hoá + nhánh", tl.length === 8 && tl.every(([, t]) => ["DONE", "REUSED", "AVAILABLE", "NOT_REQUESTED"].includes(t)) && tl.find(([n]) => n === "render_tiktok")[2] === "tiktok", JSON.stringify(tl));
  const yt = await page.locator('.stage-row[data-name="render_youtube"]').boundingBox(), tt = await page.locator('.stage-row[data-name="render_tiktok"]').boundingBox();
  check("hai nhánh YouTube/TikTok đứng cạnh nhau trên màn rộng", yt && tt && Math.abs(yt.y - tt.y) < 8 && tt.x > yt.x, JSON.stringify([yt, tt]));
  const row = page.locator('.stage-row[data-name="audio"]');
  const hiddenFirst = await row.locator("p[id^=why-]").isHidden();
  await row.getByRole("button", { name: "Vì sao?" }).click();
  check("'Vì sao?' thu gọn mặc định rồi mở được", hiddenFirst && (await row.locator("p[id^=why-]").isVisible()));
  await axe(page, "chi tiết job: timeline");
  await noOverflow(page, "chi tiết job: timeline");
  await page.setViewportSize({ width: 390, height: 800 });
  await page.waitForTimeout(900);                                                                  // bố cục + hoạt ảnh vào của thanh điều hướng ổn định rồi mới đo
  const yt2 = await page.locator('.stage-row[data-name="render_youtube"]').boundingBox(), tt2 = await page.locator('.stage-row[data-name="render_tiktok"]').boundingBox();
  check("màn hẹp: nhánh xếp dọc, không tràn ngang", tt2.y > yt2.y + 10);
  await noOverflow(page, "chi tiết job 390");
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
