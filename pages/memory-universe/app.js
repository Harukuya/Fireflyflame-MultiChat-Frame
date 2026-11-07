// 角色的思想宇宙：星图 + 时间线 + 观测卡。
// 数据只经 universeApi；页面只读，不保存正文到 URL／localStorage；所有文字用 textContent 渲染。

import { universeApi, mode, hostBridge } from "./universe-api.js";

const $ = (s, r = document) => r.querySelector(s);
const NS = "http://www.w3.org/2000/svg";
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const MAP_CAP = 200;
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

const KIND = {
  episode: { name: "长期经历", body: "恒星", hint: "大小是重要性，光晕是角色现在还记得多清楚。" },
  pack: { name: "群临时记事", body: "星团", hint: "群里一段交流写成的短记事；私聊没有这一类。" },
  fact: { name: "人物事实", body: "定星", hint: "关于某人或某件事的事实，固定大小，不会像经历那样淡去。" },
  word: { name: "角色说过的话", body: "萤火", hint: "整理时核对过的、角色本人说过的原句。" },
  thread: { name: "悬着的事", body: "彗星", hint: "还没有结果的约定或事情，到了保留期限会离开。" },
  haze: { name: "整体印象", body: "星云", hint: "许多旧事淡成的一片整体感受。" },
};
const KIND_ORDER = ["episode", "pack", "fact", "word", "thread", "haze"];
const CLARITY_NOTE = ["每个细节都还在", "记得大概的经过", "只剩核心和感受", "只剩一句核心"];

// ---------- 状态 ----------

const S = {
  meta: null,
  base: null,        // {generatedAt, offset}：时间一律按服务器时区显示
  receivedAt: null,  // 单调计时，只用于估计下一轮查询时的服务器时间
  querySnapshot: null, // 同一分页批次冻结筛选与时间边界，后续页不重算
  anchor: null,      // 第一次读到的服务器时间，星图布局固定用它，免得每次刷新星体都挪动
  sources: [],
  kinds: [],
  query: { scope: "", kinds: new Set(), q: "", topicId: null, personId: null, range: "all", fromDate: "", toDate: "", includeUndated: false },
  labels: { topic: new Map(), person: new Map() },
  items: [],
  byId: new Map(),
  page: null,
  status: "idle",
  error: null,
  loadingMore: false,
  view: "map",
  sel: null,
  detail: null,
  detailState: "idle",
  detailError: null,
  detailMode: "remembered",
  seqItems: 0,
  seqItem: 0,
  seqMeta: 0,
  tf: { k: 1, x: 0, y: 0 },
  ignite: true,
  theme: "system",
};

// ---------- 小工具 ----------

function h(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.className = opts.class;
  if (opts.text != null) node.textContent = opts.text;
  for (const [k, v] of Object.entries(opts.attrs || {})) if (v != null && v !== false) node.setAttribute(k, v === true ? "" : v);
  if (opts.on) for (const [ev, fn] of Object.entries(opts.on)) node.addEventListener(ev, fn);
  for (const c of [].concat(children)) if (c != null && c !== false) node.append(c);
  return node;
}
function s(tag, attrs = {}, parent) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) node.setAttribute(k, v);
  if (parent) parent.append(node);
  return node;
}
function hash01(str, salt = 0) {
  let x = 2166136261 ^ salt;
  for (let i = 0; i < str.length; i++) { x ^= str.charCodeAt(i); x = Math.imul(x, 16777619); }
  x ^= x >>> 13; x = Math.imul(x, 0x5bd1e995); x ^= x >>> 15;
  return (x >>> 0) / 4294967296;
}
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const p2 = (n) => String(n).padStart(2, "0");
const WEEK = ["日", "一", "二", "三", "四", "五", "六"];

function parts(t) {
  const d = new Date(t + S.base.offset * MIN);
  return { y: d.getUTCFullYear(), mo: d.getUTCMonth() + 1, d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes(), wd: d.getUTCDay() };
}
const fmt = (t) => { const p = parts(t); return `${p.y}-${p2(p.mo)}-${p2(p.d)} ${p2(p.h)}:${p2(p.mi)}`; };
const clock = (t) => { const p = parts(t); return `${p2(p.h)}:${p2(p.mi)}`; };
const dayKey = (t) => Math.floor((t + S.base.offset * MIN) / DAY);
const tzLabel = () => { const o = S.base.offset; const sign = o >= 0 ? "+" : "−"; const a = Math.abs(o); return `UTC${sign}${Math.floor(a / 60)}${a % 60 ? ":" + p2(a % 60) : ""}`; };

function fmtTime(time) {
  if (!time || time.basis === "unknown" || time.start == null) return "时间不详";
  if (time.precision === "month") {
    const a = parts(time.start);
    if (time.end == null) return `${a.y}年${a.mo}月`;
    const b = parts(time.end - 1);
    if (a.y === b.y && a.mo === b.mo) return `${a.y}年${a.mo}月`;
    return a.y === b.y ? `${a.y}年${a.mo}月～${b.mo}月` : `${a.y}年${a.mo}月～${b.y}年${b.mo}月`;
  }
  if (time.end == null || Math.floor(time.end / MIN) === Math.floor(time.start / MIN)) return fmt(time.start);
  return dayKey(time.start) === dayKey(time.end) ? `${fmt(time.start)}～${clock(time.end)}` : `${fmt(time.start)}～${fmt(time.end)}`;
}
function rel(t) {
  if (t == null) return "";
  const diff = S.base.generatedAt - t;
  const fut = diff < 0;
  const a = Math.abs(diff);
  const days = Math.abs(dayKey(S.base.generatedAt) - dayKey(t));
  let w;
  if (a < HOUR) w = `${Math.max(1, Math.round(a / MIN))} 分钟`;
  else if (days === 0) w = `${Math.round(a / HOUR)} 小时`;
  else if (days === 1) return fut ? "明天" : "昨天";
  else if (days < 14) w = `${days} 天`;
  else if (days < 60) w = `${Math.round(days / 7)} 周`;
  else w = `${Math.round(days / 30)} 个月`;
  return fut ? `${w}后` : `${w}前`;
}
const pointTime = (n) => n.time.end ?? n.time.start;

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 4200);
}
const announce = (text) => { $("#announce").textContent = text; };

