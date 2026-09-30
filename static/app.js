"use strict";
const $ = (id) => document.getElementById(id);
let mode = "demo",
  state = null,
  hydrated = "",
  pendingAction = null,
  loggedIn = false,
  refreshing = false;
let toastTimer;
const money = (n) =>
  Number.isFinite(Number(n))
    ? Number(n).toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })
    : "—";
const number = (n) =>
  Number(n).toLocaleString("en-US", { maximumFractionDigits: 8 });
const date = (t) =>
  new Date(t * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
const escape = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").className = error ? "error" : "";
  $("toast").hidden = false;
  toastTimer = setTimeout(() => ($("toast").hidden = true), 7000);
}
async function api(path, body) {
  const response = await fetch("/api/" + path, {
    method: body === undefined ? "GET" : "POST",
    headers:
      body === undefined
        ? {}
        : { "Content-Type": "application/json", "X-Desk-Request": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("服务暂不可用，请检查主机");
  }
  if (!response.ok) {
    if (response.status === 401 && path !== "login") {
      loggedIn = false;
      if (!$("login-dialog").open) $("login-dialog").showModal();
    }
    throw new Error(data.message || data.detail || "请求失败");
  }
  return data;
}
function drawChart(id, points, key, color) {
  const svg = $(id),
    width = id === "price-chart" ? 900 : 1100,
    height = id === "price-chart" ? 240 : 180;
  svg.replaceChildren();
  if (points.length < 2) return;
  const ns = "http://www.w3.org/2000/svg";
  const values = points.map((p) => Number(p[key]));
  let lo = Math.min(...values),
    hi = Math.max(...values);
  const range = hi - lo || hi * 0.01 || 1;
  lo -= range * 0.15;
  hi += range * 0.15;
  const left = 4,
    right = 70,
    top = 12,
    bottom = 14;
  const y = (v) => top + ((hi - v) / (hi - lo)) * (height - top - bottom);
  const add = (tag, attrs, text) => {
    const n = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text !== undefined) n.textContent = text;
    svg.append(n);
    return n;
  };
  for (let i = 0; i < 4; i++) {
    const value = lo + ((hi - lo) * i) / 3;
    add("line", {
      x1: left,
      x2: width - right,
      y1: y(value),
      y2: y(value),
      stroke: "#2a3038",
      "stroke-dasharray": "3 5",
    });
    add("text", { x: width - right + 10, y: y(value) + 4 }, money(value));
  }
  const d = points
    .map(
      (p, i) =>
        `${i ? "L" : "M"}${(left + (i / (points.length - 1)) * (width - right - left)).toFixed(2)},${y(p[key]).toFixed(2)}`,
    )
    .join(" ");
  add("path", {
    d:
      d + ` L${width - right},${height - bottom} L${left},${height - bottom} Z`,
    fill: color,
    opacity: ".05",
  });
  add("path", {
    d,
    fill: "none",
    stroke: color,
    "stroke-width": "2",
    "vector-effect": "non-scaling-stroke",
    "stroke-linejoin": "round",
  });
}
function plan() {
  return {
    mode,
    pair_mode: $("pair-mode").value,
    pair: $("plan-pair").value,
    top_n: Number($("top-n").value),
    budget: Number($("budget").value),
    max_loss: Number($("max-loss").value),
    hours: Number($("hours").value),
    max_position_pct: Number($("position-pct").value),
    order_quote: Number($("order-quote").value),
    max_orders_day: Number($("orders-day").value),
    strategy: $("strategy").value,
    llm_provider: provider(),
    llm_model: $("llm-model").value.trim(),
  };
}
const PROVIDERS = {
  openrouter: {
    name: "OpenRouter",
    hint: "provider/model",
    placeholder: "例如 x-ai/grok-4.5",
    note: "在 openrouter.ai/models 复制模型 ID。每根小时 K 线最多询问一次，模型只能回答买入、卖出或持有，金额与风控仍由程序决定。模型费用由 OpenRouter 另计。",
    keyNote:
      "建议在 OpenRouter 为本程序单独创建一个 Key，并设置额度上限（credit limit），避免费用失控。",
  },
  typesafe: {
    name: "TypeSafe",
    hint: "例如 jev-latest",
    placeholder: "jev-latest",
    note: "Jev 只从买入、卖出、持有中选一个，并给出各选项的概率，不给文字理由。每根小时 K 线最多询问一次，金额与风控仍由程序决定。费用按 TypeSafe 账单另计。",
    keyNote:
      "请在 TypeSafe 官方控制台为本程序单独创建 Key；不要使用第三方网站提供的 Key 或转发地址。",
  },
};
const provider = () => $("llm-provider").value;
const strategyName = (p) =>
  p.strategy === "llm"
    ? "LLM " + p.llm_model
    : p.strategy === "rsi"
      ? "RSI 回归"
      : "区间位置";
const coinsName = (p) =>
  p.pair_mode === "rotate" ? `轮动（雷达前 ${p.top_n} 名）` : p.pair;
const compact = (n) => {
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return v.toFixed(0);
};
const signedPct = (n) =>
  n === null || n === undefined
    ? "—"
    : `<span class="${n > 0 ? "positive" : n < 0 ? "negative" : ""}">${n > 0 ? "+" : ""}${Number(n).toFixed(1)}%</span>`;
function setOptions(id, pairs) {
  const select = $(id),
    current = select.value;
  const list = !current || pairs.includes(current) ? pairs : [current, ...pairs];
  if (!list.length || select.dataset.list === list.join()) return;
  select.dataset.list = list.join();
  select.replaceChildren(
    ...list.map((pair) => {
      const option = document.createElement("option");
      option.textContent = pair;
      return option;
    }),
  );
  select.value = current && list.includes(current) ? current : list[0];
}
function ensureOption(id, pair) {
  if (![...$(id).options].some((o) => o.value === pair)) {
    const option = document.createElement("option");
    option.textContent = pair;
    $(id).prepend(option);
    $(id).dataset.list = "";
  }
  $(id).value = pair;
}
function syncPairMode() {
  const rotate = $("pair-mode").value === "rotate";
  $("plan-pair-label").hidden = rotate;
  $("top-n-label").hidden = !rotate;
  $("rotate-note").hidden = !rotate;
}
let scannerAsOf = null,
  scannerParamsLoaded = false;
function renderScannerSummary(s) {
  if (!s) return;
  if (!scannerParamsLoaded && s.params) {
    scannerParamsLoaded = true;
    $("scan-volume").value = s.params.min_volume;
    $("scan-spread").value = s.params.max_spread_pct;
    $("scan-age").value = s.params.min_age_days;
    $("scan-vol").value = s.params.max_vol_pct;
  }
  $("scanner-refresh").disabled = s.running;
  const last = s.as_of
    ? `更新于 ${date(s.as_of)} · ${s.count} 个币通过筛选（OKX 共 ${s.listed} 个现货交易对）`
    : "尚未扫描。只读取 OKX 公开行情，不会下任何单。";
  $("scanner-summary").textContent = s.running
    ? `扫描中 ${s.done}/${s.total || "…"} · 只读取 OKX 公开行情，不会下单`
    : s.error
      ? `${s.error}。${s.as_of ? "下面仍是上一次的结果：" + last : ""}`
      : s.as_of
        ? `${last} · 程序运行时每小时自动刷新`
        : last;
  if (s.as_of && s.as_of !== scannerAsOf) loadScanner();
}
async function loadScanner() {
  try {
    const d = await api("scanner");
    scannerAsOf = d.result?.as_of ?? null;
    renderScanner(d.result);
  } catch (e) {
    toast(e.message, true);
  }
}
function renderScanner(r) {
  if (!r) return;
  const excluded = Object.entries(r.excluded || {})
    .map(([k, v]) => `${k} ${v}`)
    .join(" · ");
  $("scanner-excluded").textContent = excluded ? `已排除：${excluded}` : "";
  $("scanner-rows").innerHTML = r.rows.length
    ? r.rows
        .map(
          (x) =>
            `<tr><td>${x.rank}</td><td><strong>${escape(x.base)}</strong> <span class="muted">USDT</span></td><td>${number(x.price)}</td><td>${compact(x.volume_24h)}</td><td>${Number(x.spread_pct).toFixed(3)}%</td><td>${signedPct(x.mom7_pct)}</td><td>${signedPct(x.mom28_pct)}</td><td>${signedPct(x.rel_btc28_pct)}</td><td>${x.trend_up ? '<span class="positive">均线上方</span>' : '<span class="muted">均线下方</span>'}</td><td>${Number(x.vol_pct).toFixed(0)}%</td><td class="muted">${x.market_cap ? compact(x.market_cap) : "—"}</td><td><strong>${Number(x.score).toFixed(1)}</strong></td><td><button class="text-button" type="button" data-action="research" data-pair="${escape(x.pair)}">检验</button><button class="text-button" type="button" data-action="use" data-pair="${escape(x.pair)}">用于试验</button></td></tr>`,
        )
        .join("")
    : '<tr><td colspan="13" class="empty">没有币通过当前筛选条件。可以放宽成交额或价差后重新扫描。</td></tr>';
}
function syncStrategy() {
  const p = PROVIDERS[provider()];
  $("llm-fields").hidden = $("strategy").value !== "llm";
  $("llm-model-hint").textContent = p.hint;
  $("llm-model").placeholder = p.placeholder;
  $("llm-provider-note").textContent = p.note;
  $("llm-key").textContent = state?.llm_keys?.[provider()]
    ? `${p.name} 密钥已配置 ✓`
    : `设置 ${p.name} 密钥`;
}
function renderResearch(r) {
  if (!r) return;
  $("research-empty").hidden = true;
  $("research-result").hidden = false;
  const h = r.holdout;
  const metrics = [
    ["后段净收益", `${h.return_pct.toFixed(2)}%`],
    ["后段最大回撤", `${h.max_drawdown_pct.toFixed(2)}%`],
    [
      `${r.max_position_pct ?? 25}% 买入持有`,
      `${h.benchmark_return_pct.toFixed(2)}%`,
    ],
    ["后段成交次数", h.orders],
    ["前段净收益", `${r.development.return_pct.toFixed(2)}%`],
  ];
  $("research-metrics").innerHTML = metrics
    .map(
      ([a, b]) =>
        `<div><span>${escape(a)}</span><strong>${escape(b)}</strong></div>`,
    )
    .join("");
  drawChart("research-chart", h.curve, "equity", "#8bb5ff");
  $("research-note").textContent =
    `${r.pair} · ${r.strategy === "rsi" ? "RSI" : "区间位置"} · 试验资金 ${money(r.budget)} USDT · ${date(r.as_of)} · ${r.bars} 根 K 线。${r.note}`;
}
function render() {
  if (!state) return;
  const session = state.sessions[mode],
    isLive = mode === "live",
    connected = state.credentials[mode];
  document.querySelectorAll(".mode").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
    b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
  });
  $("mode-notice").classList.toggle("live", isLive);
  $("mode-notice").children[1].textContent = isLive
    ? "实盘 · 使用真实资金，只有你在主机启用实盘后才能运行。"
    : "模拟盘 · 使用 OKX 模拟资金，与真实账户分开。";
  $("connection-state").textContent = connected ? "凭证已配置" : "尚未连接";
  $("plan-mode").textContent = isLive ? "实盘" : "模拟盘";
  $("start").classList.toggle("danger", isLive);
  $("start").textContent = isLive
    ? state.live_enabled
      ? "由我启动实盘策略 ↗"
      : "实盘尚未在主机启用"
    : "启动模拟策略 ↗";
  $("start").disabled = !!session?.running || (isLive && !state.live_enabled);
  $("pause").disabled = !session?.running;
  $("flatten").disabled =
    !session ||
    session.quantity <= 0 ||
    !!session.pending ||
    (isLive && !state.live_enabled);
  // Server decides whether the remaining quantity is unsellable dust.
  $("finish").disabled = !session || session.running || !!session.pending;
  $("check").disabled = !session;
  $("connection").textContent = connected ? "账户连接 ✓" : "连接账户 ↗";
  setOptions("market-pair", state.tradable || []);
  setOptions("plan-pair", state.tradable || []);
  renderScannerSummary(state.scanner);
  const key = mode + ":" + (session?.id || "none");
  if (hydrated !== key) {
    hydrated = key;
    if (session) {
      const p = session.plan;
      for (const [id, k] of Object.entries({
        budget: "budget",
        "max-loss": "max_loss",
        hours: "hours",
        "position-pct": "max_position_pct",
        "order-quote": "order_quote",
        "orders-day": "max_orders_day",
        "plan-pair": "pair",
        "pair-mode": "pair_mode",
        "top-n": "top_n",
        strategy: "strategy",
        "llm-model": "llm_model",
        "llm-provider": "llm_provider",
      })) {
        const defaults = { "llm-provider": "openrouter", "pair-mode": "fixed", "top-n": 5 };
        if (id === "plan-pair") ensureOption(id, p[k] || "BTC-USDT");
        else $(id).value = p[k] ?? defaults[id] ?? "";
      }
    }
  }
  syncStrategy();
  syncPairMode();
  for (const id of ["budget", "plan-pair", "pair-mode"]) $(id).disabled = !!session;
  for (const id of [
    "max-loss",
    "hours",
    "position-pct",
    "order-quote",
    "orders-day",
    "strategy",
    "llm-provider",
    "llm-model",
    "top-n",
  ])
    $(id).disabled = !!session?.running;
  if (session) {
    const p = session.plan,
      equity = session.equity,
      pnl = equity - p.budget;
    $("stat-budget").textContent = money(p.budget);
    $("stat-equity").textContent = money(equity);
    $("stat-pnl").textContent = (pnl > 0 ? "+" : "") + money(pnl);
    $("stat-pnl").className = pnl > 0 ? "positive" : pnl < 0 ? "negative" : "";
    $("stat-loss").textContent = "−" + money(p.max_loss);
    $("pnl-note").textContent =
      `${((pnl / p.budget) * 100).toFixed(2)}% · 已计入已对账手续费`;
    const held = session.pair ?? (p.pair_mode === "rotate" ? null : p.pair);
    const dust = Object.keys(session.dust_left || {}).length;
    $("equity-note").textContent =
      `现金 ${money(session.cash)} · 持仓 ${number(session.quantity)} ${held ? held.split("-")[0] : ""}` +
      (dust ? ` · 另有 ${dust} 种零碎币已单独记录` : "");
    $("budget-note").textContent =
      (p.pair_mode === "rotate" ? `轮动 · ${held || "空仓"}` : p.pair) + " · " + strategyName(p);
    $("llm-note").hidden = p.strategy !== "llm";
    $("llm-note").textContent =
      p.llm_provider === "typesafe"
        ? `模型已询问 ${session.llm_calls || 0} 次 · 输入 ${session.llm_tokens || 0} tokens，费用按 TypeSafe 账单另计，不含在上面的盈亏里`
        : `模型已询问 ${session.llm_calls || 0} 次 · 模型费用 $${Number(session.llm_cost || 0).toFixed(4)}，由 OpenRouter 另计，不含在上面的盈亏里`;
    $("run-indicator").textContent = session.pending
      ? "等待订单对账"
      : session.running
        ? "策略运行中"
        : session.risk_stopped
          ? "亏损触发停止"
          : "已暂停";
    $("run-indicator").classList.toggle("running", session.running);
    $("decision").textContent = session.reason;
    $("expires").textContent = session.expires
      ? `期限至 ${date(session.expires)} · 到期需你重新启动`
      : "等待启动";
    $("engine-error").textContent = session.error || "";
    $("engine-error").hidden = !session.error;
    $("last-check").textContent = session.last_tick
      ? "最近检查 " + date(session.last_tick) + " · 每 60 秒检查"
      : "等待首次检查";
    $("orders-count").textContent = session.orders.length + " 笔";
    $("orders").innerHTML = session.orders.length
      ? session.orders
          .slice()
          .reverse()
          .map(
            (o) =>
              `<tr><td>${escape(date(o.ts))}</td><td class="${o.side === "buy" ? "positive" : "negative"}">${o.side === "buy" ? "买入" : "卖出"} ${escape((o.pair || "").split("-")[0])} <span class="muted">${escape(o.state)}</span></td><td>${money(o.price)}</td><td>${number(o.quantity)}</td><td>${number(o.fee)} ${escape(o.fee_ccy)}</td></tr>`,
          )
          .join("")
      : '<tr><td colspan="5" class="empty">暂无订单。等待策略，也是一种决策。</td></tr>';
  } else {
    for (const id of ["stat-budget", "stat-equity", "stat-pnl", "stat-loss"])
      $(id).textContent = "—";
    $("stat-pnl").className = "";
    $("budget-note").textContent = "等待你设定";
    $("equity-note").textContent = "仅统计本策略现金与持仓";
    $("pnl-note").textContent = "未开始试验";
    $("run-indicator").textContent = "尚未启动";
    $("run-indicator").classList.remove("running");
    $("decision").textContent = "设置试验资金并连接账户后，由你启动策略。";
    $("expires").textContent = "尚未启动 · 表单数字仅为初始示例";
    $("engine-error").hidden = true;
    $("llm-note").hidden = true;
    $("orders-count").textContent = "0 笔";
    $("orders").innerHTML =
      '<tr><td colspan="5" class="empty">暂无订单。等待策略，也是一种决策。</td></tr>';
  }
  $("host-state").textContent =
    state.sessions.demo?.running || state.sessions.live?.running
      ? "策略正在主机运行"
      : "主机在线 · 策略待机";
  $("events").innerHTML = state.events.length
    ? state.events
        .map(
          (e) =>
            `<div class="event"><time>${escape(date(e.ts))}</time><span class="event-kind">${escape({ start: "启动", pause: "暂停", fill: "成交", intent: "提交", risk: "触发限制", error: "异常", decision: "决策", research: "检验", connection: "连接", archive: "归档", advice: "模型" }[e.kind] || e.kind)}</span><span class="event-message">${escape(e.message)}</span></div>`,
        )
        .join("")
    : '<p class="empty">暂无事件</p>';
  renderResearch(state.research);
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await api("status");
    loggedIn = true;
    if ($("login-dialog").open) $("login-dialog").close();
    render();
  } catch (e) {
    if (loggedIn) toast(e.message, true);
  } finally {
    refreshing = false;
  }
}
async function market() {
  if (!loggedIn) return;
  try {
    const pair = $("market-pair").value;
    const d = await api("market?pair=" + encodeURIComponent(pair));
    if (pair !== $("market-pair").value) return;
    $("chart-empty").hidden = true;
    $("price").textContent = money(d.price);
    const pct = (d.price / d.open24h - 1) * 100;
    $("change").textContent = `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}% · 24h`;
    $("change").className = pct >= 0 ? "positive" : "negative";
    const base = pair.split("-")[0];
    $("market-name").replaceChildren(
      document.createTextNode(({ BTC: "Bitcoin", ETH: "Ethereum" })[base] || base),
      " ",
    );
    const label = document.createElement("span");
    label.textContent = pair.replace("-", " / ");
    $("market-name").append(label);
    $("market-time").textContent = new Date(d.timestamp).toLocaleTimeString(
      "zh-CN",
      { hour12: false },
    );
    drawChart("price-chart", d.candles, "close", "#b6f36b");
    if (d.candles.length) {
      $("chart-start").textContent = date(d.candles[0].ts / 1000);
      $("chart-end").textContent = date(d.candles.at(-1).ts / 1000);
    }
  } catch (e) {
    $("chart-empty").textContent = e.message;
    $("chart-empty").hidden = false;
  }
}
async function account() {
  const button = $("refresh-account");
  button.disabled = true;
  try {
    if (!state?.credentials[mode]) throw new Error("请先连接当前模式账户");
    const d = await api("account?mode=" + mode);
    $("account-note").textContent =
      `账户总权益 ${money(d.total_equity)} USD · 与试验预算分开`;
    $("holdings").innerHTML = d.holdings
      .map(
        (h) =>
          `<div class="holding"><span>${escape(h.currency)}</span><span>${number(h.available)} 可用</span></div>`,
      )
      .join("");
  } catch (e) {
    toast(e.message, true);
  } finally {
    button.disabled = false;
  }
}
function connection() {
  const live = mode === "live";
  $("credentials-title").textContent = live ? "连接实盘账户" : "连接模拟账户";
  $("credentials-note").textContent = live
    ? "连接只验证账户，不启动交易。不要授予提现权限。"
    : "请从 OKX「模拟交易」中创建 API，不能混用实盘密钥。";
  $("credentials-error").textContent = "";
  $("credentials-dialog").showModal();
}
function confirmAction(title, description, action, live = false, note = "") {
  pendingAction = action;
  $("confirm-title").textContent = title;
  $("confirm-description").textContent = description;
  $("confirm-note").textContent = note;
  $("live-confirm-label").hidden = !live;
  $("live-confirm").value = "";
  $("live-confirm").required = live;
  $("confirm-error").textContent = "";
  $("confirm-submit").classList.toggle("danger", live);
  $("confirm-dialog").showModal();
}
$("login-dialog").addEventListener("cancel", (e) => e.preventDefault());
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("login", { code: $("access-code").value.trim() });
    $("access-code").value = "";
    $("login-error").textContent = "";
    await refresh();
    market();
  } catch (err) {
    $("login-error").textContent = err.message;
  }
});
$("logout").addEventListener("click", async () => {
  await api("logout", {});
  location.reload();
});
$("connection").addEventListener("click", connection);
$("credentials-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  $("credentials-error").textContent = "";
  try {
    await api("credentials", {
      mode,
      api_key: $("api-key").value.trim(),
      api_secret: $("api-secret").value.trim(),
      passphrase: $("passphrase").value.trim(),
    });
    e.target.reset();
    $("credentials-dialog").close();
    toast("连接已验证并保存；策略仍需单独启动");
    await refresh();
    account();
  } catch (err) {
    $("credentials-error").textContent = err.message;
  } finally {
    button.disabled = false;
  }
});
document
  .querySelectorAll(".close-dialog")
  .forEach((b) =>
    b.addEventListener("click", () => b.closest("dialog").close()),
  );
