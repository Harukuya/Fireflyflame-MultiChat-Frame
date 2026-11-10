// MultiChat 多会话框架 · 宿主提供页面桥；本页不直接连接模型或聊天平台。
const host = window.MultiChatPage || window.AstrBotPluginPage;
const legacyAstrBot = !window.MultiChatPage && Boolean(window.AstrBotPluginPage);
function createBridge(host) {
  const send = async (method, path, data = {}) => {
    if (!host) throw new Error("请先接入 window.MultiChatPage 页面桥，详见 pages/dashboard/README.md。");
    const direct = method === "GET" ? host.apiGet : host.apiPost;
    let result;
    if (typeof direct === "function") result = await direct.call(host, path, data);
    else {
      const request = host.request || host.invoke;
      if (typeof request !== "function") throw new Error("页面桥缺少 request/invoke 或 apiGet/apiPost 方法。");
      result = await request.call(host, { method, path, [method === "GET" ? "params" : "body"]: data });
    }
    if (result?.ok === false && result.error) {
      const error = new Error(result.error.message || "操作失败，请查看宿主日志。");
      error.code = result.error.code || "operation_failed";
      throw error;
    }
    return result;
  };
  return { ready: async () => { if (typeof host?.ready === "function") await host.ready(); },
    apiGet: (path, params = {}) => send("GET", path, params),
    apiPost: (path, body = {}) => send("POST", path, body) };
}
const bridge = createBridge(host);
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function setupUniverseLink() {
  const link = $("memory-universe-link");
  const path = host?.pageUrls?.["memory-universe"];
  if (!link || typeof path !== "string" || !window.location?.href) return;
  try {
    const url = new URL(path, window.location.href);
    if (!/^https?:$/.test(url.protocol) || url.origin !== window.location.origin) return;
    link.href = url.href;
    link.hidden = false;
  } catch { /* A host without this page keeps the optional navigation hidden. */ }
}

const COLORS = { night: "var(--night)", downtime: "var(--idle)", duty: "var(--day)", mission: "var(--mission)", sleep: "var(--rest)", offline: "var(--line)", offline_mission: "var(--mission-offline)", offline_rest: "var(--rest-offline)" };
const NAMES = { night: "夜里在线", downtime: "空下来在线", duty: "白天有事时在线", mission: "💬 忙碌时在线", sleep: "休息时在线", offline: "不在线", offline_mission: "⏳ 忙碌中（不在线）", offline_rest: "🌙 休息中（不在线）" };

function presence(h) {
  // 兼容旧后台尚未给 statusKey 的刷新；在线状态优先显示。
  const key = h.online ? "online" : (h.statusKey === "mission" || (!h.statusKey && h.status === "忙碌中") ? "mission" : "rest");
  return { key, icon: { online: "💬", mission: "⏳", rest: "🌙" }[key],
    label: { online: "在线，可以聊天", mission: "忙碌中，不在线", rest: "休息中，不在线" }[key] };
}

let memTab = "episodes";
let memData = null;
let overviewData = null;
let usagePeriod = "today";
let toolModelPeriod = "total";
let intimacyRows = [];

function countValue(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
}

const countText = (value) => countValue(value).toLocaleString("zh-CN");

function setPeriodButtons(prefix, period) {
  for (const key of ["today", "total"]) {
    const button = $(prefix + "-" + key);
    button?.setAttribute?.("aria-pressed", String(key === period));
    button?.classList?.toggle?.("on", key === period);
  }
}

function cumulativeNote(data) {
  if (!data) return "累计统计将在更新后的后台开始记录。";
  const since = data.sinceText ? `累计记录始于 ${data.sinceText}。` : "累计统计从启用本次更新时开始。";
  return `${since}${data.note || "仅包含升级时仍保留的今日统计及之后的数据；更早的历史不补算。"}`;
}

function renderCounters(d) {
  const labels = ["回话", "群里说话", "无需回复", "不想回", "看手机", "判断调用", "模型调用", "模型失败", "表情"];
  const stats = d.stats || {};
  const total = d.statsTotal?.counts || {};
  $("stats").innerHTML = labels.map((key) => `<div class="stat">
    <span class="stat-name">${key}</span><div class="stat-values">
      <div><b>${countText(stats[key])}</b><span>今日</span></div>
      <div class="stat-total"><b>${d.statsTotal ? countText(total[key]) : "—"}</b><span>累计</span></div>
    </div></div>`).join("");
  $("stats-total-note").textContent = cumulativeNote(d.statsTotal);
}

