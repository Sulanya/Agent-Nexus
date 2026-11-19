"""Bounded, durable SSE history and runtime checkpoints (standard library only).

The caller assigns event sequence numbers.  The journal never renumbers events,
and keeps the sequence high-water mark and epoch even when history is trimmed.
``max_bytes`` bounds retained UTF-8 JSON payload bytes, not SQLite file overhead.
All database access is serialized; callers may use the instance across threads.
No worker, listener, or background process is started by this module.
"""

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path


_REDACTED = "[redacted]"
_BEARER = re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_API_SECRET = re.compile(r"sk-[A-Za-z0-9_-]+", re.IGNORECASE)
_SENSITIVE_KEYS = frozenset((
    "authorization", "apikey", "token", "password", "secret",
    "accesstoken", "refreshtoken", "authtoken", "bearertoken", "idtoken",
    "clientsecret", "clientpassword",
))


def redact(value):
    """Return a JSON-compatible copy with credentials removed, preserving keys.

    Credential key matching accepts common separator/camel-case spellings while
    leaving token counters such as ``tokenUsage`` and ``totalTokens`` intact.
    Strings in ordinary chat fields are scanned for Bearer and sk- credentials.
    """
    if isinstance(value, dict):
        cleaned = {}
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            cleaned[key] = _REDACTED if normalized in _SENSITIVE_KEYS else redact(child)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact(child) for child in value]
    if isinstance(value, str):
        return _API_SECRET.sub(_REDACTED, _BEARER.sub("Bearer " + _REDACTED, value))
    return value


def _sequence(event):
    # Hub.broadcast currently uses seq; older producers may supply _seq.
    value = event.get("seq", event.get("_seq"))
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("event must have a positive integer seq or _seq")
    try:
        seq = int(value)
    except (TypeError, ValueError):
        raise ValueError("event must have a positive integer seq or _seq") from None
    if seq < 1 or seq > 9223372036854775807:
        raise ValueError("event sequence is outside SQLite's positive integer range")
    if "seq" in event and "_seq" in event:
        other = event["_seq"]
        if isinstance(other, bool) or not isinstance(other, (int, str)):
            raise ValueError("seq and _seq must agree")
        try:
            if int(other) != seq:
                raise ValueError("seq and _seq must agree")
        except (TypeError, ValueError):
            raise ValueError("seq and _seq must agree") from None
    return seq


def _thread_id(event):
    for container in (event, event.get("params")):
        if not isinstance(container, dict):
            continue
        for key in ("tid", "threadId", "thread_id"):
            value = container.get(key)
            if value is not None and value != "":
                return redact(str(value))
        thread = container.get("thread")
        if isinstance(thread, dict) and thread.get("id"):
            return redact(str(thread["id"]))
    return None


