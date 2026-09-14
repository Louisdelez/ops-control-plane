import os,tempfile,json,time,importlib.util,sys,copy
from pathlib import Path
import pytest
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).parents[3]/"bridges/zulip/src"))
sys.path.insert(0,str(Path(__file__).parents[1]))
from zulip_approval_bridge.bridge import ApprovalProcessor
from fastapi.testclient import TestClient
os.environ['APP_DATA']=tempfile.mkdtemp()
spec=importlib.util.spec_from_file_location('approval_server',Path(__file__).parents[1]/'server.py');s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
AID='10101010-1010-4010-8010-101010101010'
@pytest.fixture
def env(monkeypatch,tmp_path):
    path=tmp_path/'state.json';monkeypatch.setattr(s,'SNAPSHOT',path)
    state={'action_id':AID,'status':'pending_approval','deadline':'2099-01-01T00:00:00Z','created_at':'2026-09-13T12:00:00Z','decision':None}
    def save():path.write_text(json.dumps({'updated_at':time.time(),'items':{'47':state}}))
    save()
    message={'id':47,'sender_id':10,'stream_id':5,'subject':'approbations','type':'stream','content':'Une demande longue\nOPS-APPROVAL-V1:'+AID,'reactions':[]}
    calls=[];user={'user_id':8,'is_bot':False,'is_active':True,'full_name':'Test'}
    async def zulip(method,path,auth=None,data=None,params=None):
        calls.append((method,path,data))
        if path=='fetch_api_key':return {'email':'test@example.org','api_key':'fixture-only'}
        if path=='users/me':return user
        if path=='messages':return {'messages':[copy.deepcopy(message)],'found_oldest':True}
        if path=='messages/47':return {'message':copy.deepcopy(message)}
        if path=='messages/47/reactions':
            if method=='POST':
                message['reactions'].append({'user_id':8,**data})
                if data['emoji_code'] in ['2705','274c']:
                    def submit(**kw):
                        assert kw['actor_id']=='zulip:8'
                        assert kw['action_id']==AID
                        state['decision']=kw['decision'];state['status']='approved' if kw['decision']=='approve' else 'rejected';save()
                    configured=SimpleNamespace(approver_user_ids={8},approval_stream_id=5,approval_stream='ops-approbations',approval_topic='approbations',bot_user_id=10,realm_fingerprint='fixture')
                    source=SimpleNamespace(get_user=lambda uid:user,get_message=lambda mid:{**message,'display_recipient':'ops-approbations'})
                    processor=ApprovalProcessor(configured,source,SimpleNamespace(submit_approval=submit))
                    result=processor.process({'id':1,'type':'reaction','op':'add','user_id':8,'message_id':47,**data},'fixture-queue')
                    assert result in {'approved','rejected'}
            else:message['reactions']=[r for r in message['reactions'] if r['emoji_code']!=data['emoji_code']]
            return {'result':'success'}
        raise AssertionError(path)
    monkeypatch.setattr(s,'zulip',zulip);s.attempts.clear()
    c=TestClient(s.app,base_url=s.ORIGIN,headers={'origin':s.ORIGIN})
    assert c.post('/api/login',json={'username':'test@example.org','password':'fixture'}).status_code==200
    return c,state,message,calls,save,user

def test_login_secure_cookie_and_encryption(env):
    c,*_=env
    r=c.post('/api/login',json={'username':'test@example.org','password':'fixture'})
    cookie=r.headers['set-cookie'];assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=strict' in cookie
    assert b'fixture-only' not in (s.DATA/'sessions.db').read_bytes()
    assert 'key' not in r.json()
def test_feed_requires_auth_and_origin(env):
    c,*_=env;c.cookies.clear();assert c.get('/api/feed').status_code==401
    assert c.post('/api/login',headers={'origin':'https://attacker.invalid'},json={'username':'a','password':'b'}).status_code==403
def test_disabled_user_cannot_decide(env):
    c,state,m,calls,save,user=env;user['is_active']=False
    assert c.post('/api/items/47/decision',json={'decision':'approve'}).status_code==403
    assert not any(p.endswith('reactions') for _,p,_ in calls)