function renderModelUsage(d) {
  const data = d || {};
  setPeriodButtons("usage", usagePeriod);
  const usage = usagePeriod === "total" ? data.statsTotal?.modelUsage || {} : data.stats?.model_usage || {};
  $("usage-range-note").textContent = usagePeriod === "total" ? cumulativeNote(data.statsTotal) : "今日实际请求；跨日后今日计数重新开始，累计继续保留。";
  const entries = Object.entries(usage).filter(([, row]) => row && typeof row === "object");
  const fields = ["requests", "retries", "failed", "cancelled", "cachedTokens", "cachedKnown", "inputTokens", "inputKnown",
    "outputTokens", "outputKnown", "reasoningTokens", "reasoningKnown", "usageUnknown", "usagePartial", "elapsedMs"];
  const total = Object.fromEntries(fields.map((field) => [field, entries.reduce((sum, [, row]) => sum + countValue(row[field]), 0)]));
  const renderRow = (category, u, isTotal = false) => `<tr${isTotal ? ' class="usage-total"' : ""}>
    <td>${esc(category)}</td><td>${countText(u.requests)} / ${countText(u.retries)}</td>
    <td>${countText(u.failed)} / ${countText(u.cancelled)}</td>
    <td title="缓存输入 ${countText(u.cachedTokens)}；${countText(u.cachedKnown)} 次返回缓存用量">${countValue(u.inputKnown) ? countText(u.inputTokens) : "—"}<br><span class="muted">${countText(u.inputKnown)}/${countText(u.requests)} 次有数据</span></td>
    <td title="推理输出 ${countText(u.reasoningTokens)}；${countText(u.reasoningKnown)} 次返回推理用量">${countValue(u.outputKnown) ? countText(u.outputTokens) : "—"}<br><span class="muted">${countText(u.outputKnown)}/${countText(u.requests)} 次有数据</span></td>
    <td>未返回 ${countText(u.usageUnknown)} 次<br>不完整 ${countText(u.usagePartial)} 次</td>
    <td>${esc((countValue(u.elapsedMs) / 1000).toLocaleString("zh-CN", { minimumFractionDigits: 1, maximumFractionDigits: 1 }))} 秒</td>
  </tr>`;
  $("model-usage").querySelector("tbody").innerHTML = entries.length
    ? renderRow(usagePeriod === "total" ? "累计合计" : "今日合计", total, true) + entries.map(([category, u]) => renderRow(category, u)).join("")
    : `<tr><td colspan="7" class="muted">${usagePeriod === "total" ? "累计范围内" : "今天"}还没有记录到模型请求。</td></tr>`;
}

function renderToolModelChart(data) {
  const d = data || {};
  setPeriodButtons("tool-model", toolModelPeriod);
  $("tool-model-since").textContent = `${d.sinceText ? `按模型记录始于 ${d.sinceText}。` : "按模型统计从更新后的调用开始。"}${d.note || "旧记录没有模型归属，保留在下方详细计数中，不混入图表。图中按任务结束日期计数。"}${d.details?.sinceText ? `原因和工具明细始于 ${d.details.sinceText}。` : "原因和工具明细从本版的新任务开始记录。"}`;
  const fields = [["firstSuccess", "无需补交成功", "first"], ["recovered", "补交成功", "recovered"],
    ["failed", "最终失败", "failed"], ["cancelled", "取消", "cancelled"]];
  const rows = (Array.isArray(d[toolModelPeriod]) ? d[toolModelPeriod] : []).filter((row) => row && typeof row === "object").map((row) => {
    const values = fields.map(([key]) => countValue(row[key]));
    const finished = values.reduce((sum, n) => sum + n, 0);
    const completed = values[0] + values[1] + values[2];
    return { ...row, values, finished, completed };
  }).sort((a, b) => b.finished - a.finished || String(a.model || "").localeCompare(String(b.model || "")));
  $("tool-model-chart").innerHTML = rows.map((row) => {
    const model = row.model || "未知模型";
    const provider = row.provider || "未识别提供商";
    const rate = row.completed ? ((row.values[0] + row.values[1]) / row.completed * 100).toFixed(1) + "%" : "—";
    const detail = fields.map(([, label], i) => `${label} ${countText(row.values[i])} 次`).join("，");
    const recorded = (Array.isArray(d.details?.[toolModelPeriod]) ? d.details[toolModelPeriod] : []).find((item) => item.id === row.id)?.fields || {};
    return `<div class="tool-model-row">
      <div class="tool-model-title"><div class="tool-model-name"><b>${esc(model)}</b><span>${esc(provider)}</span></div>
        <div class="tool-model-summary"><b>成功率 ${rate}</b><span>已结束 ${countText(row.finished)} 次 · 其中取消 ${countText(row.values[3])} 次</span></div></div>
      <div class="tool-model-bar" role="img" aria-label="${esc(model)}；${esc(provider)}；${esc(detail)}；成功率 ${rate}">
        ${row.finished ? fields.map(([, label, css], i) => row.values[i] ? `<span class="result-${css}" style="width:${row.values[i] / row.finished * 100}%" title="${label} ${countText(row.values[i])} 次（${(row.values[i] / row.finished * 100).toFixed(1)}%）" aria-hidden="true"></span>` : "").join("") : '<span class="tool-model-zero">尚无已结束任务</span>'}
      </div>
      <div class="tool-model-values">${fields.map(([, label, css], i) => `<span><i class="result-${css}"></i>${label} <b>${countText(row.values[i])}</b></span>`).join("")}</div>
      <div class="tool-model-stages">${toolDetailFields().map(([field, label]) => {
        const partial = ["firstMissing", "missingResponses", "repairs"].includes(field);
        return `<div class="tool-model-stage"><span class="tool-stage-label">${label}${partial ? '<small>本版明细</small>' : ""}</span>${toolDetailCell(field, partial ? null : row[field], recorded[field], `${model} · ${toolModelPeriod === "today" ? "今日" : "累计"} · ${label}`, partial)}</div>`;
      }).join("")}</div>
    </div>`;
  }).join("") || `<div class="chart-empty">${toolModelPeriod === "today" ? "今天" : "累计范围内"}还没有按模型记录的已结束任务。旧的用途统计仍在下方显示。</div>`;
}

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2200);
}