// ---------- URL（只放筛选和 ID，不放正文） ----------

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const q = S.query;
  if (p.get("view") === "timeline") S.view = "timeline";
  q.scope = p.get("s") || "";
  q.topicId = p.get("t") || null;
  q.personId = p.get("p") || null;
  if (["7d", "30d", "custom"].includes(p.get("r"))) q.range = p.get("r");
  q.fromDate = p.get("from") || "";
  q.toDate = p.get("to") || "";
  q.includeUndated = p.get("u") === "1";
  S.hashKinds = p.get("k") ? p.get("k").split(",") : null;
  S.sel = p.get("sel") || null;
}
function writeHash() {
  const p = new URLSearchParams();
  const q = S.query;
  if (S.view !== "map") p.set("view", S.view);
  if (q.scope) p.set("s", q.scope);
  if (q.kinds.size && q.kinds.size !== S.kinds.length) p.set("k", [...q.kinds].join(","));
  if (q.topicId) p.set("t", q.topicId);
  if (q.personId) p.set("p", q.personId);
  if (q.range !== "all") p.set("r", q.range);
  if (q.range === "custom") { if (q.fromDate) p.set("from", q.fromDate); if (q.toDate) p.set("to", q.toDate); }
  if (q.includeUndated && q.range !== "all") p.set("u", "1");
  if (S.sel) p.set("sel", S.sel);
  if (/(?:^|[#&])preview/.test(location.hash)) p.set("preview", "1");
  const next = p.toString();
  if (next !== location.hash.slice(1)) history.replaceState(null, "", next ? "#" + next : location.pathname + location.search);
}

// ---------- 查询 ----------

function dateToMs(value, endOfDay) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  if (!m) return null;
  return Date.UTC(+m[1], +m[2] - 1, +m[3] + (endOfDay ? 1 : 0)) - S.base.offset * MIN;
}
function acceptClock(env) {
  S.base = { generatedAt: env.generatedAt, offset: env.utcOffsetMinutes };
  S.receivedAt = performance.now();
}
function serverNow() {
  return S.base.generatedAt + Math.max(0, performance.now() - (S.receivedAt ?? performance.now()));
}
function timeBounds() {
  const q = S.query;
  const now = Math.floor(serverNow());
  if (q.range === "7d") return { from: now - 7 * DAY, to: null };
  if (q.range === "30d") return { from: now - 30 * DAY, to: null };
  if (q.range === "custom") return { from: dateToMs(q.fromDate, false), to: dateToMs(q.toDate, true) };
  return { from: null, to: null };
}
function buildQuery(cursor) {
  const q = S.query;
  const { from, to } = timeBounds();
  return {
    scope: q.scope || undefined,
    kinds: q.kinds.size === S.kinds.length ? undefined : [...q.kinds],
    q: q.q.trim() || undefined,
    topicId: q.topicId || undefined,
    personId: q.personId || undefined,
    from: from ?? undefined,
    to: to ?? undefined,
    includeUndated: q.includeUndated,
    limit: S.meta?.limits?.defaultPageSize ?? 100,
    cursor: cursor || undefined,
  };
}
const filtersActive = () => {
  const q = S.query;
  return Boolean(q.scope || q.q.trim() || q.topicId || q.personId || q.range !== "all" || q.kinds.size !== S.kinds.length);
};

function remember(nodes) {
  for (const n of nodes) {
    for (const t of n.topics) S.labels.topic.set(t.id, t.label);
    for (const p of n.people) S.labels.person.set(p.id, p.label);
  }
}

async function loadMeta() {
  const seq = ++S.seqMeta;
  S.seqItems += 1;
  S.seqItem += 1;
  S.detail = null;
  S.detailMode = "remembered";
  S.detailState = S.sel ? "loading" : "idle";
  const first = !S.meta;
  S.status = "loading";
  renderState();
  renderDossier();
  try {
    const env = await universeApi.meta();
    if (seq !== S.seqMeta) return;
    S.meta = env.data;
    acceptClock(env);
    S.anchor ??= env.generatedAt;
    S.sources = Array.isArray(env.data.sources) ? env.data.sources : [];
    S.kinds = KIND_ORDER.filter((k) => (env.data.kinds || []).includes(k));
    const wanted = (first ? S.hashKinds : [...S.query.kinds])?.filter((k) => S.kinds.includes(k));
    S.query.kinds = new Set(wanted?.length ? wanted : S.kinds);
    if (S.query.scope && !S.sources.some((x) => x.scope === S.query.scope)) S.query.scope = "";
    renderReadAt();
    renderControls();
    await loadItems();
  } catch (e) {
    if (seq !== S.seqMeta) return;
    S.status = "error";
    S.error = e;
    S.items = [];
    S.byId = new Map();
    S.page = null;
    S.detailState = S.sel ? "error" : "idle";
    S.detailError = e;
    renderState();
    renderDossier();
  }
}

async function loadItems({ append = false } = {}) {
  if (append && (!S.page?.nextCursor || S.loadingMore || !S.querySnapshot)) return;
  const seq = ++S.seqItems;
  if (append) {
    S.loadingMore = true;
    renderTally();
  } else {
    S.querySnapshot = buildQuery(null);
    S.loadingMore = false;
    // 刷新／筛选后的旧原稿不能继续挂在详情卡里，也不能让旧详情请求晚回覆盖。
    S.seqItem += 1;
    S.detail = null;
    S.detailMode = "remembered";
    S.detailState = S.sel ? "loading" : "idle";
    S.detailError = null;
    S.status = "loading";
    renderState();
    renderDossier();
  }
  writeHash();
  try {
    const query = { ...S.querySnapshot, cursor: append ? S.page.nextCursor : undefined };
    const env = await universeApi.items(query);
    if (seq !== S.seqItems) return; // 旧筛选的回应来晚了，丢掉
    acceptClock(env);
    const incoming = Array.isArray(env.data.items) ? env.data.items : [];
    if (append) {
      for (const n of incoming) if (!S.byId.has(n.id)) S.items.push(n);
    } else {
      S.items = incoming.slice();
    }
    S.byId = new Map(S.items.map((n) => [n.id, n]));
    S.page = env.data.page;
    remember(incoming);
    S.status = "ready";
    S.error = null;
    S.loadingMore = false;
    renderReadAt();
    buildStars();
    renderAll();
    if (!append && S.sel) await openItem(S.sel, { quiet: true });
  } catch (e) {
    if (seq !== S.seqItems) return;
    S.loadingMore = false;
    if (e.code === "STALE_CURSOR" && append) {
      toast("记忆刚刚有了变化，旧的分页已失效，已从第一页重新读取。");
      return loadItems();
    }
    if (append) {
      toast(`没能读取更多：${e.message}`);
      renderTally();
      return;
    }
    S.status = "error";
    S.error = e;
    S.items = [];
    S.byId = new Map();
    S.page = null;
    S.detailState = S.sel ? "error" : "idle";
    S.detailError = e;
    buildStars();
    renderAll();
  }
}

async function openItem(id, { quiet = false } = {}) {
  S.sel = id;
  S.detail = null;
  S.detailMode = "remembered";
  S.detailState = "loading";
  S.detailError = null;
  writeHash();
  renderDossier();
  draw();
  syncTimelineSelection();
  const node = S.byId.get(id);
  if (node && !quiet) announce(`${KIND[node.kind]?.name || "记忆"}：${node.title}`);
  if (!quiet && innerWidth <= 980) $("#dossier").scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "start" });
  const seq = ++S.seqItem;
  try {
    const env = await universeApi.item(id);
    if (seq !== S.seqItem) return; // 期间又点了别的星
    S.detail = env.data;
    S.detailState = "ready";
    remember([env.data.node]);
  } catch (e) {
    if (seq !== S.seqItem) return;
    S.detailState = "error";
    S.detailError = e;
  }
  renderDossier();
  draw();
}

function clearSelection() {
  S.sel = null;
  S.detail = null;
  S.detailState = "idle";
  S.seqItem += 1;
  writeHash();
  renderDossier();
  draw();
  syncTimelineSelection();
}

function setFilter(patch, { keepSel = true } = {}) {
  Object.assign(S.query, patch);
  if (!keepSel) clearSelection();
  renderControls();
  loadItems();
}

// ---------- 刊头、控件 ----------

function renderReadAt() {
  if (!S.base) return;
  $("#read-at").textContent = `星图读取于 ${fmt(S.base.generatedAt)}（服务器时间 ${tzLabel()}）`;
}

function miniGlyph(kind, size = 28, extra = {}) {
  const svg = s("svg", { viewBox: `${-size / 2} ${-size / 2} ${size} ${size}`, "aria-hidden": "true", class: `k-${kind}` });
  glyph(svg, { kind, id: `legend-${kind}`, importance: extra.importance ?? 3, clarity: extra.clarity ?? { level: 0 }, status: extra.status || "active" }, { angle: 150 });
  return svg;
}

function sourceSuffix(src) {
  const bits = [];
  if (!src.enabled) bits.push("已暂停");
  if (!src.memoryEnabled) bits.push("未开长期整理");
  if (src.pendingCount == null) bits.push("未整理：未知");
  else if (src.pendingCount > 0) bits.push(`未整理 ${src.pendingCount} 条`);
  return bits.join(" · ");
}

function renderControls() {
  const q = S.query;
  for (const b of document.querySelectorAll("[data-view]")) b.setAttribute("aria-pressed", String(b.dataset.view === S.view));
  $("#map-panel").hidden = S.view !== "map";
  $("#timeline-panel").hidden = S.view !== "timeline";

  const sel = $("#scope");
  sel.replaceChildren(h("option", { text: "全部来源", attrs: { value: "" } }));
  for (const src of S.sources) {
    const suffix = sourceSuffix(src);
    sel.append(h("option", { text: src.label + (suffix ? `（${suffix}）` : ""), attrs: { value: src.scope } }));
  }
  sel.value = q.scope;

  for (const b of document.querySelectorAll("[data-range]")) b.setAttribute("aria-pressed", String(b.dataset.range === q.range));
  $("#custom-range").hidden = q.range !== "custom";
  $("#from").value = q.fromDate;
  $("#to").value = q.toDate;
  $("#undated-field").hidden = q.range === "all";
  $("#range-note").hidden = q.range === "all";
  $("#undated").checked = q.includeUndated;
  if ($("#q").value !== q.q) $("#q").value = q.q;

  const kinds = $("#kinds");
  const focusedKind = document.activeElement?.dataset?.kind;
  kinds.replaceChildren();
  for (const k of S.kinds) {
    const on = q.kinds.has(k);
    kinds.append(h("button", {
      class: "kind-chip", attrs: { type: "button", "aria-pressed": String(on), title: KIND[k].hint, "data-kind": k },
      on: { click: () => toggleKind(k) },
    }, [miniGlyph(k, 26), h("span", { text: KIND[k].name }), h("small", { text: KIND[k].body })]));
  }
  if (focusedKind) kinds.querySelector(`[data-kind="${focusedKind}"]`)?.focus();

  const active = $("#active-filters");
  active.replaceChildren();
  const pills = [];
  if (q.topicId) pills.push(["话题", S.labels.topic.get(q.topicId) || "（未知话题）", () => setFilter({ topicId: null })]);
  if (q.personId) pills.push(["人物", S.labels.person.get(q.personId) || "（未知人物）", () => setFilter({ personId: null })]);
  for (const [kind, label, drop] of pills) {
    active.append(h("span", { class: "pill" }, [
      h("span", { text: `${kind}：${label}` }),
      h("button", { text: "×", attrs: { type: "button", "aria-label": `去掉${kind}筛选 ${label}` }, on: { click: drop } }),
    ]));
  }
  if (filtersActive()) {
    active.append(h("button", { class: "link-btn", text: "清除全部筛选", attrs: { type: "button" }, on: { click: resetFilters } }));
  }
  active.hidden = !active.childElementCount;
}

