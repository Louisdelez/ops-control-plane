from __future__ import annotations

import math

from ops_memory.embedding import LocalHashEmbedding
from ops_memory.rerank import LocalReranker


def test_local_embedding_is_deterministic_normalized_and_cpu_only() -> None:
    embedder = LocalHashEmbedding(64, "test-model")
    first = embedder.embed("Minecraft survival proxy")
    second = embedder.embed("Minecraft survival proxy")
    other = embedder.embed("Sauvegarde PostgreSQL")

    assert first == second
    assert first.vector != other.vector
    assert math.isclose(sum(value * value for value in first.vector), 1.0, rel_tol=1e-9)
    assert first.provider == "local"


def test_local_reranker_prefers_overlap_and_authority() -> None:
    candidates = [
        {
            "id": "b",
            "content": "Ancien incident proxy sans rapport",
            "semantic_score": 0.1,
            "authority_status": "historical",
            "importance": 0.4,
            "created_at": "2025-01-01T00:00:00+00:00",
        },
        {
            "id": "a",
            "content": "Version Minecraft survival actuellement déployée",
            "semantic_score": 0.8,
            "authority_status": "authoritative",
            "importance": 0.9,
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    ]
    ranked = LocalReranker("local").rerank("version Minecraft survival", candidates, 2)
    assert [item.record["id"] for item in ranked] == ["a", "b"]
    assert ranked[0].score > ranked[1].score

def test_reranker_rejects_bad_indices_and_nonfinite_scores(monkeypatch):
    import json
    import pytest
    from ops_memory.config import APIProviderConfig
    from ops_memory.rerank import APIReranker
    from ops_memory.errors import BackendUnavailableError
    import ops_memory.rerank as module
    monkeypatch.setattr(module,'provider_credential',lambda *a:'test-token')
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,*a):return json.dumps({'results':results}).encode()
    class Opener:
        def open(self,*a,**kw):return Response()
    monkeypatch.setattr(module.urllib.request,'build_opener',lambda *a:Opener())
    api=APIReranker(APIProviderConfig(enabled=True,url='https://example.invalid/rerank',model='test'))
    for results in [[{'index':-1,'relevance_score':.5}], [{'index':True,'relevance_score':.5}],
                    [{'index':0,'relevance_score':float('nan')}],
                    [{'index':0,'relevance_score':.5},{'index':0,'relevance_score':.4}]]:
        with pytest.raises(BackendUnavailableError):api.rerank('query',[{'content':'one'},{'content':'two'}],2)

def test_voyage_request_distinguishes_queries_documents_and_pins_dimensions(monkeypatch):
    import json
    from ops_memory.config import APIProviderConfig
    from ops_memory.embedding import OpenAICompatibleEmbedding
    import ops_memory.embedding as module
    calls=[]
    monkeypatch.setattr(module,'provider_credential',lambda *a:'synthetic-token')
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,*a):return json.dumps({'data':[{'embedding':[0.0]*1024}]}).encode()
    class Opener:
        def open(self,request,**kw):calls.append(json.loads(request.data));return Response()
    monkeypatch.setattr(module.urllib.request,'build_opener',lambda *a:Opener())
    api=OpenAICompatibleEmbedding(APIProviderConfig(enabled=True,protocol='voyage',url='https://api.voyageai.com/v1/embeddings',model='voyage-4-lite'),1024)
    api.embed('Document mémoire');api.embed('Question mémoire',input_type='query')
    assert [c['input_type'] for c in calls]==['document','query']
    assert all(c['output_dimension']==1024 and c['truncation'] is False and isinstance(c['input'],list) for c in calls)


def test_cohere_v2_maps_result_indices_to_original_records(monkeypatch):
    import json
    import ops_memory.rerank as module
    from ops_memory.config import APIProviderConfig
    monkeypatch.setattr(module,'provider_credential',lambda *a:'synthetic-token')
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,*a):return b'{"results":[{"index":1,"relevance_score":0.9}],"meta":{"billed_units":{"search_units":1}}}'
    class Opener:
        def open(self,request,**kw):
            assert request.full_url=='https://api.cohere.com/v2/rerank'
            assert json.loads(request.data)=={'model':'rerank-v4.0-fast','query':'question','documents':['first','second'],'top_n':1}
            return Response()
    monkeypatch.setattr(module.urllib.request,'build_opener',lambda *a:Opener())
    candidates=[{'id':'1','content':'first'},{'id':'2','content':'second'}]
    api=module.APIReranker(APIProviderConfig(enabled=True,protocol='cohere',url='https://api.cohere.com/v2/rerank',model='rerank-v4.0-fast'))
    assert api.rerank('question',candidates,1)[0].record==candidates[1]


def test_jina_request_distinguishes_queries_documents_and_pins_dimensions(monkeypatch):
    import json
    from ops_memory.config import APIProviderConfig
    from ops_memory.embedding import OpenAICompatibleEmbedding
    import ops_memory.embedding as module
    calls=[]
    monkeypatch.setattr(module,'provider_credential',lambda *a:'synthetic-token')
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,*a):return json.dumps({'data':[{'embedding':[0.0]*1024}]}).encode()
    class Opener:
        def open(self,request,**kw):calls.append(json.loads(request.data));return Response()
    monkeypatch.setattr(module.urllib.request,'build_opener',lambda *a:Opener())
    api=OpenAICompatibleEmbedding(APIProviderConfig(enabled=True,protocol='jina',url='https://api.jina.ai/v1/embeddings',model='jina-embeddings-v5-text-small'),1024)
    api.embed('Document mémoire');api.embed('Question mémoire',input_type='query')
    assert [c['task'] for c in calls]==['retrieval.passage','retrieval.query']
    assert all(c['dimensions']==1024 and c['truncate'] is False and isinstance(c['input'],list) for c in calls)


def test_jina_maps_result_indices_to_original_records(monkeypatch):
    import json
    import ops_memory.rerank as module
    from ops_memory.config import APIProviderConfig
    monkeypatch.setattr(module,'provider_credential',lambda *a:'synthetic-token')
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,*a):return b'{"results":[{"index":1,"relevance_score":0.9}],"meta":{"billed_units":{"search_units":1}}}'
    class Opener:
        def open(self,request,**kw):
            assert request.full_url=='https://api.jina.ai/v1/rerank'
            assert json.loads(request.data)=={'model':'jina-reranker-v3.5','query':'question','documents':['first','second'],'top_n':1}
            return Response()
    monkeypatch.setattr(module.urllib.request,'build_opener',lambda *a:Opener())
    candidates=[{'id':'1','content':'first'},{'id':'2','content':'second'}]
    api=module.APIReranker(APIProviderConfig(enabled=True,protocol='jina',url='https://api.jina.ai/v1/rerank',model='jina-reranker-v3.5'))
    assert api.rerank('question',candidates,1)[0].record==candidates[1]