def test_both_directions_and_terminal_conflict(env):
    c,state,m,calls,save,user=env
    assert c.get('/api/feed').json()['items'][0]['status']=='pending'
    r=c.post('/api/items/47/decision',json={'decision':'approve'});assert r.json()['status']=='accepted'
    assert m['reactions'][0]['emoji_code']=='2705'
    assert c.get('/api/feed').json()['items'][0]['status']=='accepted'
    assert c.post('/api/items/47/decision',json={'decision':'reject'}).status_code==409
    # External Zulip decision uses the same authoritative snapshot.
    state['decision']='reject';state['status']='rejected';save()
    assert c.get('/api/feed').json()['items'][0]['status']=='rejected'
def test_later_and_resume_are_not_approval(env):
    c,state,m,calls,save,user=env
    assert c.post('/api/items/47/decision',json={'decision':'later'}).status_code==200
    assert c.get('/api/feed').json()['items'][0]['status']=='later'
    assert state['decision'] is None
    assert c.post('/api/items/47/decision',json={'decision':'resume'}).status_code==200
    assert not m['reactions'] and state['decision'] is None
def test_forged_sender_marker_not_allowed(env):
    c,state,m,*_=env;m['sender_id']=8
    assert c.get('/api/feed').json()['items']==[]
    assert c.post('/api/items/47/decision',json={'decision':'approve'}).status_code==404
def test_stale_or_expired_fail_closed(env):
    c,state,m,calls,save,user=env
    state['deadline']='2020-01-01T00:00:00Z';save()
    assert c.get('/api/feed').json()['items'][0]['status']=='expired'
    assert c.post('/api/items/47/decision',json={'decision':'approve'}).status_code==409
    s.SNAPSHOT.write_text(json.dumps({'updated_at':0,'items':{}}))
    assert c.post('/api/items/47/decision',json={'decision':'approve'}).status_code==503
def test_other_pending_decision_not_overwritten(env):
    c,state,m,*_=env;m['reactions']=[{'user_id':8,'reaction_type':'unicode_emoji','emoji_code':'274c'}]
    assert c.post('/api/items/47/decision',json={'decision':'approve'}).status_code==409
def test_logout_revokes_session(env):
    c,*_=env;token=c.cookies.get(s.COOKIE);assert c.post('/api/logout').status_code==200
    c.cookies.set(s.COOKIE,token);assert c.get('/api/feed').status_code==401
def test_static_offline_and_no_api_cache(env):
    c,*_=env
    assert c.get('/').status_code==200
    assert c.get('/api/feed').headers['cache-control']=='no-store'
    assert "frame-ancestors 'none'" in c.get('/').headers['content-security-policy']
    assert c.get('/manifest.webmanifest').json()['display']=='standalone'
    assert 'api/' not in c.get('/sw.js').text
def test_resolved_zulip_topic_preserves_history(env):
    c,state,m,calls,save,user=env;m['subject']='✔ approbations';state['decision']='approve';state['status']='approved';save()
    assert c.get('/api/feed').json()['items'][0]['status']=='accepted'
    assert c.post('/api/items/47/decision',json={'decision':'reject'}).status_code==409
def test_reject_passes_existing_bridge(env):
    c,state,m,*_=env
    result=c.post('/api/items/47/decision',json={'decision':'reject'})
    assert result.json()=={'confirmed':True,'status':'rejected'}
    assert state['decision']=='reject' and m['reactions'][0]['emoji_code']=='274c'

def test_push_routes_require_session_and_origin(env):
    from test_push import subscription
    c,*_=env
    value=subscription()
    assert len(c.get('/api/push').json()['public_key'])>80
    assert c.post('/api/push/subscribe',json={'subscription':value}).status_code==200
    assert c.post('/api/push/subscribe',json={'subscription':value},headers={'origin':'https://evil.invalid'}).status_code==403
    c.post('/api/logout')
    assert c.post('/api/push/subscribe',json={'subscription':value}).status_code==401
    assert c.get('/api/push').status_code==401
