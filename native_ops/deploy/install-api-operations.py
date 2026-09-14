"""Add the fixed Hermes API identity and mission-scoped native tools, inert without API activation."""
from pathlib import Path
import os,subprocess,time,shutil,json
assert os.geteuid()==0
root=Path('/home/ops-user/ops-control-plane/native_ops/deploy')
archive=Path('/var/lib/ops-native-archives')/('api-operations-'+str(time.time_ns()));archive.mkdir(mode=0o700)
def write(p,data,mode):
 p=Path(p)
 if p.is_symlink():raise RuntimeError('Symlink refused')
 metadata=p.stat() if p.exists() else None
 if metadata:shutil.copy2(p,archive/p.name)
 tmp=p.with_name(p.name+'.next');fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
 with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
 if metadata:os.chown(tmp,metadata.st_uid,metadata.st_gid)
 os.replace(tmp,p)
program='''import yaml
from pathlib import Path
from ops_broker.policy import RBACPolicy
p=Path('/etc/ops-broker/rbac.yaml');v=yaml.safe_load(p.read_text())
expected={'roles':['supervised-operator']}
if 'hermes-native' in v['actors']:assert v['actors']['hermes-native']==expected
v['actors']['hermes-native']=expected
policy=RBACPolicy.from_data(v)
assert policy.projects_for_actor('hermes-native')==frozenset(['infra-shared','minecraft','network-shared','monitoring-shared','backup-shared'])
print(yaml.safe_dump(v,sort_keys=False))
'''
r=subprocess.run(['/opt/ops-broker/venv/bin/python','-c',program],check=True,capture_output=True)
p=Path('/etc/ops-broker/rbac.yaml');write(p,r.stdout,p.stat().st_mode&0o777)
write('/usr/local/libexec/ops-native-api-broker',(root/'ops-native-api-broker').read_bytes(),0o755)
sudoers=archive/'sudoers.reviewed';sudoers.write_text('hermesd ALL=(opsbroker:opsbroker) NOPASSWD: /usr/local/libexec/ops-native-api-broker ""\n');sudoers.chmod(0o440)
subprocess.run(['visudo','-cf',str(sudoers)],check=True,stdout=subprocess.DEVNULL)
write('/etc/sudoers.d/ops-native-api',sudoers.read_bytes(),0o440)
program='''import yaml
from pathlib import Path
p=Path('/var/lib/hermes/native-ops/hermes/config.yaml');v=yaml.safe_load(p.read_text())
v['mcp_servers']['ops-native']['tools']['include']=['get_mission_operations','search_mission_memory','get_service_health','request_mission_action','get_mission_action','execute_approved_mission_action']
print(yaml.safe_dump(v,sort_keys=False))
'''
r=subprocess.run(['/opt/ops-native/venv/bin/python','-c',program],check=True,capture_output=True)
profile=Path('/var/lib/hermes/native-ops/hermes/config.yaml');write(profile,r.stdout,profile.stat().st_mode&0o777)
soul=Path('/var/lib/hermes/native-ops/hermes/SOUL.md')
write(soul,'Tu es Ops supervisé. Les actions passent exclusivement par les outils MCP de la mission. Le broker impose les budgets et une approbation Zulip humaine pour la classe C. Aucun secret ni commande hors MCP. Réponds en français avec les preuves ou la limite exacte.\n'.encode(),0o644)
print(json.dumps({'identity':'hermes-native','tools':6,'api_activation_changed':False,'archive':str(archive)}))
