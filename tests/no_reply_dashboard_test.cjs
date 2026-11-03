// Actual counter rendering; no model, platform, network, or filesystem writes.
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = process.argv[2];
if (!root) throw new Error('Usage: node no_reply_dashboard_test.cjs <source-root>');
const app = fs.readFileSync(path.join(root, 'pages/dashboard/app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'pages/dashboard/index.html'), 'utf8');
const stop = app.indexOf('function renderModelUsage(');
assert.ok(stop > 0, 'Counter renderer boundary exists');
const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], {innerHTML:'',textContent:''}]));
const context = vm.createContext({window:{}, document:{getElementById(id) {
  assert.ok(nodes.has(id), 'Unknown page node: ' + id);
  return nodes.get(id);
}}});
const render = vm.runInContext(app.slice(0, stop) + '\nrenderCounters;', context);
const checks = [];
function test(name, check) {check(); checks.push(name); console.log('OK ' + name);}
function cards() {
  const result = new Map();
  for (const card of nodes.get('stats').innerHTML.split('<div class="stat">').slice(1)) {
    const name = card.match(/<span class="stat-name">([^<]+)<\/span>/)[1];
    result.set(name, [...card.matchAll(/<b>([^<]+)<\/b>/g)].map(m => m[1]));
  }
  return result;
}

render({stats:{'无需回复':2,'不想回':3,'回话':7},statsTotal:{counts:{'无需回复':11,'不想回':19,'回话':30}}});
test('两种不回复独立显示今日和累计', () => {
  assert.deepEqual(cards().get('无需回复'), ['2','11']);
  assert.deepEqual(cards().get('不想回'), ['3','19']);
});
test('新增卡片不覆盖既有统计', () => {
  assert.equal(cards().size,9);
  assert.deepEqual(cards().get('回话'), ['7','30']);
});
render({stats:{'不想回':8},statsTotal:{counts:{'不想回':20}}});
test('旧数据不猜测回填无需回复次数', () => {
  assert.deepEqual(cards().get('无需回复'), ['0','0']);
  assert.deepEqual(cards().get('不想回'), ['8','20']);
});
render({stats:{'无需回复':4},statsTotal:{counts:{'无需回复':13}}});
test('刷新后更新新统计且不残留旧值', () => {
  assert.deepEqual(cards().get('无需回复'), ['4','13']);
  assert.deepEqual(cards().get('不想回'), ['0','0']);
});
render({stats:{'不想回':6}});
test('完全没有累计数据时继续显示未知', () => {
  assert.deepEqual(cards().get('无需回复'), ['0','—']);
  assert.deepEqual(cards().get('不想回'), ['6','—']);
});
console.log(`no_reply dashboard ${checks.length}/${checks.length}`);
