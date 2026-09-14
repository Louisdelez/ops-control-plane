"""Separate, rebuildable API vector space; never mix model embeddings."""
import hashlib
import json
import threading
from datetime import datetime, timezone, timedelta
from dataclasses import replace
from .qdrant import QdrantClient
from .errors import BackendUnavailableError, ValidationError

class SemanticIndex:
    def __init__(self, config, catalog, embedder):
        self.catalog, self.embedder, self.api = catalog, embedder, config.embedding.api
        self.space = hashlib.sha256(json.dumps([self.api.url, self.api.model,
            config.embedding.api_dimensions, self.api.protocol], separators=(',', ':')).encode()).hexdigest()[:24]
        self.qdrant = QdrantClient(replace(config.qdrant,
            collection=config.qdrant.collection+'_api_'+self.space), config.embedding.api_dimensions)
        self.lock = threading.Lock()
        with catalog.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS api_vectors (space TEXT NOT NULL, memory_id TEXT NOT NULL, content_hash TEXT NOT NULL, vector_json TEXT NOT NULL, PRIMARY KEY(space,memory_id))')
            db.commit()

    def reindex(self, actor, records):
        if not self.api.enabled:
            raise BackendUnavailableError('embedding API is disabled')
        result={'indexed':[], 'pending':[], 'space':self.space}
        # Serialize reserve / cache / upsert so a concurrent retry cannot double bill.
        with self.lock:
            self.qdrant.ensure_collection()
            for position, record in enumerate(records):
                digest=hashlib.sha256(record['content'].encode()).hexdigest()
                with self.catalog.connect() as db:
                    cached=db.execute('SELECT vector_json FROM api_vectors WHERE space=? AND memory_id=? AND content_hash=?',
                        (self.space,record['id'],digest)).fetchone()
                if cached:
                    vector=tuple(json.loads(cached['vector_json']))
                else:
                    call_id=self.catalog.reserve_provider_call(actor,'reindex','embedding-api',self.api.model,self.api.daily_call_budget)
                    if call_id is None:
                        result['pending'].extend(r['id'] for r in records[position:])
                        result['retry_after']=(datetime.now(timezone.utc)+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
                        break
                    try:
                        vector=self.embedder.embed(record['content']).vector
                    except (BackendUnavailableError,ValidationError):
                        self.catalog.complete_provider_call(call_id,'failed')
                        result['pending'].append(record['id']);continue
                    self.catalog.complete_provider_call(call_id,'success')
                    with self.catalog.connect() as db:
                        db.execute('INSERT OR REPLACE INTO api_vectors VALUES (?,?,?,?)',
                            (self.space,record['id'],digest,json.dumps(vector)))
                        db.commit()
                try:
                    self.qdrant.upsert(record,vector)
                except BackendUnavailableError:
                    result['pending'].append(record['id'])
                else:
                    result['indexed'].append(record['id'])
        return result

    def has_records(self):
        with self.catalog.connect() as db:
            return db.execute('SELECT 1 FROM api_vectors WHERE space=? LIMIT 1',(self.space,)).fetchone() is not None
