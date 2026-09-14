from pathlib import Path
import os,subprocess,time,json,shutil
assert os.geteuid()==0
python='/opt/ops-broker/venv/bin/python'
program='''from pathlib import Path
import yaml,json
from ops_broker.policy import RBACPolicy
p=Path('/etc/ops-broker/rbac.yaml');data=yaml.safe_load(p.read_text())
role=data['roles']['native-observer']
assert role['runbooks']==['local.health.v1'] and role['action_classes']==['A']
assert set(role['projects']) in ({'infra-shared'},{'infra-shared','minecraft','network-shared','monitoring-shared','backup-shared'})
role['projects']=['infra-shared','minecraft','network-shared','monitoring-shared','backup-shared']
RBACPolicy.from_data(data)
print(yaml.safe_dump(data,sort_keys=False))
'''
r=subprocess.run([python,'-c',program],check=True,capture_output=True,text=True)
p=Path('/etc/ops-broker/rbac.yaml');m=p.stat()
archive=Path('/var/lib/ops-native-archives')/('project-intake-'+str(time.time_ns()));archive.mkdir(mode=0o700)
shutil.copy2(p,archive/'rbac.before.yaml')
tmp=p.with_name('rbac.native-next.yaml')
fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,m.st_mode&0o777)
with os.fdopen(fd,'w') as f:f.write(r.stdout);f.flush();os.fsync(f.fileno())
os.chown(tmp,m.st_uid,m.st_gid);os.replace(tmp,p)
print(json.dumps({'projects':5,'runbooks_unchanged':True,'action_classes':['A'],'archive':str(archive)}))
