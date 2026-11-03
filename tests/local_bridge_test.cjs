const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync(path.join(process.argv[2], "pages/dashboard/local.js"), "utf8");
let count = 0;
function check(name, fn) { fn(); count++; console.log("OK " + name); }
function load({ existing, legacy, token = "SYNTHETIC_LOCAL_TOKEN" } = {}) {
  const calls = [];
  const window = { location: { origin: "http://127.0.0.1:8765" }, MultiChatPage: existing, AstrBotPluginPage: legacy };
  let response = { ok: true, status: 200, json: async () => ({ ok: true, rows: [] }) };
  vm.runInNewContext(source, { window, URL, document: { querySelector: () => token ? { content: token } : null },
    fetch: async (...args) => { calls.push(args); return response; } });
  return { window, calls, reply: (r) => { response = r; } };
}
(async () => {
  const fixture = load();
  const api = fixture.window.MultiChatPage;
  await api.request({ method: "GET", path: "memory", params: { scope: "p:reader:你好" } });
  check("GET query carries exact scope and same-origin credential", () => {
    const [url, options] = fixture.calls[0];
    assert.equal(url.origin, "http://127.0.0.1:8765");
    assert.equal(url.pathname, "/api/memory");
    assert.equal(url.searchParams.get("scope"), "p:reader:你好");
    assert.equal(options.headers.Authorization, "Bearer SYNTHETIC_LOCAL_TOKEN");
    assert.equal(options.cache, "no-store");
    assert.equal(options.body, undefined);
  });
  await api.request({ method: "POST", path: "sessions/add", body: { kind: "group", id: "room:a" } });
  check("POST serializes JSON and never puts token in URL", () => {
    const [url, options] = fixture.calls[1];
    assert.equal(options.headers["Content-Type"], "application/json");
    assert.deepEqual(JSON.parse(options.body), { kind: "group", id: "room:a" });
    assert.ok(!url.toString().includes("TOKEN"));
  });
  fixture.reply({ ok: false, status: 403, json: async () => ({ ok: false, error: { message: "请求已拒绝" } }) });
  const error = await api.request({ method: "GET", path: "overview" });
  check("structured errors reach existing page bridge", () => assert.equal(error.error.message, "请求已拒绝"));
  fixture.reply({ ok: false, status: 500, json: async () => ({}) });
  await assert.rejects(api.request({ method: "GET", path: "overview" }), /500/);
  check("unstructured HTTP failure rejects instead of success", () => {});
  fixture.reply({ ok: true, status: 200, json: async () => { throw new Error("invalid json"); } });
  await assert.rejects(api.request({ method: "GET", path: "overview" }), /invalid json/);
  check("invalid response propagates as a visible request failure", () => {});
  const existing = { request() {} };
  check("existing MultiChat host stays untouched", () => assert.equal(load({ existing }).window.MultiChatPage, existing));
  check("existing AstrBot host stays untouched", () => assert.equal(load({ legacy: existing }).window.MultiChatPage, undefined));
  check("no injected local token means no bridge and no network", () => {
    const empty = load({ token: "" });
    assert.equal(empty.window.MultiChatPage, undefined);
    assert.equal(empty.calls.length, 0);
  });
  console.log(`framework local bridge ${count}/${count}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
