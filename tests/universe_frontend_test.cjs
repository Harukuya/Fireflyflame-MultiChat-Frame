'use strict';
// Pure local fixtures and gated requests. No real host, network, model, or runtime data.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { pathToFileURL } = require('node:url');
const root = process.argv[2] || path.resolve(__dirname, "..");
if (!root) throw new Error('Usage: node universe_frontend_test.cjs <source-root>');
const dir = path.join(root, 'pages', 'memory-universe');
const appSource = fs.readFileSync(path.join(dir, 'app.js'), 'utf8');
const apiSource = fs.readFileSync(path.join(dir, 'universe-api.js'), 'utf8');
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(dir, 'style.css'), 'utf8');
const clone = (x) => JSON.parse(JSON.stringify(x));
let count = 0;
const check = async (name, fn) => { await fn(); ++count; console.log('OK ' + name); };
const gate = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; };

function adapter(createMockBackend, host, hash = '', framework = false) {
  const context = vm.createContext({createMockBackend, console, Promise, Number, Boolean, String, Object, Array,
    window: host ? { [framework ? 'MultiChatPage' : 'AstrBotPluginPage']: host } : {}, location: {hash}});
  return vm.runInContext(apiSource.replace(/^import .*;\s*$/m, '').replace(/^export /gm, '')
    + '\n({universeApi, mode});', context);
}

function controller(api) {
  let tick = 0;
  const events = {};
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {textContent:'', hidden:false, replaceChildren(){this.textContent='';}, scrollIntoView(){}});
    return elements.get(id);
  };
  const context = vm.createContext({console, universeApi:api, mode:'live', hostBridge:null,
    matchMedia:()=>({matches:true}), performance:{now:()=>tick},
    document:{querySelector:element}, window:{addEventListener:(name, fn)=>{events[name]=fn;}},
    location:{hash:'',pathname:'/',search:''}, history:{replaceState(){}}, URLSearchParams,
    innerWidth:1400, setTimeout, clearTimeout});
  const start = appSource.indexOf('// ---------- 启动 ----------');
  assert(start > 0);
  const definitions = appSource.slice(0,start).replace(/^import .*;\s*$/m,'');
  const lifecycle = appSource.slice(start, appSource.indexOf('\nreadHash();',start));
  const result = vm.runInContext(definitions + `
    renderState=renderDossier=renderReadAt=renderControls=renderAll=renderTally=buildStars=draw=syncTimelineSelection=writeHash=()=>{};
    ` + lifecycle + '\n({S,loadMeta,loadItems,openItem,clearSelection,buildQuery,setFilter});',context);
  return {...result,advance:(ms)=>{tick+=ms;},events,elements};
}

