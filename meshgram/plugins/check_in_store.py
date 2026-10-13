"""SQLite persistence for the check_in plugin: participants, every check-in, and a node directory.

The database is the single source of truth: ranks and streaks are derived from it
on demand (see ``check_in_scoring``). It's a plain stdlib ``sqlite3`` database in
WAL mode, used from worker threads (``asyncio.to_thread``) through one connection
guarded by a lock; every change is one transaction.

- ``participants``: who checks in. ``node_id`` is the node's public key (64 hex,
  uppercase), or NULL while the participant is known by name only: channel messages
  carry the sender's name, not its key. Such a participant is linked to the node
  (and merged into the participant that already has the key) once a node of that
  name is the only one in the directory, or as soon as a flood advert of that name
  is heard live (by the radio or an observer): the first one decides, even if other
  nodes went by the name before.
- ``check_ins``: every check-in message, repeats included. ``week`` is the Monday of
  its week; ``message_key`` identifies one message however many radios heard it.
- ``nodes``: names from adverts (the radio's contacts, packets it and observers
  heard), the newest advert winning. Resolves senders' names to keys.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .check_in_scoring import WEEK, History

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SOURCE_RADIO = "radio"
SOURCE_OBSERVER = "observer"
SOURCE_MANUAL = "manual"
SOURCE_IMPORT = "import"
SOURCES = (SOURCE_RADIO, SOURCE_OBSERVER, SOURCE_MANUAL, SOURCE_IMPORT)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY,
    node_id TEXT UNIQUE,
    name TEXT NOT NULL,
    extra_check_ins INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS participants_by_name ON participants (name);
CREATE TABLE IF NOT EXISTS check_ins (
    id INTEGER PRIMARY KEY,
    participant_id INTEGER NOT NULL REFERENCES participants (id) ON DELETE CASCADE,
    week TEXT NOT NULL,
    received_at REAL NOT NULL,
    sender_name TEXT,
    text TEXT,
    source TEXT NOT NULL,
    observer_id TEXT,
    message_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS check_ins_by_participant ON check_ins (participant_id, week);
CREATE INDEX IF NOT EXISTS check_ins_by_week ON check_ins (week, received_at);
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    advert_at REAL,
    seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS nodes_by_name ON nodes (name);
"""

# Where a check-in that wasn't heard (entered by hand, imported) is placed in its week.
Placement = Callable[[date], float]


class CheckInStoreError(Exception):
    """The database can't be used (unreadable, corrupt or from a newer Meshgram)."""