function toggleKind(k) {
  const set = S.query.kinds;
  if (set.has(k) && set.size === 1) { toast("至少保留一种类型。"); return; }
  if (set.has(k)) set.delete(k); else set.add(k);
  renderControls();
  loadItems();
}

function resetFilters() {
  Object.assign(S.query, { scope: "", kinds: new Set(S.kinds), q: "", topicId: null, personId: null, range: "all", fromDate: "", toDate: "", includeUndated: false });
  renderControls();
  loadItems();
}

function bindControls() {
  for (const b of document.querySelectorAll("[data-view]")) b.addEventListener("click", () => {
    S.view = b.dataset.view;
    writeHash();
    renderControls();
    renderAll();
  });
  $("#scope").addEventListener("change", (e) => setFilter({ scope: e.target.value }));
  for (const b of document.querySelectorAll("[data-range]")) b.addEventListener("click", () => setFilter({ range: b.dataset.range }));
  $("#from").addEventListener("change", (e) => setFilter({ fromDate: e.target.value }));
  $("#to").addEventListener("change", (e) => setFilter({ toDate: e.target.value }));
  $("#undated").addEventListener("change", (e) => setFilter({ includeUndated: e.target.checked }));
  let timer;
  $("#q").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { S.query.q = e.target.value.slice(0, 80); renderControls(); loadItems(); }, 280);
  });
  $("#refresh").addEventListener("click", () => loadMeta());
  $("#filter-toggle").addEventListener("click", (e) => {
    const open = !$("#filters").classList.contains("open");
    $("#filters").classList.toggle("open", open);
    e.currentTarget.setAttribute("aria-expanded", String(open));
  });
}

// ---------- 主题 ----------

