"""A human approval surface. Writes only authentic user reactions through Zulip."""
import asyncio,hashlib,json,os,re,secrets,sqlite3,time
from contextlib import asynccontextmanager,suppress
from collections import defaultdict,deque
from datetime import datetime,timezone
from pathlib import Path
import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import FileResponse,JSONResponse
from pydantic import BaseModel,Field,ConfigDict
from push_notifications import PushStore

ORIGIN=os.environ.get('APP_ORIGIN','https://autorisations.example.org')
ZULIP=os.environ.get('ZULIP_ORIGIN','https://zulip.example.org')
DATA=Path(os.environ.get('APP_DATA','/var/lib/ops-approvals'))
SNAPSHOT=Path(os.environ.get('APP_SNAPSHOT','/run/ops-approvals/state.json'))
WEB=Path(__file__).parent/'web'
APPROVERS={8};BOT=10;STREAM=5;TOPIC='approbations'
COOKIE='__Host-ops_approvals'
EMOJIS={'approve':('check','2705'),'reject':('cross_mark','274c'),'later':('hourglass','231b')}
MARKER=re.compile(r'OPS-APPROVAL-V1:([0-9a-f-]{36})')
DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
keyfile=DATA/'session.key'
if not keyfile.exists():
    fd=os.open(keyfile,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:f.write(Fernet.generate_key())
cipher=Fernet(keyfile.read_bytes())
def db():
    c=sqlite3.connect(DATA/'sessions.db',timeout=10);c.row_factory=sqlite3.Row;return c
with db() as c:c.execute('CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, value BLOB NOT NULL, expires REAL NOT NULL)')
(DATA/'sessions.db').chmod(0o600)
push=PushStore(DATA,cipher,ORIGIN)
async def notify_loop():
    while True:
        status='ok'
        try:await asyncio.to_thread(push.tick,snapshot())
        except Exception:status='unavailable'
        # Diagnostic contains no endpoint, payload, key or provider response.
        try:
            report=DATA/'push-status.next'
            report.write_text(json.dumps({'checked_at':time.time(),'status':status}))
            report.replace(DATA/'push-status.json')
        except OSError:pass
        await asyncio.sleep(5)
@asynccontextmanager
async def lifespan(app):
    worker=asyncio.create_task(notify_loop())
    yield
    worker.cancel()
    with suppress(asyncio.CancelledError):await worker
app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
attempts=defaultdict(deque);locks=defaultdict(asyncio.Lock)

@app.middleware('http')
async def protect(request,call_next):
    if request.method not in {'GET','HEAD'} and request.headers.get('origin')!=ORIGIN:
        return JSONResponse({'detail':'Origine refusée.'},status_code=403)
    try:length=int(request.headers.get('content-length','0'))
    except ValueError:return JSONResponse({'detail':'Requête incorrecte.'},status_code=400)
    if request.method not in {'GET','HEAD'} and length>8192:
        return JSONResponse({'detail':'Requête trop volumineuse.'},status_code=413)
    response=await call_next(request)
    response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",'Permissions-Policy':'camera=(), microphone=(), geolocation=()'})
    return response

def snapshot():
    try:
        s=json.loads(SNAPSHOT.read_text())
        if not 0<=time.time()-s['updated_at']<15:raise ValueError()
        return s['items']
    except Exception:raise HTTPException(503,'La synchronisation est indisponible. Réessaie dans un instant.')

async def zulip(method,path,auth=None,data=None,params=None):
    try:
        async with httpx.AsyncClient(timeout=15,follow_redirects=False) as client:
            r=await client.request(method,ZULIP+'/api/v1/'+path,auth=auth,data=data,params=params)
        value=r.json()
    except (httpx.HTTPError,ValueError):raise HTTPException(503,'Zulip est momentanément indisponible.')
    if r.status_code==401:raise HTTPException(401,'Connexion expirée. Reconnecte-toi.')
    if r.status_code>=400 or value.get('result')!='success':raise HTTPException(502,'Zulip n’a pas confirmé la demande.')
    return value

async def identity(request):
    token=request.cookies.get(COOKIE,'')
    if len(token)>200:raise HTTPException(401,'Connecte-toi pour continuer.')
    with db() as c:row=c.execute('SELECT * FROM sessions WHERE digest=? AND expires>?',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
    if not row:raise HTTPException(401,'Connecte-toi pour continuer.')
    try:s=json.loads(cipher.decrypt(row['value']))
    except Exception:raise HTTPException(401,'Session expirée.')
    u=await zulip('GET','users/me',auth=(s['email'],s['key']))
    if u.get('user_id') not in APPROVERS or u.get('is_bot') or u.get('is_active') is False:
        raise HTTPException(403,'Ce compte ne peut pas gérer les autorisations.')
    s['uid']=u['user_id'];s['name']=u.get('full_name','Mon compte')
    s['session']=row['digest'];s['expires']=row['expires']
    return s

class Login(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username:str=Field(min_length=1,max_length=254)
    password:str=Field(min_length=1,max_length=1000)
@app.post('/api/login')
async def login(body:Login,request:Request):
    bucket=attempts[request.client.host]
    now=time.time()
    while bucket and bucket[0]<now-300:bucket.popleft()
    if len(bucket)>=10:raise HTTPException(429,'Trop de tentatives. Réessaie dans cinq minutes.')
    bucket.append(now)
    try:result=await zulip('POST','fetch_api_key',data={'username':body.username,'password':body.password})
    except HTTPException as e:
        if e.status_code in {401,502}:raise HTTPException(401,'Identifiant ou mot de passe incorrect.')
        raise
    u=await zulip('GET','users/me',auth=(result['email'],result['api_key']))
    if u.get('user_id') not in APPROVERS or u.get('is_bot') or u.get('is_active') is False:raise HTTPException(403,'Ce compte ne peut pas gérer les autorisations.')
    token=secrets.token_urlsafe(32);expires=time.time()+30*86400
    with db() as c:
        c.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
        c.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),cipher.encrypt(json.dumps({'email':result['email'],'key':result['api_key']}).encode()),expires))
    response=JSONResponse({'name':u.get('full_name','Mon compte')})
    response.set_cookie(COOKIE,token,secure=True,httponly=True,samesite='strict',max_age=30*86400,path='/')
    return response
