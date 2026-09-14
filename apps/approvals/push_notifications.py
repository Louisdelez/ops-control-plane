"""Authenticated, encrypted Web Push subscriptions and durable notification delivery."""
import base64,hashlib,json,os,re,sqlite3,time
from datetime import datetime
from urllib.parse import urlsplit
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import requests
from pywebpush import webpush, WebPushException

HOSTS={'web.push.apple.com','fcm.googleapis.com','updates.push.services.mozilla.com'}

def validate(value):
    if not isinstance(value,dict) or set(value)-{'endpoint','keys','expirationTime'}:raise ValueError('Invalid subscription')
    endpoint=value.get('endpoint','')
    if not isinstance(endpoint,str) or len(endpoint)>2048:raise ValueError('Invalid endpoint')
    u=urlsplit(endpoint)
    if u.scheme!='https' or u.username or u.password or u.port not in (None,443) or u.fragment or u.query or not u.path or any(c.isspace() for c in endpoint):raise ValueError('Invalid endpoint')
    host=u.hostname or ''
    if host not in HOSTS and not (host.endswith('.notify.windows.com') or host.endswith('.push.services.mozilla.com')):raise ValueError('Unsupported push service')
    keys=value.get('keys',{})
    if not isinstance(keys,dict) or set(keys)!={'auth','p256dh'}:raise ValueError('Invalid keys')
    for name,size in [('auth',16),('p256dh',65)]:
        raw=keys[name]
        if not isinstance(raw,str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,100}={0,2}',raw):raise ValueError('Invalid key')
        decoded=base64.urlsafe_b64decode(raw+'='*(-len(raw)%4))
        if len(decoded)!=size:raise ValueError('Invalid key length')
        if name=='p256dh':ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),decoded)
    return {'endpoint':endpoint,'keys':keys}

class NoRedirectSession(requests.Session):
    def __init__(self):super().__init__();self.trust_env=False
    def request(self,*args,**kwargs):kwargs['allow_redirects']=False;return super().request(*args,**kwargs)

