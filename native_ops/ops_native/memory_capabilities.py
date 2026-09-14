"""Activate prepared memory APIs from named systemd credentials, never their values."""
import json,os,re,stat,subprocess,tempfile,time,tomllib
from pathlib import Path

CONFIG=Path('/etc/ops-memory/memory.toml')
STORE=Path('/etc/credstore.encrypted')
MANAGED=Path('/etc/ops-memory/api-openbao-managed')
STATUS=Path('/run/ops-memory-api/status.json')

def managed_ready(marker=MANAGED,status=STATUS):
    if not marker.exists():return None
    try:
        st=status.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_mode&0o022 or st.st_size>4096:return False
        value=json.loads(status.read_text())
        if not 0<=time.time()-value['checked_at']<180:return False
        return value.get('providers',value.get('ready') is True)
    except (OSError,ValueError,KeyError,TypeError):return False

def desired(text,store=STORE,managed=None):
    config=tomllib.loads(text);changed=False
    for name in ['embedding','reranker']:
        api=config[name]['api'];credential=api.get('credential_credential','')
        # Leave custom integrations alone; this reconciler owns only this preset.
        if api.get('url') not in {'https://api.jina.ai/v1/'+('embeddings' if name=='embedding' else 'rerank'),'https://api.siliconflow.com/v1/'+('embeddings' if name=='embedding' else 'rerank'),
            'https://api.voyageai.com/v1/embeddings' if name=='embedding' else 'https://api.cohere.com/v2/rerank'}:continue
        if credential!=name+'-api-token':continue
        path=store/credential
        ready=path.is_file() and not path.is_symlink() and path.stat().st_size>0
        if managed is not None:ready=ready and (managed.get(name) is True if isinstance(managed,dict) else managed is True)
        if ready==api.get('enabled'):continue
        marker='['+name+'.api]';start=text.index(marker);end=text.find('\n[',start+len(marker))
        if end<0:end=len(text)
        block=re.sub(r'^enabled\s*=.*$','enabled = '+str(ready).lower(),text[start:end],flags=re.M)
        text=text[:start]+block+text[end:];changed=True
    return text,changed

def reconcile(path=CONFIG,store=STORE):
    if os.geteuid()!=0:raise PermissionError('Administrator identity required')
    previous=path.read_text();text,changed=desired(previous,store,managed_ready())
    if not changed:return False
    metadata=path.stat();fd,name=tempfile.mkstemp(prefix='.memory-capabilities-',dir=path.parent)
    try:
        os.fchmod(fd,metadata.st_mode&0o777);os.fchown(fd,metadata.st_uid,metadata.st_gid)
        with os.fdopen(fd,'w') as out:out.write(text);out.flush();os.fsync(out.fileno())
        from ops_memory.config import MemoryConfig
        MemoryConfig.from_toml(name)
        os.replace(name,path)
        result=subprocess.run(['/usr/bin/systemctl','restart','ops-memory.service'],capture_output=True,timeout=45)
        if result.returncode:
            path.write_text(previous)
            subprocess.run(['/usr/bin/systemctl','restart','ops-memory.service'],capture_output=True,timeout=45)
            raise RuntimeError('Memory activation failed; configuration restored')
    finally:
        Path(name).unlink(missing_ok=True)
    return True

if __name__=='__main__':reconcile()
