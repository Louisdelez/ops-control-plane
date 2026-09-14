#!/usr/bin/python3
"""Install only the additive native observation pilot. Never reinstall Zulip/Hermes."""
from pathlib import Path
import argparse
import base64
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
import time

SOURCE=Path('/home/ops-user/ops-control-plane/native_ops')
STAGE=Path('/home/ops-user/standard-install-review/native-integration')
DEST=Path('/opt/ops-native')
CONFIG=Path('/etc/ops-native')
STATE=Path('/var/lib/hermes/native-ops')
PYTHON=DEST/'venv/bin/python'


def run(argv,**kw):
    return subprocess.run([str(x) for x in argv],check=True,**kw)

def write(path,data,mode=0o600):
    path=Path(path)
    if path.is_symlink():raise RuntimeError('symlink destination refused')
    temporary=path.with_name(path.name+'.native-next')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    os.fchmod(fd,mode)
    with os.fdopen(fd,'wb') as f:
        f.write(data.encode() if isinstance(data,str) else data);f.flush();os.fsync(f.fileno())
    os.replace(temporary,path)

def provision():
    import yaml
    from ops_native.zulip_api import Zulip
    account=pwd.getpwnam('hermesd')
    CONFIG.mkdir(mode=0o700,exist_ok=True)
    STATE.mkdir(mode=0o700,exist_ok=True);os.chown(STATE,account.pw_uid,account.pw_gid)
    profile=STATE/'hermes';profile.mkdir(mode=0o700,exist_ok=True);os.chown(profile,account.pw_uid,account.pw_gid)
    if not (profile/'config.yaml').exists():
        write(profile/'config.yaml',(SOURCE/'deploy/hermes.yaml').read_bytes(),0o644)
        write(profile/'SOUL.md','Tu es Ops Observation. Vérifie les faits avec le seul outil autorisé. Aucun changement de service. Réponds en français.\n',0o644)
    for name in ['config.yaml','SOUL.md']:
        (profile/name).chmod(0o644)
    rbac_path=Path('/etc/ops-broker/rbac.yaml')
    original=rbac_path.read_bytes();policy=yaml.safe_load(original)
    role={'projects':['infra-shared','minecraft','network-shared','monitoring-shared','backup-shared'],'runbooks':['local.health.v1'],'action_classes':['A'],
          'lifecycle_capabilities':['mission.context.write','mission.record.write']}
    for collection,key,value in [('roles','native-observer',role),('actors','native-observer',{'roles':['native-observer']})]:
        if key in policy[collection] and policy[collection][key]!=value:
            raise RuntimeError('existing native identity differs')
        policy[collection][key]=value
    if 'native-observer' not in yaml.safe_load(original)['actors']:
        backup=CONFIG/'rbac.before-native.yaml'
        if not backup.exists():write(backup,original)
        original_metadata=rbac_path.stat()
        write(rbac_path,yaml.safe_dump(policy,sort_keys=False),original_metadata.st_mode & 0o777)
        os.chown(rbac_path,original_metadata.st_uid,original_metadata.st_gid)
    write('/usr/local/libexec/ops-native-broker',(SOURCE/'deploy/ops-native-broker').read_bytes(),0o755)
    sudoers='hermesd ALL=(opsbroker:opsbroker) NOPASSWD: /usr/local/libexec/ops-native-broker ""\n'
    sudo_temp=CONFIG/'sudoers.reviewed';write(sudo_temp,sudoers,0o440)
    run(['/usr/sbin/visudo','-cf',sudo_temp],stdout=subprocess.DEVNULL)
    write('/etc/sudoers.d/ops-native',sudoers,0o440)
    origin='https://zulip.ops.local:8443'
    ca='/etc/pki/ca-trust/source/anchors/zulip-standard-local.crt'
    # Reuse the existing local owner's prepared credential without displaying it.
    secret_path=Path('/var/lib/atlas-local-repair/credentials.json')
    info=secret_path.lstat()
    if secret_path.is_symlink() or info.st_uid!=0 or info.st_mode&0o077:raise RuntimeError('unsafe prepared credentials')
    password=json.loads(secret_path.read_bytes())['zulip_owner']
    admin=Zulip(origin,ca,'admin@ops.local','')
    login=admin.call('POST','/api/v1/fetch_api_key',{'username':'admin@ops.local','password':password})
    password=''
    admin=Zulip(origin,ca,'admin@ops.local',login.pop('api_key'));login.clear()
    owner=admin.call('GET','/api/v1/users/me')
    if owner.get('is_owner') is not True:raise RuntimeError('owner required')
    short='ops-observation'
    bots=admin.call('GET','/api/v1/bots')['bots']
    matches=[b for b in bots if b.get('username','').split('@')[0]==short+'-bot']
    if len(matches)>1:raise RuntimeError('ambiguous bot')
    if matches:bot=matches[0]
    else:bot=admin.call('POST','/api/v1/bots',{'short_name':short,'full_name':'Ops Observation'})
    bot_key=bot['api_key'];email=bot.get('username') or bot.get('email')
    if not email:
        members=admin.call('GET','/api/v1/users')['members']
        emails=[u['email'] for u in members if u.get('is_bot') and u['email'].split('@')[0]==short+'-bot']
        if len(emails)!=1:raise RuntimeError('bot not found')
        email=emails[0]
    client=Zulip(origin,ca,email,bot_key);bot_me=client.call('GET','/api/v1/users/me')
    channel='ops-pilote'
    subscriptions=admin.call('GET','/api/v1/users/me/subscriptions')['subscriptions']
    existing=[s for s in subscriptions if s['name']==channel]
    if existing and not existing[0].get('invite_only'):raise RuntimeError('pilot channel must be private')
    admin.call('POST','/api/v1/users/me/subscriptions',{
        'subscriptions':[{'name':channel,'description':'Pilotage Ops natif par API ou CLI ; actions via le broker.'}],
        'invite_only':True,'principals':[owner['user_id'],bot_me['user_id']]})
    stream_id=admin.call('GET','/api/v1/get_stream_id',{'stream':channel})['stream_id']
    write(CONFIG/'zulip.json',json.dumps({'email':email,'key':bot_key}))
    bot_key='';bot.clear()
    config={'actor':'native-observer','broker_command':'/usr/local/libexec/ops-native-broker',
        'state_dir':str(STATE/'connector'),'hermes_home':str(profile),
        'hermes_argv':['/var/lib/hermes/hermes-agent/venv/bin/python','-m','hermes_cli.main'],
        'zulip_origin':origin,'zulip_ca':ca,
        'zulip_credential_file':'/run/credentials/ops-native.service/zulip.json',
        'bot_user_id':bot_me['user_id'],'human_ids':[owner['user_id']],'stream_id':stream_id,
        'generation_enabled':False}
    if (CONFIG/'config.json').exists():
        prior=json.loads((CONFIG/'config.json').read_bytes())
        if prior.get('generation_enabled'):raise RuntimeError('do not overwrite active model configuration')
    write(CONFIG/'config.json',json.dumps(config,indent=2)+'\n')
    write('/etc/systemd/system/ops-native.service',(SOURCE/'deploy/ops-native.service').read_bytes(),0o644)
    run(['/usr/bin/systemd-analyze','verify','/etc/systemd/system/ops-native.service'],stdout=subprocess.DEVNULL)
    run(['/usr/bin/systemctl','daemon-reload'])
    run(['/usr/bin/systemctl','reset-failed','ops-native.service'],stdout=subprocess.DEVNULL)
    run(['/usr/bin/systemctl','enable','--now','ops-native.service'])
    report={'stage':'observation-intake','generation_enabled':False,'channel':channel,'stream_id':stream_id,
            'bot_user_id':bot_me['user_id'],'owner_user_id':owner['user_id'],'time':int(time.time())}
    write(CONFIG/'installation.json',json.dumps(report,indent=2)+'\n')
    print(json.dumps(report),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--provision',action='store_true')
    args=parser.parse_args()
    if args.provision:
        if os.geteuid()!=0:raise SystemExit('root required')
        provision()
        run(['/usr/bin/python3',SOURCE/'deploy/install-api-operations.py'])
        return
    import runpy
    runpy.run_path(str(SOURCE/'release.py'))['verify'](SOURCE.parent)
    print('Reviewed additive installation: ops-native runtime, private pilot bot/channel, scoped broker identities and one service.',flush=True)
    print('Provider calls remain disabled. Existing Zulip and Hermes installations are preserved.',flush=True)
    if not args.apply:return
    if os.geteuid()!=0:raise SystemExit('root required')
    os.umask(0o077);DEST.mkdir(mode=0o755,exist_ok=True);DEST.chmod(0o755)
    if not PYTHON.exists():run(['/usr/bin/python3','-m','venv',DEST/'venv'])
    with (DEST/'install.log').open('ab') as log:
        run([PYTHON,'-m','pip','install','--no-index','--find-links',STAGE/'wheels','ops-native==0.1.7','mcp==2.1.1','PyYAML==6.0.3','ops-orchestrator==0.2.0'],stdout=log,stderr=subprocess.STDOUT)
    # The runtime contains no credentials; service accounts need normal read/execute access.
    for path in (DEST/'venv').rglob('*'):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
    (DEST/'venv').chmod(0o755)
    os.execv(PYTHON,[str(PYTHON),str(Path(__file__).resolve()),'--provision'])

if __name__=='__main__':
    try:main()
    except Exception:
        print('Native installation stopped; existing products and their data were not rolled back.',file=sys.stderr)
        raise
