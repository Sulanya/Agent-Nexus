"""Durable per-message settings for Codex submissions awaiting native dispatch."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

THREAD_SETTINGS_CID='__an_thread_settings__'


class QueueIntentStore:
    def __init__(self,path,ttl_days=14):
        self.path=Path(path)
        self.ttl_seconds=max(1,int(ttl_days*86400))
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS queue_intents (
                thread_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                effort TEXT NOT NULL DEFAULT '',
                applied INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                PRIMARY KEY(thread_id,client_id)
            )''')
            db.execute('CREATE INDEX IF NOT EXISTS queue_intents_updated ON queue_intents(updated)')
            columns={row[1] for row in db.execute('PRAGMA table_info(queue_intents)')}
            if 'applied' not in columns:
                db.execute('ALTER TABLE queue_intents ADD COLUMN applied INTEGER NOT NULL DEFAULT 0')

    def _connect(self):
        db=sqlite3.connect(str(self.path),timeout=4,isolation_level=None)
        db.execute('PRAGMA busy_timeout=4000')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        return db

    def put(self,thread_id,client_id,model='',provider='',effort=''):
        if not thread_id or not client_id:
            raise ValueError('thread_id and client_id are required')
        values=(str(thread_id),str(client_id),str(model or ''),str(provider or ''),str(effort or ''))
        now=time.time()
        with closing(self._connect()) as db, db:
            db.execute('''INSERT INTO queue_intents
                (thread_id,client_id,model,provider,effort,applied,created,updated)
                VALUES (?,?,?,?,?,0,?,?)
                ON CONFLICT(thread_id,client_id) DO UPDATE SET
                  applied=CASE WHEN queue_intents.model!=excluded.model OR
                    queue_intents.provider!=excluded.provider OR queue_intents.effort!=excluded.effort
                    THEN 0 ELSE queue_intents.applied END,
                  model=excluded.model,provider=excluded.provider,effort=excluded.effort,updated=excluded.updated''',
                (*values,now,now))
        return self.get(*values[:2])

    def put_thread_settings(self,thread_id,model='',provider='',effort=''):
        return self.put(thread_id,THREAD_SETTINGS_CID,model,provider,effort)

    def get_thread_settings(self,thread_id):
        return self.get(thread_id,THREAD_SETTINGS_CID)

    def complete_thread_settings(self,thread_id):
        return self.complete(thread_id,THREAD_SETTINGS_CID)

    def pending_thread_ids(self):
        with closing(self._connect()) as db:
            rows=db.execute('SELECT thread_id FROM queue_intents WHERE client_id=?',
                            (THREAD_SETTINGS_CID,)).fetchall()
        return {str(row[0]) for row in rows}

    def mark_applied(self,thread_id,client_id):
        with closing(self._connect()) as db, db:
            return db.execute('UPDATE queue_intents SET applied=1,updated=? WHERE thread_id=? AND client_id=?',
                              (time.time(),str(thread_id),str(client_id))).rowcount==1

    def get(self,thread_id,client_id):
        with closing(self._connect()) as db:
            row=db.execute('SELECT thread_id,client_id,model,provider,effort,applied,created,updated '
                            'FROM queue_intents WHERE thread_id=? AND client_id=?',
                            (str(thread_id),str(client_id))).fetchone()
        if row is None:return None
        return dict(zip(('threadId','clientUserMessageId','model','provider','effort','applied','created','updated'),row))

    def complete(self,thread_id,client_id):
        with closing(self._connect()) as db, db:
            return db.execute('DELETE FROM queue_intents WHERE thread_id=? AND client_id=?',
                              (str(thread_id),str(client_id))).rowcount==1

    def prune(self,now=None,known_pending=None):
        # Never expire settings for a still-queued native message. Without a
        # complete authoritative queue scan the pending set is unknown.
        if known_pending is None:return 0
        pending={(str(tid),str(cid)) for tid,cid in known_pending}
        cutoff=(time.time() if now is None else float(now))-self.ttl_seconds
        with closing(self._connect()) as db:
            stale=db.execute('SELECT thread_id,client_id FROM queue_intents WHERE updated<?',(cutoff,)).fetchall()
        removed=0
        with closing(self._connect()) as db, db:
            for thread_id,client_id in stale:
                if (thread_id,client_id) not in pending:
                    removed+=db.execute('DELETE FROM queue_intents WHERE thread_id=? AND client_id=? AND updated<?',
                                        (thread_id,client_id,cutoff)).rowcount
        return removed

    def count(self):
        with closing(self._connect()) as db:
            return int(db.execute('SELECT COUNT(*) FROM queue_intents').fetchone()[0])


def apply_intent(app_server,thread_id,intent,model_provider_of,wait_settings):
    """Apply one queue item's settings and require the normal persisted read-back."""
    model=intent.get('model') or ''
    provider=intent.get('provider') or (model_provider_of(model) if model else '')
    effort=intent.get('effort') or ''
    if model and provider and provider!=app_server.provider_hint(thread_id):
        app_server.switch_thread_model(thread_id,model,provider,effort)
        return
    app_server.ensure_resumed(thread_id)
    params={'threadId':thread_id}
    if model:params['model']=model
    if effort:params['effort']=effort
    app_server.request('thread/settings/update',params,timeout=30)
    wait_settings(thread_id,model,effort,allow_missing=thread_id in app_server.nexus_created)
    if model:
        app_server.thread_model[thread_id]=model
        app_server.thread_provider[thread_id]=provider or app_server.provider_hint(thread_id)
    if effort:app_server.thread_effort[thread_id]=effort


def prepare_head(app_server,store,thread_id,queue_item,model_provider_of,wait_settings):
    """Apply only the intent bound to this exact native queue head."""
    client_id=(queue_item or {}).get('clientUserMessageId')
    if not client_id:return None
    intent=store.get(thread_id,client_id)
    if intent:
        apply_intent(app_server,thread_id,intent,model_provider_of,wait_settings)
        store.mark_applied(thread_id,client_id)
        intent=store.get(thread_id,client_id)
    return intent


def settle_started(store,thread_id,client_id):
    """Remove intent only after the native queue/start operation was accepted."""
    if not client_id:return False
    return store.complete(thread_id,client_id)


def promote_head(app_server,store,thread_id,queue_item,model_provider_of,
                 wait_settings,start):
    """Apply head settings, start its native qid, then settle durable intent."""
    intent=prepare_head(app_server,store,thread_id,queue_item,
                        model_provider_of,wait_settings)
    result=start()
    if isinstance(result,dict) and result.get('__late__'):
        return result
    if intent:
        settle_started(store,thread_id,intent['clientUserMessageId'])
    return result


def reconcile_started(store,thread_id,client_id,receipt):
    """Settle per-message intent against actual start evidence.

    If a native enqueue started before QueuePump applied its intent, make the
    skipped setting explicit and remove it so it can never leak to the next
    queued message.
    """
    intent=store.get(thread_id,client_id)
    if not intent:return receipt
    result=dict(receipt)
    if not intent.get('applied'):
        result['settingsSkipped']=True
        result['settingsDeferred']=False
    else:
        result['settingsDeferred']=False
    store.complete(thread_id,client_id)
    return result
