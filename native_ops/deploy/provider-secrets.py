#!/usr/bin/python3
"""Native local hidden input -> existing OpenBao. No model sees a credential."""
from pathlib import Path
import json,os,ssl,subprocess,urllib.request,urllib.error

STAGE='initialization'
ORIGIN='https://127.0.0.1:8200'
CA='/etc/pki/ca-trust/source/anchors/openbao-local.crt'

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise RuntimeError('redirect refused')

class Vault:
    def __init__(self):
        self.token=''
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=CA)))
    def call(self,method,path,data=None):
        request=urllib.request.Request(ORIGIN+'/v1/'+path,method=method,
            headers={'Content-Type':'application/json','X-Vault-Token':self.token},
            data=None if data is None else json.dumps(data).encode())
        with self.opener.open(request,timeout=10) as response:
            body=response.read(65537)
        if len(body)>65536:raise RuntimeError('response limit')
        return json.loads(body) if body else {}

def prompt(title,text):
    gui=json.loads(Path('/home/ops-user/standard-install-review/native-integration/gui-environment.json').read_text())
    env=['/usr/bin/env',*[k+'='+v for k,v in gui.items()]]
    p=subprocess.run(['/usr/sbin/runuser','-u','ops-user','--',*env,'/usr/bin/zenity','--password','--title='+title,'--text='+text],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=900)
    if p.returncode!=0:raise RuntimeError('input cancelled')
    value=p.stdout.decode().rstrip('\n')
    if not value or len(value)>4096 or '\n' in value:raise RuntimeError('invalid input')
    return value

def seal(name,value):
    path=Path('/etc/credstore.encrypted')/name
    if path.exists():raise RuntimeError('credential already exists; explicit rotation required')
    p=subprocess.run(['/usr/bin/systemd-creds','encrypt','--name='+name,'-',str(path)],input=value.encode(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if p.returncode:raise RuntimeError('credential encryption failed')
    path.chmod(0o600)

def main():
    global STAGE
    if os.geteuid()!=0:raise RuntimeError('root required')
    os.umask(0o077)
    vault=Vault()
    prepared=Path('/var/lib/atlas-local-repair/credentials.json')
    metadata=prepared.lstat()
    if prepared.is_symlink() or metadata.st_uid!=0 or metadata.st_mode&0o077:
        raise RuntimeError('prepared credential file permissions invalid')
    values=json.loads(prepared.read_bytes())
    password=values.get('openbao_louis');values.clear()
    if not isinstance(password,str) or not password:
        raise RuntimeError('prepared OpenBao login unavailable')
    try:
        STAGE='openbao_login'
        login=vault.call('POST','auth/userpass/login/ops-user',{'password':password})
        password='';vault.token=login['auth']['client_token'];login.clear()
        for account,label in [('qwen','Alibaba Model Studio — région Singapore / International'),('deepseek','DeepSeek')]:
            STAGE='provider_reference_'+account
            path='kv-infra-shared/data/llm/'+account
            try:
                existing=vault.call('GET',path)
                data=existing.get('data',{}).get('data',{})
                if isinstance(data.get('api_key'),str) and data['api_key'].strip():
                    existing.clear();data.clear();print('EXISTING_PROVIDER_REFERENCE',account,flush=True);continue
                raise RuntimeError('existing provider record has no api_key; review required')
            except urllib.error.HTTPError as e:
                if e.code!=404:raise
            key=prompt('Clé API '+label,'Saisie masquée. Stockage dans OpenBao uniquement ; aucun appel de modèle pendant cette saisie.')
            vault.call('POST',path,{'options':{'cas':0},'data':{'api_key':key}});key=''
            print('PROVIDER_REFERENCE_CREATED',account,flush=True)
        STAGE='service_approle'
        policy='path "kv-infra-shared/data/llm/qwen" { capabilities = ["read"] }\npath "kv-infra-shared/data/llm/deepseek" { capabilities = ["read"] }\n'
        vault.call('PUT','sys/policies/acl/ops-native-api',{'policy':policy})
        vault.call('POST','auth/approle/role/ops-native-api',{'token_policies':['ops-native-api'],'token_period':'15m','secret_id_ttl':0,'secret_id_num_uses':0})
        role=vault.call('GET','auth/approle/role/ops-native-api/role-id')['data']['role_id']
        secret=vault.call('POST','auth/approle/role/ops-native-api/secret-id',{})['data']['secret_id']
        STAGE='seal_service_credentials'
        seal('ops-native-api-role-id',role);seal('ops-native-api-secret-id',secret)
        role=secret=''
        print('NATIVE_API_APPROLE_PREPARED',flush=True)
    finally:
        password=''
        if vault.token:
            try:vault.call('POST','auth/token/revoke-self',{})
            finally:vault.token=''

if __name__=='__main__':
    try:main()
    except Exception as error:
        # Only a bounded category/status; no exception body, input or credential.
        print('PROVIDER_SETUP_STOPPED',STAGE,type(error).__name__,getattr(error,'code',''),flush=True)
        raise SystemExit(1)
