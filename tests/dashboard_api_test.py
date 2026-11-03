import sys
from pathlib import Path
from types import SimpleNamespace as N
if len(sys.argv) != 2:
    raise SystemExit('Usage: python -I -B dashboard_api_test.py <framework-directory>')
sys.path.insert(0, sys.argv[1])
from multichat import dashboard as d, profile
from multichat.jsutil import now_ms
checks=[]
def check(name, expr):
    assert expr, name
    checks.append(name)
    print('OK '+name)
profile.configure({'character_name':'测试角色','primary_user_name':'朋友','schedule_mode':'always'},Path(sys.argv[1])/'prompts')
llm=N(provider=lambda:None,supports=lambda x:False)
p=N(engines={},trailblazer='',her=N(mood=None,energy=None),trailblazer_umo=lambda:'',chat_provider=lambda umo:None,judge_llm_for=lambda umo:llm,llm_for=lambda umo:llm,caption_provider=lambda umo,l:None,
 replica_dir=lambda:sys.argv[1],data_dir=lambda:str(Path(sys.argv[1])/'data'),events=N(today=lambda:{},items=[]),embedder=N(ready=True),knowledge=N(vector_ready=False),people=N(vector_ready=False),style=N(vector_ready=False),stickers=None,sessions=N(whitelist_on=False,private={},group={}),context=N(get_config=lambda:{},get_all_stars=lambda:[]),hub=None,memories={},stores={},scope_label=lambda s:s,private_umo=lambda q:q)

import asyncio, tempfile
from contextlib import nullcontext
from multichat.registry import Registry
from multichat.dashboard_api import DashboardService
p.sessions=Registry(tempfile.mkdtemp(prefix='multichat-dashboard-api-'))
p.profile_scope=lambda:nullcontext()
p.closed=False
p.stickers=None
p.intimacy=N(people={})
p.memory_history=lambda s:[]
p.register=lambda kind,sid,**kw:p.sessions.add(kind,sid,**kw)
p.events.note=lambda *a,**k:None
pending=[]
async def update(kind,sid,field,value):
    updated=p.sessions.update(kind,sid,field,value)
    if field=='enabled' and value is False:
        for task in pending:task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
    return updated
async def remove(kind,sid):
    await update(kind,sid,'enabled',False)
    p.sessions.remove(kind,sid)
async def reload_data():
    if getattr(p,'busy',False):raise RuntimeError('Runtime is busy')
    if getattr(p,'broken',False):raise RuntimeError('SYNTHETIC_TOKEN_DO_NOT_ECHO')
p.update_session=update;p.remove_session=remove;p.reload_data=reload_data
async def work():await asyncio.Event().wait()
def spawn(scope,force=False):
    task=asyncio.create_task(work());pending.append(task);return task
p.spawn_memory=spawn
scope='p:@reader:example.org'
p.memories={scope:N(m={'episodes':[],'words':[],'facts':[],'threads':[],'haze':[]},enabled=lambda:True,unconsolidated=lambda h:[],store=N(save_memory=lambda:None))}
async def main():
    api=DashboardService(p)
    x=await api.handle('GET','overview');check('overview raw shape plus ok',x['ok'] and 'her' in x and 'data' not in x)
    check('context attribute does not enable host-specific checks',any('平台登录' in row['text'] for row in x['env']) and all('AstrBot' not in row['text'] for row in x['env']))
    x=await api.handle('GET','sessions');check('empty session view',x['ok'] and x['rows']==[])
    x=await api.handle('GET','intimacy');check('empty intimacy view',x=={'ok':True,'rows':[]})
    x=await api.handle('GET','sticker');check('empty stickers',x=={'ok':True,'library':{},'dir':''})
    x=await api.handle('GET','memory',params={'scope':scope});check('memory with colon ID',x['ok'] and x['scope']==scope)
    x=await api.handle('GET','unknown');check('unknown path error',not x['ok'] and x['error']['code']=='not_found')
    x=await api.handle('GET','reload');check('method mismatch',x['error']['code']=='method_not_allowed')
    x=await api.handle('POST','sessions/add',body=[]);check('reject non-object',x['error']['code']=='invalid_request')
    x=await api.handle('POST','sessions/add',body={'kind':'private','id':True});check('reject invalid ID',x['error']['code']=='invalid_session')
    x=await api.handle('POST','sessions/add',body={'kind':'private','id':'@reader:example.org'});check('add generic ID',x['ok'] and p.sessions.private_on('@reader:example.org'))
    x=await api.handle('POST','sessions/update',body={'kind':'private','id':'@reader:example.org','field':'enabled','value':'false'});check('reject string boolean',x['error']['code']=='invalid_value')
    x=await api.handle('POST','sessions/update',body={'kind':'private','id':'@reader:example.org','field':'name','value':'读者'});check('rename session',x['ok'] and p.sessions.private['@reader:example.org']['name']=='读者')
    p.trailblazer='@reader:example.org'
    memory_task=asyncio.create_task(api.handle('POST','memory/run',body={'scope':scope}))
    while not pending:await asyncio.sleep(0)
    x=await api.handle('POST','sessions/update',body={'kind':'private','id':'@reader:example.org','field':'enabled','value':False})
    m=await memory_task
    check('disable primary cancels tracked memory',x['ok'] and not p.sessions.private_on('@reader:example.org') and m['error']['code']=='cancelled')
    x=await api.handle('POST','sessions/remove',body={'kind':'private','id':'@reader:example.org'});check('remove primary allowed',x['ok'] and not p.sessions.private['@reader:example.org']['listed'])
    x=await api.handle('POST','sessions/add',body={'kind':'private','id':'@reader:example.org'});check('re-add enables',x['ok'] and p.sessions.private_on('@reader:example.org'))
    p.memories[scope].m['episodes']=[{'id':7}]
    x=await api.handle('POST','memory/retire',body={'scope':scope,'kind':'episode','id':7});check('existing retire endpoint',x['ok'] and not p.memories[scope].m['episodes'])
    x=await api.handle('POST','memory/retire',body={'scope':scope,'kind':'episode','id':True});check('retire rejects bool ID',x['error']['code']=='invalid_memory')
    x=await api.handle('POST','reload');check('reload response shape',x['ok'] and x['data']==p.data_dir())
    p.busy=True;x=await api.handle('POST','reload');check('reload busy surfaced',x['error']['code']=='busy');p.busy=False
    p.broken=True;x=await api.handle('POST','reload');check('provider error not leaked',not x['ok'] and 'SYNTHETIC_TOKEN' not in str(x));p.broken=False
    p.closed=True;x=await api.handle('POST','reload');check('closed runtime rejects writes',x['error']['code']=='closed')
    await actual_runtime()
    print(f'framework dashboard API {len(checks)}/{len(checks)}')

