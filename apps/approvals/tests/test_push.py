import base64,json,sqlite3,time
from pathlib import Path
from datetime import datetime,timezone,timedelta
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from push_notifications import PushStore,validate,NoRedirectSession

def subscription(host='web.push.apple.com'):
 key=ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
 enc=lambda v:base64.urlsafe_b64encode(v).decode().rstrip('=')
 return {'endpoint':'https://'+host+'/fixture-only','keys':{'auth':enc(b'a'*16),'p256dh':enc(key)}}
@pytest.fixture
def store(tmp_path):
 p=PushStore(tmp_path,Fernet(Fernet.generate_key()),'https://autorisations.example.org')
 with p.db() as db:
  db.execute('CREATE TABLE sessions(digest TEXT PRIMARY KEY,expires REAL)')
  db.execute('INSERT INTO sessions VALUES(?,?)',('session',time.time()+1000))
 return p

def pending():
 now=datetime.now(timezone.utc)
 return {'47':{'action_id':'fixture-action','status':'pending_approval','decision':None,'created_at':(now+timedelta(seconds=1)).isoformat(),'deadline':(now+timedelta(minutes=10)).isoformat()}}
@pytest.mark.parametrize('endpoint',['http://web.push.apple.com/a','https://127.0.0.1/a','https://web.push.apple.com.evil.example/a','https://user:pass@web.push.apple.com/a','https://web.push.apple.com:9128/a','https://web.push.apple.com/a?target=internal'])
def test_reject_ssrf_endpoints(endpoint):
 value=subscription();value['endpoint']=endpoint
 with pytest.raises(ValueError):validate(value)
def test_key_lengths_and_curve():
 value=subscription();value['keys']['p256dh']=base64.urlsafe_b64encode(b'x'*65).decode()
 with pytest.raises(ValueError):validate(value)
def test_encrypted_subscription_no_historical_storm_or_duplicate_after_restart(store,monkeypatch):
 value=subscription();ident=store.subscribe(value,8,'session',time.time()+1000);sent=[]
 with store.db() as db:assert value['endpoint'].encode() not in bytes(db.execute('SELECT value FROM push_subscriptions').fetchone()[0])
 monkeypatch.setattr(store,'send',lambda row,payload:sent.append(payload) or 201)
 states=pending();store.tick(states);store.tick(states)
 assert len(sent)==1 and 'fixture-action' in sent[0]['tag'] and 'fixture-only' not in str(sent)
 second=PushStore(store.folder,store.cipher,store.origin);monkeypatch.setattr(second,'send',lambda *args:pytest.fail('duplicate'))
 second.tick(states)
 states['47']['action_id']='old-action';states['47']['created_at']='2020-01-01T00:00:00Z';second.tick(states)
def test_expiry_decision_logout_and_revoked_subscription(store,monkeypatch):
 value=subscription();store.subscribe(value,8,'session',time.time()+1000)
 monkeypatch.setattr(store,'send',lambda *args:pytest.fail('unexpected send'))
 states=pending();states['47']['decision']='approve';store.tick(states)
 states['47']['decision']=None;states['47']['deadline']='2020-01-01T00:00:00Z';store.tick(states)
 store.logout('session');store.tick(pending())
 with store.db() as db:assert db.execute('SELECT count(*) FROM push_subscriptions').fetchone()[0]==0

def test_gone_endpoint_removed_and_transient_retry_bounded(store,monkeypatch):
 value=subscription();ident=store.subscribe(value,8,'session',time.time()+1000)
 codes=[];monkeypatch.setattr(store,'send',lambda *args:codes.append(503) or 503)
 states=pending()
 for i in range(8):
  store.tick(states)
  with store.db() as db:db.execute('UPDATE push_deliveries SET next_attempt=0')
 assert len(codes)==5
 with store.db() as db:db.execute('DELETE FROM push_deliveries')
 monkeypatch.setattr(store,'send',lambda *args:410);store.tick(states)
 with store.db() as db:assert db.execute('SELECT count(*) FROM push_subscriptions').fetchone()[0]==0

def test_test_notification_rate_limited(store,monkeypatch):
 value=subscription();store.subscribe(value,8,'session',time.time()+1000);sent=[]
 monkeypatch.setattr(store,'send',lambda row,data:sent.append(data) or 201)
 assert store.test(value['endpoint'],8)==201
 assert store.test(value['endpoint'],8)==429
 assert len(sent)==1
 with pytest.raises(ValueError):store.test(value['endpoint'],9)
def test_no_redirects_or_environment_proxy(monkeypatch):
 seen={}
 import requests
 monkeypatch.setattr(requests.Session,'request',lambda self,*args,**kwargs:seen.update(kwargs))
 with NoRedirectSession() as session:
  assert session.trust_env is False
  session.request('POST','https://web.push.apple.com/a',allow_redirects=True)
 assert seen['allow_redirects'] is False

def test_real_encryption_and_vapid_https_subject(store,monkeypatch):
    """Run pywebpush's encryption/signing, substituting only the HTTP transport."""
    import requests
    value=subscription();store.subscribe(value,8,'session',time.time()+1000)
    observed={}
    def transport(self,url,**kwargs):
        observed.update(url=url,**kwargs)
        response=requests.Response();response.status_code=201;return response
    monkeypatch.setattr(NoRedirectSession,'post',transport)
    assert store.test(value['endpoint'],8)==201
    assert observed['url']==value['endpoint']
    assert observed['headers']['content-encoding']=='aes128gcm'
    assert 'vapid' in observed['headers']['authorization'].lower()
    assert b'notifications' not in observed['data']