document.querySelectorAll(".mode").forEach((b) =>
  b.addEventListener("click", () => {
    mode = b.dataset.mode;
    hydrated = "";
    $("holdings").replaceChildren();
    $("account-note").textContent = "点击刷新查看当前模式账户";
    render();
  }),
);
$("market-pair").addEventListener("change", market);
$("strategy").addEventListener("change", syncStrategy);
$("llm-provider").addEventListener("change", () => {
  const model = $("llm-model").value.trim();
  if (provider() === "typesafe" && (!model || model.includes("/")))
    $("llm-model").value = "jev-latest";
  if (provider() === "openrouter" && !model.includes("/"))
    $("llm-model").value = "";
  syncStrategy();
});
function openLlmDialog() {
  const p = PROVIDERS[provider()];
  $("llm-dialog-title").textContent = `设置 ${p.name} 密钥`;
  $("llm-dialog-intro").textContent =
    `密钥只发送到你自己的主机，经 ${p.name} 验证后加密保存在本机。程序只把公开行情和本试验的账本数字发给模型，不发送交易所密钥或账户信息。`;
  $("llm-dialog-note").textContent = p.keyNote;
  $("llm-error").textContent = "";
  $("llm-dialog").showModal();
}
$("llm-key").addEventListener("click", openLlmDialog);
$("llm-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  $("llm-error").textContent = "";
  try {
    const chosen = provider();
    const r = await api("llm-key", {
      provider: chosen,
      api_key: $("llm-api-key").value.trim(),
    });
    e.target.reset();
    $("llm-dialog").close();
    toast(
      chosen === "typesafe"
        ? `TypeSafe 密钥已保存 · 可用模型：${(r.models || []).join("、") || "无"}`
        : r.limit === null || r.limit === undefined
          ? "OpenRouter 密钥已保存。这个 Key 没有额度上限，建议在 OpenRouter 设置。"
          : `OpenRouter 密钥已保存 · 剩余额度 $${Number(r.limit_remaining).toFixed(2)}`,
    );
    await refresh();
  } catch (err) {
    $("llm-error").textContent = err.message;
  } finally {
    button.disabled = false;
  }
});
$("refresh-account").addEventListener("click", account);
$("plan-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if (!state?.credentials[mode]) {
    connection();
    return;
  }
  const p = plan();
  if (p.strategy === "llm" && !state.llm_keys?.[p.llm_provider]) {
    openLlmDialog();
    return;
  }
  confirmAction(
    mode === "live" ? "由你启用真实交易" : "启动 OKX 模拟策略",
    `${coinsName(p)} · ${strategyName(p)} · ${money(p.budget)} USDT 试验资金 · ${money(p.max_loss)} USDT 亏损触发线 · ${p.hours} 小时。`,
    async (confirmation) => {
      await api("start", { ...p, confirmation });
      toast("策略已启动，等待主机检查信号");
    },
    mode === "live",
    "策略没有盈利保证。到期或暂停保留持仓；价格跳变、断网和订单失败可能使亏损超过触发线。",
  );
});
$("confirm-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  try {
    if (
      !$("live-confirm-label").hidden &&
      $("live-confirm").value !== "我自行启用实盘"
    )
      throw new Error("请准确输入确认文字");
    await pendingAction($("live-confirm").value);
    $("confirm-dialog").close();
    await refresh();
  } catch (err) {
    $("confirm-error").textContent = err.message;
  } finally {
    button.disabled = false;
  }
});
$("pause").addEventListener("click", async () => {
  try {
    await api("pause", { mode });
    toast("已暂停，现有持仓保留");
    await refresh();
  } catch (e) {
    toast(e.message, true);
  }
});
$("check").addEventListener("click", async () => {
  const button = $("check");
  button.disabled = true;
  try {
    await api("check", { mode });
    await refresh();
    toast("检查完成；所有资金和期限限制仍生效");
  } catch (e) {
    toast(e.message, true);
  } finally {
    button.disabled = false;
  }
});
$("flatten").addEventListener("click", () => {
  const selected = mode;
  confirmAction(
    "卖出本策略持仓",
    "仅卖出本试验记录的持仓；成交后暂停策略。最小订单以下的零碎币可能保留。",
    async (confirmation) => {
      await api("flatten", { mode: selected, confirmation });
      toast("退出请求已处理，请核对执行记录");
    },
    selected === "live",
  );
});
$("finish").addEventListener("click", () => {
  const selected = mode;
  confirmAction(
    "归档当前试验",
    "只允许已暂停、所有订单已对账、且剩余持仓低于 OKX 最小下单量的试验归档。零碎币会写入记录，历史数据保留。",
    async () => {
      await api("finish", { mode: selected });
      hydrated = "";
      toast("试验已归档");
    },
  );
});
$("research").addEventListener("click", () => {
  if ($("pair-mode").value === "rotate") {
    toast(
      "轮动模式无法整体回测：历史上的雷达排名拿不到。请在选币雷达里对单个币点「检验」。",
      true,
    );
    return;
  }
  runResearch(plan().pair);
});
async function runResearch(pair) {
  if ($("strategy").value === "llm") {
    toast(
      "LLM 策略无法用历史回测检验：模型可能见过这些历史行情。请在模拟盘向前观察。",
      true,
    );
    return;
  }
  const button = $("research");
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = "正在读取历史行情…";
  try {
    const p = plan();
    const r = await api("research", {
      pair,
      budget: p.budget,
      max_loss: p.max_loss,
      strategy: p.strategy,
      fee_bps: Number($("fee-bps").value),
      slippage_bps: Number($("slip-bps").value),
      max_position_pct: p.max_position_pct,
      order_quote: p.order_quote,
      max_orders_day: p.max_orders_day,
    });
    renderResearch(r);
    toast("检验完成；不代表未来收益");
    await refresh();
  } catch (e) {
    toast(e.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "运行历史检验 ↗";
  }
}
$("pair-mode").addEventListener("change", syncPairMode);
$("scanner-refresh").addEventListener("click", async () => {
  try {
    await api("scanner", {
      min_volume: Number($("scan-volume").value),
      max_spread_pct: Number($("scan-spread").value),
      min_age_days: Number($("scan-age").value),
      max_vol_pct: Number($("scan-vol").value),
    });
    toast("选币雷达开始扫描，约需 1–2 分钟；扫描只读行情，不会下单");
    await refresh();
  } catch (e) {
    toast(e.message, true);
  }
});
$("scanner-rows").addEventListener("click", (e) => {
  const button = e.target.closest("button[data-pair]");
  if (!button) return;
  const pair = button.dataset.pair;
  if (button.dataset.action === "research") {
    document.querySelector(".research-panel").scrollIntoView({ behavior: "smooth" });
    runResearch(pair);
    return;
  }
  if ($("pair-mode").disabled) {
    toast("当前试验已占用选币设置；结束并归档后才能更换", true);
    return;
  }
  $("pair-mode").value = "fixed";
  syncPairMode();
  ensureOption("plan-pair", pair);
  ensureOption("market-pair", pair);
  market();
  document.querySelector(".plan-panel").scrollIntoView({ behavior: "smooth" });
  toast(`已把 ${pair} 填入运行边界；确认参数后再启动`);
});
(async () => {
  await refresh();
  if (loggedIn) market();
})();
setInterval(() => {
  if (loggedIn) refresh();
}, 10000);
setInterval(() => {
  if (loggedIn) market();
}, 60000);
