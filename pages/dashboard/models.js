// 独立面板模块；不会把 Provider 配置或凭据送进页面。
const bridge = window.AstrBotPluginPage;
const node = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[c]);
let data = null;
let dirty = new Map();
let busy = false;
let requestId = 0;

function status(text, error = false) {
  node("models-status").textContent = text;
  node("models-status").classList.toggle("model-route-error", error);
}

function capabilityText(provider) {
  if (!provider.capabilitiesKnown) return "能力未声明";
  const mods = provider.modalities || [];
  return `工具${mods.includes("tool_use") ? "✓" : "✗"} · 看图${mods.includes("image") ? "✓" : "✗"}`;
}

function compatible(provider, route) {
  return !route.requires || !provider.capabilitiesKnown || (provider.modalities || []).includes(route.requires);
}

function render() {
  if (!data) return;
  const providers = Array.isArray(data.providers) ? data.providers : [];
  const routes = Array.isArray(data.routes) ? data.routes : [];
  node("models-note").textContent = data.note || "默认沿用各会话原配置；保存后下一轮生效，正在进行的请求继续使用原模型。";
  node("model-routes").innerHTML = routes.map((route, index) => {
    const selected = dirty.has(route.id) ? dirty.get(route.id) : route.selected || "";
    const missing = selected && !providers.some((p) => p.id === selected);
    const picked = providers.find((p) => p.id === selected);
    const invalid = missing || (picked && !compatible(picked, route));
    const options = `<option value="">${escapeHtml(route.defaultLabel || "跟随会话默认")}</option>`
      + (missing ? `<option value="${escapeHtml(selected)}" disabled>已失效：${escapeHtml(selected)}</option>` : "")
      + providers.map((p) => `<option value="${escapeHtml(p.id)}" ${compatible(p, route) ? "" : "disabled"}>${escapeHtml(p.label || p.id)} · ${escapeHtml(capabilityText(p))}</option>`).join("");
    const warning = invalid ? "所选模型已停用或能力不满足，请重新选择；不会自动切回主模型。" : route.error || "";
    return `<div class="model-route">
      <label for="model-route-${index}">${escapeHtml(route.label)}${dirty.has(route.id) ? '<span class="model-route-unsaved">未保存</span>' : ""}</label>
      <p class="muted">${escapeHtml(route.hint)}</p>
      <select id="model-route-${index}" data-model-route="${escapeHtml(route.id)}" aria-describedby="model-route-help-${index}" ${busy || !data.supported ? "disabled" : ""}>${options}</select>
      <p id="model-route-help-${index}" class="${warning ? "model-route-error" : "muted"}">${escapeHtml(warning || (dirty.has(route.id) ? "保存后下一轮使用此选择。" : `当前示例：${route.effective || "未配置可用模型"}`))}</p>
    </div>`;
  }).join("");
  for (const select of node("model-routes").querySelectorAll("select[data-model-route]")) {
    const route = routes.find((r) => r.id === select.dataset.modelRoute);
    select.value = dirty.has(route.id) ? dirty.get(route.id) : route.selected || "";
  }
  node("models-save").disabled = busy || !data.supported || dirty.size === 0;
  node("models-refresh").disabled = busy;
}

async function load() {
  if (busy) return;
  const id = ++requestId;
  try {
    const result = await bridge.apiGet("models");
    if (id !== requestId || busy) return;
    if (!result || !Array.isArray(result.routes)) throw new Error("未取得模型配置，请稍后刷新。");
    data = result;
    render();
    status(dirty.size ? "已刷新可用模型，保留尚未保存的选择。" : data.supported ? "只列出宿主中已启用的聊天模型。" : "宿主尚未提供模型目录，请在宿主配置模型。");
  } catch (error) {
    if (id === requestId) status(error.message || "读取模型列表失败。", true);
  }
}

node("model-routes").addEventListener("change", (event) => {
  const select = event.target.closest("select[data-model-route]");
  if (!select || busy || !data?.supported) return;
  const route = data.routes.find((r) => r.id === select.dataset.modelRoute);
  if (!route) return;
  if (select.value === (route.selected || "")) dirty.delete(route.id);
  else dirty.set(route.id, select.value);
  render();
  node(`model-route-${data.routes.indexOf(route)}`)?.focus();
  status(dirty.size ? `${dirty.size} 项尚未保存。` : "选择与已保存配置一致。");
});

node("models-save").addEventListener("click", async () => {
  if (busy || !data?.supported || !dirty.size) return;
  const changes = Object.fromEntries(dirty);
  busy = true;
  ++requestId;
  render();
  status("正在保存……");
  try {
    const result = await bridge.apiPost("models/update", {routes: changes});
    if (!result || !Array.isArray(result.routes)) throw new Error("保存未完成，请刷新后核对。");
    data = result;
    dirty.clear();
    status(result.message || "已保存，从下一轮生效；正在进行的请求保持原模型。");
    // 更新上方实际模型示例，复用现有总刷新入口。
    node("refresh")?.click();
  } catch (error) {
    status(error.message || "保存失败，尚未保存的选择已保留。", true);
  } finally {
    busy = false;
    render();
  }
});

node("models-refresh").addEventListener("click", load);
node("refresh")?.addEventListener("click", load);
await bridge.ready();
await load();