class CheckInError(Exception):
    """A change that can't be made; ``status`` is the fitting HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class NodeSighting:
    node_id: str
    name: str
    # The advert's own timestamp: an older advert doesn't overwrite a newer one.
    advert_at: Optional[float] = None
    # Only fill in a node that isn't known yet (MeshMapper's guess of who sent a packet).
    fill_only: bool = False
    # A flood advert heard just now (not the radio's contact list): the node is around
    # under this name, so participants known only by it are linked to it.
    flood: bool = False


@dataclass(frozen=True, slots=True)
class CheckInRecord:
    sender_name: str
    text: str
    received_at: float
    week: date
    source: str
    observer_id: Optional[str] = None
    message_key: Optional[str] = None


@dataclass(frozen=True, slots=True)
class SummaryRecord:
    """One participant of an import: a streak (and total) as of a week."""

    row: int
    node_id: Optional[str]
    name: Optional[str]
    streak: int
    total: Optional[int] = None
    last_week: Optional[date] = None


@dataclass(frozen=True, slots=True)
class BackupCheckIn:
    week: date
    received_at: float
    source: str
    sender_name: Optional[str] = None
    text: Optional[str] = None
    observer_id: Optional[str] = None
    message_key: Optional[str] = None


@dataclass(frozen=True, slots=True)
class BackupParticipant:
    """One participant of a backup (``export_backup``), with every check-in it had."""

    row: int
    node_id: Optional[str]
    name: str
    extra_check_ins: int = 0
    check_ins: tuple[BackupCheckIn, ...] = ()


_CHECK_IN_FIELDS = ("week", "received_at", "sender_name", "text", "source", "observer_id", "message_key")

_UNSET: Any = object()


class CheckInStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # --- Lifecycle ----------------------------------------------------------------

    def open(self) -> None:
        """Open (or create) the database.

        A corrupt database is moved aside and replaced with an empty one. Raises
        ``CheckInStoreError`` if the database can't be used at all.
        """
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise CheckInStoreError(f"cannot create {self.path.parent}: {exc}") from exc
            try:
                self._open_locked()
            except sqlite3.DatabaseError as exc:
                if not self.path.exists() or not _is_corruption(exc):
                    self._close_locked()
                    raise CheckInStoreError(f"cannot open {self.path}: {exc}") from exc
                aside = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
                LOGGER.error("Check-ins: %s is unreadable (%s); moving it to %s and starting fresh", self.path, exc, aside)
                self._close_locked()
                try:
                    self.path.replace(aside)
                    for suffix in ("-wal", "-shm"):
                        Path(f"{self.path}{suffix}").unlink(missing_ok=True)
                    self._open_locked()
                except (OSError, sqlite3.DatabaseError) as retry_exc:
                    self._close_locked()
                    raise CheckInStoreError(f"cannot open {self.path}: {retry_exc}") from retry_exc

    def _open_locked(self) -> None:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn = conn
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self._close_locked()
            raise CheckInStoreError(
                f"{self.path} was written by a newer Meshgram (schema {version}, this version reads {SCHEMA_VERSION})"
            )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(f"BEGIN; {_SCHEMA} PRAGMA user_version={SCHEMA_VERSION}; COMMIT;")
        conn.execute("SELECT COUNT(*) FROM check_ins").fetchone()  # surfaces a corrupt file now

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

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """The lock and one write transaction; rolled back if anything fails."""
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise CheckInStoreError("the check-in database is closed")
        return self._conn

    # --- Check-ins ------------------------------------------------------------------

    def record_check_in(self, record: CheckInRecord) -> Optional[int]:
        """Store a check-in for whoever sent it; the participant's id, or None if it's already stored."""
        with self._transaction() as conn:
            if record.message_key and conn.execute(
                "SELECT 1 FROM check_ins WHERE message_key = ?", (record.message_key,)
            ).fetchone():
                return None
            participant_id, _ = self._resolve(conn, record.sender_name, record.received_at)
            conn.execute(
                "INSERT INTO check_ins (participant_id, week, received_at, sender_name, text, source, observer_id, message_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    participant_id,
                    record.week.isoformat(),
                    record.received_at,
                    record.sender_name,
                    record.text,
                    record.source,
                    record.observer_id,
                    record.message_key,
                ),
            )
            return participant_id

    def histories(self) -> list[History]:
        """Every participant with the first check-in of each week, for ``check_in_scoring.standings``."""
        with self._lock:
            conn = self._connection()
            histories = {
                participant_id: History(participant_id, node_id, name, extra_check_ins=extra)
                for participant_id, node_id, name, extra in conn.execute(
                    "SELECT id, node_id, name, extra_check_ins FROM participants"
                )
            }
            rows = conn.execute(
                "SELECT participant_id, week, MIN(received_at), MAX(received_at) FROM check_ins GROUP BY participant_id, week"
            )
            for participant_id, week, first, last in rows:
                history = histories.get(participant_id)
                if history is None:
                    continue
                history.weeks[date.fromisoformat(week)] = first
                history.last_check_in = last if history.last_check_in is None else max(history.last_check_in, last)
            return list(histories.values())

    def check_ins(self, participant_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connection()
            self._require_participant(conn, participant_id)
            rows = conn.execute(
                "SELECT id, week, received_at, sender_name, text, source, observer_id FROM check_ins"
                " WHERE participant_id = ? ORDER BY received_at DESC, id DESC LIMIT ?",
                (participant_id, limit),
            )
            keys = ("id", "week", "received_at", "sender_name", "text", "source", "observer_id")
            return [dict(zip(keys, row)) for row in rows]

    # --- Node directory ---------------------------------------------------------------

    def upsert_nodes(self, sightings: Iterable[NodeSighting], now: float) -> int:
        """Record names from adverts; returns how many participants were renamed or linked."""
        changed = 0
        with self._transaction() as conn:
            names: set[str] = set()
            flooded: dict[str, str] = {}  # name -> the first node heard flooding an advert of it
            for sighting in sightings:
                if sighting.flood and not sighting.fill_only:
                    flooded.setdefault(sighting.name, sighting.node_id)
                row = conn.execute("SELECT name, advert_at FROM nodes WHERE node_id = ?", (sighting.node_id,)).fetchone()
                if row is not None:
                    known_name, known_advert_at = row
                    stale = (
                        sighting.advert_at is not None
                        and known_advert_at is not None
                        and sighting.advert_at < known_advert_at
                    )
                    if sighting.fill_only or stale or known_name == sighting.name:
                        conn.execute("UPDATE nodes SET seen_at = ? WHERE node_id = ?", (now, sighting.node_id))
                        continue
                    conn.execute(
                        "UPDATE nodes SET name = ?, advert_at = COALESCE(?, advert_at), seen_at = ? WHERE node_id = ?",
                        (sighting.name, sighting.advert_at, now, sighting.node_id),
                    )
                    # The old name may belong to just one node now.
                    names.add(known_name)
                else:
                    conn.execute(
                        "INSERT INTO nodes (node_id, name, advert_at, seen_at) VALUES (?, ?, ?, ?)",
                        (sighting.node_id, sighting.name, sighting.advert_at, now),
                    )
                names.add(sighting.name)
                # A participant goes by its node's latest name.
                changed += conn.execute(
                    "UPDATE participants SET name = ?, updated_at = ? WHERE node_id = ? AND name != ?",
                    (sighting.name, now, sighting.node_id, sighting.name),
                ).rowcount
            for name in names:
                node_ids = [node_id for (node_id,) in conn.execute("SELECT node_id FROM nodes WHERE name = ?", (name,))]
                if len(node_ids) == 1 and self._has_unresolved(conn, name):
                    self._link(conn, node_ids[0], name, now)
                    changed += 1
            for name, node_id in flooded.items():
                # Still unresolved: other nodes (old keys, contacts) go by the name too.
                if self._has_unresolved(conn, name) and self._directory_name(conn, node_id) == name:
                    self._link(conn, node_id, name, now)
                    changed += 1
        return changed

    def search_nodes(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        query = query.strip()
        with self._lock:
            conn = self._connection()
            rows = conn.execute(
                "SELECT node_id, name, seen_at FROM nodes WHERE name LIKE ? ESCAPE '\\' OR node_id LIKE ? ESCAPE '\\'"
                " ORDER BY seen_at DESC LIMIT ?",
                (f"%{_like_escape(query)}%", f"{_like_escape(query.upper())}%", limit),
            )
            return [{"node_id": node_id, "name": name, "seen_at": seen_at} for node_id, name, seen_at in rows]

    # --- Changes from the control panel ---------------------------------------------------

    def create_participant(self, name: str, node_id: Optional[str], now: float) -> int:
        with self._transaction() as conn:
            if node_id is not None:
                self._require_key_free(conn, node_id)
                name = self._directory_name(conn, node_id) or name
            return self._insert_participant(conn, node_id, name, now)

    def update_participant(self, participant_id: int, *, now: float, name: Any = _UNSET, node_id: Any = _UNSET) -> int:
        """Rename and/or (un)link a participant; returns its id (linking to a taken key merges into that participant)."""
        with self._transaction() as conn:
            self._require_participant(conn, participant_id)
            if node_id is not _UNSET:
                if node_id is None:
                    conn.execute("UPDATE participants SET node_id = NULL, updated_at = ? WHERE id = ?", (now, participant_id))
                else:
                    holder = self._participant_with_key(conn, node_id)
                    if holder is not None and holder != participant_id:
                        self._merge(conn, participant_id, holder)
                        participant_id = holder
                    else:
                        conn.execute(
                            "UPDATE participants SET node_id = ?, updated_at = ? WHERE id = ?", (node_id, now, participant_id)
                        )
                    directory_name = self._directory_name(conn, node_id)
                    if directory_name:
                        conn.execute("UPDATE participants SET name = ? WHERE id = ?", (directory_name, participant_id))
            if name is not _UNSET:
                conn.execute("UPDATE participants SET name = ?, updated_at = ? WHERE id = ?", (name, now, participant_id))
            return participant_id

    def delete_participant(self, participant_id: int) -> None:
        with self._transaction() as conn:
            self._require_participant(conn, participant_id)
            conn.execute("DELETE FROM participants WHERE id = ?", (participant_id,))

    def set_week(self, participant_id: int, week: date, present: bool, place: Placement) -> None:
        """Give a participant a check-in in ``week`` (if it has none), or take that week's check-ins away."""
        with self._transaction() as conn:
            self._require_participant(conn, participant_id)
            if present:
                self._ensure_week(conn, participant_id, week, place, SOURCE_MANUAL)
            else:
                conn.execute("DELETE FROM check_ins WHERE participant_id = ? AND week = ?", (participant_id, week.isoformat()))

    def set_streak(self, participant_id: int, streak: int, last_week: date, place: Placement) -> None:
        with self._transaction() as conn:
            self._require_participant(conn, participant_id)
            self._set_streak(conn, participant_id, streak, last_week, place, SOURCE_MANUAL)

    def import_summary(
        self,
        records: list[SummaryRecord],
        *,
        replace: bool,
        dry_run: bool,
        place: Placement,
        now: float,
    ) -> dict[str, Any]:
        """Bring in per-node summaries (from another check-in database), all or nothing.

        Each record's participant is found by key (or a key prefix only one known node
        has), else by name, else created; it then gets ``streak`` consecutive weekly
        check-ins up to ``last_week`` and, with ``total``, the check-ins beyond those.
        ``replace`` first removes every participant. A dry run, or a record that can't
        be imported, changes nothing; the result says what would be (or was) done.
        """
        result: dict[str, Any] = {"created": 0, "updated": 0, "linked": 0, "errors": [], "saved": False}
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                if replace:
                    self._clear(conn)
                for record in records:
                    try:
                        participant_id, outcome = self._import_target(conn, record, now)
                        if record.streak > 0:
                            if record.last_week is None:
                                raise CheckInError("needs the week of its last check-in")
                            self._set_streak(conn, participant_id, record.streak, record.last_week, place, SOURCE_IMPORT)
                        if record.total is not None:
                            weeks = conn.execute(
                                "SELECT COUNT(DISTINCT week) FROM check_ins WHERE participant_id = ?", (participant_id,)
                            ).fetchone()[0]
                            conn.execute(
                                "UPDATE participants SET extra_check_ins = ?, updated_at = ? WHERE id = ?",
                                (max(0, record.total - weeks), now, participant_id),
                            )
                        result[outcome] += 1
                    except CheckInError as exc:
                        result["errors"].append({"row": record.row, "message": str(exc)})
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            if dry_run or result["errors"]:
                conn.execute("ROLLBACK")
            else:
                conn.execute("COMMIT")
                result["saved"] = True
        return result

    def clear(self) -> dict[str, int]:
        """Delete every participant and check-in (the node directory stays); how many there were."""
        with self._transaction() as conn:
            counts = {
                "participants": conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0],
                "check_ins": conn.execute("SELECT COUNT(*) FROM check_ins").fetchone()[0],
            }
            self._clear(conn)
        return counts

    # --- Backups -------------------------------------------------------------------------

    def export_backup(self) -> list[dict[str, Any]]:
        """Every participant with every check-in, oldest first: what ``restore_backup`` takes back."""
        with self._lock:
            conn = self._connection()
            participants = {
                participant_id: {"node_id": node_id, "name": name, "extra_check_ins": extra, "check_ins": []}
                for participant_id, node_id, name, extra in conn.execute(
                    "SELECT id, node_id, name, extra_check_ins FROM participants ORDER BY id"
                )
            }
            rows = conn.execute(
                f"SELECT participant_id, {', '.join(_CHECK_IN_FIELDS)} FROM check_ins ORDER BY received_at, id"
            )
            for participant_id, *values in rows:
                participants[participant_id]["check_ins"].append(dict(zip(_CHECK_IN_FIELDS, values)))
            return list(participants.values())

    def restore_backup(
        self, participants: list[BackupParticipant], *, replace: bool, dry_run: bool, now: float
    ) -> dict[str, Any]:
        """Bring back a backup's participants and check-ins, all or nothing.

        ``replace`` first removes every participant, so the database ends up as the
        backup was. Otherwise each participant is added to the one here with the same
        key (a name-only one: the same name), and check-ins already here (the same
        message, or the same moment) are skipped, so restoring twice changes nothing.
        """
        result: dict[str, Any] = {"created": 0, "updated": 0, "check_ins": 0, "duplicates": 0, "saved": False}
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                if replace:
                    self._clear(conn)
                for participant in participants:
                    participant_id, outcome = self._restore_target(conn, participant, merge=not replace, now=now)
                    result[outcome] += 1
                    for check_in in participant.check_ins:
                        if self._restore_check_in(conn, participant_id, check_in):
                            result["check_ins"] += 1
                        else:
                            result["duplicates"] += 1
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            if dry_run:
                conn.execute("ROLLBACK")
            else:
                conn.execute("COMMIT")
                result["saved"] = True
        return result

    def _restore_target(
        self, conn: sqlite3.Connection, participant: BackupParticipant, *, merge: bool, now: float
    ) -> tuple[int, str]:
        if participant.node_id is not None:
            existing = self._participant_with_key(conn, participant.node_id)
        elif merge:
            row = conn.execute(
                "SELECT id FROM participants WHERE node_id IS NULL AND name = ? ORDER BY id LIMIT 1", (participant.name,)
            ).fetchone()
            existing = row[0] if row else None
        else:
            existing = None  # Name-only participants stay apart, as they were.
        if existing is not None:
            conn.execute(
                "UPDATE participants SET extra_check_ins = MAX(extra_check_ins, ?), updated_at = ? WHERE id = ?",
                (participant.extra_check_ins, now, existing),
            )
            return existing, "updated"
        # A node goes by its latest advertised name.
        name = (self._directory_name(conn, participant.node_id) if participant.node_id else None) or participant.name
        return self._insert_participant(conn, participant.node_id, name, now, participant.extra_check_ins), "created"

    @staticmethod
    def _restore_check_in(conn: sqlite3.Connection, participant_id: int, check_in: BackupCheckIn) -> bool:
        """Add one check-in unless it's here already; whether it was added."""
        week = check_in.week.isoformat()
        if check_in.message_key is not None and conn.execute(
            "SELECT 1 FROM check_ins WHERE message_key = ?", (check_in.message_key,)
        ).fetchone():
            return False
        if conn.execute(
            "SELECT 1 FROM check_ins WHERE participant_id = ? AND week = ? AND received_at = ? AND source = ?",
            (participant_id, week, check_in.received_at, check_in.source),
        ).fetchone():
            return False
        conn.execute(
            "INSERT INTO check_ins (participant_id, week, received_at, sender_name, text, source, observer_id, message_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                participant_id,
                week,
                check_in.received_at,
                check_in.sender_name,
                check_in.text,
                check_in.source,
                check_in.observer_id,
                check_in.message_key,
            ),
        )
        return True

    # --- Internals ----------------------------------------------------------------------

    @staticmethod
    def _clear(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM check_ins")
        conn.execute("DELETE FROM participants")


    def _resolve(self, conn: sqlite3.Connection, name: str, now: float) -> tuple[int, bool]:
        """The participant a sender's name stands for, created if need be; (id, created)."""
        node_ids = [node_id for (node_id,) in conn.execute("SELECT node_id FROM nodes WHERE name = ?", (name,))]
        if len(node_ids) == 1:
            participant_id = self._link(conn, node_ids[0], name, now)
            if participant_id is not None:
                return participant_id, False
            return self._insert_participant(conn, node_ids[0], name, now), True
        if not node_ids:
            # A participant whose key was entered by hand, its advert not heard yet.
            linked = conn.execute(
                "SELECT id FROM participants WHERE node_id IS NOT NULL AND name = ? LIMIT 2", (name,)
            ).fetchall()
            if len(linked) == 1:
                return linked[0][0], False
        row = conn.execute(
            "SELECT id FROM participants WHERE node_id IS NULL AND name = ? ORDER BY id LIMIT 1", (name,)
        ).fetchone()
        if row is not None:
            return row[0], False
        return self._insert_participant(conn, None, name, now), True

    def _link(self, conn: sqlite3.Connection, node_id: str, name: str, now: float) -> Optional[int]:
        """Attach the participants known only as ``name`` to the node; the node's participant, if any."""
        holder = self._participant_with_key(conn, node_id)
        pending = [pid for (pid,) in conn.execute(
            "SELECT id FROM participants WHERE node_id IS NULL AND name = ? ORDER BY id", (name,)
        )]
        if holder is None and pending:
            holder = pending.pop(0)
            conn.execute("UPDATE participants SET node_id = ?, updated_at = ? WHERE id = ?", (node_id, now, holder))
            LOGGER.info("Check-ins: %s is now linked to node %s", name, node_id[:12])
        if holder is not None:
            for participant_id in pending:
                self._merge(conn, participant_id, holder)
        return holder

    @staticmethod
    def _merge(conn: sqlite3.Connection, source: int, target: int) -> None:
        conn.execute("UPDATE check_ins SET participant_id = ? WHERE participant_id = ?", (target, source))
        conn.execute(
            "UPDATE participants SET extra_check_ins = extra_check_ins"
            " + (SELECT extra_check_ins FROM participants WHERE id = ?) WHERE id = ?",
            (source, target),
        )
        conn.execute("DELETE FROM participants WHERE id = ?", (source,))

    def _import_target(self, conn: sqlite3.Connection, record: SummaryRecord, now: float) -> tuple[int, str]:
        node_id = self._import_key(conn, record) if record.node_id else None
        if node_id is not None:
            directory_name = self._directory_name(conn, node_id)
            holder = self._participant_with_key(conn, node_id)
            if holder is not None:
                if record.name and not directory_name:
                    conn.execute("UPDATE participants SET name = ?, updated_at = ? WHERE id = ?", (record.name, now, holder))
                return holder, "updated"
            name = directory_name or record.name or node_id[:8]
            linked = self._link(conn, node_id, name, now)
            if linked is not None:
                return linked, "linked"
            return self._insert_participant(conn, node_id, name, now), "created"
        if not record.name:
            raise CheckInError("needs a node key or a name")
        participant_id, created = self._resolve(conn, record.name, now)
        return participant_id, "created" if created else "updated"

    @staticmethod
    def _import_key(conn: sqlite3.Connection, record: SummaryRecord) -> Optional[str]:
        key = str(record.node_id).strip().upper()
        if len(key) == 64:
            return key
        pattern = f"{_like_escape(key)}%"
        matches = {node_id for (node_id,) in conn.execute(
            "SELECT node_id FROM nodes WHERE node_id LIKE ? ESCAPE '\\'"
            " UNION SELECT node_id FROM participants WHERE node_id LIKE ? ESCAPE '\\'",
            (pattern, pattern),
        )}
        if len(matches) == 1:
            return matches.pop()
        if record.name:
            return None  # Found by name instead.
        if matches:
            raise CheckInError(f"more than one known node has a key starting with {key}")
        raise CheckInError(f"no known node has a key starting with {key}")

    def _set_streak(
        self, conn: sqlite3.Connection, participant_id: int, streak: int, last_week: date, place: Placement, source: str
    ) -> None:
        """Make the participant's streak ``streak`` weeks up to ``last_week``: those weeks checked in, the one before not."""
        for offset in range(streak):
            self._ensure_week(conn, participant_id, last_week - offset * WEEK, place, source)
        conn.execute(
            "DELETE FROM check_ins WHERE participant_id = ? AND week = ?",
            (participant_id, (last_week - streak * WEEK).isoformat()),
        )

    @staticmethod
    def _ensure_week(conn: sqlite3.Connection, participant_id: int, week: date, place: Placement, source: str) -> None:
        if conn.execute(
            "SELECT 1 FROM check_ins WHERE participant_id = ? AND week = ?", (participant_id, week.isoformat())
        ).fetchone():
            return
        conn.execute(
            "INSERT INTO check_ins (participant_id, week, received_at, source) VALUES (?, ?, ?, ?)",
            (participant_id, week.isoformat(), place(week), source),
        )

    @staticmethod
    def _insert_participant(
        conn: sqlite3.Connection, node_id: Optional[str], name: str, now: float, extra_check_ins: int = 0
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO participants (node_id, name, extra_check_ins, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (node_id, name, extra_check_ins, now, now),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _require_participant(conn: sqlite3.Connection, participant_id: int) -> None:
        if not conn.execute("SELECT 1 FROM participants WHERE id = ?", (participant_id,)).fetchone():
            raise CheckInError("There's no such participant", 404)

    def _require_key_free(self, conn: sqlite3.Connection, node_id: str) -> None:
        if self._participant_with_key(conn, node_id) is not None:
            raise CheckInError("Another participant already has that node key", 409)

    @staticmethod
    def _participant_with_key(conn: sqlite3.Connection, node_id: str) -> Optional[int]:
        row = conn.execute("SELECT id FROM participants WHERE node_id = ?", (node_id,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _has_unresolved(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute("SELECT 1 FROM participants WHERE node_id IS NULL AND name = ?", (name,)).fetchone() is not None

    @staticmethod
    def _directory_name(conn: sqlite3.Connection, node_id: str) -> Optional[str]:
        row = conn.execute("SELECT name FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
        return row[0] if row else None


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_corruption(exc: sqlite3.DatabaseError) -> bool:
    text = str(exc).lower()
    return "malformed" in text or "not a database" in text or "corrupt" in text
