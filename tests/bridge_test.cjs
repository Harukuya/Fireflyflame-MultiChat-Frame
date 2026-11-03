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

const overview={her:{name:'角色',online:true,status:'在线',statusKey:'online',timeline:[],mood:null,energy:80,nowMinute:0},model:{},stats:{},events:[],env:[]};
function fixture(method) {
  const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element(m[1])]));
  const $=id=>{assert.ok(nodes.has(id),'Unknown id '+id);return nodes.get(id)};
  const requests=[];
  const respond=msg=>{requests.push(msg);if(msg.method==='POST')return {ok:true}; switch(msg.path){case 'overview':return overview;case 'sessions':return {rows:[],whitelist:false};case 'intimacy':return {rows:[]};case 'memory':return {scopes:[],empty:true};default:throw Error(msg.path);}};
  const host=method?{[method]:async msg=>({ok:true,...respond(msg)})}:null;
  const window={addEventListener(){},innerWidth:900,innerHeight:800};
  if(host)window.MultiChatPage=host;
  let intervals=0;
  const context=vm.createContext({window,document:{getElementById:$,addEventListener(){},activeElement:null},console,setInterval(){intervals++},setTimeout(){},clearTimeout(){},confirm(){return true}});
  return {nodes,$,requests,context,intervals:()=>intervals};
}
(async()=>{
  for(const method of ['request','invoke']){
    const f=fixture(method);
    const api=await vm.runInContext('(async()=>{'+app+'\nreturn {renderSessions};})()',f.context);
    test(method+' GET bridge shape',()=>{assert.deepEqual(f.requests.map(r=>r.path).sort(),['intimacy','memory','overview','sessions']);assert.ok(f.requests.every(r=>r.method==='GET' && r.params && !('body' in r)));});
    f.$('add-id').value='@reader:example.org';f.$('add-kind').value='private';
    await f.$('add-session').listeners.submit({preventDefault(){}});
    test(method+' nonnumeric account and POST body',()=>{const r=f.requests.find(r=>r.path==='sessions/add');assert.equal(r.method,'POST');assert.equal(r.body.id,'@reader:example.org');assert.equal(r.body.kind,'private');});
    api.renderSessions({rows:[{kind:'group',id:'room:"<test>',name:'safe',enabled:true}]});
    test(method+' account ID HTML escaped',()=>{const markup=f.$('sessions').querySelector('tbody').innerHTML;assert.match(markup,/room:&quot;&lt;test&gt;/);assert.doesNotMatch(markup,/room:"<test>/);});
    await f.$('sessions').listeners.click({target:{dataset:{remove:'group:room:example.org'}}});
    test(method+' remove keeps colon in account',()=>assert.equal(f.requests.find(r=>r.path==='sessions/remove').body.id,'room:example.org'));
    f.context.window.MultiChatPage[method]=async()=>({ok:false,error:{code:'busy',message:'当前忙碌，请稍后'}});
    await f.$('reload-data').listeners.click();
    test(method+' service error is shown without success toast',()=>assert.equal(f.$('toast').textContent,'当前忙碌，请稍后'));
  }
  const f=fixture(null);
  await vm.runInContext('(async()=>{'+app+'})()',f.context);
  test('no host shows actionable message',()=>assert.match(f.$('updated').textContent,/window.MultiChatPage/));
  test('no host does not poll or request',()=>{assert.equal(f.requests.length,0);assert.equal(f.intervals(),0);});
  console.log(`framework dashboard bridge ${checks.length}/${checks.length}`);
})().catch(e=>{console.error(e);process.exitCode=1;});
