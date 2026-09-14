import argparse
import asyncio
import fcntl
import json
import os
import stat
import time
from pathlib import Path
from .broker import connect
from .state import State
from .dispatch import select_backend, select_project, available_cli
from .execution import api_allowed, execution_mode
from .worker import Hermes, Worker
from .mission_tools import Operations
from .zulip_api import Zulip


def private_json(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        metadata=os.fstat(fd)
        mode=stat.S_IMODE(metadata.st_mode)
        allowed_mode=mode in {0o400,0o600} or (mode==0o440 and (metadata.st_gid in {os.getgid(),*os.getgroups()} or (metadata.st_uid==0 and metadata.st_gid==0)))
        if not stat.S_ISREG(metadata.st_mode) or not allowed_mode or metadata.st_uid not in {0,os.getuid()} or metadata.st_size>65536:
            raise ValueError('private configuration required')
        with os.fdopen(fd,'r') as stream:
            fd=-1
            return json.load(stream)
    finally:
        if fd>=0:os.close(fd)

async def run(config):
    folder=Path(config['state_dir'])
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (folder/'worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state=State(folder/'delivery.sqlite3')
        secret=private_json(config['zulip_credential_file'])
        zulip=Zulip(config['zulip_origin'],config['zulip_ca'],secret['email'],secret['key'])
        secret.clear()
        me=await asyncio.to_thread(zulip.call,'GET','/api/v1/users/me')
        if me['user_id'] != config['bot_user_id'] or me.get('is_bot') is not True:
            raise ValueError('wrong bot identity')
        stream_id=config['stream_id']
        hermes=Hermes(config['hermes_argv'],config['hermes_home'])
        async with connect(config['broker_command'],[],config['actor']) as broker:
            worker=Worker(state,broker,hermes,zulip,stream_id,operations=Operations())
            if state.meta('cursor') is None:
                existing=await asyncio.to_thread(zulip.messages,stream_id,'newest',1,0)
                state.meta('cursor',max([m['id'] for m in existing],default=0))
            while True:
                try:
                    cursor=state.meta('cursor')
                    messages=await asyncio.to_thread(zulip.messages,stream_id,cursor,0,100)
                    for msg in messages:
                        if msg['id']<=cursor:continue
                        trusted=await asyncio.to_thread(zulip.trusted_message,msg,stream_id,config['human_ids'])
                        if trusted and isinstance(msg.get('content'),str) and 1<=len(msg['content'])<=4000:
                            try:
                                project=select_project(msg['subject'])
                                state.accept(msg['id'],msg['subject'],msg['content'],project_id=project)
                            except ValueError:
                                state.accept(msg['id'],msg['subject'],msg['content'])
                                state.set(msg['id'],backend='manual',error='unknown_project')
                        # Intake and cursor are ordered: a crash can repeat ingestion, never lose it.
                        state.meta('cursor',msg['id'])
                    for job in state.pending():
                        if job.get('backend') is None:
                            backend=select_backend(job['content'],execution_mode(),available_cli())
                            if backend is None:continue
                            state.set(job['source_id'],backend=backend)
                            job=state.get(job['source_id'])
                        if job['phase']=='received':
                            mission=await broker.call('create_mission',
                                request_id=worker.request_id(job['source_id'],'mission'),
                                project_id=job['project_id'],title=job['content'][:200])
                            state.set(job['source_id'],mission_id=mission['id'],phase='prepared')
                            job=state.get(job['source_id'])
                        if job['phase']=='prepared':
                            await broker.call('add_mission_record',
                                request_id=worker.request_id(job['source_id'],'source'),
                                mission_id=job['mission_id'],kind='observation',content=job['content'],
                                evidence=['zulip-source:'+str(job['source_id'])])
                            await broker.call('add_mission_record',
                                request_id=worker.request_id(job['source_id'],'dispatch'),
                                mission_id=job['mission_id'],kind='observation',
                                content='Destination de traitement : '+job['backend'],
                                evidence=['zulip-dispatch:'+str(job['source_id'])+':'+job['backend']])
                            await worker.accept_handoff(job)
                            job=state.get(job['source_id'])
                        if job['phase']=='prepared' and job.get('error')=='unknown_project':
                            state.set(job['source_id'],phase='ready',response='Projet inconnu dans le sujet. Utilise [infra-shared], [minecraft], [network-shared], [monitoring-shared] ou [backup-shared].')
                            job=state.get(job['source_id'])
                        if (job.get('backend')=='api' and config.get('generation_enabled') is True and api_allowed()) or job['phase'] in {'ready','sending'}:
                            await worker.process(job)
                    await worker.approval_followups()
                    status={'status':'running','generation_enabled':config.get('generation_enabled') is True and api_allowed(),'execution_mode':execution_mode(),
                            'updated_at':int(time.time()),'pending':len(state.pending())}
                except Exception:
                    # Keep credentials and unexpected remote output out of journalctl.
                    status={'status':'degraded','updated_at':int(time.time()),'error':'operation_failed'}
                target=folder/'status.json'
                temporary=folder/'status.next'
                temporary.write_text(json.dumps(status)+'\n');os.replace(temporary,target)
                await asyncio.sleep(5)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    args=parser.parse_args()
    os.umask(0o077)
    config=private_json(args.config)
    if config.get('actor')!='native-observer':
        raise SystemExit('fixed service identity required')
    try:asyncio.run(run(config))
    except KeyboardInterrupt:pass
    except Exception:raise SystemExit('Ops native could not start; check configuration and dependencies') from None

if __name__=='__main__':main()