function setupTheme() {
  const btn = $("#theme-toggle");
  if (hostBridge) {
    // 宿主维护 <html data-theme>，这里只需要在主题变化时重画一次星图
    hostBridge.ready?.().then(() => draw()).catch(() => {});
    hostBridge.onContext?.(() => draw());
    return;
  }
  const names = { system: "外观：跟随系统", light: "外观：纸本", dark: "外观：夜空" };
  btn.hidden = false;
  const apply = () => {
    if (S.theme === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", S.theme);
    btn.textContent = names[S.theme];
    draw();
  };
  btn.addEventListener("click", () => {
    S.theme = S.theme === "system" ? "light" : S.theme === "light" ? "dark" : "system";
    apply();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => draw());
  apply();
}

function setupNavigation() {
  // 仅接受宿主明确提供的同源地址；不推测AstrBot路由或页面token。
  const value = hostBridge?.pageUrls?.dashboard;
  if (typeof value !== "string" || !value) return;
  try {
    const target = new URL(value, location.href);
    if (target.origin !== location.origin || !["http:", "https:"].includes(target.protocol)) return;
    const link = $("#back-dashboard");
    link.href = target.href;
    link.hidden = false;
  } catch { /* 不支持导航的宿主仍可用其自己的Pages入口。 */ }
}

function setupBench() {
  if (mode !== "mock") return;
  $("#badge-mock").hidden = false;
  const bench = $("#bench");
  bench.hidden = false;
  const sel = $("#scenario");
  for (const [value, label] of universeApi.scenarios) sel.append(h("option", { text: label, attrs: { value } }));
  sel.value = universeApi.scenario;
  sel.addEventListener("change", () => {
    universeApi.setScenario(sel.value);
    S.ignite = true;
    clearSelection();
    loadMeta();
  });
}

// ---------- 星体图形 ----------

function glyph(parent, n, { angle = 0 } = {}) {
  const g = s("g", { class: "glyph" }, parent);
  switch (n.kind) {
    case "episode": {
      const r = 2.6 + (n.importance ?? 3) * 1.25;
      const lv = n.clarity ? n.clarity.level : 1;
      s("circle", { class: "halo-soft", r: (r * [3.7, 3.0, 2.4, 1.9][lv]).toFixed(1), fill: "url(#halo-episode)", style: `--glow:${[.9, .62, .4, .24][lv]}` }, g);
      const rings = [[3, 6, 9], [3.5, 7], [4.5], [4.5]][lv];
      rings.forEach((d) => s("circle", { class: `halo-ring${lv === 2 ? " dash" : lv === 3 ? " dot" : ""}`, r: (r + d).toFixed(1), opacity: (1 - d / 14).toFixed(2) }, g));
      s("circle", { class: "core", r: r.toFixed(1) }, g);
      break;
    }
    case "pack": {
      const cold = n.status === "cold";
      s("circle", { class: "cluster-ring", r: 9.5 }, g);
      for (const a of [-90, 30, 150]) {
        const rad = (a * Math.PI) / 180;
        s("circle", { class: cold ? "hollow" : "core", cx: (Math.cos(rad) * 4.6).toFixed(2), cy: (Math.sin(rad) * 4.6).toFixed(2), r: cold ? 2.4 : 2.5 }, g);
      }
      break;
    }
    case "fact":
      s("rect", { class: "fact-mark", x: -4.6, y: -4.6, width: 9.2, height: 9.2, rx: 1, transform: "rotate(45)" }, g);
      break;
    case "word":
      s("circle", { class: "halo-soft", r: 11, fill: "url(#halo-word)", style: "--glow:.8" }, g);
      s("path", { class: "core", d: "M0,-7.5 L1.7,-1.7 L7.5,0 L1.7,1.7 L0,7.5 L-1.7,1.7 L-7.5,0 L-1.7,-1.7Z" }, g);
      break;
    case "thread": {
      const t = s("g", { transform: `rotate(${angle.toFixed(1)})` }, g);
      s("path", { class: "tail", d: "M0,-3.4 L24,-0.7 L24,0.7 L0,3.4 Z" }, t);
      s("circle", { class: "halo-soft", r: 8, fill: "url(#halo-thread)", style: "--glow:.6" }, t);
      s("circle", { class: "core", r: 3.8 }, t);
      break;
    }
    case "haze": {
      const pts = [];
      const k = 8;
      for (let i = 0; i < k; i++) {
        const a = (i / k) * Math.PI * 2;
        const r = 11 + 6 * hash01(n.id, 100 + i);
        pts.push([Math.cos(a) * r, Math.sin(a) * r]);
      }
      let d = "";
      for (let i = 0; i < k; i++) {
        const [x0, y0] = pts[i];
        const [x1, y1] = pts[(i + 1) % k];
        const mx = (x0 + x1) / 2;
        const my = (y0 + y1) / 2;
        d += i === 0 ? `M${mx.toFixed(1)},${my.toFixed(1)}` : "";
        d += ` Q${x1.toFixed(1)},${y1.toFixed(1)} ${(((x1 + pts[(i + 2) % k][0]) / 2)).toFixed(1)},${(((y1 + pts[(i + 2) % k][1]) / 2)).toFixed(1)}`;
      }
      s("path", { class: "nebula", d: d + "Z", filter: "url(#soft)" }, g);
      s("circle", { class: "nebula-core", r: 2.2 }, g);
      break;
    }
    default:
      s("circle", { class: "core", r: 4 }, g);
  }
  return g;
}

// ---------- 星图几何 ----------

// 世界坐标：中心是「此刻」，越往外越久；BELT 是时间不详的星带，BAND0～EXT 是写来源名字的外圈。
const W = { IN: 74, OUT: 840, BELT: 905, BAND0: 935, EXT: 1072 };
const RINGS = [[1, "一日"], [7, "七日"], [30, "一月"], [90, "三月"], [180, "半年"]];
// 按小时取对数：今天几小时内的事也能分开，不会全挤在中心。
const ageR = (days) => W.IN + (W.OUT - W.IN) * Math.min(1, Math.log1p(Math.max(0, days) * 8) / Math.log1p(180 * 8));
const posCache = new Map();
let svg, gGrid, gEdges, gStars, gMarks;
let VW = 0;
let VH = 0;
let s0 = 1;
const starEls = new Map();

function sectorOf(scope) {
  const n = Math.max(1, S.sources.length);
  const i = Math.max(0, S.sources.findIndex((x) => x.scope === scope));
  const span = (Math.PI * 2) / n;
  const gap = n > 1 ? 0.07 : 0;
  const a0 = -Math.PI / 2 + i * span + gap / 2;
  return { a0, a1: a0 + span - gap, mid: a0 + (span - gap) / 2, i };
}
function worldPos(n) {
  const cached = posCache.get(n.id);
  if (cached) return cached;
  const sec = sectorOf(n.source.scope);
  const a = sec.a0 + (sec.a1 - sec.a0) * (0.07 + 0.86 * hash01(n.id, 11));
  const t = pointTime(n);
  let r = t == null || n.time.basis === "unknown"
    ? W.BELT + (hash01(n.id, 23) - 0.5) * 34
    : ageR((S.anchor - t) / DAY) + (hash01(n.id, 37) - 0.5) * 46;
  r = Math.max(W.IN + 16, r);
  const p = { x: Math.cos(a) * r, y: Math.sin(a) * r, a, r };
  posCache.set(n.id, p);
  return p;
}
const toScreen = (p) => ({ x: VW / 2 + p.x * s0 * S.tf.k + S.tf.x, y: VH / 2 + p.y * s0 * S.tf.k + S.tf.y });
const polar = (a, r) => toScreen({ x: Math.cos(a) * r, y: Math.sin(a) * r });
const shown = () => S.items.slice(0, MAP_CAP);

function setupChart() {
  svg = $("#chart");
  const defs = s("defs", {}, svg);
  for (const k of KIND_ORDER) {
    const g = s("radialGradient", { id: `halo-${k}` }, defs);
    s("stop", { offset: "0%", style: `stop-color:var(--k-${k});stop-opacity:.75` }, g);
    s("stop", { offset: "38%", style: `stop-color:var(--k-${k});stop-opacity:.22` }, g);
    s("stop", { offset: "100%", style: `stop-color:var(--k-${k});stop-opacity:0` }, g);
  }
  const tail = s("linearGradient", { id: "tail", x1: "0", x2: "1", y1: "0", y2: "0" }, defs);
  s("stop", { offset: "0%", style: "stop-color:var(--k-thread);stop-opacity:.85" }, tail);
  s("stop", { offset: "100%", style: "stop-color:var(--k-thread);stop-opacity:0" }, tail);
  const soft = s("filter", { id: "soft", x: "-50%", y: "-50%", width: "200%", height: "200%" }, defs);
  s("feGaussianBlur", { stdDeviation: "2.4" }, soft);
  gGrid = s("g", { class: "layer-grid" }, svg);
  gEdges = s("g", { class: "layer-edges" }, svg);
  gStars = s("g", { class: "layer-stars" }, svg);
  gMarks = s("g", { class: "layer-marks" }, svg);
  new ResizeObserver(() => draw()).observe($("#map-panel"));
  bindChartInput();
}

function buildStars() {
  posCache.clear();
  gStars.replaceChildren();
  starEls.clear();
  const list = shown();
  const ignite = S.ignite && !reducedMotion.matches;
  for (const n of list) {
    const p = worldPos(n);
    const el = s("g", { class: `star k-${n.kind}`, "data-id": n.id }, gStars);
    s("circle", { class: "hit", r: 16 }, el);
    const g = glyph(el, n, { angle: (p.a * 180) / Math.PI });
    if (ignite) g.style.setProperty("--d", `${Math.round((p.r / W.BELT) * 900 + hash01(n.id, 5) * 240)}ms`);
    starEls.set(n.id, el);
  }
  if (ignite && list.length) {
    svg.classList.add("ignite");
    S.ignite = false;
    setTimeout(() => svg.classList.remove("ignite"), 1700);
  }
}

// ---------- 绘制 ----------

let drawPending = false;
function scheduleDraw() {
  if (drawPending) return;
  drawPending = true;
  requestAnimationFrame(() => { drawPending = false; draw(); });
}

function draw() {
  if (!svg || !S.base || S.view !== "map") return;
  const box = svg.getBoundingClientRect();
  VW = Math.max(1, box.width);
  VH = Math.max(1, box.height);
  svg.setAttribute("viewBox", `0 0 ${VW} ${VH}`);
  s0 = (Math.min(VW, VH) / 2 - 4) / W.EXT;
  svg.classList.toggle("zoomed", S.tf.k > 1.02);
  drawGrid();
  for (const [id, el] of starEls) {
    const n = S.byId.get(id);
    if (!n) continue;
    const p = toScreen(worldPos(n));
    el.setAttribute("transform", `translate(${p.x.toFixed(1)},${p.y.toFixed(1)})`);
  }
  drawFocus();
}

function drawGrid() {
  gGrid.replaceChildren();
  const k = s0 * S.tf.k;
  const c = toScreen({ x: 0, y: 0 });
  const rimPx = (W.EXT - 4) * k;
  s("circle", { class: "g-outer", cx: c.x, cy: c.y, r: rimPx }, gGrid);
  s("circle", { class: "g-outer", cx: c.x, cy: c.y, r: W.BAND0 * k, opacity: ".5" }, gGrid);
  s("circle", { class: "g-belt", cx: c.x, cy: c.y, r: W.BELT * k }, gGrid);
  for (const [d] of RINGS) s("circle", { class: `g-ring${d === 30 || d === 180 ? " major" : ""}`, cx: c.x, cy: c.y, r: ageR(d) * k }, gGrid);
  // 外圈刻度：像星图边上的赤经刻线（按像素画，缩放时不变粗）
  for (let i = 0; i < 120; i++) {
    const a = (i / 120) * Math.PI * 2;
    const len = i % 10 === 0 ? 7 : 3.5;
    s("line", {
      class: "g-tick",
      x1: c.x + Math.cos(a) * rimPx, y1: c.y + Math.sin(a) * rimPx,
      x2: c.x + Math.cos(a) * (rimPx - len), y2: c.y + Math.sin(a) * (rimPx - len),
    }, gGrid);
  }
  s("circle", { class: "g-disk", cx: c.x, cy: c.y, r: W.IN * k }, gGrid);
  s("text", { class: "g-center", x: c.x, y: c.y + 4, "text-anchor": "middle" }, gGrid).textContent = "此刻";

  const n = S.sources.length;
  const band0 = W.BAND0 * k;
  const bandPx = rimPx - band0 - 8;
  const twoLines = bandPx >= 30;
  S.sources.forEach((src, i) => {
    const sec = sectorOf(src.scope);
    if (n > 1) {
      const p0 = polar(sec.a0, W.IN);
      const p1 = polar(sec.a0, W.EXT - 4);
      s("line", { class: "g-sector", x1: p0.x, y1: p0.y, x2: p1.x, y2: p1.y }, gGrid);
    }
    // 来源名称沿外圈排，半径按像素算；下半圈反向画路径，文字才不会倒过来。
    // 上半圈文字朝外长，所以副标题放内侧；下半圈文字朝内长，副标题放外侧。
    const bottom = Math.sin(sec.mid) > 0.2;
    const arc = (rpx, id) => {
      const from = bottom ? sec.a1 : sec.a0;
      const to = bottom ? sec.a0 : sec.a1;
      return s("path", {
        id, fill: "none", stroke: "none",
        d: `M${c.x + Math.cos(from) * rpx},${c.y + Math.sin(from) * rpx} A${rpx},${rpx} 0 0 ${bottom ? 0 : 1} ${c.x + Math.cos(to) * rpx},${c.y + Math.sin(to) * rpx}`,
      }, gGrid);
    };
    const mainR = twoLines ? (bottom ? band0 + 15 : band0 + 28) : (bottom ? band0 + 13 : band0 + 4);
    const subR = bottom ? band0 + 30 : band0 + 12;
    arc(mainR, `src-arc-${i}`);
    const room = Math.max(4, Math.floor(((sec.a1 - sec.a0) * mainR * 0.86) / 13.5));
    const label = src.label.length > room ? src.label.slice(0, room - 1) + "…" : src.label;
    const off = !src.enabled ? " off" : "";
    const t1 = s("text", { class: `g-src${off}`, "text-anchor": "middle" }, gGrid);
    s("textPath", { href: `#src-arc-${i}`, startOffset: "50%" }, t1).textContent = label;
    if (twoLines) {
      arc(subR, `src-sub-${i}`);
      const sub = [src.kind === "group" ? "群" : "私聊", sourceSuffix(src)].filter(Boolean).join(" · ");
      const t2 = s("text", { class: `g-src-sub${off}`, "text-anchor": "middle" }, gGrid);
      s("textPath", { href: `#src-sub-${i}`, startOffset: "50%" }, t2).textContent = sub;
    }
    // 点星区外圈 = 只看这个来源
    const r0 = band0;
    const r1 = rimPx;
    const pA = polar(sec.a0, W.BAND0);
    const pB = polar(sec.a1, W.BAND0);
    const pC = polar(sec.a1, W.EXT - 4);
    const pD = polar(sec.a0, W.EXT - 4);
    const large = sec.a1 - sec.a0 > Math.PI ? 1 : 0;
    const hit = s("path", {
      class: "g-sector-hit", "data-scope": src.scope,
      d: `M${pA.x},${pA.y} A${r0},${r0} 0 ${large} 1 ${pB.x},${pB.y} L${pC.x},${pC.y} A${r1},${r1} 0 ${large} 0 ${pD.x},${pD.y} Z`,
    }, gGrid);
    s("title", {}, hit).textContent = `只看「${src.label}」`;
  });

  // 时间环的刻度字：放在第一星区左边的界线旁
  // 小屏上环挨得太近时跳过放不下的字，别叠在一起
  const a = -Math.PI / 2 - 0.035;
  let lastY = Infinity;
  for (const [r, label] of [...RINGS.map(([d, l]) => [ageR(d), l]), [W.BELT, "时间不详"]]) {
    const p = polar(a, r);
    if (lastY - p.y < 14) continue;
    lastY = p.y;
    s("text", { class: "g-ring-label", x: p.x - 4, y: p.y + 4, "text-anchor": "end" }, gGrid).textContent = label;
  }
}

function curve(a, b, bend = 0.12) {
  const mx = (a.x + b.x) / 2;
  const my = (a.y + b.y) / 2;
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  return `M${a.x.toFixed(1)},${a.y.toFixed(1)} Q${(mx - dy * bend).toFixed(1)},${(my + dx * bend).toFixed(1)} ${b.x.toFixed(1)},${b.y.toFixed(1)}`;
}

function centroid(nodes) {
  const pts = nodes.map(worldPos);
  return { x: pts.reduce((v, p) => v + p.x, 0) / pts.length, y: pts.reduce((v, p) => v + p.y, 0) / pts.length };
}

// 话题／人物的聚合点。点击统一在指针抬起时处理（见 bindChartInput），这里只登记动作。
const markerActions = new WeakMap();
const markerWidth = (label) => Math.max(44, label.length * 12.5 + 22);
let placed = []; // 本次已占用的屏幕区域：选中星、它的名字、已放的聚合点

// 聚合点挤在一起时，沿着「远离选中星」的方向一步步挪开，直到不和别的框重叠。
function freeSpot(world, label, from) {
  const p = toScreen(world);
  const w = markerWidth(label);
  let dx = p.x - from.x;
  let dy = p.y - from.y;
  const len = Math.hypot(dx, dy) || 1;
  dx /= len; dy /= len;
  if (len < 2) { dx = 0; dy = 1; }
  let x = p.x;
  let y = p.y;
  for (let i = 0; i < 12; i++) {
    const hit = placed.some((b) => Math.abs(b.x - x) < (b.w + w) / 2 + 6 && Math.abs(b.y - y) < (b.h || 26) / 2 + 15);
    if (!hit) break;
    x += dx * 24 - dy * (i % 2 ? 10 : -10);
    y += dy * 24 + dx * (i % 2 ? 10 : -10);
  }
  placed.push({ x, y, w, h: 24 });
  return { x, y };
}

function marker(p, label, cls, onClick, title) {
  const g = s("g", { class: `marker ${cls}`, transform: `translate(${p.x.toFixed(1)},${p.y.toFixed(1)})` }, gMarks);
  const w = markerWidth(label);
  s("rect", { x: -w / 2, y: -12, width: w, height: 24, rx: 12 }, g);
  s("text", { "text-anchor": "middle", y: 4.5 }, g).textContent = label;
  if (title) s("title", {}, g).textContent = title;
  markerActions.set(g, onClick);
  return p;
}

function placeNear(sel, members, i) {
  const self = worldPos(sel);
  let c = members.length ? centroid([sel, ...members]) : { x: self.x, y: self.y };
  let dx = c.x - self.x;
  let dy = c.y - self.y;
  if (Math.hypot(dx, dy) < 70) {
    const a = Math.atan2(self.y, self.x) + 0.55 + i * 0.5;
    c = { x: self.x + Math.cos(a) * 110, y: self.y + Math.sin(a) * 110 };
  } else {
    const a = i * 0.32;
    c = { x: c.x + Math.cos(a) * i * 26, y: c.y + Math.sin(a) * i * 26 };
  }
  return c;
}

function drawFocus() {
  gEdges.replaceChildren();
  gMarks.replaceChildren();
  placed = [];
  const list = shown();
  const sel = S.sel ? S.byId.get(S.sel) : null;
  const related = new Set();
  const edge = (d, cls) => s("path", { d, class: `${cls} edge-in` }, gEdges);
  const onMap = sel && starEls.has(sel.id);
  const ps = onMap ? toScreen(worldPos(sel)) : { x: VW / 2 + S.tf.x, y: VH / 2 + S.tf.y };
  let selTitle = "";
  let labelLeft = false;
  const center = toScreen({ x: 0, y: 0 });
  placed.push({ x: center.x, y: center.y, w: Math.max(48, W.IN * s0 * S.tf.k * 2), h: Math.max(30, W.IN * s0 * S.tf.k * 2) }); // 「此刻」
  if (onMap) {
    selTitle = sel.title.length > 18 ? sel.title.slice(0, 18) + "…" : sel.title;
    labelLeft = ps.x > VW * 0.62;
    const lw = selTitle.length * 13.5;
    placed.push({ x: ps.x, y: ps.y, w: 44, h: 44 });
    placed.push({ x: ps.x + (labelLeft ? -32 - lw / 2 : 32 + lw / 2), y: ps.y, w: lw, h: 20 });
  }

  // 整页按话题／人物筛选时：一个聚合点，辐条连到每颗星（只是同标签，不是因果）
  const q = S.query;
  for (const [id, kind] of [[q.topicId, "topic"], [q.personId, "person"]]) {
    if (!id || !list.length) continue;
    const members = list.filter((n) => (kind === "topic" ? n.topics : n.people).some((x) => x.id === id));
    if (!members.length) continue;
    const label = (kind === "topic" ? S.labels.topic : S.labels.person).get(id) || "（未知）";
    const text = kind === "topic" ? `# ${label}` : label;
    const pm = freeSpot(centroid(members), text, ps);
    for (const m of members.slice(0, 60)) {
      const basis = kind === "person" ? m.people.find((x) => x.id === id)?.basis : null;
      edge(curve(pm, toScreen(worldPos(m)), 0.05), kind === "topic" ? "e-topic spoke" : `e-person${basis === "pack_participant" ? " part" : ""}`);
    }
    marker(pm, text, kind === "topic" ? "topic" : "person",
      () => setFilter(kind === "topic" ? { topicId: null } : { personId: null }), "点一下取消这个筛选");
  }

  if (onMap) {
    related.add(sel.id);
    // 来自该会话：连到外圈这一星区的名字
    const sec = sectorOf(sel.source.scope);
    const rim = polar(sec.mid, W.BAND0);
    edge(curve(ps, rim, 0.08), "e-source");

    sel.topics.forEach((t, i) => {
      const members = list.filter((n) => n.id !== sel.id && n.topics.some((x) => x.id === t.id));
      const text = `# ${t.label}`;
      const pm = freeSpot(placeNear(sel, members, i), text, ps);
      edge(curve(ps, pm, 0.1), "e-topic");
      for (const m of members.slice(0, 14)) { related.add(m.id); edge(curve(pm, toScreen(worldPos(m)), 0.06), "e-topic spoke"); }
      marker(pm, text, "topic", () => setFilter({ topicId: t.id }), `只看话题「${t.label}」（同标签，不代表因果）`);
    });
    sel.people.forEach((p, i) => {
      const members = list.filter((n) => n.id !== sel.id && n.people.some((x) => x.id === p.id));
      const pm = freeSpot(placeNear(sel, members, i + sel.topics.length), p.label, ps);
      edge(curve(ps, pm, -0.1), `e-person${p.basis === "pack_participant" ? " part" : ""}`);
      for (const m of members.slice(0, 14)) {
        related.add(m.id);
        const b = m.people.find((x) => x.id === p.id)?.basis;
        edge(curve(pm, toScreen(worldPos(m)), -0.06), `e-person${b === "pack_participant" ? " part" : ""}`);
      }
      marker(pm, p.label, "person", () => setFilter({ personId: p.id }),
        p.basis === "fact_subject" ? "这条事实说的是他" : "他参加过这段聊天");
    });
    if (S.detail && S.detail.node.id === sel.id) {
      for (const l of S.detail.links) {
        const t = S.byId.get(l.target.id);
        if (!t || !starEls.has(t.id)) continue; // 目标不在星图上就不画悬空线
        related.add(t.id);
        edge(curve(ps, toScreen(worldPos(t)), 0.18), "e-link");
      }
    }
    // 准星
    const ret = s("g", { class: "reticle", transform: `translate(${ps.x.toFixed(1)},${ps.y.toFixed(1)})` }, gMarks);
    s("circle", { r: 17, class: reducedMotion.matches ? "" : "draw" }, ret);
    for (const [x1, y1, x2, y2] of [[0, -27, 0, -21], [0, 21, 0, 27], [-27, 0, -21, 0], [21, 0, 27, 0]]) s("line", { x1, y1, x2, y2 }, ret);
    const lab = s("text", { class: "sel-label", x: ps.x + (labelLeft ? -32 : 32), y: ps.y + 5, "text-anchor": labelLeft ? "end" : "start" }, gMarks);
    lab.textContent = selTitle;
  }

  for (const [id, el] of starEls) {
    el.classList.toggle("dim", Boolean(onMap && !related.has(id)));
    el.classList.toggle("hot", id === S.sel);
  }
}

// ---------- 星图输入：指针、滚轮、键盘 ----------

function zoomAt(f, cx = VW / 2, cy = VH / 2) {
  const k0 = S.tf.k;
  const k1 = clamp(k0 * f, 0.7, 8);
  const r = k1 / k0;
  S.tf.x = (cx - VW / 2) - (cx - VW / 2 - S.tf.x) * r;
  S.tf.y = (cy - VH / 2) - (cy - VH / 2 - S.tf.y) * r;
  S.tf.k = k1;
  scheduleDraw();
}
function resetView() {
  S.tf = { k: 1, x: 0, y: 0 };
  scheduleDraw();
}

function localPoint(e) {
  const b = svg.getBoundingClientRect();
  return { x: e.clientX - b.left, y: e.clientY - b.top };
}

function bindChartInput() {
  const pts = new Map();
  let drag = null;
  let pinch = null;
  let moved = false;
  let downTarget = null;

  svg.addEventListener("pointerdown", (e) => {
    pts.set(e.pointerId, localPoint(e));
    if (pts.size === 1) {
      const p = localPoint(e);
      drag = { x: p.x, y: p.y, tx: S.tf.x, ty: S.tf.y };
      moved = false;
      downTarget = e.target.closest(".star, .marker, .g-sector-hit");
      if (e.pointerType === "mouse" || S.tf.k > 1.02) {
        try { svg.setPointerCapture(e.pointerId); } catch { /* 指针已经不在了：不捕获也能用 */ }
      }
    } else if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), k: S.tf.k };
      drag = null;
      moved = true;
    }
  });
  svg.addEventListener("pointermove", (e) => {
    if (!pts.has(e.pointerId)) {
      hoverTip(e);
      return;
    }
    pts.set(e.pointerId, localPoint(e));
    if (pinch && pts.size === 2) {
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      zoomAt((pinch.k * (d / Math.max(1, pinch.d))) / S.tf.k, (a.x + b.x) / 2, (a.y + b.y) / 2);
      return;
    }
    if (!drag) return;
    const p = localPoint(e);
    const dx = p.x - drag.x;
    const dy = p.y - drag.y;
    if (!moved && Math.hypot(dx, dy) > 5) moved = true;
    if (moved && (e.pointerType === "mouse" || S.tf.k > 1.02)) {
      S.tf.x = drag.tx + dx;
      S.tf.y = drag.ty + dy;
      hideTip();
      scheduleDraw();
    }
  });
  const end = (e) => {
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (pts.size === 0) {
      if (!moved && downTarget && e.type === "pointerup") {
        if (downTarget.classList.contains("star")) openItem(downTarget.dataset.id);
        else if (downTarget.classList.contains("marker")) markerActions.get(downTarget)?.();
        else if (downTarget.dataset.scope) setFilter({ scope: downTarget.dataset.scope });
      } else if (!moved && !downTarget && e.type === "pointerup" && S.sel) {
        clearSelection(); // 点空白处收起选择
      }
      drag = null;
      downTarget = null;
    }
  };
  svg.addEventListener("pointerup", end);
  svg.addEventListener("pointercancel", end);
  svg.addEventListener("pointerleave", hideTip);
  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    const p = localPoint(e);
    zoomAt(Math.exp(-e.deltaY * 0.0016), p.x, p.y);
  }, { passive: false });

  svg.addEventListener("keydown", (e) => {
    const dirs = { ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] };
    if (dirs[e.key]) {
      e.preventDefault();
      stepSelection(...dirs[e.key]);
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (S.sel) $("#dossier").focus();
      else stepSelection(0, 0);
    } else if (e.key === "Escape") {
      if (S.sel) clearSelection();
    } else if (e.key === "+" || e.key === "=") zoomAt(1.25);
    else if (e.key === "-" || e.key === "_") zoomAt(0.8);
    else if (e.key === "0") resetView();
  });

  $("#zoom-in").addEventListener("click", () => zoomAt(1.3));
  $("#zoom-out").addEventListener("click", () => zoomAt(1 / 1.3));
  $("#zoom-reset").addEventListener("click", resetView);
}