async function call(fn) {
  try {
    return await fn();
  } catch (e) {
    toast(e.message || String(e));
    return null;
  }
}

// ---------------- 角色现在 / 模型 / 今天 / 环境 ----------------

function renderOverview(d) {
  overviewData = d;
  const h = d.her;
  $("character-heading").textContent = `${h.name || "角色"}现在`;
  $("status").textContent = h.status;
  const state = presence(h);
  const icon = $("status-dot");
  icon.className = "status-icon " + state.key;
  icon.textContent = state.icon;
  icon.title = state.label;
  icon.setAttribute("aria-label", state.label);
  $("activity").textContent = h.activity + (h.destination ? `（${h.destination.split("，")[0]}）` : "");
  $("detail").textContent = h.detail;
  $("her-hour").textContent = h.herHour;
  $("online-span").textContent = h.online
    ? (h.offlineAt ? `${h.onlineSince || "—"} 上线 → ${h.offlineAt} 下线` : "持续在线，没有计划下线时间")
    : `下次上线 ${h.nextOnline || "48 小时内没有"}`;
  $("mood").innerHTML = h.mood == null ? "—" : `<span class="bar"><i style="width:${h.mood}%"></i></span>${h.mood}（${esc(h.moodBand)}）`;
  $("energy").innerHTML = `<span class="bar"><i style="width:${h.energy}%"></i></span>${h.energy}（${esc(h.energyBand)}）`;
  $("thought").textContent = h.thought ? `${h.thought.kind}：${h.thought.topic || "—"}（${h.thought.due} 掂量）` : "没有";

  const tl = $("timeline");
  tl.innerHTML = h.timeline.map(([a, b, k]) => `<span title="${esc(NAMES[k] || k)} ${fmt(a)}–${fmt(b)}" style="width:${((b - a) / 1440) * 100}%;background:${COLORS[k] || "var(--line)"}"></span>`).join("")
    + `<i class="now" style="left:${(h.nowMinute / 1440) * 100}%"></i>`;
  const used = [...new Set(h.timeline.map((x) => x[2]))];
  $("legend").innerHTML = used.map((k) => `<span><i style="background:${COLORS[k]}"></i>${esc(NAMES[k] || k)}</span>`).join("");

  const m = d.model;
  $("m-chat").textContent = m.chat;
  $("m-judge").textContent = m.judgeIsChat ? `${m.judge}（与对话使用同一模型）` : m.judge;
  $("m-summary").textContent = m.summary || "未配置";
  $("m-caps").textContent = `工具调用 ${m.tools ? "✓" : "✗"} · 看图 ${m.image ? "✓" : m.caption ? `✗，转述：${m.caption}` : "✗"}`;
  $("m-vec").textContent = m.vectors;
  $("m-data").textContent = m.data;
  $("m-version").textContent = m.version || "—";

  renderCounters(d);
  renderModelUsage(d);
  renderToolResults(d.toolResults);
  renderToolModelChart(d.toolResults?.models);
  $("env").innerHTML = d.env.map((x) => `<li class="${x.level}">${esc(x.text)}</li>`).join("");
  const uf = d.unfinished || [];
  $("unfinished").innerHTML = uf.length
    ? uf.map((x) => `<li class="warn">${x.scope ? `<b>${esc(x.scope)}</b>：` : ""}${esc(x.text)}</li>`).join("")
    : `<li class="ok">现在没有正在进行的临时任务。</li>`;

  const st = d.stickers || {};
  const available = Object.entries(st).filter(([, n]) => n > 0);
  $("sticker-count").textContent = available.map(([k, n]) => `${k} ${n} 张`).join(" · ") || "还没有表情图片，添加后才会向模型提供发表情工具。";
  if (d.stickerDir) $("sticker-dir").textContent = "本机实际目录：" + d.stickerDir;

  $("events").innerHTML = d.events.map((e) => `<div class="ev ${e.level}"><span class="muted">${e.time}</span><span class="k">${esc(e.kind)}</span><span class="t">${e.scope ? `<span class="s">${esc(e.scope)}</span>` : ""}${esc(e.text)}</span></div>`).join("") || `<span class="muted">还没有动静</span>`;
  $("updated").textContent = "更新于 " + new Date().toLocaleTimeString();
}

