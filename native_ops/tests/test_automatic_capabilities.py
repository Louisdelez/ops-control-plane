import json,runpy
from pathlib import Path

def helper(tmp_path):
    values=runpy.run_path(str(Path(__file__).parents[1]/'deploy/ops-execution-mode'))
    function=values['automatic'];env=function.__globals__
    mode=tmp_path/'mode';config=tmp_path/'config';config.write_text('{"generation_enabled":false}')
    keys=(tmp_path/'qwen',);calls=[]
    def run(*args):calls.append(args);return True
    from types import SimpleNamespace
    env.update(MODE=mode,CONFIG=config,KEYS=keys,ACCOUNTS=tmp_path/'accounts',run=run,
        subprocess=SimpleNamespace(run=lambda *a,**k:SimpleNamespace(returncode=0,stdout=b'{"codex":true,"claude":false}')))
    return function,mode,config,keys,calls

def test_missing_keys_keeps_cli_and_does_not_start_provider(tmp_path):
    fn,mode,config,keys,calls=helper(tmp_path);fn()
    assert json.loads(mode.read_text())['mode']=='cli'
    assert ('enable','--now','ops-native-model.service') not in calls
    assert not json.loads(config.read_text())['generation_enabled']

def test_qwen_key_alone_enables_api_once_and_keeps_cli(tmp_path):
    fn,mode,config,keys,calls=helper(tmp_path)
    for key in keys:key.write_text('fixture-token')
    fn();fn()
    assert json.loads(mode.read_text())=={'mode':'hybrid','selection':'automatic'}
    assert json.loads(config.read_text())['generation_enabled']
    assert calls.count(('restart','ops-native.service'))==1

def test_missing_key_after_activation_disables_generation(tmp_path):
    fn,mode,config,keys,calls=helper(tmp_path)
    for key in keys:key.write_text('fixture-token')
    fn();keys[0].unlink();fn()
    assert json.loads(mode.read_text())['mode']=='cli'
    assert not json.loads(config.read_text())['generation_enabled']
    assert calls[-1]==('stop','ops-native-model.service')

def test_memory_defaults_preserve_hash_index_and_do_not_activate():
    import tomllib
    root=Path(__file__).parents[2]
    render=runpy.run_path(str(root/'native_ops/deploy/prepare-memory-api.py'))['render']
    text=(root/'memory/config/memory.toml').read_text()
    preset=json.loads((root/'memory/config/providers-api.json').read_text())
    result=tomllib.loads(render(text,preset));old=tomllib.loads(text)
    assert result['qdrant']==old['qdrant']
    assert result['embedding']['dimensions']==old['embedding']['dimensions']
    assert result['embedding']['api_dimensions']==1024
    assert not result['embedding']['api']['enabled']
    assert not result['reranker']['api']['enabled']
    assert result['actors']==old['actors']

def test_memory_activation_uses_presence_only_and_remains_independent(tmp_path):
    import tomllib
    from ops_native.memory_capabilities import desired
    root=Path(__file__).parents[2]
    render=runpy.run_path(str(root/'native_ops/deploy/prepare-memory-api.py'))['render']
    text=render((root/'memory/config/memory.toml').read_text(),json.loads((root/'memory/config/providers-api.json').read_text()))
    same,changed=desired(text,tmp_path);assert not changed and same==text
    (tmp_path/'embedding-api-token').write_bytes(b'encrypted-fixture')
    updated,changed=desired(text,tmp_path);assert changed
    data=tomllib.loads(updated)
    assert data['embedding']['api']['enabled']
    assert not data['reranker']['api']['enabled']
    (tmp_path/'embedding-api-token').unlink()
    restored,changed=desired(updated,tmp_path);assert changed
    assert not tomllib.loads(restored)['embedding']['api']['enabled']