@app.post('/api/logout')
async def logout(request:Request):
    token=request.cookies.get(COOKIE,'')
    push.logout(hashlib.sha256(token.encode()).hexdigest())
    with db() as c:c.execute('DELETE FROM sessions WHERE digest=?',(hashlib.sha256(token.encode()).hexdigest(),))
    response=JSONResponse({'ok':True});response.delete_cookie(COOKIE,path='/',secure=True,httponly=True,samesite='strict');return response

@app.get('/api/push')
async def push_config(request:Request):
    await identity(request)
    return {'public_key':push.public_key}
class Subscription(BaseModel):
    model_config=ConfigDict(extra='forbid')
    subscription:dict
@app.post('/api/push/subscribe')
async def push_subscribe(body:Subscription,request:Request):
    s=await identity(request)
    try:push.subscribe(body.subscription,s['uid'],s['session'],s['expires'])
    except (ValueError,TypeError,KeyError):raise HTTPException(422,'Abonnement aux notifications incorrect.')
    return {'ok':True}
class Endpoint(BaseModel):
    model_config=ConfigDict(extra='forbid')
    endpoint:str=Field(min_length=1,max_length=2048)
@app.post('/api/push/unsubscribe')
async def push_unsubscribe(body:Endpoint,request:Request):
    s=await identity(request);push.unsubscribe(body.endpoint,s['uid']);return {'ok':True}
@app.post('/api/push/test')
async def push_test(body:Endpoint,request:Request):
    s=await identity(request)
    try:code=await asyncio.to_thread(push.test,body.endpoint,s['uid'])
    except ValueError:raise HTTPException(404,'Active les notifications sur cet appareil.')
    if code==429:raise HTTPException(429,'Attends une minute avant un autre essai.')
    if not 200<=code<300:raise HTTPException(503,'Notification non transmise. Réessaie dans un instant.')
    return {'ok':True,'message':'Notification transmise. Vérifie sa réception sur cet appareil.'}

def verify_message(m,states):
    markers=MARKER.findall(m.get('content',''))
    return (m.get('sender_id')==BOT and m.get('stream_id')==STREAM and m.get('subject') in {TOPIC,'✔ '+TOPIC} and m.get('type')=='stream' and len(markers)==1 and states.get(str(m['id']),{}).get('action_id')==markers[0])