function renderToolResults(data) {
  const d = data || {};
  $("tool-results-since").textContent = d.sinceText
    ? `从 ${d.sinceText} 开始记录；累计数据会保存，重启不清零。更新前的调用不回填。`
    : "安装本次更新后的第一次必需结果调用开始记录；更新前的调用不回填。";
  $("tool-results-since").textContent += d.details?.sinceText
    ? ` 原因和工具明细从 ${d.details.sinceText} 开始；此前总数保留，不猜测旧原因。`
    : " 原因和工具明细从本版开始；旧总数保留，未记录的原因单列。";
  const fields = ["tasks", "firstMissing", "missingResponses", "repairs", "recovered", "failed", "cancelled"];
  const rows = [];
  for (const [key, label] of [["today", "今日"], ["total", "累计"]]) {
    const entries = Object.entries(d[key] || {}).filter(([, row]) => row && typeof row === "object");
    const total = Object.fromEntries(fields.map((field) => [field, entries.reduce((sum, [, row]) => sum + toolDetailNumber(row[field]), 0)]));
    const details = d.details?.[key] || {};
    const totalDetails = Object.fromEntries(fields.map((field) => [field, entries.flatMap(([category]) => Array.isArray(details[category]?.[field]) ? details[category][field] : [])]));
    const cells = (counts, buckets, title) => fields.map((field) => `<td>${field === "tasks" ? esc(toolDetailNumber(counts[field])) : toolDetailCell(field, counts[field], buckets?.[field], `${title} · ${toolDetailFields().find(([f]) => f === field)[1]}`)}</td>`).join("");
    rows.push(`<tr class="tool-result-total"><td>${label}合计</td>${cells(total, totalDetails, `${label}合计`)}</tr>`);
    for (const [category, row] of entries) {
      rows.push(`<tr><td>${label} · ${esc(category)}</td>${cells(row, details[category], `${label} · ${category}`)}</tr>`);
    }
  }
  $("tool-results").querySelector("tbody").innerHTML = rows.join("");
}

function toolDetailFields() {
  return [["firstMissing", "首次缺失"], ["missingResponses", "缺失响应"], ["repairs", "发起补交"],
    ["recovered", "补交成功"], ["failed", "最终失败"], ["cancelled", "取消"]];
}

function toolDetailNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
}

function toolDetailLabel(reason) {
  return ({ "text-only": "仅回复正文", "wrong-tool": "调用错工具", "no-result-tools": "未配置提交工具",
    "no-key": "未配置模型", auth: "认证失败", "bad-endpoint": "接口地址错误", "bad-model": "模型配置错误",
    "no-network": "连接失败", quota: "额度不足", timeout: "超时", "rate-limit": "限流／过载",
    server: "服务端错误", empty: "空回复", unknown: "未知错误", cancelled: "任务取消" })[reason] || "未知错误";
}

function toolDetailTools(tools) {
  const names = { save_memory: "整理记忆", tag_memories: "补记忆话题", judge: "接话判断", read_more: "多看几条" };
  return tools.length ? tools.map((name) => names[name] ? `${names[name]}（${name}）` : name).join(" / ") : "未记录工具";
}

function toolDetailBuckets(value) {
  const merged = new Map();
  for (const item of Array.isArray(value) ? value : []) {
    if (!item || typeof item !== "object") continue;
    const count = toolDetailNumber(item.count);
    if (!count) continue;
    const reason = typeof item.reason === "string" ? item.reason : "unknown";
    const tools = [...new Set((Array.isArray(item.tools) ? item.tools : []).filter((name) => typeof name === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(name)))].sort().slice(0, 8);
    const exception = typeof item.exception === "string" && /^[A-Za-z_][A-Za-z0-9_]{0,79}$/.test(item.exception) ? item.exception : "";
    const http = Number.isInteger(item.http) && item.http >= 100 && item.http <= 599 ? item.http : null;
    const key = JSON.stringify([reason, tools, exception, http]);
    if (merged.has(key)) merged.get(key).count += count;
    else merged.set(key, { reason, tools, exception, http, count });
  }
  return [...merged.values()].sort((a, b) => b.count - a.count || JSON.stringify(a).localeCompare(JSON.stringify(b)));
}

function toolDetailSummary(total, value, partial = false) {
  const buckets = toolDetailBuckets(value);
  const known = buckets.reduce((sum, item) => sum + item.count, 0);
  const count = total == null ? known : toolDetailNumber(total);
  const missing = Math.max(0, count - known);
  const causes = new Map();
  for (const item of buckets) causes.set(item.reason, (causes.get(item.reason) || 0) + item.count);
  const reasons = [...causes].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const top = reasons[0];
  const same = top ? reasons.filter(([, n]) => n === top[1]).length : 0;
  const toolCounts = new Map();
  for (const item of buckets.filter((x) => x.reason === top?.[0])) {
    const key = JSON.stringify(item.tools);
    toolCounts.set(key, (toolCounts.get(key) || 0) + item.count);
  }
  const topTools = [...toolCounts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0];
  return { count, known, missing, buckets, reasons, partial,
    top: top ? `${missing || partial ? "已知最多" : "最多"}：${toolDetailLabel(top[0])} ${top[1]} 次${same > 1 ? "（并列）" : ""}` : (missing ? "旧记录／未记录明细" : "—"),
    tools: topTools ? toolDetailTools(JSON.parse(topTools[0])) : "" };
}

