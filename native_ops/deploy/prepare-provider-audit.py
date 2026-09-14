"""Provision a narrow local provider reader; no external provider requests here."""
import json,os,runpy,subprocess,time,urllib.error
from pathlib import Path
assert os.geteuid()==0
os.umask(0o077);root=Path('/home/ops-user/ops-control-plane');lib=runpy.run_path(str(root/'native_ops/deploy/provider-secrets.py'));vault=lib['Vault']()
refs=runpy.run_path(str(root/'broker/deploy/helpers/provider-access'))['REFS'];policy='\n'.join('path "'+p+'" { capabilities = ["read"] }' for p in refs.values())+'\n'
a=Path('/var/lib/ops-native-archives')/('provider-audit-'+str(time.time_ns()));a.mkdir(mode=0o700)
p=Path('/var/lib/atlas-local-repair/credentials.json');st=p.lstat();assert not p.is_symlink() and st.st_uid==0 and not st.st_mode&0o077
owner=json.loads(p.read_text())
try:
 v=vault.call('POST','auth/userpass/login/ops-user',{'password':owner['openbao_louis']});owner.clear();vault.token=v['auth']['client_token'];v.clear()
 try:prior=vault.call('GET','sys/policies/acl/ops-provider-audit')['data']['policy']
 except urllib.error.HTTPError as e:
  if e.code!=404:raise
  prior=None
 if prior is not None and prior!=policy:raise ValueError('Existing different policy requires review')
 if prior is None:vault.call('PUT','sys/policies/acl/ops-provider-audit',{'policy':policy})
 credentials=[Path('/etc/credstore.encrypted')/('ops-provider-audit-'+n) for n in ['role-id','secret-id']]
 if any(p.exists() for p in credentials):
  if not all(p.exists() for p in credentials):raise ValueError('Partial credentials require review')
 else:
  vault.call('POST','auth/approle/role/ops-provider-audit',{'token_policies':['ops-provider-audit'],'token_ttl':'5m','token_max_ttl':'10m','secret_id_ttl':0,'secret_id_num_uses':0,'token_bound_cidrs':['127.0.0.1/32']})
  role=vault.call('GET','auth/approle/role/ops-provider-audit/role-id')['data']['role_id'];secret=vault.call('POST','auth/approle/role/ops-provider-audit/secret-id',{})['data']['secret_id']
  for path,value in zip(credentials,[role,secret]):
   subprocess.run(['/usr/bin/systemd-creds','encrypt','--name='+path.name,'-',str(path)],input=value.encode(),capture_output=True,check=True,timeout=10);path.chmod(0o600)
  role=secret=''
 report={'status':'prepared','identity':'ops-provider-audit','references':list(refs.values()),'scope':'read-only exact references; local root consumer; no model credential output','provider_requests':0,'authorization':'direct-owner-local-construction-2026-09-14'}
 (a/'result.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
finally:
 owner.clear()
 if vault.token:vault.call('POST','auth/token/revoke-self',{});vault.token=''
