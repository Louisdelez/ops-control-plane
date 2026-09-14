from pathlib import Path
import json,runpy,tomllib
import pytest
from ops_native.memory_models import render
from ops_native.memory_capabilities import desired
ROOT=Path(__file__).parents[2]

def test_switch_preserves_memory_and_scope_and_other_provider():
    old=(ROOT/'memory/config/memory.toml').read_text()
    prepared=render(old,'voyage-4-lite','rerank-v4.0-fast',True)
    upgraded=render(prepared,embedding='voyage-4-large')
    a,b=map(tomllib.loads,[prepared,upgraded])
    assert a['actors']==b['actors'] and a['qdrant']==b['qdrant']
    assert a['reranker']==b['reranker']
    assert b['embedding']['api']['model']=='voyage-4-large'
    assert render(upgraded,embedding='voyage-4-lite')==prepared
    with pytest.raises(ValueError):render(prepared,embedding='arbitrary-model')

def test_separate_keys_cannot_activate_wrong_provider(tmp_path):
    text=render((ROOT/'memory/config/memory.toml').read_text(),'voyage-4-lite','rerank-v4.0-fast',True)
    for section in ['embedding','reranker']:(tmp_path/(section+'-api-token')).write_bytes(b'fixture')
    updated,_=desired(text,tmp_path,{'embedding':True,'reranker':False})
    config=tomllib.loads(updated)
    assert config['embedding']['api']['enabled']
    assert not config['reranker']['api']['enabled']


def test_jina_migration_requires_explicit_migration_and_preserves_other_state():
    old=render((ROOT/'memory/config/memory.toml').read_text(),'voyage-4-lite','rerank-v4.0-fast',True)
    with pytest.raises(ValueError):render(old,'jina-embeddings-v5-text-small')
    migrated=render(old,'jina-embeddings-v5-text-small','jina-reranker-v3.5',True)
    a,b=map(tomllib.loads,[old,migrated])
    assert a['actors']==b['actors'] and a['qdrant']==b['qdrant']
    assert b['embedding']['api']['protocol']=='jina'
    assert b['reranker']['api']['protocol']=='jina'
    assert not b['embedding']['api']['enabled'] and not b['reranker']['api']['enabled']