async def actual_runtime():
    from multichat.runtime import Runtime
    from multichat.ports import ConsoleTransport
    from multichat.model_port import ScriptedModelProvider
    from multichat.jsutil import now_ms
    transport=ConsoleTransport()
    provider=ScriptedModelProvider([])
    root=tempfile.mkdtemp(prefix='multichat-dashboard-real-')
    runtime=Runtime(root,transport,lambda c:provider,config={'character_name':'接口测试角色','primary_user_id':'reader:main','primary_user_name':'测试联系人','schedule_mode':'always'},assets_dir=sys.argv[1])
    api=DashboardService(runtime)
    try:
        x=await api.handle('GET','overview');check('real runtime profile rendered',x['ok'] and x['her']['name']=='接口测试角色')
        for kind,sid in [('private','reader:main'),('group','room:example')]:
            x=await api.handle('POST','sessions/add',body={'kind':kind,'id':sid,'memory':True});check('real register '+kind,x['ok'])
        x=await api.handle('GET','sessions');check('real sessions views available',x['ok'] and len(x['rows'])==2)
        x=await api.handle('GET','memory',params={'scope':'g:room:example'});check('real group memory view',x['ok'] and x['scope']=='g:room:example' and x['pending']==0)
        x=await api.handle('POST','memory/run',body={'scope':'p:reader:main'});check('real memory task no model call for empty history',x['ok'] and not provider.requests)
        old=runtime.engine('reader:main')
        started=asyncio.Event();cancelled=asyncio.Event()
        async def pending_send():
            started.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set();raise
        with runtime.profile_scope():old._spawn(pending_send())
        await started.wait()
        x=await api.handle('POST','reload');check('real reload rejects active task',x['error']['code']=='busy')
        x=await api.handle('POST','sessions/update',body={'kind':'private','id':'reader:main','field':'enabled','value':False})
        check('real disable cancels and removes engine',x['ok'] and cancelled.is_set() and old.closed and 'reader:main' not in runtime.engines)
        x=await api.handle('POST','sessions/update',body={'kind':'private','id':'reader:main','field':'enabled','value':True})
        check('real re-enable creates fresh engine',x['ok'] and runtime.engines['reader:main'] is not old and not runtime.engines['reader:main'].closed)
        group=runtime.chat('room:example')
        x=await api.handle('POST','sessions/remove',body={'kind':'group','id':'room:example'})
        check('real remove closes group',x['ok'] and group.closed and 'room:example' not in runtime.hub.chats)
        await asyncio.sleep(0)
        x=await api.handle('POST','reload');check('real idle reload works',x['ok'])
        check('dashboard never invokes model or transport',provider.requests==[] and transport.sent==[])
    finally:
        await runtime.close()
    x=await api.handle('POST','sessions/add',body={'kind':'private','id':'new'})
    check('real closed Runtime blocks API writes',x['error']['code']=='closed')

asyncio.run(main())