// 方向键：在大致那个方向上，找离当前最近的一颗星
function stepSelection(dx, dy) {
  const list = shown();
  if (!list.length) return;
  const cur = S.sel && starEls.has(S.sel) ? toScreen(worldPos(S.byId.get(S.sel))) : { x: VW / 2 + S.tf.x, y: VH / 2 + S.tf.y };
  let best = null;
  let bestScore = Infinity;
  for (const n of list) {
    if (n.id === S.sel) continue;
    const p = toScreen(worldPos(n));
    const vx = p.x - cur.x;
    const vy = p.y - cur.y;
    const d = Math.hypot(vx, vy);
    if (d < 1) continue;
    let score = d;
    if (dx || dy) {
      const cos = (vx * dx + vy * dy) / d;
      if (cos < 0.45) continue;
      score = d * (2.2 - cos);
    }
    if (score < bestScore) { bestScore = score; best = n; }
  }
  if (best) {
    openItem(best.id);
    const p = toScreen(worldPos(best));
    if (p.x < 0 || p.y < 0 || p.x > VW || p.y > VH) { S.tf.x += VW / 2 - p.x; S.tf.y += VH / 2 - p.y; scheduleDraw(); }
  }
}

function hoverTip(e) {
  const star = e.target.closest?.(".star");
  if (!star) return hideTip();
  const n = S.byId.get(star.dataset.id);
  if (!n) return hideTip();
  const tip = $("#tooltip");
  const p = toScreen(worldPos(n));
  tip.replaceChildren(
    h("b", { text: n.title }),
    h("span", { text: `${KIND[n.kind]?.name || n.kind} · ${n.time.basis === "unknown" ? "时间不详" : rel(pointTime(n)) || fmtTime(n.time)}` }),
    h("span", { text: ` · ${n.source.label}` }),
  );
  tip.style.left = `${p.x}px`;
  tip.style.top = `${p.y}px`;
  tip.hidden = false;
}
function hideTip() { $("#tooltip").hidden = true; }