function toolDetailCell(field, total, buckets, title, partial = false) {
  const d = toolDetailSummary(total, buckets, partial);
  const payload = { field, total: total == null ? null : d.count, buckets: d.buckets, title, partial };
  const number = partial && !d.known ? "—" : d.count.toLocaleString("zh-CN");
  return `<button type="button" class="tool-detail-cell" data-tool-detail="${esc(JSON.stringify(payload))}" aria-label="${esc(`${title}：${number}${partial ? "（仅已记录明细）" : "次"}；${d.top}；${d.tools}。查看完整分布`)}" aria-haspopup="dialog">
    <b>${number}${partial && d.known ? '<small>已记录</small>' : ""}</b><span class="tool-detail-top">${esc(d.top)}</span>
    ${d.tools ? `<span class="tool-detail-tool">${esc(d.tools)}</span>` : ""}${d.missing ? `<span class="tool-detail-unknown">另有 ${d.missing} 次未记录明细</span>` : ""}</button>`;
}

function toolDetailContent(payload) {
  const d = toolDetailSummary(payload.total, payload.buckets, payload.partial);
  const note = ["repairs", "recovered"].includes(payload.field) ? "这里列的是补交前的缺失原因，补交成功不算失败。"
    : payload.field === "cancelled" ? "取消单独统计，不计入失败或成功率。" : "每次事件按一个原因和本轮可提交的工具集合记录。";
  const denominator = Math.max(d.count, d.known);
  const percentage = (n) => denominator ? `${(n / denominator * 100).toFixed(1)}%` : "—";
  return `<div class="tool-detail-head"><h3 id="tool-detail-title">${esc(payload.title)}</h3><button type="button" class="small ghost" data-tool-detail-close aria-label="关闭类型明细">关闭</button></div>
    <p class="muted">${esc(note)}工具有多个时，表示本轮可提交其中之一。</p>
    <p class="tool-detail-overview">${d.reasons.length ? d.reasons.map(([reason, n]) => `<span>${esc(toolDetailLabel(reason))} <b>${n}</b></span>`).join("") : "暂无已记录的原因。"}</p>
    ${payload.partial ? '<p class="muted">仅统计本版记录的阶段明细；更早的该阶段次数无法按模型还原，不以零代替。</p>' : ""}
    ${d.missing ? `<p class="tool-detail-unknown">${d.missing} 次旧记录／未记录明细（${percentage(d.missing)}）；不能据此推断原因。</p>` : ""}
    ${d.buckets.length ? `<div class="tool-detail-list">${d.buckets.map((item) => `<div class="tool-detail-item"><div><b>${esc(toolDetailLabel(item.reason))}</b>${item.exception || item.http ? `<span class="muted">${esc([item.exception, item.http ? `HTTP ${item.http}` : ""].filter(Boolean).join(" · "))}</span>` : ""}<span>${esc(toolDetailTools(item.tools))}</span></div><strong>${item.count} 次<small>${percentage(item.count)}</small></strong></div>`).join("")}</div>` : '<p class="muted">暂无明细。</p>'}`;
}

const fmt = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

