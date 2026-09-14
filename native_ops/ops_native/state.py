"""Connector delivery state only. Operational missions remain in the broker."""
import json
import os
import sqlite3
from pathlib import Path

class State:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs(
                source_id INTEGER PRIMARY KEY, topic TEXT NOT NULL, content TEXT NOT NULL,
                mission_id TEXT, phase TEXT NOT NULL DEFAULT 'received',
                response TEXT, delivery_id INTEGER, error TEXT);
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS approval_followups(action_id TEXT PRIMARY KEY,source_id INTEGER NOT NULL,phase TEXT NOT NULL DEFAULT 'waiting',response TEXT,delivery_id INTEGER);
        ''')
        columns={r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}
        if 'backend' not in columns:
            self.db.execute('ALTER TABLE jobs ADD COLUMN backend TEXT')
        if 'project_id' not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN project_id TEXT NOT NULL DEFAULT 'infra-shared'")
        # An interrupted generation is not automatically billed a second time.
        with self.db:
            self.db.execute("UPDATE approval_followups SET phase='needs_review' WHERE phase IN ('executing','sending')")
            self.db.execute("UPDATE jobs SET phase='needs_review', error='interrupted_generation' WHERE phase='generating'")

    def accept(self, source_id, topic, content, project_id="infra-shared"):
        from .dispatch import PROJECTS
        if project_id not in PROJECTS:raise ValueError("Unknown project")
        if type(source_id) is not int or source_id <= 0 or not 1 <= len(content) <= 4000:
            raise ValueError('invalid incoming message')
        if not isinstance(topic, str) or not 1 <= len(topic) <= 200:
            raise ValueError('invalid topic')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO jobs(source_id,topic,content,project_id) VALUES(?,?,?,?)',
                            (source_id, topic, content, project_id))

    def set(self, source_id, **fields):
        allowed = {'mission_id','phase','response','delivery_id','error','backend'}
        if not fields or set(fields) - allowed:
            raise ValueError('invalid state fields')
        with self.db:
            self.db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE source_id=?',
                            (*fields.values(), source_id))

    def get(self, source_id):
        row = self.db.execute('SELECT * FROM jobs WHERE source_id=?',(source_id,)).fetchone()
        return dict(row) if row else None

    def pending(self):
        return [dict(x) for x in self.db.execute(
            "SELECT * FROM jobs WHERE phase IN ('received','prepared','ready','sending') ORDER BY source_id LIMIT 20")]

    def meta(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',(key,json.dumps(value)))
        row = self.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()

    def watch_approval(self, source_id, action_id):
        import uuid
        action_id=str(uuid.UUID(action_id))
        with self.db:self.db.execute('INSERT OR IGNORE INTO approval_followups(action_id,source_id) VALUES(?,?)',(action_id,source_id))

    def approval_jobs(self):
        return [dict(r) for r in self.db.execute("SELECT f.*,j.mission_id,j.topic FROM approval_followups f JOIN jobs j USING(source_id) WHERE f.phase IN ('waiting','ready') ORDER BY f.rowid LIMIT 20")]

    def set_approval(self, action_id, **fields):
        if not fields or set(fields)-{'phase','response','delivery_id'}:raise ValueError('Invalid approval state')
        with self.db:self.db.execute('UPDATE approval_followups SET '+','.join(k+'=?' for k in fields)+' WHERE action_id=?',(*fields.values(),action_id))
