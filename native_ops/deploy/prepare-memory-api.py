"""Render reviewed API defaults without enabling calls or modifying the hash index."""
import json,re,tomllib
from pathlib import Path

def render(text,preset):
    current=tomllib.loads(text)
    for section in ['embedding','reranker']:
        existing=current.get(section,{}).get('api',{})
        wanted=preset[section]
        if existing.get('enabled'):
            raise ValueError('Existing enabled memory provider must be reviewed before migration')
        if existing.get('model') not in {None,'','configure-me',wanted['model']}:
            raise ValueError('Existing custom provider preserved; explicit migration required')
        marker='['+section+'.api]'
        start=text.index(marker);end=text.find('\n[',start+len(marker))
        if end<0:end=len(text)
        block=text[start:end]
        values={'protocol':json.dumps(wanted.get('protocol','openai')),'enabled':'false','url':json.dumps(wanted['url']),'model':json.dumps(wanted['model']),
            'credential_env':'""','credential_credential':json.dumps(wanted['credential_credential']),
            'daily_call_budget':str(wanted['daily_call_budget'])}
        for key,value in values.items():
            pattern=r'^'+re.escape(key)+r'\s*=.*$'
            if re.search(pattern,block,re.M):block=re.sub(pattern,key+' = '+value,block,flags=re.M)
            else:block+='\n'+key+' = '+value+'\n'
        text=text[:start]+block+text[end:]
    if 'api_dimensions' not in current.get('embedding',{}):
        text=text.replace('[embedding]\n','[embedding]\napi_dimensions = '+str(preset['embedding']['dimensions'])+'\n')
    tomllib.loads(text)
    return text
