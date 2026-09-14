from dataclasses import replace
import pytest
from conftest import FakeQdrant, memory_payload
from test_service import search_payload
from ops_memory.config import APIProviderConfig
from ops_memory.embedding import EmbeddingResult
from ops_memory.errors import BackendUnavailableError, NotFoundError
from ops_memory.service import MemoryService

class Embedder:
    def __init__(self):self.calls=[];self.fail=False
    def embed(self,text):
        self.calls.append(text)
        if self.fail:raise BackendUnavailableError('unavailable')
        return EmbeddingResult((1.0,)+(0.0,)*63,'test-api','api',1.0)

def configured(config):
    return replace(config,embedding=replace(config.embedding,api_dimensions=64,
        api=APIProviderConfig(enabled=True,url='https://example.invalid/embeddings',model='test-api',daily_call_budget=20)))

def create(config):
    local=FakeQdrant();remote=FakeQdrant();service=MemoryService(config,qdrant=local)
    embedder=Embedder();service.api_embedding=embedder;service.semantic.embedder=embedder;service.semantic.qdrant=remote
    return service,local,remote,embedder

def test_query_never_uses_api_vector_against_hash_index(memory_config,uid):
    service,local,remote,embedder=create(configured(memory_config))
    ident=service.ingest(memory_payload(),uid)['memory']['id']
    answer=service.search(search_payload(allow_api=True,important=True),uid)
    assert answer['embedding']['provider']=='local' and not embedder.calls
    service.reindex_api('operator',uid,[ident])
    assert ident in local.points and ident in remote.points
    service.search(search_payload(allow_api=True,important=True),uid)
    assert len(embedder.calls)==2 and remote.search_calls
    assert local.points[ident][1] != remote.points[ident][1]

def test_reindex_cached_across_restart_and_qdrant_failure(memory_config,uid):
    config=configured(memory_config);service,_,remote,embedder=create(config)
    ident=service.ingest(memory_payload(),uid)['memory']['id']
    result=service.reindex_api('operator',uid,[ident]);assert result['indexed']==[ident]
    second,_,second_remote,second_embedder=create(config)
    second.reindex_api('operator',uid,[ident])
    assert not second_embedder.calls and ident in second_remote.points
    assert len(embedder.calls)==1

def test_reindex_checks_entire_batch_before_provider(memory_config,uid):
    service,_,_,embedder=create(configured(memory_config))
    ident=service.ingest(memory_payload(),uid)['memory']['id']
    with pytest.raises(NotFoundError):service.reindex_api('operator',uid,[ident,'missing'])
    assert not embedder.calls

def test_model_change_creates_new_space(memory_config):
    first,*_=create(configured(memory_config))
    config=configured(memory_config)
    second,*_=create(replace(config,embedding=replace(config.embedding,api=replace(config.embedding.api,model='other'))))
    assert first.semantic.space!=second.semantic.space

def test_api_outage_keeps_deterministic_search(memory_config,uid):
    service,_,remote,embedder=create(configured(memory_config))
    ident=service.ingest(memory_payload(),uid)['memory']['id']
    service.reindex_api('operator',uid,[ident]);embedder.fail=True
    answer=service.search(search_payload(allow_api=True,important=True),uid)
    assert answer['embedding']['provider']=='local' and answer['results']
    assert not remote.search_calls

@pytest.mark.parametrize('status', [402, 429, 503])
def test_jina_http_refusal_preserves_search_and_new_memories(memory_config, uid, monkeypatch, status):
    """Exercise real HTTP adapters with synthetic refusals, without spending credit."""
    import io
    import urllib.error
    import ops_memory.embedding as embedding_module
    import ops_memory.rerank as rerank_module
    from ops_memory.embedding import OpenAICompatibleEmbedding
    from ops_memory.rerank import APIReranker

    config = configured(memory_config)
    api = replace(config.embedding.api, protocol='jina')
    config = replace(config, embedding=replace(config.embedding, api=api),
                     reranker=replace(config.reranker, api=replace(api, url='https://example.invalid/rerank')))
    service, local, remote, _ = create(config)
    ident = service.ingest(memory_payload(), uid)['memory']['id']
    service.reindex_api('operator', uid, [ident])
    service.api_embedding = OpenAICompatibleEmbedding(api, 64)
    service.api_reranker = APIReranker(config.reranker.api)
    calls = []
    class Refusal:
        def open(self, request, **kwargs):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, status, 'synthetic refusal', {},
                                         io.BytesIO(b'synthetic private provider response'))
    for module in (embedding_module, rerank_module):
        monkeypatch.setattr(module, 'provider_credential', lambda *args: 'synthetic-token')
        monkeypatch.setattr(module.urllib.request, 'build_opener', lambda *args: Refusal())
    answer = service.search(search_payload(allow_api=True, important=True), uid)
    assert answer['embedding']['provider'] == answer['reranker']['provider'] == 'local'
    assert answer['results'][0]['id'] == ident
    assert len(calls) == 2  # one attempt per function; no retry storm
    assert 'synthetic private' not in str(answer)
    stored = service.ingest(memory_payload(content='Nouvelle décision Minecraft après panne API.'), uid)
    assert stored['stored'] and stored['memory']['id'] in local.points
    assert len(calls) == 2

def test_exhausted_budget_defers_remaining_batch_without_repeated_denials(memory_config,uid):
    from datetime import datetime,timezone
    service,_,_,embedder=create(configured(memory_config))
    ids=[service.ingest(memory_payload(content='Bounded semantic test '+str(i),source_ref='semantic-'+str(i)),uid)['memory']['id'] for i in range(3)]
    attempts=[]
    def denied(*args):attempts.append(args);return None
    service.catalog.reserve_provider_call=denied
    result=service.reindex_api('operator',uid,ids)
    assert result['pending']==ids and result['indexed']==[]
    assert len(attempts)==1 and not embedder.calls
    retry=datetime.fromtimestamp(result['retry_after'],timezone.utc)
    assert retry.hour==0 and retry.minute==0
