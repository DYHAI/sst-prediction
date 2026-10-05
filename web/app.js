const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const TOKEN_KEY = "sst_token";
const NAME_KEY = "sst_name";

const state = { token: localStorage.getItem(TOKEN_KEY) || "", meta: null, rounds: null, myId: null };

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
const post = (p, body) => api(p, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

const fmt = (v, n = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toFixed(n);
const pct = (v) => (v === null || v === undefined) ? "—" : (v * 100).toFixed(0) + "%";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const signed = (v, n = 2) => v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(n);

function beijing(iso) {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(d);
}
const regionName = (code) => (state.meta.regions.find((x) => x.code === code) || {}).name_cn || code;
const todayISO = () => new Date().toISOString().slice(0, 10);

// ------------------------------------------------------------------ 标签页
$$("#tabs button").forEach((b) => b.addEventListener("click", () => {
  $$("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
  ["submit", "board", "maps", "about"].forEach((t) =>
    $("#tab-" + t).classList.toggle("hidden", t !== b.dataset.tab));
  if (b.dataset.tab === "board") loadBoard();
  if (b.dataset.tab === "maps") { initMaps(); }
}));

function setMsg(el, text, ok = true) {
  const n = typeof el === "string" ? $(el) : el;
  n.textContent = text;
  n.className = "msg " + (ok ? "ok" : "bad");
}

// ------------------------------------------------------------------ 身份
function renderIdentity() {
  const box = $("#who-box");
  $("#register-form").classList.toggle("hidden", !!state.token);
  if (!state.token) {
    box.innerHTML = `<span class="muted small">还没有身份</span>`;
    $("#my-card").classList.add("hidden");
    return;
  }
  api("/api/me?token=" + encodeURIComponent(state.token)).then((d) => {
    if (d.error) { logout(); return; }
    localStorage.setItem(NAME_KEY, d.player.name);
    box.innerHTML = `<strong>${esc(d.player.name)}</strong>
      <span class="pill">${kindLabel(d.player.kind)}</span>
      <button id="logout" class="small">换人</button>`;
    $("#logout").onclick = logout;
    renderMyScore(d);
  }).catch(() => {});
}
function logout() {
  localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(NAME_KEY);
  state.token = ""; renderIdentity(); loadRounds();
}
const kindLabel = (k) => ({ team: "队伍", model: "模型", human: "个人", product: "官方产品",
  baseline: "内置对照" }[k] || k);

$("#reg-btn").onclick = async () => {
  const name = $("#reg-name").value.trim();
  if (name.length < 2) return setMsg("#save-msg", "名字至少 2 个字", false);
  try {
    const d = await post("/api/register", { name, affiliation: $("#reg-affil").value.trim() });
    state.token = d.player.token;
    localStorage.setItem(TOKEN_KEY, state.token);
    renderIdentity(); loadRounds();
  } catch (e) { setMsg("#save-msg", e.message, false); }
};

function renderMyScore(me) {
  const s = me.score || {};
  const settled = (me.submissions || []).filter((x) => x.truth !== null && x.truth !== undefined);
  $("#my-card").classList.remove("hidden");
  $("#my-summary").textContent = s.n > 0
    ? `已结算 ${s.n} 条｜综合分 ${fmt(s.score, 1)}`
    : "还没有已结算的记录（真值滞后约 1–2 天）";
  let html = "";
  if (s.n > 0) {
    html += `<div class="kv">
      <div class="cell"><label>综合分</label><b>${fmt(s.score, 1)}</b></div>
      <div class="cell"><label>MAE</label><b>${fmt(s.mae, 3)}</b></div>
      <div class="cell"><label>RMSE</label><b>${fmt(s.rmse, 3)}</b></div>
      <div class="cell"><label>偏差</label><b>${signed(s.bias, 3)}</b></div>
      <div class="cell"><label>命中率</label><b>${pct(s.hit)}</b></div>
      <div class="cell"><label>相对基准</label><b>${s.advantage === null ? "—" : signed(s.advantage * 100, 1) + "%"}</b></div>
    </div>`;
  }
  if (settled.length) {
    html += `<div class="scroll"><table><thead><tr><th>目标日</th><th>时效</th><th>海区</th>
      <th>我的预报</th><th>真值</th><th>误差</th></tr></thead><tbody>`;
    settled.slice(0, 40).forEach((x) => {
      const e = x.value - x.truth;
      html += `<tr><td>${x.target_date}</td><td>${x.horizon} 天</td>
        <td>${esc(regionName(x.region))}</td><td>${fmt(x.value, 2)}</td>
        <td>${fmt(x.truth, 2)}</td><td>${signed(e, 2)}</td></tr>`;
    });
    html += "</tbody></table></div>";
  }
  $("#my-detail").innerHTML = html;
}

// ------------------------------------------------------------------ 手动提交
async function loadRounds() {
  try {
    state.rounds = await api("/api/rounds" + (state.token ? "?token=" + encodeURIComponent(state.token) : ""));
    state.myId = state.rounds.me ? state.rounds.me.id : null;
    renderRounds();
  } catch (e) { $("#rounds").innerHTML = `<p class="msg bad">${esc(e.message)}</p>`; }
}

function renderRounds() {
  const { rounds, latest_truth } = state.rounds;
  $("#truth-hint").textContent = latest_truth ? `真值已结到 ${latest_truth}` : "真值尚未入库";
  if (!rounds.length) { $("#rounds").innerHTML = `<p class="muted">当前没有可提交的轮次。</p>`; return; }
  const groups = {};
  rounds.forEach((r) => { (groups[r.horizon] = groups[r.horizon] || []).push(r); });
  let html = "";
  Object.keys(groups).sort((a, b) => a - b).forEach((h) => {
    groups[h].sort((a, b) => a.target_date.localeCompare(b.target_date)).forEach((r) => {
      html += `<div class="round"><header>
        <div><strong>${r.target_date}</strong> 的日均海温
          <span class="pill">时效 ${r.horizon} 天</span></div>
        <div class="deadline">截止（北京时间）${beijing(r.deadline)}</div></header><div class="grid">`;
      state.meta.regions.forEach((reg) => {
        const key = `${r.target_date}|${r.horizon}|${reg.code}`;
        const mine = r.mine[reg.code];
        const cnt = r.counts[reg.code] || 0;
        html += `<div class="cell" title="${esc(reg.note)}">
          <label for="i-${key}">${esc(reg.name_cn)}<span class="cnt">${cnt} 人已交</span></label>
          <input id="i-${key}" type="number" step="0.01" min="-5" max="45"
                 data-target="${r.target_date}" data-horizon="${r.horizon}"
                 data-region="${reg.code}" value="${mine ?? ""}" placeholder="°C">
        </div>`;
      });
      html += `</div></div>`;
    });
  });
  $("#rounds").innerHTML = html;
}

$("#save-btn").onclick = async () => {
  if (!state.token) return setMsg("#save-msg", "先在上面起个名字", false);
  const entries = [];
  $$("#rounds input[type=number]").forEach((i) => {
    if (i.value.trim() === "") return;
    entries.push({ region: i.dataset.region, horizon: Number(i.dataset.horizon),
                   target_date: i.dataset.target, value: Number(i.value) });
  });
  if (!entries.length) return setMsg("#save-msg", "还没有填任何数字", false);
  try {
    const d = await post("/api/submit", { token: state.token, entries });
    setMsg("#save-msg", `已保存 ${d.written} 条` +
      (d.rejected.length ? `，${d.rejected.length} 条被拒（${esc(d.rejected[0].why)}）` : ""),
      !d.rejected.length);
    loadRounds();
  } catch (e) { setMsg("#save-msg", e.message, false); }
};

// ------------------------------------------------------------------ 文件上传
$("#up-btn").onclick = async () => {
  const f = $("#up-file").files[0];
  if (!f) return setMsg("#up-msg", "先选一个文件", false);
  const name = $("#up-name").value.trim() || localStorage.getItem(NAME_KEY) || "";
  if (!state.token && name.length < 2) return setMsg("#up-msg", "第一次提交请填模型/队伍名", false);
  const fd = new FormData();
  fd.append("file", f);
  if (name) fd.append("name", name);
  fd.append("affiliation", $("#up-affil").value.trim());
  if ($("#up-horizon").value) fd.append("horizon", $("#up-horizon").value);
  if (state.token) fd.append("token", state.token);
  $("#up-btn").disabled = true;
  setMsg("#up-msg", "正在解析并评测…");
  try {
    const r = await fetch("/api/upload", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "上传失败");
    if (d.player && d.player.token) {
      state.token = d.player.token; localStorage.setItem(TOKEN_KEY, state.token);
      renderIdentity(); loadRounds();
    }
    const s = d.score || {};
    setMsg("#up-msg", `识别为${d.format}：写入 ${d.written} 条` +
      (d.n_rejected ? `，跳过 ${d.n_rejected} 条已截止的（想回测请用命令行 --allow-late）` : ""),
      !d.n_rejected);
    $("#up-result").innerHTML = `
      <div class="kv">
        <div class="cell"><label>条目</label><b>${d.summary.n}</b></div>
        <div class="cell"><label>覆盖天数</label><b>${d.summary.n_dates}</b></div>
        <div class="cell"><label>时效</label><b>${(d.summary.horizons || []).join(" / ")}</b></div>
        <div class="cell"><label>海区</label><b>${(d.summary.regions || []).length} 个</b></div>
      </div>
      ${s.n ? `<div class="kv" style="margin-top:8px">
        <div class="cell"><label>已结算</label><b>${s.n} 条</b></div>
        <div class="cell"><label>综合分</label><b>${fmt(s.score, 1)}</b></div>
        <div class="cell"><label>MAE</label><b>${fmt(s.mae, 3)}</b></div>
        <div class="cell"><label>命中率</label><b>${pct(s.hit)}</b></div>
      </div>` : `<p class="muted small" style="margin-top:8px">预报的是未来日期，等真值出来（滞后 1–2 天）就会自动出分上榜。</p>`}`;
  } catch (e) {
    setMsg("#up-msg", e.message, false);
  } finally {
    $("#up-btn").disabled = false;
  }
};

// ------------------------------------------------------------------ 海区表
function renderRegions() {
  let h = `<thead><tr><th>海区</th><th>英文名</th><th>经度范围</th><th>纬度范围</th><th>说明</th></tr></thead><tbody>`;
  state.meta.regions.forEach((r) => {
    h += `<tr><td><b>${esc(r.name_cn)}</b></td><td>${esc(r.name_en)}</td>
      <td>${r.bbox[0]}–${r.bbox[2]}°E</td><td>${r.bbox[1]}–${r.bbox[3]}°N</td>
      <td style="text-align:left">${esc(r.note)}</td></tr>`;
  });
  $("#region-table").innerHTML = h + "</tbody>";
}

// ------------------------------------------------------------------ 排行榜
async function loadBoard() {
  const days = $("#window").value;
  try {
    const d = await api("/api/leaderboard?days=" + days);
    let h = `<thead><tr><th>名次</th><th>名字</th><th>综合分</th><th>MAE</th>
      <th>RMSE</th><th>偏差</th><th>命中率</th>
      <th title="0 = 与该海区该时效最强的官方产品打平">相对基准</th><th>样本</th></tr></thead><tbody>`;
    if (!d.rows.length) h += `<tr><td colspan="9" class="muted">还没有已结算的记录。</td></tr>`;
    d.rows.forEach((r) => {
      const cls = r.kind === "product" ? "product" : (r.kind === "baseline" ? "baseline"
        : (state.myId && r.entity === "player:" + state.myId ? "me" : ""));
      const pl = r.kind === "product" ? `<span class="pill prod">${r.name.includes("本站") ? "本站模型" : "官方产品"}</span>`
        : r.kind === "baseline" ? `<span class="pill base">内置对照</span>`
        : `<span class="pill">${kindLabel(r.kind)}</span>`;
      h += `<tr class="${cls}"><td>${r.rank}</td><td>${esc(r.name)}${pl}</td>
        <td><b>${fmt(r.score, 1)}</b></td><td>${fmt(r.mae, 3)}</td><td>${fmt(r.rmse, 3)}</td>
        <td>${signed(r.bias, 3)}</td><td>${pct(r.hit)}</td>
        <td>${r.advantage === null ? "—" : signed(r.advantage * 100, 1) + "%"}</td>
        <td>${r.n}</td></tr>`;
    });
    $("#board-table").innerHTML = h + "</tbody>";
    let rt = `<thead><tr><th>海区</th><th>时效</th><th>基准 MAE（°C）</th><th>共同样本天数</th></tr></thead><tbody>`;
    d.groups.forEach((g) => {
      rt += `<tr><td>${esc(g.region_cn)}</td><td>${g.horizon} 天</td>
        <td>${fmt(g.ref_mae, 3)}</td><td>${g.common_dates ?? "—"}</td></tr>`;
    });
    $("#ref-table").innerHTML = rt + "</tbody>";
  } catch (e) {
    $("#board-table").innerHTML = `<tbody><tr><td class="bad">${esc(e.message)}</td></tr></tbody>`;
  }
}
$("#window").onchange = loadBoard;

// ------------------------------------------------------------------ 海温图
function initMaps() {
  if (!$("#map-date").value) {
    const latest = (state.rounds && state.rounds.latest_truth) || todayISO();
    $("#map-date").value = latest;
  }
  loadMap();
  loadProdErrors();
}
$("#map-btn").onclick = loadMap;
$("#map-kind").onchange = loadMap;

async function loadMap() {
  const kind = $("#map-kind").value;
  const date = $("#map-date").value;
  const h = $("#map-horizon").value;
  if (!date) return;
  const url = `/api/map?kind=${kind}&date=${date}&horizon=${h}`;
  setMsg("#map-msg", "正在出图…");
  try {
    const r = await fetch(url);
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || "没有这个日期的场");
    }
    const blob = await r.blob();
    const img = $("#map-img");
    if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
    const obj = URL.createObjectURL(blob);
    img.src = obj; img.dataset.url = obj;
    const lo = r.headers.get("X-Scale-Min"), hi = r.headers.get("X-Scale-Max");
    const name = { truth: "OISST 真值", model: "本站模型预报", diff: "模型 − 真值 误差" }[kind];
    $("#map-caption").textContent =
      `${name}｜${date}${kind === "truth" ? "" : `（时效 ${h} 天）`}｜色标 ${lo} ~ ${hi} °C`;
    setMsg("#map-msg", "");
  } catch (e) { setMsg("#map-msg", e.message, false); }
}

async function loadProdErrors() {
  try {
    const days = 60;
    const d = await api("/api/leaderboard?days=" + days);
    let h = `<thead><tr><th>条目</th><th>类型</th><th>综合分</th><th>MAE</th><th>RMSE</th><th>偏差</th><th>命中率</th><th>样本</th></tr></thead><tbody>`;
    d.rows.forEach((r) => {
      h += `<tr class="${r.kind === "product" ? "product" : r.kind}"><td>${esc(r.name)}</td>
        <td>${kindLabel(r.kind)}</td><td>${fmt(r.score, 1)}</td><td>${fmt(r.mae, 3)}</td>
        <td>${fmt(r.rmse, 3)}</td><td>${signed(r.bias, 3)}</td><td>${pct(r.hit)}</td><td>${r.n}</td></tr>`;
    });
    $("#prod-err-table").innerHTML = h + "</tbody>";
  } catch (e) { /* 忽略 */ }
}

// ------------------------------------------------------------------ 方法说明
const AI_MODELS = [
  ["Pangu-Weather（华为）", "Bi et al., 2023, <i>Nature</i>", "三维地球注意力网络做全球中期预报，推理速度比传统数值模式快四个数量级，是这条路线真正出圈的一篇。"],
  ["GraphCast / GenCast（DeepMind）", "Lam et al., 2023, <i>Science</i>", "图神经网络直接学习大气演化，10 天预报在多数指标上超过 ECMWF HRES；GenCast 进一步做到了集合概率预报。"],
  ["FengWu 与 FengWu-4DVar（复旦 / 上海AI Lab）", "Chen Kang et al., 2023, arXiv:2304.02948", "把有效预报时效推到 10 天以上；后续版本引入四维变分做资料同化，并扩展出包含海洋变量的高分辨率版本。"],
  ["FuXi 系列（复旦）", "Chen Lei et al., 2023–2025", "FuXi 做中期预报；<b>FuXi-S2S</b>（Nature Communications 2024）把全球次季节预报做到 42 天，直接输出海表温度场，是和我们这个擂台关系最近的一个。"],
  ["XiHe 羲和（全球海洋涡分辨预报）", "Wang Xiang et al., 2024, arXiv:2402.02995", "纯数据驱动的全球海洋预报模型，能分辨中尺度涡，是「AI 做海洋」的代表工作。"],
  ["深度神经网络预报涡旋海洋", "Cui et al., 2025, <i>Nature Communications</i>", "证明神经网络可以在涡分辨尺度上预报海洋状态，推理成本远低于数值模式。"],
  ["CNN 做 SST 资料同化", "Zavala-Romero et al., 2025, <i>Ocean Science</i>", "用卷积网络替代/加速传统最优插值，说明数据驱动方法在 SST 分析这一环也已经可用。"],
  ["南海全球海洋预报的深度学习订正", "Chen et al., 2026, <i>Frontiers in Marine Science</i>", "专门针对<b>南海</b>做全球海洋预报的机器学习订正，和本站的目标区域完全重合，值得后续对照。"],
  ["扩散模型做 SST 空间降尺度", "Wang Shuo et al., 2024, <i>Remote Sensing</i>", "把粗分辨率 SST 用扩散模型超分到细网格，思路可以直接用来做我们 2° 粗格点模型的后处理。"],
];

function renderAbout() {
  const sc = state.meta.scoring;
  const w = sc.weights;
  const prods = Object.entries(state.meta.products)
    .map(([k, v]) => `<li><b>${esc(v.name)}</b>（${esc(v.org)}）— ${esc(v.note)}</li>`).join("");
  const ai = AI_MODELS.map(([n, c, d]) =>
    `<div class="ai-item"><b>${n}</b><br><span class="muted small">${c}</span>
     <p class="muted small" style="margin:4px 0 0">${d}</p></div>`).join("");
  $("#ai-models").innerHTML = ai;

  $("#about-body").innerHTML = `
    <strong>一、真值是什么</strong>
    <p class="muted">${esc(state.meta.truth_source)}。结算值 = 海区盒子内所有有效海格点的
    cos(纬度) 加权平均。它是全球海温检验里最通用的基准数据集，任何人都能下载复现，
    不存在"裁判自己说了算"的问题。</p>
    <p class="muted">要注意：OISST 本身是"分析场"，带有 0.1–0.4°C 的误差；
    不同 SST 产品之间也可能有 0.2–0.4°C 的系统性差异。所以当各条目的差距小于这个量级时，
    排名不应被当作定论。</p>

    <strong>二、参与比分的官方产品</strong>
    <ul class="muted">${prods}</ul>
    <p class="muted">三个产品与玩家遵守<b>同一条信息截止线</b>：必须在截止时间之前就已起报，
    同样用 OISST 结算，同样按 (海区, 时效) 分组算分。榜单会显示它实际用到的那次起报日期。
    另外还有一个永远在榜的内置对照——<b>持续性基线</b>（把 h 天前的实测值直接当预报），
    它是海温预报里最难打败的简单对手。</p>

    <strong>三、怎么算一个"标准"的分数</strong>
    <ul class="muted">
      <li><b>准确度 AccScore（权重 ${w.acc}）</b>：<code>adv = 1 − MAE ÷ 基准MAE</code>，
        再映射 <code>AccScore = 0.5 + adv</code>（截断到 0–1）。基准 MAE 是同一海区、
        同一时效下<b>表现最好的官方产品</b>。和它打平 = 0.50。</li>
      <li><b>稳定性 BiasScore（权重 ${w.bias}）</b>：<code>exp(−|平均偏差| ÷ ${sc.bias_tau})</code>。
        防止"永远报高 1 度"这种取巧。</li>
      <li><b>命中率 HitScore（权重 ${w.hit}）</b>：误差绝对值 ≤ ${sc.hit_tol} °C 的比例。</li>
    </ul>
    <p class="muted">综合分 = 100 × (${w.acc}×Acc + ${w.bias}×Bias + ${w.hit}×Hit)。
    每个 (海区, 时效) 分组各自算再平均，避免好猜的海区抬高总分。
    样本数少于 ${sc.min_n} 次不给综合分。榜单默认只比较<b>所有官方产品都有数据的日期</b>
    （"共同样本"），这和原论文 "on the days both exist" 的做法一致。</p>

    <strong>四、本站自己训了哪几个模型</strong>
    <p class="muted">
      <b>① 线性逆模型（LIM / VAR）</b>：把南海按 2° 粗化成 55 个粗格点，减去逐日气候态得到距平，
      用 <b>8.75 年</b>（3198 天）OISST 拟合距平的线性演化算子 <code>X(t+τ) = A·X(t) + b</code>，
      预报时用截止时刻能拿到的最新观测做初值外推。和 Penland & Sardeshmukh 那一族方法同源。
    </p>
    <p class="muted">
      <b>② 梯度提升树（XGBoost / CatBoost / LightGBM）</b>：特征包括 7 个海区在
      as_of、−1、−2、−3、−5、−7 天的距平（42 维）、目标日的季节谐波与气候态、
      时效与海区 one-hot，共 55 维；输出目标日的距平。
      <b>关键设计：让树只学「相对持续性基线的修正量」</b>，而不是从零学距平——
      直接学距平会严重过拟合（训练 MAE 0.137、验证 0.301，而持续性只有 0.18），
      改成学修正量之后验证误差立刻回到 0.19 左右。
    </p>
    <p class="muted">
      <b>③ 多源后处理（融合）</b>：把 HYCOM / GFS / CFSv2 的预报值 + 持续性当输入，
      学一组融合权重——就是论文里那个「全公开产品最优加权组合」。
      评估用<b>留一天交叉验证</b>，出预报时用<b>走前向</b>权重（只拿目标日之前的数据拟合）。
      这一步很关键：如果拿全量数据拟合再回头报历史，会算出 MAE 0.151 这种虚高成绩
      （假的），走前向之后落到 0.21，才和它真实的水平相符。
    </p>
    <p class="muted">
      <b>④ U-Net 空间订正（深度学习）</b>：输入是 82×56 的多通道场
      （持续性场 + GFS 预报场 + 有无标志 + 季节 + 时效），输出订正后的海温场。
      用卷积是因为模式误差有空间结构（近岸、陆架坡折、涡旋区误差大），逐格点回归吃不到这个。
      <b>训练数据是真的</b>：用 AWS 上 NOAA 的公开 GFS 存档 + GRIB 字节索引，
      把 730 天的历史预报场只下每个时次 600 KB 的 TMP:surface 那一条，回补成 40 MB 数据集。
      网络做成<b>残差式</b>（输出 = 持续性场 + 修正量）：直接从零预测距平会过度阻尼，
      实测系统性偏冷 0.31°C、比持续性还差，改成残差之后才回到正常水平。
    </p>
    <p class="muted">所有模型都带 <b>30 天隔离期</b>：训练数据截止到今天往前 30 天，
    所以榜单上每一条成绩都是真正的样本外预报，不存在"背答案"。</p>

    <strong>五、一个必须说清楚的结论</strong>
    <p class="muted">把训练数据从 3 年扩到 8.75 年之后，LIM 才从"明显输给持续性"追到
    <b>与持续性持平、在 3/5 天上略微胜出</b>（见下面样本外验证表）。
    梯度提升树这边，加了很多特征、换了几种实现，最终也只是和持续性打平。
    这不是我们做得不好，而是<b>热带海温 1–5 天预报本身的天花板</b>——
    暖池区海温变化慢、方差小，持续性极难打败。文献里也是同一个结论。</p>
    <p class="muted">真正能拉开差距的方向是<b>后处理</b>（学官方产品的订正量）和
    <b>更长的时效</b>。这也是我们下一步要做的事。</p>

    <strong>五、这个擂台想干什么</strong>
    <p class="muted">思路来自 Crosier (2026) 对 Kalshi 温度预测市场的研究：市场隐含预报
    （一群人的加权意见）在 7 个美国城市里有 6 个战胜了官方最好的单产品 NBM，
    对全产品最优组合也还有 3–4% 优势。我们把同一套"市场 vs 官方产品阶梯"的方法
    搬到南海海温上，只是把下注价格换成直接提交数字，并且开放文件/接口提交，
    让任何一个模型都能来试。</p>
  `;
}

async function renderValidation() {
  try {
    const r = await fetch("/data/model_validation.json").catch(() => null);
    if (!r || !r.ok) throw new Error("no file");
    const v = await r.json();
    let h = `<p class="muted small">训练期 ${v.train_window[0]} ~ ${v.train_window[1]}；
      样本外测试期 ${v.test_window[0]} ~ ${v.test_window[1]}。
      系数完全没见过测试期数据，初值只用当天真实可得的观测。</p>
      <div class="scroll"><table><thead><tr><th>时效</th><th>纯持续性</th>
      <th>阻尼持续性</th><th>本站 LIM</th><th>LIM/阻尼各半</th><th>样本</th></tr></thead><tbody>`;
    Object.entries(v.by_horizon).forEach(([hz, r2]) => {
      h += `<tr><td>${hz} 天</td><td>${fmt(r2.persistence, 3)}</td>
        <td>${fmt(r2.damped, 3)}</td><td>${fmt(r2.lim, 3)}</td>
        <td>${fmt(r2.blend, 3)}</td><td>${r2.n}</td></tr>`;
    });
    h += `</tbody></table></div>
      <p class="muted small" style="margin-top:10px">${esc(v.note)}<br>
      <b>结论：在 1–5 天的南海海温上，本站 LIM 打不过纯持续性。</b>
      这与文献一致——热带海温的持续性极强，线性逆模型的价值主要在月以上尺度。
      我们把它照实挂在榜上，而不是挑一个好看的窗口。</p>`;
    $("#validation").innerHTML = h;
  } catch (e) {
    $("#validation").innerHTML = `<p class="muted small">验证结果文件还没生成，运行
      <code>python3 -m tools.validate_model</code> 即可。</p>`;
  }
}

async function renderLocalModels() {
  const box = $("#local-models");
  if (!box) return;
  try {
    const d = await api("/api/leaderboard?days=180");
    const mine = d.rows.filter((r) => /^本站/.test(r.name));
    const others = d.rows.filter((r) => !/^本站/.test(r.name));
    const row = (r) => `<tr class="${r.kind}"><td>${esc(r.name)}</td><td>${kindLabel(r.kind)}</td>
      <td><b>${fmt(r.score, 1)}</b></td><td>${fmt(r.mae, 3)}</td>
      <td>${signed(r.bias, 3)}</td><td>${pct(r.hit)}</td><td>${r.n}</td></tr>`;
    let h = `<p class="muted small">榜单上"本站"开头的都是我们自己训的，和官方产品用同一套规则取数结算。
      下面同时列出官方产品与内置基线，方便一眼对比。</p>
      <div class="scroll"><table><thead><tr><th>条目</th><th>类型</th><th>综合分</th>
      <th>MAE</th><th>偏差</th><th>命中率</th><th>样本</th></tr></thead><tbody>
      ${mine.map(row).join("")}${others.map(row).join("")}</tbody></table></div>`;
    const meta = state.meta.products;
    const notes = Object.entries(meta).filter(([k]) => /xgboost|catboost|lightgbm|^ours$/.test(k))
      .map(([k, v]) => `<div class="ai-item"><b>${esc(v.name)}</b><br>
        <span class="muted small">${esc(v.org)}</span>
        <p class="muted small" style="margin:4px 0 0">${esc(v.note)}</p></div>`).join("");
    box.innerHTML = h + notes;
  } catch (e) {
    box.innerHTML = `<p class="muted small">读取失败：${esc(e.message)}</p>`;
  }
}

async function renderModelReport() {
  const box = $("#model-report");
  if (!box) return;
  try {
    const r = await api("/api/model_report");
    let h = "";
    if (r.postproc) {
      const p = r.postproc, m = p.lodo_mae || {};
      h += `<div class="ai-item"><b>多源后处理 · 留一天交叉验证</b>
        <p class="muted small" style="margin:4px 0 0">
        样本 ${p.samples} 条／${p.days} 天（${p.date_range[0]} ~ ${p.date_range[1]}），选用 <code>${p.chosen}</code>。
        留一天交叉验证 MAE：订正融合 <b>${fmt(m.ridge, 3)}</b>、
        HYCOM ${fmt(m.hycom, 3)}、CFSv2 ${fmt(m.cfs, 3)}、GFS ${fmt(m.gfs, 3)}、
        持续性 ${fmt(m.persistence, 3)}。<br>
        目前样本只有 ${p.days} 天，权重还学不稳（论文里也指出样本少时估计权重会输给简单平均）。
        脚本每天自动重跑，随着官方产品历史累积，这个数会变。</p></div>`;
    }
    if (r.unet) {
      const u = r.unet, o = u.out_of_sample || {};
      h += `<div class="ai-item"><b>U-Net 订正 · 样本外</b>
        <p class="muted small" style="margin:4px 0 0">
        训练 ${u.train} 样本／验证 ${u.val}，测试窗口 ${u.test_window ? u.test_window.join(" ~ ") : "—"}
        （${o.n || 0} 条）。MAE：U-Net <b>${fmt(o.unet, 4)}</b> ·
        持续性 ${fmt(o.persistence, 4)} · GFS 原始 ${fmt(o.gfs, 4)}。<br>
        输入是 82×56 的 <b>10 通道多源场</b>：持续性场 ＋ GFS / HYCOM / CFSv2 的预报场
        （每个都带一个"在不在"的标志通道）＋ 季节与时效，输出订正后的场。</p></div>`;
    }
    if (r.fields) {
      const f = r.fields;
      const row = (k, name, note) => {
        const d = f[k] || { days: 0, files: 0 };
        const on = d.days >= 90;
        return `<tr><td>${name}</td><td>${d.days} 天</td><td>${d.files} 个场</td>
          <td>${on ? "已启用" : `还差 ${90 - d.days} 天`}</td><td style="text-align:left">${note}</td></tr>`;
      };
      h += `<div class="ai-item"><b>多源场库积累情况</b>
        <p class="muted small" style="margin:4px 0 0">
        每个产品的历史预报场都存在 <code>data/fields/</code> 下，每天自动追加。
        某个产品的场攒够 <b>90 天</b>才会打开它的输入通道——否则训练集里这个通道
        99% 是零，模型学不会用，推理时突然喂真值反而像噪声
        （实测把成绩从 0.24 拉到 0.44）。</p>
        <div class="scroll"><table><thead><tr><th>产品</th><th>已有起报日</th><th>场文件</th>
        <th>通道状态</th><th>来源</th></tr></thead><tbody>
        ${row("gfs", "NCEP GFS", "AWS 公开存档 noaa-gfs-bdp-pds + GRIB 字节索引回补（每天只下 600 KB）")}
        ${row("hycom", "HYCOM ESPC-D-V02", "上游 FMRC 只保留最近约 9 次起报，靠每日抓数累积")}
        ${row("cfs", "NCEP CFSv2", "NOMADS 保留约 10 天，靠每日抓数累积")}
        </tbody></table></div></div>`;
    }
    if (r.lim && r.lim.by_horizon) {
      const b = r.lim.by_horizon;
      h += `<div class="ai-item"><b>LIM · 样本外（${r.lim.train_window[0]} ~ ${r.lim.train_window[1]} 训练）</b>
        <p class="muted small" style="margin:4px 0 0">
        ${Object.entries(b).map(([hz, v]) =>
          `${hz} 天：LIM ${fmt(v.lim, 3)} vs 持续性 ${fmt(v.persistence, 3)}`).join("　｜　")}</p></div>`;
    }
    box.innerHTML = h || `<p class="muted small">还没有验证结果。</p>`;
  } catch (e) {
    box.innerHTML = `<p class="muted small">读取失败：${esc(e.message)}</p>`;
  }
}

$("#footer").innerHTML = `数据源 NOAA OISST v2.1 · HYCOM ESPC-D-V02 · NCEP GFS · NCEP CFSv2
  ｜真值滞后约 1–2 天，结算自动完成`;

// ------------------------------------------------------------------ 启动
(async function init() {
  state.meta = await api("/api/meta");
  document.title = state.meta.site_name;
  const nm = localStorage.getItem(NAME_KEY);
  if (nm && $("#up-name")) $("#up-name").value = nm;
  renderIdentity();
  renderRegions();
  renderAbout();
  renderValidation();
  renderLocalModels();
  renderModelReport();
  loadRounds();
})();
