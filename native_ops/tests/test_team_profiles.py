from pathlib import Path
import runpy
import pytest

ROOT=Path(__file__).parents[2]
TEAM=runpy.run_path(str(ROOT/'native_ops/team_profiles.py'))

@pytest.mark.parametrize('name',list(TEAM['PROFILES']))
def test_profiles_use_native_oauth_and_fixed_mcp_identities(name):
    config,soul,description=TEAM['render'](name,'gpt-test',ROOT)
    profile,actor,account,_=TEAM['PROFILES'][name]
    assert config['model']=={'provider':'openai-codex','default':'gpt-test','openai_runtime':'auto'}
    assert not {'secrets','providers','gateway'} & config.keys()
    assert config['agent']['max_turns']==8
    assert {'terminal','file','code_execution','delegation','browser','computer_use','web','kanban'} <= set(config['agent']['disabled_toolsets'])
    broker=config['mcp_servers']['ops-broker']
    assert broker['command']=='/usr/bin/sudo'
    assert broker['args']==['-n','-u','opsbroker','-g','opsbroker','--','/usr/local/libexec/ops-broker-mcp-profile',actor]
    memory=config['mcp_servers']['ops-memory']
    assert memory['args']==['-n','-u',account,'-g',account,'--','/usr/local/libexec/ops-memory-mcp-profile',profile]
    assert 'auth.json' not in str(config) and 'auth.json' not in soul
    assert config['mcp_servers']['ops-broker']['sampling']=={'enabled':False}


def test_sudoers_never_grants_root_or_arbitrary_arguments():
    rules=TEAM['sudoers']()
    assert '*' not in rules and '(root)' not in rules
    assert 'SETENV' not in rules
    for profile,actor,account,_ in TEAM['PROFILES'].values():
        assert '/usr/local/libexec/ops-broker-mcp-profile '+actor in rules
        assert '/usr/local/libexec/ops-memory-mcp-profile '+profile in rules
