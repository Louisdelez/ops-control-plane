"""Install the reviewed local completion payload once; preserve rollback material.

Run with sudo after review. Does not deploy remote targets or request model output.
"""
from pathlib import Path
import hashlib,json,os,runpy,shutil,sqlite3,subprocess,time
ROOT=Path('/home/ops-user/ops-control-plane')
OUT=ROOT/'artifacts/completion-2026-09-11'
if os.geteuid()!=0:raise SystemExit('Administrator authentication required for system installation')
runpy.run_path(str(ROOT/'native_ops/release.py'))['verify'](ROOT)
manifest=json.loads((OUT/'payload.json').read_text())
for name,digest in manifest['files'].items():
 path=ROOT/name
 if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise SystemExit('Reviewed payload changed: '+name)
archive=Path('/var/lib/ops-native-archives')/('completion-20260911-'+str(time.time_ns()))
archive.mkdir(parents=True,mode=0o700)
desktop=Path('/home/ops-user/.local/lib/ops-desktop/ops-desktop')
shutil.copy2(desktop,archive/'desktop-before')
def run(*args):return subprocess.run(args,check=True,capture_output=True,timeout=120)
venv=Path('/opt/ops-native/venv');memory=Path('/opt/ops-memory/src')
shutil.copytree(venv,archive/'native-venv',symlinks=True)
shutil.copytree(memory,archive/'memory-src',symlinks=True)
for directory in ['/etc/ops-native','/etc/ops-memory']:
 shutil.copytree(directory,archive/Path(directory).name,symlinks=True)
# SQLite's online backup includes the WAL; never copy a live DB file by itself.
for name,path in [('memory','/var/lib/ops-memory/memory.sqlite3'),('native','/var/lib/hermes/native-ops/connector/delivery.sqlite3')]:
 if Path(path).exists():
  with sqlite3.connect('file:'+path+'?mode=ro',uri=True) as source, sqlite3.connect(archive/(name+'.sqlite3')) as target:source.backup(target)
files={}
def install(source,target,mode=0o644):
 target=Path(target)
 if target.is_symlink():raise RuntimeError('Symlink destination refused')
 files[str(target)]=target.read_bytes() if target.exists() else None
 temp=target.with_name(target.name+'.completion-next')
 fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
 with os.fdopen(fd,'wb') as out:out.write(Path(source).read_bytes());out.flush();os.fsync(out.fileno())
 os.chown(temp,0,target.stat().st_gid if target.exists() else 0);os.chmod(temp,mode);os.replace(temp,target)
mode_path=Path('/etc/ops-execution-mode.json')
old_mode=mode_path.read_bytes() if mode_path.exists() else None
active={u:subprocess.run(['systemctl','is-active','--quiet',u]).returncode==0 for u in ['ops-native.service','ops-memory.service']}
try:
 for u in active:run('systemctl','stop',u)
 for wheel in ['ops_native-0.1.8-py3-none-any.whl','ops_memory-0.1.1-py3-none-any.whl']:
  run(str(venv/'bin/python'),'-m','pip','install','--no-index','--no-deps','--force-reinstall',str(OUT/'wheels'/wheel))
 run(str(venv/'bin/python'),'-m','pip','check')
 staged=memory.with_name('src.completion-next')
 if staged.exists():raise RuntimeError('Memory staging directory already exists')
 shutil.copytree(ROOT/'memory/src',staged,ignore=shutil.ignore_patterns('__pycache__','*.egg-info'))
 for item in [staged,*staged.rglob('*')]:
  if item.is_symlink():raise RuntimeError('Source symlink refused')
  os.chown(item,0,0);os.chmod(item,0o755 if item.is_dir() else 0o644)
 memory.rename(archive/'memory-src-replaced');staged.rename(memory)
 preset=json.loads((ROOT/'memory/config/providers-api.json').read_text())
 config_path=Path('/etc/ops-memory/memory.toml')
 rendered=runpy.run_path(str(ROOT/'native_ops/deploy/prepare-memory-api.py'))['render'](config_path.read_text(),preset)
 memory_config=archive/'memory-config.reviewed.toml';memory_config.write_text(rendered)
 install(memory_config,config_path,config_path.stat().st_mode&0o777)
 install(ROOT/'native_ops/deploy/ops-execution-mode','/usr/local/libexec/ops-execution-mode',0o755)
 for name in ['ops-native-capabilities.service','ops-native-capabilities.timer']:
  install(ROOT/'native_ops/deploy'/name,'/etc/systemd/system/'+name)
 # Add only the mission memory tool to the dedicated Hermes profile.
 config=Path('/var/lib/hermes/native-ops/hermes/config.yaml')
 shutil.copy2(config,archive/'hermes-config.yaml')
 code="import yaml;from pathlib import Path;p=Path('/var/lib/hermes/native-ops/hermes/config.yaml');v=yaml.safe_load(p.read_text());t=v['mcp_servers']['ops-native']['tools']['include'];t.append('search_mission_memory') if 'search_mission_memory' not in t else None;p.write_text(yaml.safe_dump(v,sort_keys=False))"
 # The dedicated profile is root-managed; preserve its existing ownership.
 run(str(venv/'bin/python'),'-c',code)
 run('/usr/bin/env','PYTHONPATH=/opt/ops-memory/src','/usr/bin/python3','-m','ops_memory.cli','check-config','--config','/etc/ops-memory/memory.toml')
 run('systemctl','daemon-reload')
 for u in active:run('systemctl','start',u)
 run('systemctl','enable','--now','ops-native-capabilities.timer')
 run('systemctl','start','ops-native-capabilities.service')
 for u in active:run('systemctl','is-active',u)
 run('/usr/sbin/runuser','-u','ops-user','--','/usr/bin/python3',str(ROOT/'apps/ops-desktop/install.py'))
 time.sleep(10)
 run('/usr/bin/python3',str(ROOT/'native_ops/deploy/verify-native-release.py'))
 result={'status':'installed','native_version':'0.1.8','memory_version':'0.1.1','archive':str(archive),'remote_changes':False,'provider_calls_requested':False}
 (archive/'result.json').write_text(json.dumps(result));print(json.dumps(result))
except BaseException:
 subprocess.run(['systemctl','disable','--now','ops-native-capabilities.timer'])
 for u in active:subprocess.run(['systemctl','stop',u])
 desktop_recovery=desktop.with_name('ops-desktop.recovery-next')
 shutil.copy2(archive/'desktop-before',desktop_recovery)
 os.chown(desktop_recovery,1000,1000);os.replace(desktop_recovery,desktop)
 if venv.exists():venv.rename(archive/'failed-native-venv')
 (archive/'native-venv').rename(venv)
 if memory.exists():memory.rename(archive/'failed-memory-src')
 (archive/'memory-src').rename(memory)
 for name in ['ops-native','ops-memory']:
  original=archive/name
  shutil.copytree(original,Path('/etc')/name,dirs_exist_ok=True,symlinks=True)
 if old_mode is not None:mode_path.write_bytes(old_mode)
 for name,content in files.items():
  p=Path(name)
  if content is None:p.unlink(missing_ok=True)
  else:p.write_bytes(content)
 config=Path('/var/lib/hermes/native-ops/hermes/config.yaml')
 if (archive/'hermes-config.yaml').exists():shutil.copy2(archive/'hermes-config.yaml',config)
 subprocess.run(['systemctl','daemon-reload'])
 for u,was_active in active.items():
  if was_active:subprocess.run(['systemctl','start',u])
 raise
