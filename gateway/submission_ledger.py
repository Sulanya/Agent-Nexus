"""Durable at-most-once admission and evidence-based receipt recovery.

An HTTP receipt confirms admission or rejection, not completion of a model turn.
Unknown outcomes stay reserved: a timeout must never cause another submission.
Only request hashes, HTTP receipts, and recovery metadata are persisted.
"""
import hashlib
import json
import sqlite3
import time
from contextlib import closing


class SubmissionLedger:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self):
        # Upgrade the original four-column ledger in one short transaction.
        with closing(self._connect()) as db, db:
            if db.execute('PRAGMA user_version').fetchone()[0] >= 1:
                return
            db.execute('BEGIN IMMEDIATE')
            # Another instance may have completed migration while we waited.
            if db.execute('PRAGMA user_version').fetchone()[0] >= 1:
                return
            db.execute('CREATE TABLE IF NOT EXISTS submissions '
                       '(tid TEXT, cid TEXT, fingerprint TEXT, receipt TEXT, '
                       'PRIMARY KEY(tid,cid))')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(submissions)')}
            additions = {
                'created': 'REAL', 'updated': 'REAL', 'state': 'TEXT',
                'recovery_attempts': 'INTEGER NOT NULL DEFAULT 0',
                'recovery_failures': 'INTEGER NOT NULL DEFAULT 0',
                'unknown_exceptions': 'INTEGER NOT NULL DEFAULT 0',
                'last_recovery': 'REAL', 'receipt_source': 'TEXT',
            }
            for name, definition in additions.items():
                if name not in columns:
                    db.execute('ALTER TABLE submissions ADD COLUMN ' + name + ' ' + definition)
            now = time.time()
            db.execute("UPDATE submissions SET created=COALESCE(created,?), "
                       "updated=COALESCE(updated,?), "
                       "state=CASE WHEN receipt IS NULL THEN 'unknown' ELSE 'confirmed' END, "
                       "receipt_source=CASE WHEN receipt IS NULL THEN NULL ELSE 'legacy' END "
                       "WHERE created IS NULL OR updated IS NULL OR state IS NULL", (now, now))
            db.execute('CREATE TABLE IF NOT EXISTS submission_counters '
                       '(name TEXT PRIMARY KEY, value INTEGER NOT NULL)')
            received = db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0]
            unknown = db.execute("SELECT COUNT(*) FROM submissions WHERE state='unknown'").fetchone()[0]
            for name, initial in (('received', received), ('unknown', unknown),
                                  ('resolved', 0), ('replayed', 0)):
                db.execute('INSERT OR IGNORE INTO submission_counters VALUES (?,?)', (name, initial))
            db.execute('PRAGMA user_version=1')

    @staticmethod
    def _bump(db, name):
        db.execute('UPDATE submission_counters SET value=value+1 WHERE name=?', (name,))

    @staticmethod
    def _decode(receipt):
        return tuple(json.loads(receipt))

    def _stored(self, tid, cid):
        with closing(self._connect()) as db:
            row = db.execute('SELECT receipt FROM submissions WHERE tid=? AND cid=?',
                             (tid, cid)).fetchone()
        return self._decode(row['receipt']) if row and row['receipt'] is not None else None

    def run(self, tid, body, submit, reconcile=None):
        """Submit once; duplicates only return or reconcile the durable receipt.

        reconcile(tid, cid) must use positive evidence and return (payload, status)
        or None. It runs without a SQLite transaction and may never submit.
        """
        cid = str(body.get('clientUserMessageId') or '')
        if not cid:
            return submit()  # compatibility for old callers
        if len(cid) > 200:
            return self.error('invalid_message_id', '消息编号过长', 400)
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True,
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT fingerprint, receipt FROM submissions '
                             'WHERE tid=? AND cid=?', (tid, cid)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    return self.error('message_id_conflict', '同一消息编号不能用于不同内容', 409)
                if row['receipt'] is not None:
                    self._bump(db, 'replayed')
                    return self._decode(row['receipt'])
            else:
                now = time.time()
                db.execute('INSERT INTO submissions '
                           '(tid,cid,fingerprint,receipt,created,updated,state) '
                           "VALUES (?,?,?,NULL,?,?,'received')", (tid, cid, fingerprint, now, now))
                self._bump(db, 'received')
        if row:
            return self.resolve(tid, cid, reconcile)
        try:
            result = submit()
            # A direct response may be richer than admission evidence obtained
            # while submit was running. Recovery confirmations only write NULL.
            encoded = json.dumps(result, ensure_ascii=False)
            with closing(self._connect()) as db, db:
                db.execute("UPDATE submissions SET receipt=?,state='confirmed',"
                           "updated=?,receipt_source='submit' WHERE tid=? AND cid=?",
                           (encoded, time.time(), tid, cid))
            return result
        except Exception:
            recovered = self._reconcile(tid, cid, reconcile)
            if recovered is not None:
                return recovered
            with closing(self._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                changed = db.execute("UPDATE submissions SET state='unknown',updated=?,"
                                     'unknown_exceptions=unknown_exceptions+1 '
                                     "WHERE tid=? AND cid=? AND receipt IS NULL AND state!='unknown'",
                                     (time.time(), tid, cid)).rowcount
                if changed:
                    self._bump(db, 'unknown')
            # A concurrent confirmation may arrive after the reconciliation read.
            recovered = self._stored(tid, cid)
            if recovered is not None:
                return recovered
            raise

    def _confirm(self, tid, cid, result, source):
        encoded = json.dumps(result, ensure_ascii=False)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE submissions SET receipt=?,state='confirmed',"
                                 'updated=?,receipt_source=? '
                                 'WHERE tid=? AND cid=? AND receipt IS NULL',
                                 (encoded, time.time(), source, tid, cid)).rowcount
            if changed:
                self._bump(db, 'resolved')
        return bool(changed)

    def confirm(self, tid, cid, receipt, status=200):
        """Persist evidence only if still NULL; return whether written.

        Accept confirm(tid, cid, payload, status) or a (payload, status) tuple.
        Missing reservations are never created by this method.
        """
        result = receipt if isinstance(receipt, tuple) and len(receipt) == 2 else (receipt, status)
        return self._confirm(tid, cid, result, 'confirmation')

    def _reconcile(self, tid, cid, reconcile):
        if reconcile is None:
            return self._stored(tid, cid)
        with closing(self._connect()) as db, db:
            changed = db.execute('UPDATE submissions SET recovery_attempts=recovery_attempts+1, '
                                 'last_recovery=?,updated=? '
                                 'WHERE tid=? AND cid=? AND receipt IS NULL',
                                 (time.time(), time.time(), tid, cid)).rowcount
        if not changed:
            return self._stored(tid, cid)
        try:
            # Engine, journal, and queue reads happen outside all ledger locks.
            result = reconcile(tid, cid)
            if (isinstance(result, (tuple, list)) and len(result) == 2
                    and isinstance(result[1], int) and not isinstance(result[1], bool)
                    and 100 <= result[1] <= 599):
                self._confirm(tid, cid, result, 'reconciliation')
        except Exception:
            # Evidence lookup failure does not mean admission failed.
            with closing(self._connect()) as db, db:
                db.execute('UPDATE submissions SET recovery_failures=recovery_failures+1,updated=? '
                           'WHERE tid=? AND cid=? AND receipt IS NULL', (time.time(), tid, cid))
        # A concurrent submit or recovery may have won the write race.
        return self._stored(tid, cid)

    def resolve(self, tid, cid, reconcile=None):
        """Read or recover a reservation, without ever calling submit again."""
        with closing(self._connect()) as db:
            row = db.execute('SELECT receipt FROM submissions WHERE tid=? AND cid=?',
                             (tid, cid)).fetchone()
        if not row:
            return self.error('submission_not_found', '尚未找到该消息的提交记录', 404)
        if row['receipt'] is not None:
            stored = self._decode(row['receipt'])
            receipt = stored[0] if isinstance(stored, (tuple, list)) and stored else None
            # Admission is not execution evidence. Reconcile accepted-but-not-
            # started receipts on status reads, and only upgrade on native proof.
            if isinstance(receipt, dict) and receipt.get('mode') == 'accepted' and reconcile:
                try:
                    recovered = reconcile(tid, cid)
                    if (isinstance(recovered, (tuple, list)) and len(recovered) == 2
                            and isinstance(recovered[1], int) and 200 <= recovered[1] < 300
                            and isinstance(recovered[0], dict)
                            and recovered[0].get('mode') in ('queued', 'started')):
                        encoded = json.dumps(recovered, ensure_ascii=False)
                        with closing(self._connect()) as db, db:
                            db.execute('UPDATE submissions SET receipt=?,state=\'confirmed\',updated=?,'
                                       'receipt_source=\'reconciliation\' WHERE tid=? AND cid=? AND receipt=?',
                                       (encoded, time.time(), tid, cid, row['receipt']))
                        return self._stored(tid, cid)
                except Exception:
                    pass
            return stored
        recovered = self._reconcile(tid, cid, reconcile)
        if recovered is not None:
            return recovered
        return self.error('submission_unconfirmed',
                          '消息已接收，结果尚未确认；请查看会话和队列，勿重复发送', 409)

    def lookup(self, tid, cid):
        """Safe receipt metadata, with no hashes or exception text."""
        with closing(self._connect()) as db:
            row = db.execute('SELECT receipt,state,created,updated,recovery_attempts,'
                             'recovery_failures,unknown_exceptions,last_recovery,receipt_source '
                             'FROM submissions WHERE tid=? AND cid=?', (tid, cid)).fetchone()
        if not row:
            return {'found': False, 'state': 'not_found', 'receipt': None, 'status': None}
        result = self._decode(row['receipt']) if row['receipt'] is not None else (None, None)
        return {'found': True, 'state': row['state'], 'receipt': result[0], 'status': result[1],
                'created': row['created'], 'updated': row['updated'],
                'recoveryAttempts': row['recovery_attempts'], 'recoveryFailures': row['recovery_failures'],
                'unknownExceptions': row['unknown_exceptions'], 'lastRecovery': row['last_recovery'],
                'receiptSource': row['receipt_source']}

    def stats(self):
        """Durable aggregate counts; received counts unique reservations.

        unknown counts reservations whose submit raised without evidence;
        resolved counts successful NULL-to-receipt confirmations; replayed counts
        run() returning a stored receipt. resolve() reads add no counts.
        """
        with closing(self._connect()) as db:
            db.execute('BEGIN')
            result = {row['name']: row['value'] for row in db.execute(
                'SELECT name,value FROM submission_counters')}
            result['pending'] = db.execute('SELECT COUNT(*) FROM submissions '
                                           'WHERE receipt IS NULL').fetchone()[0]
        return result

    @staticmethod
    def error(code, message, status):
        return {'error': {'code': code, 'message': message}}, status