class PushStore:
    def __init__(self,folder,cipher,origin):
        self.folder,self.cipher,self.origin=folder,cipher,origin
        self.keyfile=folder/'webpush.pem'
        if not self.keyfile.exists():
            key=ec.generate_private_key(ec.SECP256R1())
            data=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
            fd=os.open(self.keyfile,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as f:f.write(data)
        key=serialization.load_pem_private_key(self.keyfile.read_bytes(),None)
        self.public_key=base64.urlsafe_b64encode(key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)).decode().rstrip('=')
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS push_subscriptions(
              endpoint_hash TEXT PRIMARY KEY,uid INTEGER NOT NULL,session TEXT NOT NULL,
              value BLOB NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,last_test REAL NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS push_deliveries(
              endpoint_hash TEXT NOT NULL,action_id TEXT NOT NULL,state TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
              PRIMARY KEY(endpoint_hash,action_id));''')
    def db(self):
        db=sqlite3.connect(self.folder/'sessions.db',timeout=10);db.row_factory=sqlite3.Row;return db
    def subscribe(self,subscription,uid,session,expires):
        value=validate(subscription);ident=hashlib.sha256(value['endpoint'].encode()).hexdigest();now=time.time()
        with self.db() as db:
            existing=db.execute('SELECT uid FROM push_subscriptions WHERE endpoint_hash=?',(ident,)).fetchone()
            if existing and existing['uid']!=uid:raise ValueError('Subscription belongs to another account')
            if db.execute('SELECT count(*) FROM push_subscriptions WHERE uid=? AND expires>?',(uid,now)).fetchone()[0]>=10 and not existing:raise ValueError('Too many devices')
            db.execute('INSERT INTO push_subscriptions(endpoint_hash,uid,session,value,created,expires) VALUES(?,?,?,?,?,?) ON CONFLICT(endpoint_hash) DO UPDATE SET session=excluded.session,value=excluded.value,expires=excluded.expires',(ident,uid,session,self.cipher.encrypt(json.dumps(value).encode()),now,expires))
        return ident
    def unsubscribe(self,endpoint,uid):
        ident=hashlib.sha256(endpoint.encode()).hexdigest()
        with self.db() as db:
            if db.execute('SELECT 1 FROM push_subscriptions WHERE endpoint_hash=? AND uid=?',(ident,uid)).fetchone():
                db.execute('DELETE FROM push_deliveries WHERE endpoint_hash=?',(ident,))
                db.execute('DELETE FROM push_subscriptions WHERE endpoint_hash=?',(ident,))
    def logout(self,session):
        with self.db() as db:
            db.execute('DELETE FROM push_deliveries WHERE endpoint_hash IN (SELECT endpoint_hash FROM push_subscriptions WHERE session=?)',(session,))
            db.execute('DELETE FROM push_subscriptions WHERE session=?',(session,))
    def send(self,row,payload):
        subscription=validate(json.loads(self.cipher.decrypt(row['value'])))
        try:
            with NoRedirectSession() as client:
                response=webpush(subscription_info=subscription,data=json.dumps(payload),vapid_private_key=str(self.keyfile),vapid_claims={'sub':self.origin},ttl=300,timeout=12,requests_session=client)
            return response.status_code
        except WebPushException as exc:return exc.response.status_code if exc.response is not None else 503
        except Exception:return 503
    def test(self,endpoint,uid):
        ident=hashlib.sha256(endpoint.encode()).hexdigest();now=time.time()
        with self.db() as db:
            row=db.execute('SELECT * FROM push_subscriptions WHERE endpoint_hash=? AND uid=? AND expires>?',(ident,uid,now)).fetchone()
            if not row:raise ValueError('Device unavailable')
            if row['last_test']>now-60:return 429
            db.execute('UPDATE push_subscriptions SET last_test=? WHERE endpoint_hash=?',(now,ident))
        return self.send(row,{'title':'Autorisations','body':'Les notifications sont activées sur cet appareil.','tag':'ops-push-test','url':'/'})
    def tick(self,states):
        now=time.time();sent=0
        with self.db() as db:
            db.execute('DELETE FROM push_subscriptions WHERE expires<=? OR session NOT IN (SELECT digest FROM sessions WHERE expires>?)',(now,now))
            db.execute('DELETE FROM push_deliveries WHERE endpoint_hash NOT IN (SELECT endpoint_hash FROM push_subscriptions)')
            rows=db.execute('SELECT * FROM push_subscriptions LIMIT 10').fetchall()
        for row in rows:
            for state in states.values():
                try:
                    if state['decision'] or state['status']!='pending_approval':continue
                    if datetime.fromisoformat(state['deadline'].replace('Z','+00:00')).timestamp()<=now:continue
                    if datetime.fromisoformat(state['created_at'].replace('Z','+00:00')).timestamp()<row['created']:continue
                    aid=state['action_id']
                except (ValueError,KeyError,TypeError):continue
                with self.db() as db:
                    db.execute('INSERT OR IGNORE INTO push_deliveries VALUES(?,?,?,0,0)',(row['endpoint_hash'],aid,'queued'))
                    delivery=db.execute('SELECT * FROM push_deliveries WHERE endpoint_hash=? AND action_id=?',(row['endpoint_hash'],aid)).fetchone()
                    if delivery['state']!='queued' or delivery['next_attempt']>now:continue
                    # Claim before network I/O; crash retries carry the same notification tag.
                    db.execute('UPDATE push_deliveries SET attempts=attempts+1,next_attempt=? WHERE endpoint_hash=? AND action_id=?',(now+300,row['endpoint_hash'],aid))
                code=self.send(row,{'title':'Nouvelle autorisation','body':'Une demande attend ta décision.','tag':'ops-approval-'+aid,'url':'/'})
                phase='sent' if 200<=code<300 else 'failed' if delivery['attempts']>=4 else 'queued'
                with self.db() as db:
                    if code in (404,410):db.execute('DELETE FROM push_subscriptions WHERE endpoint_hash=?',(row['endpoint_hash'],))
                    db.execute('UPDATE push_deliveries SET state=? WHERE endpoint_hash=? AND action_id=?',(phase,row['endpoint_hash'],aid))
                sent+=1
                if sent>=10:return
                if code in (404,410):break
