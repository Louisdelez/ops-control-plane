"""Reviewed model selections; source memories and previous vector spaces are retained."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time
import tomllib
from . import memory_capabilities, memory_secrets

EMBEDDINGS = ('jina-embeddings-v5-text-small', 'jina-embeddings-v5-text-nano', 'voyage-4-lite', 'voyage-4', 'voyage-4-large')
RERANKERS = ('jina-reranker-v3.5', 'jina-reranker-v3', 'rerank-v4.0-fast', 'rerank-v4.0-pro')

def render(text, embedding=None, reranker=None, migrate=False):
    config=tomllib.loads(text)
    for section, model, allowed, url, protocol in [
        ('embedding',embedding,EMBEDDINGS,'https://api.voyageai.com/v1/embeddings','voyage'),
        ('reranker',reranker,RERANKERS,'https://api.cohere.com/v2/rerank','cohere')]:
        if model is None:continue
        if model not in allowed:raise ValueError('Model not in reviewed selection')
        if model.startswith('jina-'):
            url='https://api.jina.ai/v1/'+('embeddings' if section=='embedding' else 'rerank');protocol='jina'
        old=config[section]['api']
        if not migrate and old.get('url')!=url:raise ValueError('Provider migration requires installer')
        start=text.index('['+section+'.api]');end=text.find('\n[',start+1)
        if end<0:end=len(text)
        block=text[start:end]
        values={'model':json.dumps(model),'url':json.dumps(url),'protocol':json.dumps(protocol),
                'credential_env':'""','credential_credential':json.dumps(section+'-api-token')}
        if migrate:values['enabled']='false'
        for key,value in values.items():
            pattern=r'^'+key+r'\s*=.*$'
            if re.search(pattern,block,re.M):block=re.sub(pattern,key+' = '+value,block,flags=re.M)
            else:block+='\n'+key+' = '+value+'\n'
        text=text[:start]+block+text[end:]
    if embedding:
        # All reviewed presets use the same dimension. Keep model-specific
        # collections so a change never silently mixes representations.
        start=text.index('[embedding]');end=text.index('[embedding.api]')
        block=text[start:end]
        if re.search(r'^api_dimensions\s*=',block,re.M):
            block=re.sub(r'^api_dimensions\s*=.*$','api_dimensions = 1024',block,flags=re.M)
        else:block+='api_dimensions = 1024\n'
        text=text[:start]+block+text[end:]
    tomllib.loads(text)
    return text

def main():
    parser=argparse.ArgumentParser(description='Choix des modèles mémoire sans effacer les souvenirs')
    parser.add_argument('--embedding',choices=EMBEDDINGS)
    parser.add_argument('--reranker',choices=RERANKERS)
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('Exécuter avec sudo pour lire/modifier la configuration système.')
    with open('/run/lock/ops-execution-mode.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=memory_capabilities.CONFIG
        old=path.read_text();text=render(old,args.embedding,args.reranker)
        if text!=old:
            from ops_memory.config import MemoryConfig
            archive=Path('/var/lib/ops-native-archives')/('memory-model-'+str(time.time_ns()))
            archive.mkdir(mode=0o700);memory_secrets.atomic(archive/'memory.toml',old.encode())
            st=path.stat()
            try:
                memory_secrets.atomic(path,text.encode(),st.st_mode & 0o777)
                os.chown(path,st.st_uid,st.st_gid)
                MemoryConfig.from_toml(path)
                subprocess.run(['/usr/bin/systemctl','restart','ops-memory.service'],check=True,capture_output=True,timeout=45)
            except BaseException:
                memory_secrets.atomic(path,old.encode(),st.st_mode & 0o777);os.chown(path,st.st_uid,st.st_gid)
                subprocess.run(['/usr/bin/systemctl','restart','ops-memory.service'],capture_output=True,timeout=45)
                raise
            memory_secrets.atomic(archive/'receipt.json',json.dumps({'embedding':args.embedding,'reranker':args.reranker,'source_memories_preserved':True}).encode())
        config=tomllib.loads(path.read_text())
        print(json.dumps({name:{'model':config[name]['api']['model'],'enabled':config[name]['api']['enabled']} for name in ['embedding','reranker']}))

if __name__=='__main__':main()