(async()=>{
  const {createMockBackend,MOCK_NOW} = await import(pathToFileURL(path.join(dir,'mock-data.js')).href);
  let now = MOCK_NOW;
  const backend=createMockBackend({clock:()=>now,delayMs:0});
  const meta=await backend.meta();
  const getAll=async (b,query={})=>{let cursor,items=[];do{const r=await b.items({...query,limit:200,cursor});assert(r.ok);items.push(...r.data.items);cursor=r.data.page.nextCursor;}while(cursor);return items;};

  await check('installed page defaults to live, local relative resources and text-only rendering',()=>{
    assert(apiSource.includes('DESIGN_PREVIEW = false'));
    assert(html.includes('./app.js')&&html.includes('./style.css'));
    assert(!appSource.includes('innerHTML'));
    assert(!appSource.includes('localStorage.'));
    assert(css.includes('prefers-reduced-motion')&&css.includes('320'));
  });
  await check('metadata declares readonly capabilities and full server clock envelope',()=>{
    assert.equal(meta.schemaVersion,1);assert.equal(meta.generatedAt,now);assert.equal(meta.utcOffsetMinutes,480);
    assert.equal(meta.data.capabilities.mutations,false);assert.equal(meta.data.capabilities.rawChat,false);
  });
  const normal=await getAll(backend);
  await check('six memory types with unique scope-aware IDs and nullable unknown time',()=>{
    assert.equal(new Set(normal.map(x=>x.id)).size,normal.length);
    assert.equal(new Set(normal.map(x=>x.kind)).size,6);
    assert(normal.some(x=>x.time.start===null));
  });
  await check('rough episodes keep full summary just like actual memory formatting',async()=>{
    const n=normal.find(x=>x.kind==='episode'&&x.clarity.level===1);
    const d=(await backend.item(n.id)).data;
    assert(d.rememberedText.includes(d.original.summary));
    assert(n.preview.includes(d.original.summary.slice(0,35)));
  });
  await check('very fuzzy episode uses linked overall impression rather than hidden summary',async()=>{
    const n=normal.find(x=>x.kind==='episode'&&x.clarity.level===3);
    const d=(await backend.item(n.id)).data;
    const link=d.links.find(x=>x.type==='faded_into');assert(link);
    const haze=(await backend.item(link.target.id)).data;
    assert(d.rememberedText.includes(haze.rememberedText));
    assert(!d.rememberedText.includes(d.original.summary));
    assert.equal(n.preview,haze.node.preview);
  });
  await check('literal search does not search hidden original material',async()=>{
    const r=await backend.items({q:'折叠伞'});assert.equal(r.data.page.matched,0);
  });
  backend.setScenario('large');
  const large=await getAll(backend);
  await check('large fixture remains useful for 200-node budget and filters expired memories',()=>{
    assert(large.length>200);
    for(const n of large) if(n.expiresAt!==null) assert(n.expiresAt>now,n.id);
    for(const n of large.filter(x=>x.kind==='episode'&&x.importance<=3&&x.time.end!==null)){
      assert.equal(n.expiresAt,n.time.end+30*86400000);assert(n.expiresAt>now);
    }
  });
  await check('server response clock really progresses between pages',async()=>{
    const first=await backend.items({limit:2});now+=1000;
    const next=await backend.items({limit:2,cursor:first.data.page.nextCursor});
    assert(next.ok);assert.equal(next.generatedAt-first.generatedAt,1000);
  });
  await check('mock rejects changed query when cursor belongs to older boundaries',async()=>{
    const q={from:now-30*86400000,limit:2};const first=await backend.items(q);
    const next=await backend.items({...q,from:q.from+1000,cursor:first.data.page.nextCursor});
    assert.equal(next.error.code,'INVALID_QUERY');
  });
  await check('invalid time range is a structured business error',async()=>{
    assert.equal((await backend.items({from:100,to:99})).error.code,'INVALID_QUERY');
  });
  await check('expiry after read removes item and dependent overall impression without resurrecting',async()=>{
    backend.setScenario('normal');const n=(await getAll(backend)).find(x=>x.kind==='episode'&&x.clarity.level===3&&x.expiresAt);
    const d=(await backend.item(n.id)).data;const link=d.links.find(x=>x.type==='faded_into');
    now=n.expiresAt;
    assert.equal((await backend.item(n.id)).error.code,'ITEM_NOT_FOUND');
    if(link)assert.equal((await backend.item(link.target.id)).error.code,'ITEM_NOT_FOUND');
    now=MOCK_NOW;
  });

  await check('no host never silently pretends to have real data',async()=>{
    const a=adapter(createMockBackend,null);assert.equal(a.mode,'none');
    await assert.rejects(a.universeApi.meta(),e=>e.code==='REQUEST_FAILED');
  });
  await check('explicit preview still works without a host',()=>assert.equal(adapter(createMockBackend,null,'#preview=1').mode,'mock'));
  let handler=async()=>meta;
  let readyCalls=0,requests=[];
  const host={ready:async()=>{readyCalls++;},apiGet:async(endpoint,params)=>{requests.push({endpoint,params});return handler(endpoint,params);}};
  const live=adapter(createMockBackend,host).universeApi;
  await check('live adapter keeps envelope and uses only agreed GET endpoint',async()=>{
    const r=await live.meta();assert.equal(r.generatedAt,meta.generatedAt);
    await live.items({kinds:['episode','pack'],from:123,includeUndated:true,limit:100});
    assert.equal(requests.at(-1).endpoint,'memory-universe/items');
    assert.equal(requests.at(-1).params.kinds,'episode,pack');assert.equal(requests.at(-1).params.includeUndated,'true');
    assert.equal(readyCalls,1);
  });
  await check('structured business errors keep their code; network errors do not invent a cause',async()=>{
    handler=async()=>({ok:false,schemaVersion:1,error:{code:'STALE_CURSOR',message:'changed',retryable:true}});
    await assert.rejects(live.meta(),e=>e.code==='STALE_CURSOR');
    handler=async()=>{throw new Error('synthetic-private-stack-marker');};
    await assert.rejects(live.meta(),e=>e.code==='REQUEST_FAILED'&&!e.message.includes('synthetic-private-stack-marker'));
  });
  await check('schema mismatch and invalid clock never render as success',async()=>{
    handler=async()=>({...meta,schemaVersion:2});await assert.rejects(live.meta(),e=>e.code==='UNSUPPORTED_SCHEMA');
    handler=async()=>({...meta,generatedAt:NaN});await assert.rejects(live.meta(),e=>e.code==='UNSUPPORTED_SCHEMA');
  });
  await check('failed bridge readiness can be retried on next refresh',async()=>{
    let tries=0;const a=adapter(createMockBackend,{ready:async()=>{if(++tries===1)throw new Error('once');},apiGet:async()=>meta});
    await assert.rejects(a.universeApi.meta(),e=>e.code==='REQUEST_FAILED');
    assert.equal((await a.universeApi.meta()).ok,true);assert.equal(tries,2);
  });
  if(apiSource.includes('window.MultiChatPage')) await check('framework accepts platform-neutral request without ready method',async()=>{
    let sent;const a=adapter(createMockBackend,{request:async(x)=>{sent=x;return meta;}},'',true);
    assert.equal((await a.universeApi.meta()).ok,true);assert.equal(sent.method,'GET');assert.equal(sent.path,'memory-universe/meta');
  });

  backend.setScenario('large');
  const calls=[];
  let clock=MOCK_NOW;
  const c=controller({meta:async()=>({...meta,generatedAt:clock}),items:async(q)=>{
    calls.push(clone(q));const r=await backend.items(q);return {...r,generatedAt:clock+=1000};
  },item:(id)=>backend.item(id)});
  c.S.query.range='30d';
  await c.loadMeta();
  await check('real controller freezes the whole query while server time changes',async()=>{
    c.advance(500);await c.loadItems({append:true});
    assert.equal(calls[1].from,calls[0].from);assert(calls[1].cursor);assert.equal(c.S.status,'ready');
  });
  await check('new refresh recalculates relative time after pagination batch ends',async()=>{
    c.advance(1000);await c.loadItems();assert(calls.at(-1).from>calls[0].from);
  });
  const seed=normal[0];
  const envelope=(items,at=MOCK_NOW)=>({...meta,generatedAt:at,data:{items,page:{matched:items.length,returned:items.length,nextCursor:null}}});
  await check('gated old list cannot overwrite the newer filter response',async()=>{
    const slow=gate();let entered=false;
    const api={meta:async()=>meta,items:async(q)=>{if(q.q==='old'){entered=true;return slow.promise;}return envelope([{...seed,id:'new'}]);},item:async()=>{throw new Error('unused');}};
    const h=controller(api);await h.loadMeta();h.S.query.q='old';const pending=h.loadItems();assert(entered);
    h.S.query.q='new';await h.loadItems();slow.resolve(envelope([{...seed,id:'old'}]));await pending;
    assert.equal(h.S.items[0].id,'new');
  });
  await check('gated old detail cannot replace the last clicked star',async()=>{
    const slow=gate();let entered=false;
    const h=controller({item:async(id)=>{if(id==='old'){entered=true;return slow.promise;}return {...meta,data:{node:{...seed,id},rememberedText:'new'}};}});
    const pending=h.openItem('old',{quiet:true});assert(entered);await h.openItem('new',{quiet:true});
    slow.resolve({...meta,data:{node:{...seed,id:'old'},rememberedText:'old'}});await pending;
    assert.equal(h.S.detail.node.id,'new');
  });
  await check('refresh drops stale original and rechecks selected item existence',async()=>{
    const h=controller({meta:async()=>meta,items:async()=>envelope([]),item:async()=>{throw {code:'ITEM_NOT_FOUND',message:'gone'};}});
    h.S.sel='gone';h.S.detail={node:seed,original:{summary:'old secret'} };h.S.detailMode='original';
    const p=h.loadMeta();assert.equal(h.S.detail,null);await p;
    assert.equal(h.S.detail,null);assert.equal(h.S.detailError.code,'ITEM_NOT_FOUND');assert.equal(h.S.detailMode,'remembered');
  });
  await check('first-page STALE_CURSOR does not recurse forever',async()=>{
    let requests=0;const h=controller({meta:async()=>meta,items:async()=>{requests++;throw {code:'STALE_CURSOR',message:'bad server'};}});
    await h.loadMeta();assert.equal(requests,1);assert.equal(h.S.status,'error');
  });
  await check('gated old metadata cannot overwrite a later reload',async()=>{
    const slow=gate();let entered=false,n=0;
    const h=controller({meta:async()=>{if(++n===1){entered=true;return slow.promise;}return {...meta,data:{...meta.data,sources:[]}};},items:async()=>envelope([])});
    const pending=h.loadMeta();assert(entered);await h.loadMeta();slow.resolve(meta);await pending;assert.equal(h.S.sources.length,0);
  });
  await check('leaving clears originals and invalidates an in-flight detail',async()=>{
    const slow=gate();const h=controller({item:async()=>slow.promise});const pending=h.openItem('old',{quiet:true});
    h.S.detail={original:{summary:'private'}};h.events.pagehide();slow.resolve({...meta,data:{node:seed,original:{summary:'late'}}});await pending;
    assert.equal(h.S.detail,null);assert.equal(h.S.items.length,0);assert.equal(h.S.querySnapshot,null);
  });
  console.log(`universe frontend ${count}/${count}`);
})().catch(error=>{console.error(error);process.exitCode=1;});