// ---------- 时间线 ----------

function timeCol(n) {
  const t = n.time;
  if (t.basis === "unknown" || t.start == null) return "—";
  if (t.precision === "month") return "整月";
  if (t.end != null && Math.floor(t.end / MIN) !== Math.floor(t.start / MIN)) {
    return dayKey(t.start) === dayKey(t.end) ? `${clock(t.start)}–${clock(t.end)}` : `${clock(t.start)}–次日起`;
  }
  return clock(t.start);
}
function metaBits(n) {
  const bits = [[KIND[n.kind]?.name || n.kind, "k"], [n.source.label]];
  if (n.time.basis !== "unknown") bits.push([n.time.label]);
  if (n.clarity) bits.push([`清晰度：${n.clarity.label}`]);
  if (n.kind === "pack") bits.push([n.status === "cold" ? "已退出活跃临时段，仍保留" : "活跃临时段"]);
  if (n.kind === "thread" && n.expiresAt != null) bits.push([`保留到 ${fmt(n.expiresAt)}`]);
  return bits;
}

function renderTimeline() {
  const ol = $("#timeline");
  ol.replaceChildren();
  if (S.status !== "ready" || !S.items.length) return;
  let last;
  for (const n of S.items) {
    const t = pointTime(n);
    const key = t == null || n.time.basis === "unknown" ? "unknown" : n.time.precision === "month" ? `m${parts(n.time.start).y}-${parts(n.time.start).mo}` : `d${dayKey(t)}`;
    if (key !== last) {
      last = key;
      let title;
      let sub = "";
      if (key === "unknown") title = "时间不详";
      else if (key[0] === "m") { const p = parts(n.time.start); title = `${p.y}年${p.mo}月`; sub = "只记得月份"; }
      else { const p = parts(t); title = `${p.mo}月${p.d}日`; sub = `${p.y}-${p2(p.mo)}-${p2(p.d)} 星期${WEEK[p.wd]}`; }
      ol.append(h("li", { class: "tl-day" }, [h("h3", {}, [title, sub ? h("small", { text: sub }) : null])]));
    }
    const glyphSvg = s("svg", { class: `tl-glyph k-${n.kind}`, viewBox: "-12 -12 24 24", "aria-hidden": "true" });
    glyph(glyphSvg, n, { angle: 150 });
    ol.append(h("li", { class: "tl-item" }, [
      h("button", {
        class: "tl-entry", attrs: { type: "button", "aria-pressed": String(n.id === S.sel), "data-id": n.id },
        on: { click: () => openItem(n.id) },
      }, [
        h("span", { class: "tl-time", text: timeCol(n) }),
        h("span", { class: "tl-body" }, [
          glyphSvg,
          h("span", { class: "tl-title", text: n.title }),
          n.preview ? h("span", { class: "tl-preview", text: n.preview }) : null,
          h("span", { class: "tl-meta" }, metaBits(n).map(([text, cls]) => h("span", { class: cls || "", text }))),
        ]),
      ]),
    ]));
  }
  if (S.page?.nextCursor) {
    ol.append(h("li", { class: "tl-more" }, [h("button", {
      class: "btn ghost", text: S.loadingMore ? "读取中…" : "继续往前读", attrs: { type: "button", disabled: S.loadingMore },
      on: { click: () => loadItems({ append: true }) },
    })]));
  }
}
function syncTimelineSelection() {
  for (const b of document.querySelectorAll(".tl-entry")) b.setAttribute("aria-pressed", String(b.dataset.id === S.sel));
}