def item(m,state):
    decision=state['decision']
    expired=state['status']=='approval_expired' or (not decision and datetime.fromisoformat(state['deadline'].replace('Z','+00:00'))<=datetime.now(timezone.utc))
    deferred=any(r.get('user_id') in APPROVERS and r.get('reaction_type')=='unicode_emoji' and r.get('emoji_code')=='231b' for r in m.get('reactions',[]))
    status={'approve':'accepted','reject':'rejected'}.get(decision,'expired' if expired else 'later' if deferred else 'pending')
    if not decision and state['status']!='pending_approval' and not expired:status='unavailable'
    text=MARKER.sub('',m['content']).strip()
    return {'id':m['id'],'message':text,'status':status,'created_at':state['created_at'],'deadline':state['deadline'],'url':ZULIP+'/#narrow/channel/'+str(STREAM)+'/near/'+str(m['id'])}
@app.get('/api/feed')
async def feed(request:Request,before:int=0):
    s=await identity(request);states=snapshot()
    if before<0:raise HTTPException(422,'Page incorrecte.')
    result=await zulip('GET','messages',auth=(s['email'],s['key']),params={'anchor':str(before) if before else 'newest','num_before':100,'num_after':0,'include_anchor':'false','apply_markdown':'false','narrow':json.dumps([{'operator':'channel','operand':'ops-approbations'}])})
    messages=result['messages'];items=[item(m,states[str(m['id'])]) for m in messages if verify_message(m,states)]
    return {'name':s['name'],'items':list(reversed(items)),'next':min((m['id'] for m in messages),default=0) if not result.get('found_oldest') else 0}
class Decision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    decision:str=Field(pattern='^(approve|reject|later|resume)$')
@app.post('/api/items/{mid}/decision')
async def decide(mid:int,body:Decision,request:Request):
    s=await identity(request)
    async with locks[mid]:
        states=snapshot();m=(await zulip('GET',f'messages/{mid}',auth=(s['email'],s['key']),params={'apply_markdown':'false'}))['message']
        if not verify_message(m,states):raise HTTPException(404,'Demande introuvable.')
        current=item(m,states[str(mid)])
        if m.get('subject')!=TOPIC or current['status'] not in {'pending','later'}:raise HTTPException(409,'Cette demande a déjà été traitée ou a expiré.')
        opposite='274c' if body.decision=='approve' else '2705'
        if body.decision in {'approve','reject'} and any(r.get('user_id') in APPROVERS and r.get('reaction_type')=='unicode_emoji' and r.get('emoji_code')==opposite for r in m.get('reactions',[])):
            raise HTTPException(409,'Une décision est déjà en cours dans Zulip. Actualise le feed.')
        name,code=EMOJIS.get(body.decision,EMOJIS['later'])
        own=any(r.get('user_id')==s['uid'] and r.get('emoji_code')==code and r.get('reaction_type')=='unicode_emoji' for r in m.get('reactions',[]))
        if body.decision=='resume':
            if own:await zulip('DELETE',f'messages/{mid}/reactions',auth=(s['email'],s['key']),data={'emoji_name':name,'emoji_code':code,'reaction_type':'unicode_emoji'})
        elif not own:await zulip('POST',f'messages/{mid}/reactions',auth=(s['email'],s['key']),data={'emoji_name':name,'emoji_code':code,'reaction_type':'unicode_emoji'})
        if body.decision in {'approve','reject'}:
            for _ in range(12):
                await asyncio.sleep(1)
                state=snapshot().get(str(mid),{})
                if state.get('decision'):return {'confirmed':True,'status':{'approve':'accepted','reject':'rejected'}[state['decision']]}
            return JSONResponse({'confirmed':False,'message':'Transmis à Zulip. Confirmation en attente.'},status_code=202)
        return {'confirmed':True,'status':'pending' if body.decision=='resume' else 'later'}
@app.get('/healthz')
def health():snapshot();return {'ok':True}
@app.get('/{path:path}')
def static(path:str):
    files={'':'index.html','app.js':'app.js','style.css':'style.css','manifest.webmanifest':'manifest.webmanifest','sw.js':'sw.js','icon-192.png':'icon-192.png','icon-512.png':'icon-512.png'}
    if path not in files:raise HTTPException(404)
    return FileResponse(WEB/files[path])
