"""Durable queue cursor and event-id idempotence state."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import stat
import time

from .errors import ConfigurationError


SCHEMA = """
CREATE TABLE IF NOT EXISTS queues (
    realm_fingerprint TEXT PRIMARY KEY,
    queue_id TEXT NOT NULL,
    last_event_id INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_events (
    realm_fingerprint TEXT NOT NULL,
    queue_id TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    processed_at INTEGER NOT NULL,
    PRIMARY KEY (realm_fingerprint, queue_id, event_id)
);

CREATE TABLE IF NOT EXISTS publications (
    kind TEXT NOT NULL CHECK (kind IN ('approval', 'alert_firing', 'alert_resolved', 'daily')),
    publication_key TEXT NOT NULL,
    marker TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('intent', 'published')),
    message_id INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (kind, publication_key),
    CHECK (
        (status = 'intent' AND message_id IS NULL)
        OR (status = 'published' AND message_id > 0)
    )
);

CREATE TABLE IF NOT EXISTS alert_state (
    fingerprint TEXT PRIMARY KEY,
    episode_key TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    display_name TEXT NOT NULL,
    severity TEXT NOT NULL,
    project TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mobile_publications (
    publication_key TEXT PRIMARY KEY,
    marker TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('intent', 'published')),
    message_id INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CHECK (
        (status = 'intent' AND message_id IS NULL)
        OR (status = 'published' AND message_id > 0)
    )
);
"""


@dataclass(frozen=True, slots=True)
class StoredCursor:
    queue_id: str
    last_event_id: int


@dataclass(frozen=True, slots=True)
class Publication:
    kind: str
    publication_key: str
    marker: str
    status: str
    message_id: int | None


@dataclass(frozen=True, slots=True)
class StoredAlert:
    fingerprint: str
    episode_key: str
    active: bool
    display_name: str
    severity: str
    project: str


class EventState:
    """Small SQLite store; no credentials or full message bodies are persisted."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._prepare_path()
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _prepare_path(self) -> None:
        if not self.path.parent.exists():
            self.path.parent.mkdir(mode=0o700, parents=True)
        if self.path.exists() or self.path.is_symlink():
            metadata = self.path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ConfigurationError("ZULIP_BRIDGE_STATE must be a regular, non-symlink file")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ConfigurationError("ZULIP_BRIDGE_STATE must not be accessible by group or others")
            return
        descriptor = os.open(
            self.path,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def load_cursor(self, realm_fingerprint: str) -> StoredCursor | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT queue_id, last_event_id FROM queues WHERE realm_fingerprint = ?",
                (realm_fingerprint,),
            ).fetchone()
        if row is None:
            return None
        return StoredCursor(queue_id=str(row[0]), last_event_id=int(row[1]))

    def save_cursor(
        self, realm_fingerprint: str, queue_id: str, last_event_id: int
    ) -> StoredCursor:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO queues(realm_fingerprint, queue_id, last_event_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(realm_fingerprint) DO UPDATE SET
                    queue_id = excluded.queue_id,
                    last_event_id = excluded.last_event_id,
                    updated_at = excluded.updated_at
                """,
                (realm_fingerprint, queue_id, last_event_id, now),
            )
        return StoredCursor(queue_id=queue_id, last_event_id=last_event_id)

    def clear_cursor(self, realm_fingerprint: str, queue_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM queues WHERE realm_fingerprint = ? AND queue_id = ?",
                (realm_fingerprint, queue_id),
            )

    def is_processed(self, realm_fingerprint: str, queue_id: str, event_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM processed_events
                WHERE realm_fingerprint = ? AND queue_id = ? AND event_id = ?
                """,
                (realm_fingerprint, queue_id, event_id),
            ).fetchone()
        return row is not None

    def commit_event(
        self,
        *,
        realm_fingerprint: str,
        queue_id: str,
        event_id: int,
        outcome: str,
    ) -> StoredCursor:
        """Atomically remember an event and advance only its matching queue."""

        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO processed_events(
                    realm_fingerprint, queue_id, event_id, outcome, processed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (realm_fingerprint, queue_id, event_id, outcome, now),
            )
            changed = connection.execute(
                """
                UPDATE queues
                SET last_event_id = ?, updated_at = ?
                WHERE realm_fingerprint = ? AND queue_id = ? AND last_event_id < ?
                """,
                (event_id, now, realm_fingerprint, queue_id, event_id),
            ).rowcount
            row = connection.execute(
                """
                SELECT queue_id, last_event_id FROM queues
                WHERE realm_fingerprint = ?
                """,
                (realm_fingerprint,),
            ).fetchone()
            if row is None or row[0] != queue_id:
                raise ConfigurationError("the active Zulip queue changed while committing an event")
            if changed not in {0, 1}:
                raise ConfigurationError("the Zulip event cursor update was not atomic")
        return StoredCursor(queue_id=str(row[0]), last_event_id=int(row[1]))

    def begin_publication(self, *, kind: str, publication_key: str, marker: str) -> tuple[Publication, bool]:
        """Durably record intent before a non-idempotent Zulip send.

        Returns the publication and whether this call inserted the intent.  A
        marker mismatch for an existing key is treated as state corruption,
        never as permission to publish another message.
        """

        if kind not in {"approval", "alert_firing", "alert_resolved", "daily"}:
            raise ConfigurationError("invalid publication kind")
        if not 1 <= len(publication_key) <= 300 or not 1 <= len(marker) <= 300:
            raise ConfigurationError("invalid publication identity")
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO publications(
                    kind, publication_key, marker, status, message_id, created_at, updated_at
                ) VALUES (?, ?, ?, 'intent', NULL, ?, ?)
                """,
                (kind, publication_key, marker, now, now),
            ).rowcount == 1
            row = connection.execute(
                """
                SELECT kind, publication_key, marker, status, message_id
                FROM publications WHERE kind = ? AND publication_key = ?
                """,
                (kind, publication_key),
            ).fetchone()
            if row is None or row[2] != marker:
                raise ConfigurationError("publication marker conflicts with durable state")
        return self._publication(row), inserted

    def mark_published(
        self, *, kind: str, publication_key: str, marker: str, message_id: int
    ) -> Publication:
        if type(message_id) is not int or message_id <= 0:
            raise ConfigurationError("invalid published Zulip message ID")
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE publications
                SET status = 'published', message_id = ?, updated_at = ?
                WHERE kind = ? AND publication_key = ? AND marker = ?
                  AND (status = 'intent' OR message_id = ?)
                """,
                (message_id, now, kind, publication_key, marker, message_id),
            ).rowcount
            if changed != 1:
                raise ConfigurationError("publication state changed unexpectedly")
            row = connection.execute(
                """
                SELECT kind, publication_key, marker, status, message_id
                FROM publications WHERE kind = ? AND publication_key = ?
                """,
                (kind, publication_key),
            ).fetchone()
        assert row is not None
        return self._publication(row)

    def begin_mobile_publication(
        self, *, publication_key: str, marker: str
    ) -> tuple[Publication, bool]:
        """Persist one mobile reply intent outside the legacy publication table."""

        if not 1 <= len(publication_key) <= 300 or not 1 <= len(marker) <= 300:
            raise ConfigurationError("invalid mobile publication identity")
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO mobile_publications(
                    publication_key, marker, status, message_id, created_at, updated_at
                ) VALUES (?, ?, 'intent', NULL, ?, ?)
                """,
                (publication_key, marker, now, now),
            ).rowcount == 1
            row = connection.execute(
                """
                SELECT publication_key, marker, status, message_id
                FROM mobile_publications WHERE publication_key = ?
                """,
                (publication_key,),
            ).fetchone()
            if row is None or row[1] != marker:
                raise ConfigurationError("mobile publication marker conflicts with durable state")
        assert row is not None
        return Publication("mobile", str(row[0]), str(row[1]), str(row[2]), row[3]), inserted

    def mark_mobile_published(
        self, *, publication_key: str, marker: str, message_id: int
    ) -> Publication:
        if type(message_id) is not int or message_id <= 0:
            raise ConfigurationError("invalid published Zulip message ID")
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE mobile_publications
                SET status = 'published', message_id = ?, updated_at = ?
                WHERE publication_key = ? AND marker = ?
                  AND (status = 'intent' OR message_id = ?)
                """,
                (message_id, now, publication_key, marker, message_id),
            ).rowcount
            if changed != 1:
                raise ConfigurationError("mobile publication state changed unexpectedly")
        return Publication("mobile", publication_key, marker, "published", message_id)

    def get_alert(self, fingerprint: str) -> StoredAlert | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT fingerprint, episode_key, active, display_name, severity, project
                FROM alert_state WHERE fingerprint = ?
                """,
                (fingerprint,),
            ).fetchone()
        return None if row is None else self._alert(row)

    def active_alerts(self) -> tuple[StoredAlert, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT fingerprint, episode_key, active, display_name, severity, project
                FROM alert_state WHERE active = 1 ORDER BY fingerprint
                """
            ).fetchall()
        return tuple(self._alert(row) for row in rows)

    def save_alert(
        self,
        *,
        fingerprint: str,
        episode_key: str,
        active: bool,
        display_name: str,
        severity: str,
        project: str,
    ) -> StoredAlert:
        values = (fingerprint, episode_key, display_name, severity, project)
        if any(not isinstance(value, str) or not value for value in values):
            raise ConfigurationError("invalid durable alert identity")
        if len(fingerprint) != 64 or len(episode_key) != 64:
            raise ConfigurationError("invalid durable alert digest")
        if len(display_name) > 120 or len(severity) > 40 or len(project) > 120:
            raise ConfigurationError("durable alert display fields are too long")
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO alert_state(
                    fingerprint, episode_key, active, display_name, severity, project, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    episode_key = excluded.episode_key,
                    active = excluded.active,
                    display_name = excluded.display_name,
                    severity = excluded.severity,
                    project = excluded.project,
                    updated_at = excluded.updated_at
                """,
                (
                    fingerprint,
                    episode_key,
                    1 if active else 0,
                    display_name,
                    severity,
                    project,
                    now,
                ),
            )
        return StoredAlert(fingerprint, episode_key, active, display_name, severity, project)

    @staticmethod
    def _publication(row: sqlite3.Row | tuple[object, ...]) -> Publication:
        return Publication(
            kind=str(row[0]),
            publication_key=str(row[1]),
            marker=str(row[2]),
            status=str(row[3]),
            message_id=None if row[4] is None else int(row[4]),
        )

    @staticmethod
    def _alert(row: sqlite3.Row | tuple[object, ...]) -> StoredAlert:
        return StoredAlert(
            fingerprint=str(row[0]),
            episode_key=str(row[1]),
            active=bool(row[2]),
            display_name=str(row[3]),
            severity=str(row[4]),
            project=str(row[5]),
        )