def _dump(value):
    return json.dumps(redact(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class EventJournal:
    def __init__(self, path, max_events=4000, max_bytes=12 * 1024 * 1024,
                 retention_seconds=86400):
        self.max_events = int(max_events)
        self.max_bytes = int(max_bytes)
        self.retention_seconds = float(retention_seconds)
        if self.max_events < 0 or self.max_bytes < 0 or self.retention_seconds < 0:
            raise ValueError("journal limits must be nonnegative")
        if not self.retention_seconds < float("inf"):
            raise ValueError("retention_seconds must be finite")
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=10000")
            with self._db:
                self._db.execute("""CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY, tid TEXT, recorded_at REAL NOT NULL,
                    payload TEXT NOT NULL, payload_bytes INTEGER NOT NULL)""")
                self._db.execute("""CREATE INDEX IF NOT EXISTS events_tid_seq
                    ON events(tid, seq)""")
                self._db.execute("""CREATE INDEX IF NOT EXISTS events_recorded_at
                    ON events(recorded_at)""")
                self._db.execute("""CREATE TABLE IF NOT EXISTS runtime_checkpoints (
                    tid TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at REAL NOT NULL)""")
                self._db.execute("INSERT OR IGNORE INTO metadata VALUES ('epoch', ?)",
                                 (uuid.uuid4().hex,))
                self._db.execute("""INSERT OR IGNORE INTO metadata VALUES
                    ('max_seq', (SELECT CAST(COALESCE(MAX(seq), 0) AS TEXT) FROM events))""")
                self._db.execute("INSERT OR IGNORE INTO metadata VALUES ('evicted_through', '0')")
                self._trim_locked(time.time())
        except BaseException:
            self._db.close()
            self._db = None
            raise

    def _connection(self):
        if self._db is None:
            raise RuntimeError("event journal is closed")
        return self._db

    def epoch(self):
        """Return this database's stable epoch, shared by all reopenings."""
        with self._lock:
            return self._connection().execute(
                "SELECT value FROM metadata WHERE key='epoch'").fetchone()[0]

    def max_seq(self):
        """Return the highest committed sequence, including evicted events."""
        with self._lock:
            return int(self._connection().execute(
                "SELECT value FROM metadata WHERE key='max_seq'").fetchone()[0])

    def append(self, event):
        """Commit a redacted copy; return False for a duplicate or evicted seq.

        The first committed payload wins when a producer retries the same seq.
        No event fields or sequence numbers are added to the supplied dictionary.
        """
        return self.append_many((event,)) == 1

    def append_many(self, events):
        """Commit one batch in one transaction; return newly inserted row count.

        Validation and redaction finish before any writes, so an invalid member
        leaves the entire batch uncommitted.  A retry's first payload wins.
        """
        now = time.time()
        rows = []
        for event in events:
            if not isinstance(event, dict):
                raise TypeError("event must be a dictionary")
            seq = _sequence(event)
            payload = _dump(event)
            rows.append((seq, _thread_id(event), now, payload, len(payload.encode("utf-8"))))
        with self._lock:
            db = self._connection()
            with db:
                cursor = db.executemany("""INSERT OR IGNORE INTO events
                    (seq, tid, recorded_at, payload, payload_bytes)
                    SELECT ?, ?, ?, ?, ? WHERE ? > (SELECT CAST(value AS INTEGER)
                    FROM metadata WHERE key='evicted_through')""",
                                        [row + (row[0],) for row in rows])
                if rows:
                    db.execute("""UPDATE metadata SET value=CAST(
                        MAX(CAST(value AS INTEGER), ?) AS TEXT) WHERE key='max_seq'""",
                               (max(row[0] for row in rows),))
                self._trim_locked(now)
                return cursor.rowcount

    def _trim_locked(self, now):
        db = self._connection()
        expired = db.execute("SELECT MAX(seq) FROM events WHERE recorded_at <= ?",
                             (now - self.retention_seconds,)).fetchone()[0]
        db.execute("DELETE FROM events WHERE recorded_at <= ?",
                   (now - self.retention_seconds,))
        if expired is not None:
            self._advance_eviction_locked(expired)
        count, size = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(payload_bytes), 0) FROM events").fetchone()
        if count <= self.max_events and size <= self.max_bytes:
            return
        cutoff = None
        for seq, event_size in db.execute("SELECT seq, payload_bytes FROM events ORDER BY seq"):
            count -= 1
            size -= event_size
            cutoff = seq
            if count <= self.max_events and size <= self.max_bytes:
                break
        if cutoff is not None:
            db.execute("DELETE FROM events WHERE seq <= ?", (cutoff,))
            self._advance_eviction_locked(cutoff)

    def _advance_eviction_locked(self, seq):
        self._connection().execute("""UPDATE metadata SET value=CAST(
            MAX(CAST(value AS INTEGER), ?) AS TEXT) WHERE key='evicted_through'""", (seq,))

    def trim(self):
        """Apply all retention limits; runtime checkpoints are unaffected."""
        with self._lock:
            db = self._connection()
            with db:
                self._trim_locked(time.time())

    def load_recent(self):
        """Return retained event dictionaries in ascending sequence order."""
        return self.events_after(0, limit=None)

    def events_after(self, seq, tid=None, limit=4000):
        """Return events strictly after seq, optionally restricted to a thread.

        ``limit=None`` returns the complete bounded window.  A missing history
        interval can be detected by the caller using the first returned seq and
        ``max_seq()``; the journal does not manufacture resynchronization events.
        """
        seq = int(seq)
        if seq < 0:
            raise ValueError("replay sequence must be nonnegative")
        if limit is not None:
            limit = int(limit)
            if limit < 0:
                raise ValueError("replay limit must be nonnegative")
        with self._lock:
            db = self._connection()
            with db:
                self._trim_locked(time.time())
                query = "SELECT payload FROM events WHERE seq > ?"
                args = [seq]
                if tid is not None:
                    query += " AND tid = ?"
                    args.append(redact(str(tid)))
                query += " ORDER BY seq"
                if limit is not None:
                    query += " LIMIT ?"
                    args.append(limit)
                return [json.loads(row[0]) for row in db.execute(query, args)]

    def load_runtime(self):
        """Return {thread_id: state} for all persisted runtime checkpoints.

        States use JSON values: tuple signatures are restored as lists.  Callers
        comparing an internal tuple signature should normalize it after loading.
        """
        with self._lock:
            return {tid: json.loads(state) for tid, state in self._connection().execute(
                "SELECT tid, state FROM runtime_checkpoints ORDER BY tid")}

    def save_runtime(self, tid, state):
        """Atomically replace a thread's checkpoint with its redacted JSON copy."""
        if not isinstance(state, dict):
            raise TypeError("runtime state must be a dictionary")
        if tid is None or str(tid) == "":
            raise ValueError("runtime checkpoint requires a thread id")
        payload = _dump(state)
        with self._lock:
            db = self._connection()
            with db:
                db.execute("""INSERT INTO runtime_checkpoints (tid, state, updated_at)
                    VALUES (?, ?, ?) ON CONFLICT(tid) DO UPDATE SET
                    state=excluded.state, updated_at=excluded.updated_at""",
                           (redact(str(tid)), payload, time.time()))

    def clear(self):
        """Clear only event history; preserve sequence, epoch and checkpoints."""
        with self._lock:
            db = self._connection()
            with db:
                db.execute("DELETE FROM events")
                db.execute("""UPDATE metadata SET value=(SELECT value FROM metadata
                    WHERE key='max_seq') WHERE key='evicted_through'""")

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def __enter__(self):
        self._connection()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
