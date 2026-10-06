/**
 * 页面渲染自检：用最小 DOM 桩把每个页签的渲染函数真跑一遍，抓运行时错误。
 *
 * 起因：加页签时忘了同步"隐藏/显示区块"的列表，点新页签会把所有区块都藏起来，
 * 表现成一片空白——而接口是好的、HTML 也是好的，静态检查完全发现不了。
 *
 * 用法（服务要在 127.0.0.1:8770 上跑着）：
 *     node tools/render_check.js
 */

const fs = require("fs");
const path = require("path");

const BASE = process.env.SST_BASE || "http://127.0.0.1:8770";
const ROOT = path.dirname(__dirname);

const els = {};
function mkEl(id) {
  return {
    id, _html: "", value: "", checked: true, options: [],
    classList: { toggle() {}, add() {}, remove() {} },
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    set outerHTML(v) { this._html = String(v); },
    get outerHTML() { return this._html; },
    set textContent(v) { this._html = String(v); },
    get textContent() { return this._html; },
    appendChild(o) { this.options.push(o); },
    onclick: null, onchange: null,
  };
}

const html = fs.readFileSync(path.join(ROOT, "web", "index.html"), "utf8");
[...new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]))]
  .forEach((i) => { els[i] = mkEl(i); });

global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.document = {
  querySelector: (s) => els[String(s).replace(/^#/, "")] || null,
  querySelectorAll: () => [],
  createElement: (t) => mkEl(t),
  title: "",
};
const _fetch = global.fetch;
global.fetch = (u, o) =>
  _fetch(String(u).startsWith("http") ? u : BASE + u, o);
global.window = global;

const src = fs.readFileSync(path.join(ROOT, "web", "app.js"), "utf8")
  .replace(/\(async function init\(\)[\s\S]*$/m, "");
eval(`var S = {}; ${src}
S.state = state; S.api = api;
S.loadBoard = loadBoard; S.loadMhw = loadMhw; S.loadExtremes = loadExtremes;
S.loadMhwBoard = loadMhwBoard; S.loadLongtest = loadLongtest; S.initMaps = initMaps;`);

// 先检查"页签按钮 ↔ 区块"是否一一对应（就是踩过的那个坑）
const tabIds = [...html.matchAll(/<button data-tab="([^"]+)"/g)].map((m) => m[1]);
const secIds = [...html.matchAll(/id="tab-([^"]+)"/g)].map((m) => m[1]);
const missSec = tabIds.filter((t) => !secIds.includes(t));
const missBtn = secIds.filter((t) => !tabIds.includes(t));
if (missSec.length || missBtn.length) {
  console.log(`✗ 页签与区块不匹配：按钮缺区块 ${missSec}，区块缺按钮 ${missBtn}`);
  process.exit(1);
}
console.log(`页签 ↔ 区块：${tabIds.length} 个，一一对应 ✓`);

const PAGES = [
  ["海洋热浪", "loadMhw", [["mhw-status", "状态表"], ["mhw-pred", "预测表"],
    ["mhw-events", "事件表"], ["mhw-yearly", "逐年图"]]],
  ["热浪专项榜", "loadMhwBoard", [["hwb-summary", "汇总"], ["hwb-table", "表格"]]],
  ["极端值比拼", "loadExtremes", [["ex-summary", "汇总"], ["ex-table", "表格"]]],
  ["长期回测", "loadLongtest", [["lt-table", "逐时效"], ["lt-year", "分年"], ["lt-hot", "热浪表"]]],
  ["排行榜", "loadBoard", [["board-table", "主榜"], ["ref-table", "基准表"]]],
];

(async () => {
  S.state.meta = await (await fetch("/api/meta")).json();
  S.state.rounds = { latest_truth: "2026-10-03", me: null };
  let fail = 0;
  for (const [name, fn, checks] of PAGES) {
    try {
      await S[fn]();
      const empty = checks.filter(([id]) => !els[id] || els[id]._html.length < 50);
      if (empty.length) {
        console.log(`✗ ${name}：${empty.map((e) => e[1]).join("、")} 是空的`);
        fail++;
      } else {
        console.log(`✓ ${name}：` +
          checks.map(([id, l]) => `${l} ${els[id]._html.length}B`).join("，"));
      }
    } catch (e) {
      console.log(`✗ ${name} 报错：${e.message}`);
      fail++;
    }
  }
  console.log(fail ? `\n有 ${fail} 个页面没通过` : "\n全部页面渲染通过");
  process.exit(fail ? 1 : 0);
})();