// ---------- 计数、状态 ----------

function renderTally() {
  const t = $("#tally");
  t.replaceChildren();
  if (S.status !== "ready" || !S.page) return;
  const num = (v) => h("span", { class: "num", text: v == null ? "未知" : String(v) });
  const loaded = S.items.length;
  if (S.view === "map") t.append(h("span", {}, ["星图中 ", num(Math.min(loaded, MAP_CAP)), " 颗"]));
  t.append(h("span", {}, ["已读取 ", num(loaded), " ／ 符合筛选 ", num(S.page.matched), " 条"]));
  if (S.page.nextCursor && (S.view === "timeline" || loaded < MAP_CAP)) {
    t.append(h("button", { class: "btn ghost", text: S.loadingMore ? "读取中…" : "再读一页", attrs: { type: "button", disabled: S.loadingMore }, on: { click: () => loadItems({ append: true }) } }));
  }
  if (S.view === "map" && (loaded > MAP_CAP || (loaded === MAP_CAP && S.page.nextCursor))) {
    t.append(h("span", { class: "warn", text: `星图最多同时显示 ${MAP_CAP} 颗；更多请缩小筛选，或切到时间线继续往前读。` }));
  }
  if (loaded && S.page.matched > loaded && S.query.topicId) t.append(h("span", { text: "（关联只按已读取的部分画出）" }));
}

const ERR = {
  INVALID_QUERY: ["筛选条件不合法", "换一组筛选再试。"],
  ITEM_NOT_FOUND: ["这条记忆已不存在", "可能已整理或到期，星图需要重新读取。"],
  STALE_CURSOR: ["内容已更新", "请重新读取。"],
  NOT_READY: ["插件还没准备好", "稍等一会儿，再手动重试。"],
  REQUEST_FAILED: ["没能读到星图", "请检查插件版本和连接，或从 AstrBot 仪表盘重新进入。"],
  UNSUPPORTED_SCHEMA: ["版本不兼容", "页面和插件后端的数据格式对不上，没有按旧格式猜着显示。"],
};

function renderState() {
  const box = $("#state");
  box.replaceChildren();
  box.className = "state";
  let content = null;
  if (S.status === "loading" && !S.items.length) {
    box.classList.add("loading");
    content = [h("div", { class: "orbit", attrs: { "aria-hidden": "true" } }), h("h2", { text: "正在点亮星图" }), h("p", { text: "只读取现在还保存着的记忆。" })];
  } else if (S.status === "error") {
    const [title, hint] = ERR[S.error?.code] || ERR.REQUEST_FAILED;
    content = [
      h("h2", { text: title }),
      h("p", { text: S.error?.message || hint }),
      h("code", { text: S.error?.code || "REQUEST_FAILED" }),
      h("button", { class: "btn", text: "重新读取", attrs: { type: "button" }, on: { click: () => (S.meta ? loadItems() : loadMeta()) } }),
    ];
    if (mode === "none") {
      content.push(h("button", { class: "btn ghost", text: "以虚构示例预览界面", attrs: { type: "button" }, on: { click: () => { location.hash = "preview=1"; location.reload(); } } }));
    }
  } else if (S.status === "ready" && !S.items.length) {
    content = filtersActive()
      ? [h("h2", { text: "没有符合筛选的记忆" }), h("p", { text: "这一页没有，不代表整个记忆库都没有。" }),
        h("button", { class: "btn", text: "清除筛选", attrs: { type: "button" }, on: { click: resetFilters } })]
      : [h("h2", { text: "这片星空还很安静" }), h("p", { text: "角色还没有留下可以展示的记忆。聊过、整理过之后，星星会出现在这里。" })];
  }
  box.hidden = !content;
  if (content) box.append(h("div", { class: "box" }, content));
}

function renderAll() {
  renderControls();
  renderState();
  renderTally();
  if (S.view === "map") draw();
  else renderTimeline();
  renderDossier();
}

// ---------- 观测卡 ----------

function row(label, value, sub, block = false) {
  return h("div", { class: `row${block ? " block" : ""}` }, [
    h("dt", { text: label }),
    block ? null : h("span", { class: "lead", attrs: { "aria-hidden": "true" } }),
    h("dd", {}, [value, sub ? h("span", { class: "sub", text: sub }) : null]),
  ]);
}

function renderIntro(panel) {
  panel.append(h("div", { class: "ds-intro" }, [
    h("p", { class: "ds-kind", text: "怎样看这片星空" }),
    h("h2", { attrs: { id: "dossier-title" }, text: "选一颗星看看" }),
    h("p", { class: "poem", text: "每一颗星，都是角色现在还留着的一段记忆。越靠近中心越新，越往外越久；同一个会话的记忆，落在同一片星区。" }),
    h("p", { text: "点星体看角色的记述、实际的交谈时间和来源；点话题或人物，可以把同类的记忆聚到一起。不想用星图的话，切到「时间线」也能找到全部。" }),
    h("ul", { class: "legend" }, KIND_ORDER.filter((k) => S.kinds.includes(k) || !S.kinds.length).map((k) =>
      h("li", {}, [miniGlyph(k, 30), h("div", {}, [h("b", { text: `${KIND[k].name} · ${KIND[k].body}` }), h("span", { text: KIND[k].hint })])]))),
    h("p", { class: "ds-note", text: "星体的亮暗只表示记得多清楚，不表示好感，也不表示快要被删除。" }),
  ]));
}

