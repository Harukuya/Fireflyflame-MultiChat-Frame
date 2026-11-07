'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = process.argv[2];
if (!root) throw new Error('Usage: node model_routes240_dashboard_test.cjs <source-root>');
const source = fs.readFileSync(path.join(root, 'pages/dashboard/models.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'pages/dashboard/index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'pages/dashboard/models.css'), 'utf8');
const nodes = new Map();
class Node {
  constructor(id) {this.id = id; this.handlers = {}; this.dataset = {}; this.disabled = false; this.textContent = ''; this.value = ''; this._html = ''; this.classList = {toggle(){}};}
  addEventListener(name, handler) {(this.handlers[name] ||= []).push(handler);}
  async emit(name, event = {}) {for (const fn of this.handlers[name] || []) await fn(event);}
  click() {return this.emit('click');}
  closest(selector) {return selector === 'select[data-model-route]' && this.dataset.modelRoute ? this : null;}
  focus() {focused = this.id;}
  set innerHTML(value) {
    this._html = value;
    if (this.id !== 'model-routes') return;
    this.selects = [...value.matchAll(/<select id="([^"]+)" data-model-route="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)].map((match) => {
      const node = new Node(match[1]); node.dataset.modelRoute = match[2]; node._html = match[3];
      node.disabled = / disabled>/.test(match[0].split('</select>')[0]); nodes.set(node.id,node); return node;
    });
  }
  get innerHTML() {return this._html;}
  querySelectorAll() {return this.selects || [];}
}
for (const match of html.matchAll(/\bid="([^"]+)"/g)) nodes.set(match[1], new Node(match[1]));
let focused = null, current, getImpl, postImpl, sent;
const fixture = () => ({supported:true, note:'使用已启用模型', providers:[
  {id:'chat',label:'主模型',model:'a',modalities:['text','tool_use','image'],capabilitiesKnown:true},
  {id:'cheap',label:'便宜<img src=x>',model:'b',modalities:['text'],capabilitiesKnown:true},
  {id:'legacy',label:'旧配置',model:'c',modalities:[],capabilitiesKnown:false}
], routes:[
  {id:'summary',label:'临时记事摘要',hint:'角色口吻',requires:'',selected:'',effective:'主模型',available:true,defaultLabel:'跟随会话默认'},
  {id:'memory',label:'长期记忆',hint:'需要工具',requires:'tool_use',selected:'chat',effective:'主模型',available:true},
  {id:'caption',label:'图片转述',hint:'需要图片',requires:'image',selected:'removed',effective:'',available:false}
]});
current = fixture();
getImpl = async () => JSON.parse(JSON.stringify(current));
postImpl = async (_url, body) => {sent=body; for(const r of current.routes) if(Object.hasOwn(body.routes,r.id)) r.selected=body.routes[r.id]; return JSON.parse(JSON.stringify(current));};
const context = vm.createContext({document:{getElementById(id){assert(nodes.has(id),id); return nodes.get(id);}},
  window:{AstrBotPluginPage:{ready:async()=>{},apiGet:(...args)=>getImpl(...args),apiPost:(...args)=>postImpl(...args)}},console});
const code = source.replace('await bridge.ready();\nawait load();','') + '\n({load,capabilityText,compatible,getData:()=>data,getDirty:()=>[...dirty]});';
const api = vm.runInContext(code, context);
let count = 0;
async function check(name, fn) {await fn(); ++count; console.log('OK '+name);}
async function change(index, value) {const select=nodes.get('model-route-'+index); select.value=value; await nodes.get('model-routes').emit('change',{target:select});}
(async()=>{
  await check('页面独立加载路由模块与浅深主题样式',()=>{assert(html.includes('./models.js')); assert(html.includes('./models.css')); assert(css.includes('var(--text)')); assert(css.includes('max-width: 640px'));});
  await api.load();
  await check('目录列出已启用模型并转义名称',()=>{assert(nodes.get('model-routes').innerHTML.includes('便宜&lt;img')); assert(!nodes.get('model-routes').innerHTML.includes('<img src=x>'));});
  await check('失效配置只作为不可选占位保留',()=>{assert(nodes.get('model-route-2').innerHTML.includes('value="removed" disabled')); assert.equal(nodes.get('model-route-2').value,'removed');});
  await check('工具及图片用途拒绝明确缺少能力的候选',()=>{assert(!api.compatible(current.providers[1],current.routes[1]));assert(!api.compatible(current.providers[1],current.routes[2]));});
  await check('能力未知按旧配置兼容且不冒充实测支持',()=>{assert(api.compatible(current.providers[2],current.routes[1]));assert.equal(api.capabilityText(current.providers[2]),'能力未声明');});
  await change(0,'cheap');
  await check('选择不会自动保存并保留键盘焦点',()=>{assert.equal(sent,undefined);assert.equal(focused,'model-route-0');assert.equal(nodes.get('models-save').disabled,false);});
  await api.load();
  await check('刷新不覆盖未保存选择',()=>{assert.equal(nodes.get('model-route-0').value,'cheap');assert.equal(api.getDirty().length,1);});
  await nodes.get('models-save').click();
  await check('保存只提交改动用途并清除脏状态',()=>{assert.equal(JSON.stringify(sent),JSON.stringify({routes:{summary:'cheap'}}));assert.equal(api.getDirty().length,0);assert.equal(nodes.get('models-save').disabled,true);});
  await change(0,'');
  await nodes.get('models-save').click();
  await check('空值恢复默认不覆盖其他用途',()=>{assert.equal(sent.routes.summary,'');assert.equal(current.routes[1].selected,'chat');});
  await change(0,'cheap');
  postImpl=async()=>{throw new Error('模型已停用');};
  await nodes.get('models-save').click();
  await check('保存失败明确提示并保留编辑值',()=>{assert.equal(nodes.get('models-status').textContent,'模型已停用');assert.equal(api.getDirty().length,1);assert.equal(nodes.get('models-save').disabled,false);});
  let resolveSave;
  postImpl=async()=>new Promise(resolve=>{resolveSave=resolve;});
  const pending=nodes.get('models-save').click();
  await check('等待真实保存响应期间按钮锁定',()=>{assert.equal(typeof resolveSave,'function');assert.equal(nodes.get('models-save').disabled,true);assert.equal(nodes.get('models-refresh').disabled,true);});
  resolveSave(fixture()); await pending;
  let resolveOld;
  getImpl=async()=>new Promise(resolve=>{resolveOld=resolve;});
  const old=api.load();
  getImpl=async()=>({...fixture(),note:'新的结果'});
  await api.load(); resolveOld({...fixture(),note:'过时的结果'}); await old;
  await check('晚返回的旧列表不会覆盖新结果',()=>assert.equal(nodes.get('models-note').textContent,'新的结果'));
  getImpl=async()=>({...fixture(),supported:false,providers:[]});await api.load();
  await check('旧宿主未实现目录时禁用选择但保持面板可读',()=>{assert.equal(nodes.get('models-save').disabled,true);assert(nodes.get('models-status').textContent.includes('宿主尚未'));});
  getImpl=async()=>{throw new Error('无法读取模型列表');}; await api.load();
  await check('列表失败显示错误不伪造成功',()=>assert.equal(nodes.get('models-status').textContent,'无法读取模型列表'));
  console.log(`model routes dashboard ${count}/${count}`);
})().catch(error=>{console.error(error);process.exitCode=1;});
