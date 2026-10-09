"""SQLite persistence for the packet map, so nodes and packet history survive restarts.

The store is a plain stdlib ``sqlite3`` database in WAL mode. ``PacketMapState``
records what changed; the plugin hands those changes to :meth:`PacketMapStore.write`
in a worker thread every few seconds, and once more on shutdown. Each write is a
single transaction, so a crash or power loss loses at most the last few seconds
and never leaves a half-written database.

Packets and nodes are stored as JSON documents (exactly what the web app is sent),
with a few indexed columns for retention. Retention mirrors the in-memory ring
buffers: anything that can't be in ``max_packets``, ``max_remote_packets`` or
``max_messages`` any more is deleted.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS packets (
    id INTEGER PRIMARY KEY,
    received_at REAL NOT NULL,
    remote INTEGER NOT NULL,
    has_message INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS packets_by_source ON packets (remote, id);
CREATE INDEX IF NOT EXISTS packets_with_message ON packets (has_message, id);
"""

_PRUNE = """
DELETE FROM packets
WHERE id NOT IN (SELECT id FROM packets WHERE remote = 0 ORDER BY id DESC LIMIT :max_packets)
  AND id NOT IN (SELECT id FROM packets WHERE remote = 1 ORDER BY id DESC LIMIT :max_remote_packets)
  AND id NOT IN (SELECT id FROM packets WHERE has_message = 1 ORDER BY id DESC LIMIT :max_messages)
"""


class PacketMapStoreError(Exception):
    """The database can't be used (unreadable, corrupt or from a newer Meshgram)."""


@dataclass(slots=True)
class SavedState:
    self_id: Optional[str] = None
    nodes: list[dict[str, Any]] = field(default_factory=list)
    # Oldest first.
    packets: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class PendingChanges:
    """Rows to write, serialized on the event loop so the worker thread never touches live state."""

    self_id: Optional[str]
    nodes: list[tuple[str, str]]
    # (id, received_at, remote, has_message, data)
    packets: list[tuple[int, float, int, int, str]]


@dataclass(slots=True)
class RetentionLimits:
    max_packets: int
    max_remote_packets: int
    max_messages: int


def encode(document: dict[str, Any]) -> str:
    return json.dumps(document, separators=(",", ":"))


class PacketMapStore:
    def __init__(self, path: Path, limits: RetentionLimits):
        self.path = Path(path)
        self.limits = limits
        self._conn: Optional[sqlite3.Connection] = None
        # Writes run in worker threads; a cancelled flush may still be running
        # when the final one starts on shutdown.
        self._lock = threading.Lock()

    def open(self) -> SavedState:
        """Open (or create) the database and return what it holds.

        A corrupt database is moved aside and replaced with an empty one, so a bad
        file never keeps the map from starting. Raises ``PacketMapStoreError`` if
        the database can't be used at all.
        """
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PacketMapStoreError(f"cannot create {self.path.parent}: {exc}") from exc
            try:
                return self._open_locked()
            except sqlite3.DatabaseError as exc:
                if not self.path.exists() or not _is_corruption(exc):
                    raise PacketMapStoreError(f"cannot open {self.path}: {exc}") from exc
                aside = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
                LOGGER.error("Packet map: %s is unreadable (%s); moving it to %s and starting fresh", self.path, exc, aside)
                self._close_locked()
                try:
                    self.path.replace(aside)
                    for suffix in ("-wal", "-shm"):
                        Path(f"{self.path}{suffix}").unlink(missing_ok=True)
                    return self._open_locked()
                except (OSError, sqlite3.DatabaseError) as retry_exc:
                    self._close_locked()
                    raise PacketMapStoreError(f"cannot open {self.path}: {retry_exc}") from retry_exc

    def _open_locked(self) -> SavedState:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn = conn
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self._close_locked()
            raise PacketMapStoreError(
                f"{self.path} was written by a newer Meshgram (schema {version}, this version reads {SCHEMA_VERSION})"
            )
        conn.execute("PRAGMA journal_mode=WAL")
        # WAL + NORMAL: durable across application crashes; a power loss can drop
        # only the last committed flush.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(f"BEGIN; {_SCHEMA} PRAGMA user_version={SCHEMA_VERSION}; COMMIT;")

        saved = SavedState()
        row = conn.execute("SELECT value FROM meta WHERE key = 'self_id'").fetchone()
        saved.self_id = row[0] if row else None
        saved.nodes = [json.loads(data) for (data,) in conn.execute("SELECT data FROM nodes ORDER BY rowid")]
        conn.execute("BEGIN")
        conn.execute(_PRUNE, self._limit_params())  # limits may have been lowered since the last run
        conn.execute("COMMIT")
        saved.packets = [json.loads(data) for (data,) in conn.execute("SELECT data FROM packets ORDER BY id")]
        return saved

    def write(self, changes: PendingChanges) -> None:
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            conn.execute("BEGIN")
            try:
                if changes.self_id:
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES ('self_id', ?) "
                        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                        (changes.self_id,),
                    )
                conn.executemany(
                    "INSERT INTO nodes (id, data) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET data = excluded.data",
                    changes.nodes,
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO packets (id, received_at, remote, has_message, data) VALUES (?, ?, ?, ?, ?)",
                    changes.packets,
                )
                if changes.packets:
                    conn.execute(_PRUNE, self._limit_params())
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def _limit_params(self) -> dict[str, int]:
        return {
            "max_packets": self.limits.max_packets,
            "max_remote_packets": self.limits.max_remote_packets,
            "max_messages": self.limits.max_messages,
        }


def _is_corruption(exc: sqlite3.DatabaseError) -> bool:
    text = str(exc).lower()
    return "malformed" in text or "not a database" in text or "corrupt" in text
