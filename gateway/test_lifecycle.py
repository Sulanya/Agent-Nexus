"""Explicitly opted-in test threads only; archive, never delete transcripts."""
import sqlite3
import time
from contextlib import closing


class TestLifecycle:
    def __init__(self, path):
        self.path = str(path)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.execute('CREATE TABLE IF NOT EXISTS tests (tid TEXT PRIMARY KEY, created REAL, idle REAL, archived REAL)')
        return db

    def register(self, tid):
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO tests VALUES (?, ?, NULL, NULL)', (tid, time.time()))

    def sweep(self, snapshot, busy, archive, now=None):
        now = time.time() if now is None else now
        if not snapshot or now - snapshot.get('generatedAt', 0) > 15:
            return  # stale/missing index cannot prove safety
        items = {t['id']: t for t in snapshot.get('threads', [])}
        with closing(self.connect()) as db, db:
            rows = db.execute('SELECT tid,created,idle FROM tests WHERE archived IS NULL').fetchall()
            for tid, created, idle in rows:
                t = items.get(tid)
                if t and t.get('archived'):
                    db.execute('UPDATE tests SET archived=? WHERE tid=?', (now, tid))
                    continue
                if not t or t.get('running') or t.get('queueLen') or busy(tid):
                    db.execute('UPDATE tests SET idle=NULL WHERE tid=?', (tid,))
                    continue
                if t.get('archived'):
                    db.execute('UPDATE tests SET archived=? WHERE tid=?', (now, tid))
                    continue
                # User naming/pinning promotes a temporary test to a retained task.
                if t.get('name') or t.get('pinned'):
                    db.execute('UPDATE tests SET idle=NULL WHERE tid=?', (tid,))
                    continue
                if idle is None:
                    db.execute('UPDATE tests SET idle=? WHERE tid=?', (now, tid))
                    continue
                if now-idle < 300:
                    continue
                archive(tid)  # leave pending on error; retry next sweep
                db.execute('UPDATE tests SET archived=? WHERE tid=?', (now, tid))
