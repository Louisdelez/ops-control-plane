"""One live Hermes/DeepSeek continuity check with only read tools exposed."""
import asyncio
import json
import os
from pathlib import Path
import time
import yaml

from ops_native.mission_tools import invoke
from ops_native.worker import Hermes, parse_answer

MID='11111111-1111-4111-8111-111111111111'


async def main():
    os.umask(0o077)
    os.environ['OPS_NATIVE_MISSION_ID']=MID
    folder=Path('/var/lib/hermes/native-ops/acceptance')
    folder.mkdir(mode=0o700,exist_ok=True)
    home=folder/('memory-'+str(time.time_ns()));home.mkdir(mode=0o700)
    config=yaml.safe_load(Path('/var/lib/hermes/native-ops/hermes/config.yaml').read_text())
    config['mcp_servers']['ops-native']['tools']['include']=['get_mission_operations','search_mission_memory']
    (home/'config.yaml').write_text(yaml.safe_dump(config,sort_keys=False))
    (home/'SOUL.md').write_text('Recette de continuité en lecture seule. Ne demande aucune action et ne publie aucun message.\n')
    context=await invoke('context')
    if not context.get('ok'):raise ValueError('Scoped context unavailable')
    expected=context['data']['mission']['current_step']
    hermes=Hermes(['/var/lib/hermes/hermes-agent/venv/bin/python','-m','hermes_cli.main'],str(home),timeout=110)
    raw=await hermes.explain(MID,
        'Recette de reprise : lis get_mission_operations, puis consulte search_mission_memory sur la continuité de cette mission. '
        'Dans le champ message de ta réponse JSON, recopie exactement la valeur current_step du contexte de mission, '
        'puis indique un travail restant. Aucune action à demander, aucun message à publier.')
    try:answer=parse_answer(raw)
    except Exception:
        print(json.dumps({'stage':'parse_final','length':len(raw),'lines':len(raw.splitlines()),'starts_json':raw.startswith('{'),'ends_json':raw.endswith('}')}))
        raise
    result={'checked_at':time.time(),'status':answer['status'],'current_step_matches':expected in answer['message'],
            'only_read_tools_exposed':True,'published_message':False,'home':str(home),
            'provider':'DeepSeek through installed budget facade','mission_id':MID}
    (folder/'memory-result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
    return 0 if result['current_step_matches'] else 2


if __name__=='__main__':
    try:raise SystemExit(asyncio.run(main()))
    except Exception as error:
        safe={'invalid structured answer','output_limit','invalid final output','Hermes failed; no automatic paid retry','Scoped context unavailable'}
        print(json.dumps({'exception_type':type(error).__name__,'category':str(error) if str(error) in safe else 'verification_failed'}))
        raise SystemExit('Hermes continuity check failed; no automatic paid retry') from None