// 浮层放在 body，避免被可横滑表格裁切；悬停、焦点和触屏点击都能查看。
const toolDetailPopup = $("tool-detail-popover");
let toolDetailAnchor = null;
let toolDetailPinned = false;
let toolDetailTimer = null;
function hideToolDetail() {
  clearTimeout(toolDetailTimer);
  if (toolDetailPopup) toolDetailPopup.hidden = true;
  toolDetailAnchor?.removeAttribute?.("aria-expanded");
  toolDetailAnchor = null;
  toolDetailPinned = false;
}
function showToolDetail(button, pin = false) {
  if (!button || !toolDetailPopup) return;
  if (toolDetailPinned && !pin) return;
  clearTimeout(toolDetailTimer);
  let payload;
  try { payload = JSON.parse(button.dataset.toolDetail); } catch { return; }
  toolDetailAnchor?.removeAttribute?.("aria-expanded");
  toolDetailAnchor = button;
  toolDetailPinned = pin;
  toolDetailPopup.innerHTML = toolDetailContent(payload);
  toolDetailPopup.hidden = false;
  button.setAttribute("aria-expanded", "true");
  const rect = button.getBoundingClientRect();
  const box = toolDetailPopup.getBoundingClientRect();
  toolDetailPopup.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - box.width - 12))}px`;
  toolDetailPopup.style.top = `${Math.max(12, Math.min(rect.bottom + 8, window.innerHeight - box.height - 12))}px`;
}
document.addEventListener?.("pointerover", (event) => {
  const button = event.target.closest?.("[data-tool-detail]");
  if (button) {
    clearTimeout(toolDetailTimer);
    if (button !== toolDetailAnchor) showToolDetail(button);
  }
  if (toolDetailPopup?.contains(event.target)) clearTimeout(toolDetailTimer);
});
document.addEventListener?.("pointerout", (event) => {
  if (!toolDetailPinned && toolDetailAnchor && !toolDetailAnchor.contains(event.relatedTarget)
      && !toolDetailPopup?.contains(event.relatedTarget)) toolDetailTimer = setTimeout(hideToolDetail, 150);
});
document.addEventListener?.("focusin", (event) => {
  const button = event.target.closest?.("[data-tool-detail]");
  if (button) showToolDetail(button);
});
document.addEventListener?.("focusout", (event) => {
  if (!toolDetailPinned && !toolDetailPopup?.contains(event.relatedTarget)
      && !event.relatedTarget?.closest?.("[data-tool-detail]")) hideToolDetail();
});
document.addEventListener?.("click", (event) => {
  const button = event.target.closest?.("[data-tool-detail]");
  if (button) {
    showToolDetail(button, true);
    if (event.detail === 0) toolDetailPopup?.querySelector?.("[data-tool-detail-close]")?.focus();
    return;
  }
  if (event.target.closest?.("[data-tool-detail-close]")) {
    const anchor = toolDetailAnchor;
    hideToolDetail();
    if (event.detail === 0) {
      anchor?.focus();
      hideToolDetail();
    }
  } else if (!toolDetailPopup?.contains(event.target)) hideToolDetail();
});
document.addEventListener?.("keydown", (event) => {
  if (event.key === "Escape" && toolDetailAnchor) {
    event.preventDefault();
    const anchor = toolDetailAnchor;
    hideToolDetail();
    anchor.focus();
    hideToolDetail();
  }
});
window.addEventListener("resize", hideToolDetail);

for (const period of ["today", "total"]) {
  $("usage-" + period).addEventListener("click", () => {
    usagePeriod = period;
    renderModelUsage(overviewData);
  });
  $("tool-model-" + period).addEventListener("click", () => {
    toolModelPeriod = period;
    renderToolModelChart(overviewData?.toolResults?.models);
  });
}

// ---------------- 会话 ----------------

function sw(row, field, disabled = false) {
  return `<label class="switch"><input type="checkbox" data-kind="${esc(row.kind)}" data-id="${esc(row.id)}" data-field="${field}" ${row[field] ? "checked" : ""} ${disabled ? "disabled" : ""}><span></span></label>`;
}

function roundStatus(r) {
  const accumulating = r.open || 0;
  const coldPending = r.coldPending || 0;
  const waiting = r.frozenPending ?? Math.max(0, r.pending - accumulating - coldPending);
  return `当段记事 ${esc(r.live)} 包 · 正在积累 ${esc(accumulating)} 段 · 待写 ${esc(waiting)} 段${r.gaveUp ? ` · 写不成 ${esc(r.gaveUp)} 段` : ""}${r.busy ? " · 正在写记事" : ""}<br>往段待整理记事 ${esc(Math.max(0, r.packs - r.live))} 包 · 未成包 ${esc(coldPending)} 段`;
}

function renderSessions(d) {
  $("wl-note").textContent = d.note || (d.whitelist ? "和宿主白名单同步：这里加、删会写回白名单" : "由框架会话名单管理；平台自身权限仍由适配器负责");
  $("sessions").querySelector("tbody").innerHTML = d.rows.map((r) => `<tr>
      <td>${r.kind === "group" ? "群 · " : "私聊 · "}${esc(r.name)}</td>
      <td class="muted">${esc(r.id)}</td>
      <td>${sw(r, "enabled")}</td>
      <td>${r.kind === "group" || r.trailblazer ? sw(r, "proactive") : `<span class="muted">—</span>`}</td>
      <td>${sw(r, "memory")}</td>
      <td class="muted">${esc(r.last || "")}${r.kind === "group" ? `<br>本群看手机 ${esc(r.lookCount ?? 0)}/${esc(r.lookMax ?? 2)} 次（当前时段）` : ""}${r.lastLook ? `<br>上次看 ${esc(r.lastLook)}` : ""}${r.history ? `<br>原文档案 ${esc(r.history.messages)} 条` : ""}${r.rounds ? `<br>${roundStatus(r.rounds)}${r.summaryModel ? `<br>记事模型 ${esc(r.summaryModel)}` : ""}` : ""}</td>
      <td class="row-actions"><button class="small ghost" data-remove="${esc(r.kind)}:${esc(r.id)}">拿掉</button></td>
    </tr>`).join("") || `<tr><td colspan="7" class="muted">还没有受管会话，请在下方添加群聊或账号 ID。主要联系人账号请在框架配置中设置。</td></tr>`;
}

$("sessions").addEventListener("change", async (ev) => {
  const el = ev.target;
  if (!el.dataset.field) return;
  const ok = await call(() => bridge.apiPost("sessions/update", { kind: el.dataset.kind, id: el.dataset.id, field: el.dataset.field, value: el.checked }));
  if (ok) toast("改好了");
  loadSessions();
});

$("sessions").addEventListener("click", async (ev) => {
  const v = ev.target.dataset?.remove;
  if (!v) return;
  const split = v.indexOf(":");
  const kind = v.slice(0, split), id = v.slice(split + 1);
  if (!confirm(`把 ${id} 从名单里拿掉？会停止接新消息，并中止该会话当前任务；已经发出的消息保留。`)) return;
  const r = await call(() => bridge.apiPost("sessions/remove", { kind, id }));
  if (r) toast(r.reloaded === false ? "拿掉了（重启宿主后白名单生效）" : "拿掉了");
  loadSessions();
});

$("add-session").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const id = $("add-id").value.trim();
  if (!id || id.length > 200 || /[\u0000-\u001f\u007f]/.test(id)) return toast("请填不超过 200 字的有效会话或账号 ID");
  if (legacyAstrBot && !/^\d+$/.test(id)) return toast("此 AstrBot QQ 适配器的账号和群号需要填数字");
  const r = await call(() => bridge.apiPost("sessions/add", { kind: $("add-kind").value, id }));
  if (r) {
    $("add-id").value = "";
    $("add-note").textContent = r.reloaded === false ? "已写进宿主白名单，重启宿主后生效" : "";
    toast("加进名单了");
  }
  loadSessions();
});

// ---------------- 表情包 ----------------

$("load-stickers").addEventListener("click", async () => {
  const r = await call(() => bridge.apiGet("sticker"));
  if (!r) return;
  const available = Object.entries(r.library || {}).filter(([, items]) => items.length);
  $("stickers").innerHTML = available.map(([k, items]) => `<div class="emo"><b>${esc(k)}</b>${items.map((i) => `<img src="${esc(i.src)}" alt="${esc(i.name)}" title="${esc(i.name)}">`).join("")}</div>`).join("")
    || `<span class="muted">还没有图片。请先在上面的实际目录中新建中文类别文件夹并放入图片。</span>`;
  $("sticker-count").textContent = available.map(([k, items]) => `${k} ${items.length} 张`).join(" · ") || "还没有表情图片，添加后才会向模型提供发表情工具。";
  $("sticker-dir").textContent = "本机实际目录：" + r.dir;
});

// ---------------- 记忆 ----------------

const MEM_RENDER = {
  episodes: (x) => `<div><span class="tag">${esc(x.level)}</span><span class="tag">重要 ${x.weight}</span>${esc(x.summary)}<div class="meta">${esc(x.when)} · 记得最久：${esc(x.gist)} · ${esc(x.feeling)}${x.topics.length ? " · " + esc(x.topics.join("、")) : ""}</div></div>`,
  facts: (x) => `<div><span class="tag">${esc(x.category)}</span>${x.who ? `<span class="tag">${esc(x.who)}</span>` : ""}${esc(x.content)}<div class="meta">${esc(x.when)}</div></div>`,
  words: (x) => `<div>「${esc(x.text)}」<div class="meta">${esc(x.when)} · 在聊${esc(x.about)}</div></div>`,
  threads: (x) => `<div>${x.kind ? `<span class="tag">${esc(x.kind)}</span>` : ""}${esc(x.content)}<div class="meta">记下于 ${esc(x.when)}${x.expires ? ` · 保留至 ${esc(x.expires)}` : ""}${x.reason ? `<br>值得再关心：${esc(x.reason)}` : ""}</div></div>`,
  haze: (x) => `<div><span class="tag">${esc(x.month)}</span>${esc(x.text)}</div>`,
};
const MEM_KIND = { episodes: "episode", facts: "fact", words: "word", threads: "thread" };
const MEM_VERB = { episodes: "忘掉", facts: "作废", words: "忘掉", threads: "有结果了" };

function renderMemory() {
  const d = memData;
  if (!d) return;
  const sel = $("mem-scope");
  const cur = d.scope;
  sel.innerHTML = d.scopes.map((s) => `<option value="${esc(s.scope)}" ${s.scope === cur ? "selected" : ""}>${esc(s.label)}</option>`).join("");
  if (d.empty) {
    $("mem-note").textContent = d.scopes.length ? "这个会话还没有记忆" : "还没有受管会话；添加会话并开启长期记忆后，可在这里查看和整理。";
    $("mem-list").innerHTML = "";
    $("mem-run").disabled = true;
    return;
  }
  $("mem-run").disabled = false;
  $("mem-note").textContent = `${d.enabled ? "整理记忆开着" : "整理记忆关着（在会话里打开）"} · 还没整理的 ${d.pending} 条`;
  const list = d[memTab] || [];
  $("mem-list").innerHTML = list.length
    ? list.map((x) => `<div class="item">${MEM_RENDER[memTab](x)}${MEM_KIND[memTab] ? `<button class="small ghost" data-retire="${MEM_KIND[memTab]}:${x.id}">${MEM_VERB[memTab]}</button>` : ""}</div>`).join("")
    : `<span class="muted">没有</span>`;
}

async function loadMemory(scope) {
  const r = await call(() => bridge.apiGet("memory", scope ? { scope } : {}));
  if (r) {
    memData = r;
    renderMemory();
  }
}

$("mem-scope").addEventListener("change", () => loadMemory($("mem-scope").value));
$("mem-tabs").addEventListener("click", (ev) => {
  const t = ev.target.dataset?.tab;
  if (!t) return;
  memTab = t;
  [...$("mem-tabs").children].forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  renderMemory();
});
$("mem-list").addEventListener("click", async (ev) => {
  const v = ev.target.dataset?.retire;
  if (!v || !memData) return;
  const [kind, id] = v.split(":");
  if (!confirm("确定？")) return;
  await call(() => bridge.apiPost("memory/retire", { scope: memData.scope, kind, id: Number(id) }));
  loadMemory(memData.scope);
});
$("mem-run").addEventListener("click", async () => {
  if (!memData) return;
  $("mem-run").disabled = true;
  const r = await call(() => bridge.apiPost("memory/run", { scope: memData.scope }));
  $("mem-run").disabled = false;
  if (r) toast(r.message);
  loadMemory(memData.scope);
});

// ---------------- 亲密度 ----------------

function resizeIntimacyViewport() {
  const wrap = $("intimacy-scroll");
  const table = $("intimacy");
  if (!wrap || !table) return;
  const rows = Array.from(table.querySelectorAll("tbody tr"));
  // 固定行高后仍按实际排版量六行；长内容在只读详情里完整显示。
  const headerHeight = table.querySelector("thead")?.getBoundingClientRect().height || 0;
  const rowsHeight = rows.slice(0, 6).reduce((height, row) => height + row.getBoundingClientRect().height, 0);
  const horizontalScrollbar = Math.max(0, wrap.offsetHeight - wrap.clientHeight);
  wrap.style.maxHeight = `${Math.ceil(headerHeight + rowsHeight + horizontalScrollbar)}px`;
}

async function loadIntimacy() {
  const r = await call(() => bridge.apiGet("intimacy"));
  if (!r) return;
  const wrap = $("intimacy-scroll");
  const scrollTop = wrap.scrollTop, scrollLeft = wrap.scrollLeft;
  intimacyRows = r.rows;
  $("intimacy-count").textContent = `共 ${countText(r.rows.length)} 人`;
  $("intimacy").querySelector("tbody").innerHTML = r.rows.map((x, index) => `<tr>
      <td><div class="intimacy-cell"><span class="intimacy-preview">${esc(x.name)}</span></div></td>
      <td class="muted"><div class="intimacy-cell">${esc(x.qq)}</div></td>
      <td><div class="intimacy-cell"><span class="bar"><i style="width:${Math.min(100, countValue(x.value))}%"></i></span>${esc(x.value)}</div></td>
      <td><div class="intimacy-cell">${esc(x.band)}</div></td>
      <td class="muted"><div class="intimacy-cell intimacy-recent">
        <div class="intimacy-preview">${Number.isFinite(x.lastDelta) && x.lastDelta ? (x.lastDelta > 0 ? "+" : "") + x.lastDelta + " " : ""}${esc(x.lastReason)}${x.talkedAt ? `<br>${esc(x.talkedAt)}` : ""}${x.impression ? `<br>以前的印象：${esc(x.impression)}` : ""}</div>
        <button type="button" class="small ghost" data-person-detail="${index}" aria-label="查看 ${esc(x.name || x.qq)} 的完整详情">查看详情</button>
      </div></td>
      <td class="muted"><div class="intimacy-cell"><span class="intimacy-preview">${esc((x.groups || []).join("、"))}</span></div></td></tr>`).join("") || `<tr><td colspan="6" class="muted">还没有认识的人</td></tr>`;
  resizeIntimacyViewport();
  wrap.scrollTop = scrollTop;
  wrap.scrollLeft = scrollLeft;
}

function showIntimacyDetail(index) {
  const row = intimacyRows[index];
  if (!row) return;
  const delta = Number.isFinite(row.lastDelta) ? (row.lastDelta > 0 ? "+" : "") + row.lastDelta : "—";
  const fields = [["谁", row.name], ["账号 ID", row.qq], ["亲密度", row.value], ["档位", row.band],
    ["最近变化", delta], ["变化理由", row.lastReason], ["最近聊过", row.talkedAt],
    ["以前的印象", row.impression], ["在哪些群", (row.groups || []).join("、")]];
  $("intimacy-detail-body").innerHTML = `<dl class="kv">${fields.map(([label, value]) => `<dt>${label}</dt><dd>${esc(value === 0 ? 0 : value || "—")}</dd>`).join("")}</dl>`;
  const dialog = $("intimacy-detail");
  if (!dialog.open) dialog.showModal();
}

$("intimacy").addEventListener("click", (event) => {
  const button = event.target.closest("[data-person-detail]");
  if (!button) return;
  const index = Number(button.dataset.personDetail);
  if (Number.isInteger(index) && index >= 0) showIntimacyDetail(index);
});

if (typeof ResizeObserver !== "undefined") {
  new ResizeObserver(resizeIntimacyViewport).observe($("intimacy"));
} else {
  window.addEventListener("resize", resizeIntimacyViewport);
}

// ---------------- 刷新 ----------------

async function loadOverview() {
  const d = await call(() => bridge.apiGet("overview"));
  if (d?.loadError) $("updated").textContent = d.loadError;
  else if (d && !d.loading) renderOverview(d);
  else if (d?.loading) $("updated").textContent = "框架还在启动……";
}

async function loadSessions() {
  const d = await call(() => bridge.apiGet("sessions"));
  if (d) renderSessions(d);
}

async function loadAll() {
  await Promise.all([loadOverview(), loadSessions(), loadIntimacy(), loadMemory(memData?.scope)]);
}

$("refresh").addEventListener("click", loadAll);
$("reload-data").addEventListener("click", async () => {
  const r = await call(() => bridge.apiPost("reload", {}));
  if (r) toast("重新读了：" + r.data);
});

if (host) {
  try {
    await bridge.ready();
    setupUniverseLink();
    await loadAll();
    setInterval(loadOverview, 30_000);
  } catch (error) {
    $("updated").textContent = "页面桥启动失败：" + (error.message || String(error));
  }
} else {
  $("updated").textContent = "尚未接入宿主，请配置 window.MultiChatPage（见页面 README）。";
}
