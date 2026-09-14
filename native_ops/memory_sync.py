"""Persist scoped broker history in shared memory, without raw command outputs.

No model, remote provider, remote shell, policy promotion or message publishing.
The broker remains the source of truth. Bounded windows are reported explicitly.
"""
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from ops_native.broker import connect
from ops_memory.models import check_for_secrets
from ops_memory.protocol import socket_request

PROJECTS = ('infra-shared', 'minecraft', 'network-shared', 'monitoring-shared', 'backup-shared')
ROLES = {
    'infra-shared': ['hermes-coordinator', 'infra-shared', 'security-ops'],
    'minecraft': ['hermes-coordinator', 'minecraft-ops'],
    'network-shared': ['hermes-coordinator', 'network-shared'],
    'monitoring-shared': ['hermes-coordinator', 'monitoring-shared'],
    'backup-shared': ['hermes-coordinator', 'backup-shared'],
}
SUSPECT = re.compile(r'(?i)(password|passwd|mot de passe|api.?key|api.?token|bearer\s|private.key|AGE-SECRET-KEY|-----BEGIN|\bsk-[a-z0-9]|\bgh[pousr]_)')
ROOT = Path('/home/ops-user/ops-control-plane')
DOCUMENTS = ('docs/memoire-globale.md', 'native_ops/docs/PILOTAGE.md',
             'docs/architecture.md', 'docs/reprise-apres-redemarrage.md',
             'docs/comptes-memoire-api.md')


