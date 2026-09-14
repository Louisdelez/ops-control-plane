import copy
from pathlib import Path
import runpy
import pytest

@pytest.fixture
def worker():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / 'deploy/control-plane/bin/control-plane-deployment-worker'))

@pytest.fixture
def baseline():
    return {'nftables': [{'metainfo': {'version': '1.1.6'}}, {'table': {'family': 'inet', 'name': 'firewalld', 'handle': 4}},
                        {'chain': {'family': 'inet', 'table': 'firewalld', 'name': 'input', 'handle': 8}}]}

def test_exact_empty_tables_cleanup_and_replay(worker, baseline):
    expected = worker['nft_document_sha256'](baseline)
    current = copy.deepcopy(baseline)
    tables = [{'family': 'ip', 'name': name} for name in ('filter', 'nat', 'raw')]
    current['nftables'][1:1] = [{'table': {**table, 'handle': i + 20}} for i, table in enumerate(tables)]
    assert worker['empty_docker_nft_cleanup'](current, expected) == tables
    assert worker['empty_docker_nft_cleanup'](baseline, expected) == []

@pytest.mark.parametrize('change', ['nonempty', 'other_table', 'rule_drift', 'owned'])
def test_refuses_any_unproved_cleanup(worker, baseline, change):
    expected = worker['nft_document_sha256'](baseline)
    current = copy.deepcopy(baseline)
    table = {'family': 'ip', 'name': 'filter'}
    current['nftables'].append({'table': table})
    if change == 'nonempty':
        current['nftables'].append({'chain': {'family': 'ip', 'table': 'filter', 'name': 'INPUT'}})
    elif change == 'other_table':
        table['name'] = 'user_table'
    elif change == 'rule_drift':
        current['nftables'][2]['chain']['name'] = 'changed'
    else:
        table['flags'] = ['owner', 'persist']
    with pytest.raises(worker['DeploymentError']):
        worker['empty_docker_nft_cleanup'](current, expected)

def test_runtime_cleanup_checks_services_and_verifies_after(worker, baseline, monkeypatch):
    expected = worker['nft_document_sha256'](baseline)
    current = copy.deepcopy(baseline)
    current['nftables'].append({'table': {'family': 'ip', 'name': 'filter'}})
    g = worker['restore_empty_docker_nft_tables'].__globals__
    monkeypatch.setitem(g, 'nft_ruleset_document', lambda: current)
    monkeypatch.setitem(g, 'docker_service_snapshot', lambda: {'docker': {'active': True}})
    calls = []
    monkeypatch.setitem(g, 'run_command', lambda *a, **kw: calls.append(a))
    with pytest.raises(worker['DeploymentError'], match='must be stopped'):
        worker['restore_empty_docker_nft_tables'](expected)
    assert not calls
    monkeypatch.setitem(g, 'docker_service_snapshot', lambda: {'docker': {'active': False}})
    with pytest.raises(worker['DeploymentError'], match='after empty table cleanup'):
        worker['restore_empty_docker_nft_tables'](expected)
    assert calls[0][1] == ['/usr/sbin/nft', 'delete table ip filter']
