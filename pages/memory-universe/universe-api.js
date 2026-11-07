// universeApi：「角色的思想宇宙」唯一的数据入口。
// 对外只有 meta()／items(query)／item(id) 三个 Promise；成功返回完整 UniverseSuccess 包，
// 失败统一抛 {code, message, retryable}。页面其他地方不直接碰 bridge，也不直接读假数据。

import { createMockBackend } from "./mock-data.js";

export const SCHEMA_VERSION = 1;

// 三个只读接口：memory-universe/meta|items|item。
// 正式页面为 false；设计稿为 true，始终明确标出「虚构示例」。
export const DESIGN_PREVIEW = false;

const host = typeof window !== "undefined" ? (window.MultiChatPage || window.AstrBotPluginPage) : undefined;
// 平台无关页面桥；本地HTTP服务注入MultiChatPage，嵌入宿主可提供request/apiGet。
const bridge = host ? {
  ready: async () => { if (typeof host.ready === "function") return host.ready(); },
  pageUrls: host.pageUrls,
  apiGet: (path, params = {}) => {
    if (typeof host.apiGet === "function") return host.apiGet(path, params);
    const request = host.request || host.invoke;
    if (typeof request !== "function") throw new Error("宿主页面桥缺少request或apiGet。");
    return request.call(host, { method: "GET", path, params });
  },
  onContext: typeof host.onContext === "function" ? (fn) => host.onContext(fn) : undefined,
} : undefined;
const previewHash = /(?:^|[#&])preview(?:=1)?(?:&|$)/.test(location.hash);

// live：宿主桥 + 只读接口；mock：明确的设计预览；none：没有宿主也没要求预览，不偷偷用假数据。
export const mode = DESIGN_PREVIEW || (!bridge && previewHash) ? "mock" : bridge ? "live" : "none";

const failure = (code, message, retryable = false) => ({ code, message, retryable });

const ERROR_TEXT = {
  REQUEST_FAILED: "没能连上插件接口。请检查框架版本和连接，或从获授权的宿主仪表盘重新进入。",
  UNSUPPORTED_SCHEMA: "页面和插件后端的数据版本不兼容，没有按旧格式猜测着显示。",
};

function unwrap(envelope) {
  if (!envelope || typeof envelope !== "object") throw failure("UNSUPPORTED_SCHEMA", ERROR_TEXT.UNSUPPORTED_SCHEMA);
  if (envelope.schemaVersion !== SCHEMA_VERSION) throw failure("UNSUPPORTED_SCHEMA", ERROR_TEXT.UNSUPPORTED_SCHEMA);
  if (envelope.ok === false) {
    const e = envelope.error || {};
    throw failure(String(e.code || "REQUEST_FAILED"), String(e.message || ERROR_TEXT.REQUEST_FAILED), Boolean(e.retryable));
  }
  if (envelope.ok !== true || !Number.isFinite(envelope.generatedAt)
      || !Number.isFinite(envelope.utcOffsetMinutes) || !envelope.data || typeof envelope.data !== "object") {
    throw failure("UNSUPPORTED_SCHEMA", ERROR_TEXT.UNSUPPORTED_SCHEMA);
  }
  return envelope; // 保留 generatedAt／utcOffsetMinutes，页面要用它们当时间基准
}

async function guarded(call) {
  let envelope;
  try {
    envelope = await call();
  } catch (e) {
    // 宿主鉴权、路由、网络拒绝在 bridge 里都是普通 Error，分不清具体原因，不猜。
    if (e && typeof e === "object" && "code" in e && "retryable" in e) throw e;
    throw failure("REQUEST_FAILED", ERROR_TEXT.REQUEST_FAILED, true);
  }
  return unwrap(envelope);
}

// 只把有值的筛选传出去；kinds 用逗号，includeUndated 用字符串。
function toParams(query = {}) {
  const out = {};
  const put = (k, v) => { if (v !== undefined && v !== null && v !== "") out[k] = String(v); };
  put("scope", query.scope);
  if (Array.isArray(query.kinds) && query.kinds.length) put("kinds", query.kinds.join(","));
  put("q", query.q ? String(query.q).slice(0, 80) : "");
  put("topicId", query.topicId);
  put("personId", query.personId);
  put("from", query.from);
  put("to", query.to);
  if (query.from != null || query.to != null) put("includeUndated", query.includeUndated ? "true" : "false");
  put("limit", query.limit);
  put("cursor", query.cursor);
  return out;
}

function liveAdapter() {
  let ready = null;
  const get = async (endpoint, params) => {
    ready ||= Promise.resolve().then(() => bridge.ready());
    try { await ready; } catch (error) { ready = null; throw error; }
    return bridge.apiGet(endpoint, params);
  };
  return {
    meta: () => guarded(() => get("memory-universe/meta", {})),
    items: (query) => guarded(() => get("memory-universe/items", toParams(query))),
    item: (id) => guarded(() => get("memory-universe/item", { id: String(id) })),
  };
}

function mockAdapter() {
  const backend = createMockBackend();
  return {
    meta: () => guarded(() => backend.meta()),
    items: (query) => guarded(() => backend.items(toParams(query))),
    item: (id) => guarded(() => backend.item(String(id))),
    scenarios: backend.scenarios,
    get scenario() { return backend.scenario; },
    setScenario: (s) => backend.setScenario(s),
  };
}

function noneAdapter() {
  const reject = () => Promise.reject(failure("REQUEST_FAILED", "没有连接到宿主仪表盘。请打开本机仪表盘或接入 MultiChatPage 页面桥。", false));
  return { meta: reject, items: reject, item: reject };
}

export const universeApi = mode === "live" ? liveAdapter() : mode === "mock" ? mockAdapter() : noneAdapter();
export const hostBridge = bridge || null;
