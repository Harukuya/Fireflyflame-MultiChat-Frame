const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const plugin = process.argv[2];
if (!plugin) throw new Error('Usage: node '+__filename+' <framework-directory>');
const html = fs.readFileSync(path.join(plugin, 'pages/dashboard/index.html'), 'utf8');
const app = fs.readFileSync(path.join(plugin, 'pages/dashboard/app.js'), 'utf8');
const checks = [];
const test = (name, f) => { f(); checks.push(name); console.log('OK ' + name); };
class Element {
  constructor(id) { this.id = id; this.innerHTML = ''; this.textContent = ''; this.value = ''; this.style = {}; this.dataset = {}; this.listeners = {}; this.children = []; this.attrs = {}; this.scrollTop=0; this.scrollLeft=0; this.offsetHeight=100; this.clientHeight=100; this.classList={toggle(){},add(){},remove(){}}; }
  addEventListener(k,f) { this.listeners[k] = f; }
  setAttribute(k,v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  querySelector(k) { return this[k] ||= new Element(this.id + '/' + k); }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return {x:0,y:0,width:100,height:40,bottom:40,left:0}; }
  contains() { return false; }
}
const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => [m[1],new Element(m[1])]));
const $ = id => { assert.ok(nodes.has(id), 'Unknown HTML id ' + id); return nodes.get(id); };
const requested = [];
const overview = {her:{name:'<测试角色>',status:'休息',online:false,statusKey:'rest',activity:'休息',detail:'',herHour:'00:00',mood:70,moodBand:'平静',energy:100,energyBand:'有精神',timeline:[],nowMinute:0}, model:{chat:'测试主模型',judge:'测试判断模型',tools:true,image:false,caption:'',vectors:'正常',data:'SYNTHETIC_DATA',version:''}, stats:{}, stickers:{'开心':0},stickerDir:'SYNTHETIC_DATA/stickers',events:[],env:[],unfinished:[]};
let stickerResponse = {dir:'SYNTHETIC_DATA/stickers',library:{'开心':[]}};
const bridge = {ready:async()=>{},apiGet:async (route) => { requested.push(route); switch(route) {case 'overview':return overview;case 'sessions':return {whitelist:false,rows:[]};case 'intimacy':return {rows:[]};case 'memory':return {scope:'',scopes:[],empty:true};case 'sticker':return stickerResponse;default:throw Error(route);}},apiPost:async route=>{throw Error('Unexpected write ' + route);}};
const context = vm.createContext({window:{AstrBotPluginPage:bridge,addEventListener(){},innerWidth:900,innerHeight:800},document:{getElementById:$,addEventListener(){},activeElement:null},console,setInterval(){},setTimeout(){},clearTimeout(){},confirm(){return true;}});
(async()=>{
  const ui = await vm.runInContext('(async()=>{' + app + '\nreturn {renderOverview,renderSessions,renderMemory};})()',context);
  test('全脚本初始化只请求可用接口，无点赞字段也不报错',()=>assert.deepEqual(requested.sort(),['intimacy','memory','overview','sessions']));
  test('HTML和JS没有点赞UI或请求残留',()=>assert.doesNotMatch(html+app,/点赞|likes\/|data-like=|like-now/));
  test('页面名称正确且主要联系人由空配置开始',()=>{assert.match(html,/<h1>MultiChat 多会话框架<\/h1>/);const config=JSON.parse(fs.readFileSync(path.join(plugin,'config.example.json'),'utf8'));assert.equal(config.primary_user_id,'');assert.equal($('character-heading').textContent,'<测试角色>现在');});
  test('空会话有指引',()=>assert.match($('sessions').querySelector('tbody').innerHTML,/还没有受管会话/));
  test('主要联系人私聊行显示独立主动联系开关，普通私聊仍为—',()=>{
    assert.match(html,/<th title="群：主动看消息、接着聊；主要联系人私聊：安静一阵后独立主动联系（念头、追问不受影响）">主动<\/th>/);
    ui.renderSessions({whitelist:false,rows:[
      {kind:'private',id:'reader:main',name:'主要联系人',trailblazer:true,enabled:true,memory:true,proactive:false,last:''},
      {kind:'private',id:'reader:other',name:'读者',trailblazer:false,enabled:true,memory:true,proactive:null,last:''},
      {kind:'group',id:'room:a',name:'房间',enabled:true,memory:true,proactive:true,last:''}]});
    const rows=$('sessions').querySelector('tbody').innerHTML.split('<tr>').slice(1);
    assert.match(rows[0],/data-kind="private" data-id="reader:main" data-field="proactive"  >/);
    assert.doesNotMatch(rows[1],/data-field="proactive"/);assert.match(rows[1],/—/);
    assert.match(rows[2],/data-kind="group" data-id="room:a" data-field="proactive" checked/);
  });
  ui.renderSessions({whitelist:false,rows:[]});
  test('空记忆展示说明并禁用整理',()=>{assert.match($('mem-note').textContent,/还没有受管会话/);assert.equal($('mem-run').disabled,true);});
  test('空表情提示且总览展示实际路径',()=>{assert.match($('sticker-count').textContent,/还没有表情图片/);assert.match($('sticker-dir').textContent,/SYNTHETIC_DATA\/stickers/);});
  await $('load-stickers').listeners.click();
  test('刷新空表情库可用',()=>assert.match($('stickers').innerHTML,/还没有图片/));
  stickerResponse={dir:'SYNTHETIC_DATA/stickers',library:{'疑问':[{src:'data:image/png;base64,TEST',name:'<synthetic>.png'}]}};
  await $('load-stickers').listeners.click();
  test('中文类别图片展示并转义文件名',()=>{assert.match($('stickers').innerHTML,/>疑问</);assert.match($('stickers').innerHTML,/&lt;synthetic&gt;.png/);assert.doesNotMatch($('stickers').innerHTML,/<synthetic>/);});
  await $('refresh').listeners.click();
  test('总览刷新不覆盖实际路径',()=>assert.equal($('sticker-dir').textContent,'本机实际目录：SYNTHETIC_DATA/stickers'));
  test('添加说明包含数据目录和全部图片格式',()=>{for(const token of ['&lt;data_root&gt;/stickers/','中文类别','JPG','JPEG','PNG','GIF','WebP','自动识别'])assert.ok(html.includes(token),token);});
  test('无任何接口报错toast',()=>assert.equal($('toast').textContent,''));
  test('自定义指引位于页面底部且没有私人邮箱',()=>{assert.match(html,/prompts\//);assert.match(html,/data\//);assert.doesNotMatch(html,/mailto:|流萤|开拓者/);assert.ok(html.indexOf('feedback-title')>html.indexOf('id="events"'));});
  test('动态角色名使用纯文本',()=>assert.equal($('character-heading').textContent,'<测试角色>现在'));
  overview.her.online=true; overview.her.status='在线'; overview.her.offlineAt='';
  await $('refresh').listeners.click();
  test('始终在线模式不显示问号下线时间',()=>assert.equal($('online-span').textContent,'持续在线，没有计划下线时间'));
  overview.her.online=false; overview.her.status='忙碌中'; overview.her.statusKey='mission';
  await $('refresh').listeners.click();
  test('忙碌状态图标和文字是中性说明',()=>{assert.equal($('status-dot').textContent,'⏳');assert.equal($('status-dot').attrs['aria-label'],'忙碌中，不在线');});
  overview.loading=true;overview.loadError='知识资料加载失败，请修复资料后重载插件。';
  await $('refresh').listeners.click();
  test('资料损坏明确提示而非一直启动',()=>assert.equal($('updated').textContent,overview.loadError));
  delete overview.loading;delete overview.loadError;
  await $('refresh').listeners.click();
  test('正常状态恢复后不残留错误',()=>assert.doesNotMatch($('updated').textContent,/知识资料加载失败/));
  console.log(`framework dashboard frontend ${checks.length}/${checks.length}`);
})().catch(e=>{console.error(e);process.exitCode=1;});