def documentation(root=ROOT):
    """Explicit allowlist, no recursive home scan or conversation import."""
    sources = []
    inventory = root/'inventory/ops-v1.json'
    for resource in json.loads(inventory.read_text())['resources']:
        sources.append((inventory, 'resource:'+resource['id'], 'Inventaire déclaré, à vérifier contre les observations actuelles.\n'+json.dumps(resource, ensure_ascii=False, sort_keys=True)))
    snapshot = root/'inventory/services-observed-2026-09-11.json'
    for resource, observed in json.loads(snapshot.read_text())['resources'].items():
        text = 'Services réellement observés le 11 septembre 2026 ; vérifier leur état actuel avec le broker.\n'+json.dumps(observed, ensure_ascii=False, sort_keys=True)
        for offset in range(0, len(text), 6000):
            sources.append((snapshot, 'observed:'+resource+':'+str(offset//6000), text[offset:offset+6000]))
    for name in DOCUMENTS:
        path = root/name
        if path.is_symlink() or path.stat().st_size > 131072:
            raise ValueError('Documentation outside the bounded source registry')
        text = path.read_text()
        for offset in range(0, len(text), 6000):
            sources.append((path, 'chunk:'+str(offset//6000), text[offset:offset+6000]))
    for path, source_key, text in sources:
        check_for_secrets(text, {})
        digest = hashlib.sha256(text.encode()).hexdigest()
        yield dict(content='Document de référence historique, version '+digest[:12]+'.\n'+text,
            project='infra-shared', environment='production', classification='internal',
            category='system-documentation', kind='information', tier='cold',
            allowed_roles=['hermes-coordinator','infra-shared','network-shared','monitoring-shared',
                           'backup-shared','deploy-ops','security-ops','minecraft-ops'],
            source_type='git-docs', source_uri=path.as_uri(), source_ref=digest,
            source_authority='ops-repository', reviewed=False, promote=False,
            metadata={'tags': ['historical', 'source-document', source_key]})


def payload(project, mission_id, item, item_type):
    """Copy only public broker fields; never stdout, parameters or raw requests."""
    if project not in PROJECTS:
        raise ValueError('Unknown project')
    if item_type == 'record':
        if any(str(e).startswith('zulip-source:') for e in item.get('evidence', [])):
            return None
        selected = {k: item[k] for k in ('id', 'kind', 'content', 'evidence', 'created_at') if k in item}
    elif item_type == 'action':
        selected = {k: item[k] for k in ('id', 'runbook_id', 'runbook_version', 'action_class', 'status', 'created_at', 'updated_at') if k in item}
    elif item_type == 'mission':
        if item.get('id') != mission_id or item.get('project_id') != project:
            raise ValueError('Mismatched mission state scope')
        selected = {k: item[k] for k in ('id', 'status', 'objective', 'summary', 'plan',
                    'current_step', 'remaining_steps', 'next_action', 'updated_at') if k in item}
    else:
        raise ValueError('Unknown event type')
    body = json.dumps(selected, ensure_ascii=False, sort_keys=True)
    if len(body.encode()) > 24000 or SUSPECT.search(body):
        return None
    check_for_secrets(body, {})
    digest = hashlib.sha256(body.encode()).hexdigest()
    return dict(content='Historique Ops, à vérifier contre le broker. Mission '+mission_id+'\n'+body,
        project=project, environment='production', classification='restricted',
        category='operations-history', kind='information', tier='cold',
        allowed_roles=ROLES[project], source_type='agent-observation',
        source_uri='ops-broker://missions/'+mission_id+'/'+item_type+'/'+str(uuid.UUID(item['id'])),
        source_ref=digest, source_authority='ops-broker', reviewed=False, promote=False,
        metadata={'mission_id': mission_id, 'tags': ['historical', 'automatic-capture']})


def store(value):
    response = socket_request(Path('/run/ops-memory/ops-memory.sock'),
        {'version': 1, 'request_id': str(uuid.uuid4()), 'actor': 'codex-supervised',
         'operation': 'ingest', 'payload': value})
    if not response.get('ok') or not response.get('result', {}).get('stored'):
        raise RuntimeError('Memory did not confirm persistence')
    return response['result']['memory']['id']


def capture(db, value, ingest):
    key = value['source_uri']+':'+value['source_ref']
    existing = db.execute('SELECT memory_id FROM captured WHERE source=?', (key,)).fetchone()
    if existing and db.execute('SELECT 1 FROM shared_projection WHERE source=?', (key,)).fetchone():
        return False
    if existing:
        # Owner requested a common memory for the scoped Hermes team too.
        # Replace only this collector's earlier projection, keeping its history.
        value = {**value, 'supersedes_id': existing[0]}
    memory_id = ingest(value)
    db.execute('INSERT OR REPLACE INTO captured VALUES (?,?)', (key, memory_id))
    db.execute('INSERT OR REPLACE INTO shared_projection VALUES (?)', (key,))
    db.commit()
    return True


def capture_mission(db, value, ingest):
    """Keep the current checkpoint searchable while preserving former versions."""
    db.execute('CREATE TABLE IF NOT EXISTS mission_states (source TEXT PRIMARY KEY, digest TEXT, memory_id TEXT)')
    source, digest = value['source_uri'], value['source_ref']
    previous = db.execute('SELECT digest, memory_id FROM mission_states WHERE source=?', (source,)).fetchone()
    if previous and previous[0] == digest:
        return False
    if previous:
        value = {**value, 'supersedes_id': previous[1],
                 'source_ref': hashlib.sha256((digest+previous[1]).encode()).hexdigest()}
    memory_id = ingest(value)
    db.execute('INSERT OR REPLACE INTO captured VALUES (?,?)', (source+':'+value['source_ref'], memory_id))
    db.execute('INSERT OR REPLACE INTO mission_states VALUES (?,?,?)', (source, digest, memory_id))
    db.commit()
    return True


def index_pending(db):
    """Bounded round-robin; provider-side failures never block local capture.

    The service checks scopes and its daily budget before every call. Its durable
    vector cache avoids rebilling unchanged documents on the next pass.
    """
    db.execute('CREATE TABLE IF NOT EXISTS index_backoff (id INTEGER PRIMARY KEY, retry_after REAL)')
    pause=db.execute('SELECT retry_after FROM index_backoff WHERE id=1').fetchone()
    if pause and time.time()<pause[0]:return 'budget_wait'
    db.execute('CREATE TABLE IF NOT EXISTS index_cursor (id INTEGER PRIMARY KEY, position INTEGER)')
    cursor = db.execute('SELECT position FROM index_cursor WHERE id=1').fetchone()
    position = cursor[0] if cursor else 0
    rows = db.execute('SELECT rowid, memory_id FROM captured WHERE rowid>? ORDER BY rowid LIMIT 20', (position,)).fetchall()
    if not rows:
        rows = db.execute('SELECT rowid, memory_id FROM captured ORDER BY rowid LIMIT 20').fetchall()
    if not rows:
        return 'empty'
    ids = []
    for _, ident in rows:
        response = socket_request(Path('/run/ops-memory/ops-memory.sock'),
            {'version':1, 'request_id':str(uuid.uuid4()), 'actor':'codex-supervised',
             'operation':'get', 'payload':{'id':ident}})
        record = response.get('result', {})
        if response.get('ok') and not any(record.get(k) for k in
                ('superseded_by_id', 'invalidated_at', 'stale', 'valid_until')):
            ids.append(ident)
    if ids:
        response = socket_request(Path('/run/ops-memory/ops-memory.sock'),
            {'version':1, 'request_id':str(uuid.uuid4()), 'actor':'codex-supervised',
             'operation':'reindex_api', 'payload':{'ids':list(dict.fromkeys(ids))}}, timeout=180)
        if not response.get('ok'):
            return 'unavailable'
        pending = bool(response['result'].get('pending'))
        retry=response['result'].get('retry_after')
        if isinstance(retry,(int,float)) and time.time()<retry<time.time()+86401:
            db.execute('INSERT OR REPLACE INTO index_backoff VALUES (1,?)',(retry,));db.commit()
    else:
        pending = False
    db.execute('INSERT OR REPLACE INTO index_cursor VALUES (1,?)', (rows[-1][0],))
    db.commit()
    return 'pending' if pending else 'indexed' if ids else 'no_current_records'


async def sync(broker, db, ingest=store):
    report = {'checked_at': time.time(), 'stored': 0, 'skipped': 0, 'bounded_windows': [], 'failed': 0}
    db.execute('CREATE TABLE IF NOT EXISTS history_cursor (project TEXT PRIMARY KEY, position INTEGER)')
    db.execute('CREATE TABLE IF NOT EXISTS mission_cursor (project TEXT PRIMARY KEY, position INTEGER)')
    db.execute('CREATE TABLE IF NOT EXISTS shared_projection (source TEXT PRIMARY KEY)')
    # Once, replay retained source records to migrate our initial private projection.
    db.execute('CREATE TABLE IF NOT EXISTS sync_version (version INTEGER PRIMARY KEY)')
    if not db.execute('SELECT 1 FROM sync_version WHERE version=2').fetchone():
        db.execute('DELETE FROM history_cursor')
        db.execute('INSERT INTO sync_version VALUES (2)')
        db.commit()
    for project in PROJECTS:
        cursor = db.execute('SELECT position FROM history_cursor WHERE project=?', (project,)).fetchone()
        position = cursor[0] if cursor else 0
        for _ in range(10):
            page = await broker.call('list_memory_history', project_id=project, after=position, limit=50)
            failed = False
            for record in page['records']:
                if record['project_id'] != project:
                    raise ValueError('Mismatched history scope')
                db.execute('INSERT OR IGNORE INTO missions VALUES (?,?)', (record['mission_id'], project))
                try:
                    value = payload(project, record['mission_id'], record, 'record')
                    if value is None:
                        report['skipped'] += 1
                        continue
                    if capture(db, value, ingest):
                        report['stored'] += 1
                except Exception:
                    failed = True
                    report['failed'] += 1
            if failed:
                break
            position = page['next_cursor']
            db.execute('INSERT OR REPLACE INTO history_cursor VALUES (?,?)', (project, position))
            db.commit()
            if len(page['records']) < 50:
                break
        else:
            report['bounded_windows'].append(project+':history_backlog')
        saved = db.execute('SELECT position FROM mission_cursor WHERE project=?', (project,)).fetchone()
        position = saved[0] if saved else 0
        for _ in range(10):
            page = await broker.call('list_memory_missions', project_id=project, after=position, limit=50)
            for m in page['missions']:
                if m['project_id'] != project:
                    raise ValueError('Mismatched broker scope')
                db.execute('INSERT OR IGNORE INTO missions VALUES (?,?)', (m['id'], project))
            position = page['next_cursor']
            db.execute('INSERT OR REPLACE INTO mission_cursor VALUES (?,?)', (project, position))
            db.commit()
            if len(page['missions']) < 50:
                break
        else:
            report['bounded_windows'].append(project+':mission_backlog')
    # Remember discovered missions after closure, so their final event is captured.
    db.execute('CREATE TABLE IF NOT EXISTS action_pages (mission_id TEXT PRIMARY KEY, position INTEGER)')
    for mission_id, project in db.execute('SELECT id, project FROM missions').fetchall():
        try:
            state = await broker.call('get_mission_state', mission_id=mission_id)
            value = payload(project, mission_id, state, 'mission')
            if value is None:
                report['skipped'] += 1
            elif capture_mission(db, value, ingest):
                report['stored'] += 1
        except Exception:
            report['failed'] += 1
        saved = db.execute('SELECT position FROM action_pages WHERE mission_id=?', (mission_id,)).fetchone()
        offset = saved[0] if saved else 0
        for _ in range(10):
            items = await broker.call('list_actions_for_mission', mission_id=mission_id, limit=50, offset=offset)
            failed = False
            for item in items:
                try:
                    value = payload(project, mission_id, item, 'action')
                    if value is None:
                        report['skipped'] += 1
                        continue
                    if capture(db, value, ingest):
                        report['stored'] += 1
                except Exception:
                    # Do not log rejected content or exception bodies.
                    report['failed'] += 1
                    failed = True
            if failed:
                break
            offset = offset+len(items) if len(items) == 50 else 0
            db.execute('INSERT OR REPLACE INTO action_pages VALUES (?,?)', (mission_id, offset))
            db.commit()
            if len(items) < 50:
                break
        else:
            report['bounded_windows'].append(mission_id+':action_backlog')
    report['status'] = 'partial' if report['failed'] or report['bounded_windows'] else 'ok'
    return report


async def main():
    os.umask(0o077)
    folder = Path.home()/'.local/state/ops-memory-sync'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder/'sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect(folder/'state.sqlite3') as db:
            db.execute('CREATE TABLE IF NOT EXISTS missions (id TEXT PRIMARY KEY, project TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS captured (source TEXT PRIMARY KEY, memory_id TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS documents (source TEXT PRIMARY KEY, memory_id TEXT NOT NULL)')
            for value in documentation():
                key = value['source_uri']+':'+value['source_ref']
                logical_key = value['source_uri']+':'+value['metadata']['tags'][-1]
                if not db.execute('SELECT 1 FROM captured WHERE source=?', (key,)).fetchone():
                    previous = db.execute('SELECT memory_id FROM documents WHERE source=?', (logical_key,)).fetchone()
                    if previous:
                        value['supersedes_id'] = previous[0]
                    memory_id = store(value)
                    db.execute('INSERT INTO captured VALUES (?,?)', (key, memory_id))
                else:
                    memory_id = db.execute('SELECT memory_id FROM captured WHERE source=?', (key,)).fetchone()[0]
                db.execute('INSERT OR REPLACE INTO documents VALUES (?,?)', (logical_key, memory_id))
                db.commit()
            async with connect('/usr/bin/sudo', ['-n','-u','opsbroker','-g','opsbroker','--','/usr/local/libexec/ops-broker/ops-broker-mcp-codex'], 'codex-supervised') as broker:
                report = await sync(broker, db)
            try:
                report['semantic_api'] = index_pending(db)
            except Exception:
                report['semantic_api'] = 'unavailable'
        temporary = folder/'latest.next'
        temporary.write_text(json.dumps(report, indent=2)+'\n')
        temporary.replace(folder/'latest.json')
        print(json.dumps(report))
        return 0 if not report['failed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception:
        raise SystemExit('Memory synchronization unavailable; source history preserved') from None
