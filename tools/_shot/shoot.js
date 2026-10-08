// 给本站每个页签截图，用来肉眼检查排版。
// 用法：node shoot.js [baseUrl] [outDir]
const { chromium } = require("playwright-core");
const path = require("path");
const fs = require("fs");

const BASE = process.argv[2] || "http://127.0.0.1:8770";
const OUT = process.argv[3] || "/tmp/sst_shots";
const TABS = [
  ["overview", "概览"],
  ["board", "排行榜"],
  ["analysis", "模型分析"],
  ["mhw", "海洋热浪"],
  ["submit", "提交预测"],
  ["about", "方法"],
];

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
    deviceScaleFactor: 2,
  });
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500); // 等异步渲染（榜单、图表）

  for (const [tab, label] of TABS) {
    await page.click(`#tabs button[data-tab="${tab}"]`);
    await page.waitForTimeout(2200);
    await page.screenshot({
      path: path.join(OUT, `${tab}.png`),
      fullPage: true,
    });
    console.log(`  ${tab.padEnd(9)} ${label}`);
  }

  // 手机尺寸单独看一眼
  const m = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  await m.goto(BASE, { waitUntil: "networkidle" });
  await m.waitForTimeout(2500);
  await m.screenshot({ path: path.join(OUT, "mobile-overview.png"), fullPage: true });
  console.log("  mobile    概览");

  // 手机上的海洋热浪页单独看：那张逐格点热浪图是纵向的，最容易撑破布局
  await m.click(`#tabs button[data-tab="mhw"]`);
  await m.waitForTimeout(2600);
  await m.screenshot({ path: path.join(OUT, "mobile-mhw.png"), fullPage: true });
  console.log("  mobile    海洋热浪");

  await browser.close();
  if (errors.length) {
    console.log("\n控制台错误：");
    errors.slice(0, 10).forEach((e) => console.log("  " + e.slice(0, 160)));
  } else {
    console.log("\n无控制台错误 ✓");
  }
})();
