#!/usr/bin/python3
"""Owner-run, eight-hour local project delegation; no password storage."""
import os,subprocess,time
from pathlib import Path

HELPER=Path('/usr/local/libexec/ops-native-project')
RULE=Path('/etc/sudoers.d/ops-native-project')
LOG=Path('/var/lib/ops-native-project-audit')

def main():
    if os.geteuid()!=0 or os.environ.get('SUDO_UID')!='1000':
        raise SystemExit('Run once as ops-user using sudo in your own terminal.')
    expiry=int(time.time())+8*3600
    program='''#!/usr/bin/python3 -I
import hashlib,json,os,sys,time
from pathlib import Path
if os.geteuid()!=0 or os.environ.get('SUDO_UID')!='1000':raise SystemExit('Owner identity required')
if sys.argv[1:]==['--finish']:
    Path('/etc/sudoers.d/ops-native-project').unlink(missing_ok=True)
    Path('/usr/local/libexec/ops-native-project').unlink(missing_ok=True)
    print('TEMPORARY_PROJECT_ACCESS_REMOVED');raise SystemExit(0)
if time.time()>EXPIRY:raise SystemExit('Temporary project access expired')
if not 2<=len(sys.argv)<=4:raise SystemExit('One local project script required')
p=Path(sys.argv[1]).resolve(strict=True)
roots=[Path('/home/ops-user/standard-install-review/native-integration'),Path('/home/ops-user/ops-control-plane/native_ops/deploy')]
if not any(p.is_relative_to(root) for root in roots) or p.suffix!='.py' or p.stat().st_uid!=1000:raise SystemExit('Outside the owner-authorized local project')
if any(arg not in ['--apply','--provision'] for arg in sys.argv[2:]):raise SystemExit('Unexpected argument')
with Path('/var/lib/ops-native-project-audit/events.jsonl').open('a') as log:
    log.write(json.dumps({'time':int(time.time()),'script':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'args':sys.argv[2:]})+'\\n')
os.execv('/usr/bin/python3',['/usr/bin/python3','-I',str(p),*sys.argv[2:]])
'''.replace('EXPIRY',str(expiry))
    LOG.mkdir(mode=0o700,exist_ok=True)
    if HELPER.is_symlink() or RULE.is_symlink():raise SystemExit('Refusing symlink')
    HELPER.write_text(program);HELPER.chmod(0o555)
    temporary=LOG/'sudoers.next'
    temporary.write_text('ops-user ALL=(root) NOPASSWD: /usr/local/libexec/ops-native-project *\n');temporary.chmod(0o440)
    subprocess.run(['/usr/sbin/visudo','-cf',str(temporary)],check=True)
    os.replace(temporary,RULE)
    print('Accès local temporaire prêt pour 8 heures. Aucune autre fenêtre Fedora nécessaire pour ce projet.')
    print('Révocation immédiate : sudo -n /usr/local/libexec/ops-native-project --finish')

if __name__=='__main__':main()