function renderDossier() {
  const panel = $("#dossier");
  panel.replaceChildren();
  panel.style.removeProperty("--k");
  if (!S.sel) { renderIntro(panel); return; }

  const listed = S.byId.get(S.sel);
  if (S.detailState === "error" && S.detailError?.code === "ITEM_NOT_FOUND") {
    panel.append(
      h("p", { class: "ds-kind", text: "观测记录" }),
      h("h2", { attrs: { id: "dossier-title" }, text: "这条记忆已不存在" }),
      h("p", { class: "ds-note", text: "旧的详情已经关掉。星图需要重新读取，才能和现在保存的内容一致。" }),
      h("div", { class: "chips" }, [
        h("button", { class: "btn", text: "重新读取星图", attrs: { type: "button" }, on: { click: () => { clearSelection(); loadItems(); } } }),
        h("button", { class: "btn ghost", text: "关闭", attrs: { type: "button" }, on: { click: clearSelection } }),
      ]),
    );
    return;
  }
  const node = S.detail?.node || listed;
  if (!node) {
    panel.append(h("h2", { attrs: { id: "dossier-title" }, text: "正在读取…" }), h("div", { class: "skeleton" }), h("div", { class: "skeleton w2" }));
    if (S.detailState === "error") detailError(panel);
    return;
  }
  panel.style.setProperty("--k", `var(--k-${node.kind})`);
  const info = KIND[node.kind] || { name: node.kind, body: "" };

  const glyphBox = h("div", { class: `ds-glyph k-${node.kind}` }, [miniGlyph(node.kind, 34, { importance: node.importance, clarity: node.clarity || { level: 0 }, status: node.status })]);
  panel.append(h("div", { class: "ds-catalog" }, [glyphBox, h("div", {}, [
    h("div", { class: "ds-kind" }, [h("b", { text: info.name }), ` · ${info.body}`]),
    h("div", { class: "ds-src", text: node.source.label }),
  ])]));
  panel.append(h("h2", { attrs: { id: "dossier-title" }, text: node.title }));
  if (node.titleBasis === "category") panel.append(h("span", { class: "ds-basis", text: "没有可摘的正文，标题只表示类别" }));

  if (!listed && S.status === "ready") {
    panel.append(h("div", { class: "ds-banner" }, [
      "这条不在当前筛选结果里（或超出了星图同时显示的数量），星图上暂时看不到它。 ",
      h("button", { class: "link-btn", text: "只看它的来源", attrs: { type: "button" }, on: { click: () => setFilter({ scope: node.source.scope, topicId: null, personId: null, q: "", range: "all", kinds: new Set(S.kinds) }) } }),
    ]));
  }

  const d = S.detail && S.detail.node.id === node.id ? S.detail : null;
  if (d?.original) {
    const seg = h("div", { class: "seg small ds-switch", attrs: { role: "group", "aria-label": "查看哪个版本" } });
    for (const [m, label] of [["remembered", "角色现在记得的样子"], ["original", "原始整理记录"]]) {
      seg.append(h("button", { text: label, attrs: { type: "button", "aria-pressed": String(S.detailMode === m) }, on: { click: () => { S.detailMode = m; renderDossier(); } } }));
    }
    panel.append(seg);
  }
  if (!d) {
    panel.append(h("div", { class: "skeleton" }), h("div", { class: "skeleton w2" }));
    if (node.preview) panel.append(h("p", { class: "ds-text", text: node.preview }));
  } else if (S.detailMode === "original" && d.original) {
    panel.append(
      h("p", { class: "ds-note", text: "这是整理时写下的记录，供你核对；不是原始聊天全文。角色现在想起来时用的是上一页那个版本，查看原稿不会让角色记得更清楚。" }),
      h("dl", { class: "ds-orig ds-text original" }, [
        h("dt", { text: "完整经过" }), h("dd", { text: d.original.summary || "（空）" }),
        h("dt", { text: "核心一句" }), h("dd", { text: d.original.gist || "（空）" }),
        h("dt", { text: "当时的感受" }), h("dd", { text: d.original.feeling || "（没有记下）" }),
      ]),
    );
  } else {
    panel.append(h("p", { class: "ds-text", text: d.rememberedText }));
  }
  if (S.detailState === "error") detailError(panel);

  // 账目
  const ledger = h("dl", { class: "ledger" });
  const t = node.time;
  const pt = pointTime(node);
  ledger.append(row(t.label || "时间", fmtTime(t), t.basis === "unknown" ? "没有可靠的时间记录，不按今天补" : rel(pt)));
  const src = node.source;
  ledger.append(row("来源", h("button", { class: "link-btn", text: src.label, attrs: { type: "button" }, on: { click: () => setFilter({ scope: src.scope }) } }),
    [src.kind === "group" ? "群" : "私聊", sourceSuffix(src)].filter(Boolean).join(" · ")));
  if (node.clarity) {
    const meter = h("span", { class: "meter", attrs: { "aria-hidden": "true" } }, [0, 1, 2, 3].map((i) => h("i", { class: i >= node.clarity.level ? "on" : "" })));
    ledger.append(row("清晰度", h("span", {}, [meter, node.clarity.label]), CLARITY_NOTE[node.clarity.level] || ""));
  }
  if (node.importance != null) {
    const dots = h("span", { class: "dots", attrs: { "aria-hidden": "true" } }, [1, 2, 3, 4, 5].map((i) => h("i", { class: i <= node.importance ? "on" : "" })));
    ledger.append(row("重要性", h("span", {}, [dots, `${node.importance} / 5`])));
  }
  if (node.expiresAt != null) {
    if (node.kind === "thread") ledger.append(row("保留到", fmt(node.expiresAt), `${rel(node.expiresAt)} · 这是关注期限，不是约定发生的时间`));
    else ledger.append(row("删除期限", fmt(node.expiresAt), `${rel(node.expiresAt)} · 低重要性经历满 30 天删除，再想起也不延长`));
  }
  if (node.kind === "pack") ledger.append(row("状态", node.status === "cold" ? "已退出活跃临时段" : "活跃临时段", node.status === "cold" ? "仍然保留，等长期整理" : null));
  if (d?.recall) {
    ledger.append(row("回想计数", `${d.recall.count} 次`, (d.recall.lastCountedAt ? `最近一次计数 ${fmt(d.recall.lastCountedAt)}；` : "") + "每 30 分钟最多计一次，不等于实际使用次数"));
  }
  const a = d?.attributes;
  if (a) {
    if (a.category) ledger.append(row("类别", a.category));
    if (a.about) ledger.append(row("说这句话时在聊", a.about));
    if (a.threadKind) ledger.append(row("悬事类型", a.threadKind));
    if (a.reason) ledger.append(row("为什么记着", a.reason, null, true));
    if (a.createdAt && node.kind !== "thread") ledger.append(row("记下时间", fmt(a.createdAt)));
    if (a.updatedAt && node.kind !== "fact") ledger.append(row("更新时间", fmt(a.updatedAt)));
    if (a.anchors?.length) ledger.append(row("这段的锚点", a.anchors.join("、"), null, true));
    if (a.unresolved?.length) ledger.append(row("没落定的", a.unresolved.join("；"), "只是长期整理时的候选，会重新判断，不等于悬着的事", true));
  }
  panel.append(ledger);

  if (node.topics.length) {
    panel.append(h("div", { class: "ds-section" }, [
      h("h3", { text: "话题" }),
      h("div", { class: "chips" }, node.topics.map((tp) => h("button", {
        class: "chip", attrs: { type: "button", title: "只看这个话题的记忆（跨会话；同标签不代表因果）" },
        on: { click: () => setFilter({ topicId: tp.id }) },
      }, [`# ${tp.label}`]))),
    ]));
  }
  if (node.people.length || node.kind === "fact") {
    panel.append(h("div", { class: "ds-section" }, [
      h("h3", { text: "人物" }),
      node.people.length
        ? h("div", { class: "chips" }, node.people.map((pp) => h("button", {
          class: "chip", attrs: { type: "button", title: "只看和这个账号有关的记忆" },
          on: { click: () => setFilter({ personId: pp.id }) },
        }, [pp.label, h("small", { text: pp.basis === "fact_subject" ? "这条事实说的是他" : "参加过这段聊天" })])))
        : h("p", { class: "ds-note", text: "没有记录可靠的人物字段，不按正文猜是谁。" }),
    ]));
  }
  if (d?.links?.length) {
    const REL = { word_of_episode: "这句话出自", faded_into: "已淡入整体印象" };
    panel.append(h("div", { class: "ds-section" }, [
      h("h3", { text: "已保存的关联" }),
      h("ul", { class: "ds-links" }, d.links.map((l) => h("li", {}, [
        h("span", { class: "rel", text: REL[l.type] || l.type }),
        h("button", { class: "link-btn", text: l.target.title || "未命名记忆", attrs: { type: "button" }, on: { click: () => {
          if (!S.byId.has(l.target.id)) toast("这条关联的记忆不在当前筛选结果里，已单独打开它的详情。");
          openItem(l.target.id);
        } } }),
        S.byId.has(l.target.id) ? null : h("span", { class: "rel", text: "（不在当前星图中）" }),
      ]))),
    ]));
  }
  panel.append(h("p", { class: "ds-foot", text: "查看不会让角色重新想起，也不会增加回想次数。" }));
}

function detailError(panel) {
  const e = S.detailError || {};
  const [title] = ERR[e.code] || ERR.REQUEST_FAILED;
  panel.append(h("div", { class: "ds-banner" }, [
    `${title}：${e.message || ""} `,
    h("button", { class: "link-btn", text: "重试", attrs: { type: "button" }, on: { click: () => openItem(S.sel, { quiet: true }) } }),
  ]));
}

// ---------- 启动 ----------

window.addEventListener("pagehide", () => {
  S.seqMeta += 1;
  S.seqItems += 1;
  S.seqItem += 1;
  S.detail = null;
  S.items = [];
  S.byId.clear();
  S.labels.topic.clear();
  S.labels.person.clear();
  S.querySnapshot = null;
  S.detailMode = "remembered";
  $("#dossier").replaceChildren();
  // 保留SVG图层和defs，浏览器前进/后退缓存恢复后可重新绘制。
  for (const layer of [gGrid, gEdges, gStars, gMarks]) layer?.replaceChildren();
  starEls.clear();
  posCache.clear();
  S.base = null;
  $("#tooltip").replaceChildren();
  $("#tooltip").hidden = true;
  $("#timeline").replaceChildren();
});
window.addEventListener("pageshow", (event) => { if (event.persisted) loadMeta(); });

readHash();
setupChart();
setupTheme();
setupNavigation();
setupBench();
bindControls();
renderControls();
loadMeta();
