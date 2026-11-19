"""Durable one-shot schedule and explicit cross-thread handoff records."""
import json
import math
import hashlib
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

STATES={'scheduled','needs_review','dispatching','accepted','queued','running','reconciling',
        'completed','failed','cancelled'}
TERMINAL={'completed','failed','cancelled'}


class WorkflowTaskStore:
    def __init__(self,path,clock=time.time,lease_seconds=60):
        self.path=Path(path);self.clock=clock;self.lease_seconds=lease_seconds
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as db,db:
            db.execute('''CREATE TABLE IF NOT EXISTS workflow_tasks (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_thread_id TEXT, source_engine TEXT,
                target_thread_id TEXT NOT NULL, target_engine TEXT NOT NULL,
                title TEXT NOT NULL, instruction TEXT NOT NULL, run_at REAL NOT NULL,
                cid TEXT NOT NULL UNIQUE, state TEXT NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0, event_cursor INTEGER NOT NULL DEFAULT 0,
                turn_id TEXT, receipt_json TEXT, last_error TEXT, request_json TEXT,
                uncertain_since REAL
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS workflow_commands (
                request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
                receipt_json TEXT NOT NULL, created REAL NOT NULL,
                FOREIGN KEY(task_id) REFERENCES workflow_tasks(id)
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS workflow_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                request_id TEXT, event_type TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
                details_json TEXT NOT NULL, created REAL NOT NULL,
                FOREIGN KEY(task_id) REFERENCES workflow_tasks(id)
            )''')
            db.execute('CREATE INDEX IF NOT EXISTS workflow_events_task ON workflow_events(task_id,seq)')
            db.execute('CREATE INDEX IF NOT EXISTS workflow_due ON workflow_tasks(state,run_at,lease_until)')
            db.execute('CREATE INDEX IF NOT EXISTS workflow_target ON workflow_tasks(target_thread_id,updated)')
            columns={row[1] for row in db.execute('PRAGMA table_info(workflow_tasks)')}
            if 'source_engine' not in columns:
                db.execute('ALTER TABLE workflow_tasks ADD COLUMN source_engine TEXT')
            if 'request_json' not in columns:
                db.execute('ALTER TABLE workflow_tasks ADD COLUMN request_json TEXT')
            if 'uncertain_since' not in columns:
                db.execute('ALTER TABLE workflow_tasks ADD COLUMN uncertain_since REAL')

    def connect(self):
        db=sqlite3.connect(str(self.path),timeout=5,isolation_level=None)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA busy_timeout=5000');db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
        return db

    @staticmethod
    def clean(row):
        if row is None:return None
        value=dict(row)
        receipt=value.pop('receipt_json',None)
        value['receipt']=json.loads(receipt) if receipt else None
        value.pop('lease_until',None)
        value.pop('request_json',None)
        value['outcomeUnknown']=bool(value.pop('uncertain_since',None))
        return value

    def create(self,kind,target_thread_id,target_engine,instruction,run_at,
               source_thread_id=None,title='',event_cursor=0,source_engine=None,request_id=None):
        if kind not in ('scheduled','handoff'):raise ValueError('invalid_workflow_kind')
        if target_engine not in ('codex','claude','workbuddy'):raise ValueError('invalid_target_engine')
        target_thread_id=str(target_thread_id or '').strip()
        source_thread_id=str(source_thread_id or '').strip() or None
        instruction=str(instruction or '').strip()
        if not target_thread_id or not instruction:raise ValueError('target_and_instruction_required')
        if kind=='handoff' and (not source_thread_id or
                (source_thread_id==target_thread_id and source_engine==target_engine)):
            raise ValueError('handoff_requires_distinct_source_and_target')
        if kind=='scheduled' and source_thread_id not in (None,target_thread_id):
            raise ValueError('scheduled_task_source_must_match_target')
        now=float(self.clock());run_at=float(run_at)
        if not math.isfinite(run_at):raise ValueError('run_at_out_of_range')
        if len(instruction)>32000:raise ValueError('instruction_too_long')
        task_id=hashlib.sha256(str(request_id).encode()).hexdigest()[:32] if request_id else uuid.uuid4().hex
        fields={'kind':kind,'source_thread_id':source_thread_id,'source_engine':source_engine,
                'target_thread_id':target_thread_id,'target_engine':target_engine,
                'instruction':instruction,'run_at':run_at,'title':str(title or instruction.splitlines()[0][:120]).strip()[:160]}
        request_json=json.dumps(fields,sort_keys=True,ensure_ascii=False)
        cid='workflow:'+task_id
        state='scheduled'
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT * FROM workflow_tasks WHERE id=?',(task_id,)).fetchone()
            if existing:
                if existing['request_json']!=request_json:raise ValueError('workflow_request_conflict')
                if request_id:
                    command=db.execute('SELECT payload_hash,task_id FROM workflow_commands WHERE request_id=?',
                                       (str(request_id),)).fetchone()
                    if command and (command['payload_hash']!=hashlib.sha256(request_json.encode()).hexdigest()
                                    or command['task_id']!=task_id):
                        raise ValueError('workflow_request_conflict')
                    if not command:
                        db.execute('INSERT INTO workflow_commands VALUES (?,?,?,?,?)',
                            (str(request_id),task_id,hashlib.sha256(request_json.encode()).hexdigest(),
                             json.dumps({'taskId':task_id,'state':existing['state']},separators=(',',':')),now))
                return self.clean(existing)
            if run_at<now-5 or run_at>now+90*86400:raise ValueError('run_at_out_of_range')
            db.execute('''INSERT INTO workflow_tasks
                (id,kind,source_thread_id,source_engine,target_thread_id,target_engine,title,instruction,run_at,cid,state,
                 created,updated,attempts,lease_until,event_cursor,request_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)''',
                (task_id,kind,source_thread_id,source_engine,target_thread_id,target_engine,
                 str(title or instruction.splitlines()[0][:120]).strip()[:160],instruction,
                 run_at,cid,state,now,now,0,int(event_cursor or 0),request_json))
            if request_id:
                db.execute('INSERT INTO workflow_commands VALUES (?,?,?,?,?)',
                    (str(request_id),task_id,hashlib.sha256(request_json.encode()).hexdigest(),
                     json.dumps({'taskId':task_id,'state':state},separators=(',',':')),now))
            db.execute('INSERT INTO workflow_events '
                       '(task_id,request_id,event_type,from_state,to_state,details_json,created) '
                       'VALUES (?,?,\'command.accepted\',NULL,?,?,?)',
                       (task_id,str(request_id) if request_id else None,state,
                        json.dumps({'kind':kind,'targetThreadId':target_thread_id,
                                    'targetEngine':target_engine},separators=(',',':')),now))
        return self.get(task_id)

    def get(self,task_id):
        with closing(self.connect()) as db:
            row=db.execute('SELECT * FROM workflow_tasks WHERE id=?',(str(task_id),)).fetchone()
        return self.clean(row)

    def find_request(self,request_id):
        if not request_id:return None
        task_id=hashlib.sha256(str(request_id).encode()).hexdigest()[:32]
        return self.get(task_id)

    @staticmethod
    def _record_transition(db,task_id,event_type,to_state,details=None,request_id=None,created=None,from_state=None):
        current=db.execute('SELECT state FROM workflow_tasks WHERE id=?',(str(task_id),)).fetchone()
        from_state=from_state if from_state is not None else (current['state'] if current else None)
        db.execute('''INSERT INTO workflow_events
            (task_id,request_id,event_type,from_state,to_state,details_json,created)
            VALUES (?,?,?,?,?,?,?)''',
            (str(task_id),request_id,event_type,from_state,to_state,
             json.dumps(details or {},ensure_ascii=False,separators=(',',':')),
             float(time.time() if created is None else created)))

    def events(self,task_id,limit=100):
        with closing(self.connect()) as db:
            rows=db.execute('''SELECT seq,request_id,event_type,from_state,to_state,details_json,created
                FROM workflow_events WHERE task_id=? ORDER BY seq DESC LIMIT ?''',
                (str(task_id),max(1,min(int(limit),500)))).fetchall()
        result=[]
        for row in reversed(rows):
            item=dict(row);item['details']=json.loads(item.pop('details_json'));result.append(item)
        return result

    def list(self,thread_id=None,limit=100):
        with closing(self.connect()) as db:
            if thread_id:
                rows=db.execute('''SELECT * FROM workflow_tasks WHERE source_thread_id=? OR target_thread_id=?
                    ORDER BY created DESC LIMIT ?''',(str(thread_id),str(thread_id),max(1,min(int(limit),300)))).fetchall()
            else:
                rows=db.execute('SELECT * FROM workflow_tasks ORDER BY created DESC LIMIT ?',
                                (max(1,min(int(limit),300)),)).fetchall()
        return [self.clean(row) for row in rows]

    def claim_due(self,limit=8):
        now=float(self.clock());claimed=[]
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            # After a prolonged shutdown, never burst through old schedules.
            expired=db.execute("SELECT id FROM workflow_tasks WHERE state='scheduled' AND run_at<?",
                               (now-1800,)).fetchall()
            for expired_row in expired:
                db.execute("UPDATE workflow_tasks SET state='needs_review',last_error=?,updated=? WHERE id=?",
                           ('执行时间已超过 30 分钟，请确认是否仍需执行',now,expired_row['id']))
                self._record_transition(db,expired_row['id'],'schedule.expired','needs_review',
                                        {'thresholdSeconds':1800},created=now,from_state='scheduled')
            rows=db.execute('''SELECT candidate.id,candidate.state FROM workflow_tasks candidate
                WHERE ((candidate.state='scheduled' AND candidate.run_at<=?) OR
                   (candidate.state='dispatching' AND candidate.lease_until<=?)) AND
                  NOT EXISTS (
                    SELECT 1 FROM workflow_tasks earlier
                    WHERE earlier.target_thread_id=candidate.target_thread_id
                      AND earlier.target_engine=candidate.target_engine
                      AND earlier.id!=candidate.id
                      AND earlier.state NOT IN ('completed','failed','cancelled')
                      AND (earlier.state!='needs_review' OR earlier.receipt_json IS NOT NULL)
                      AND (earlier.state IN ('dispatching','accepted','queued','running','reconciling')
                           OR (earlier.state='needs_review' AND earlier.receipt_json IS NOT NULL)
                           OR (earlier.state='scheduled' AND
                               (earlier.run_at<candidate.run_at OR
                                (earlier.run_at=candidate.run_at AND (earlier.created<candidate.created OR
                                 (earlier.created=candidate.created AND earlier.id<candidate.id))))))
                  )
                ORDER BY candidate.run_at,candidate.created LIMIT ?''',
                (now,now,max(1,min(int(limit),32)))).fetchall()
            for row in rows:
                cursor=db.execute('''UPDATE workflow_tasks SET state='dispatching',attempts=attempts+1,
                    lease_until=?,updated=? WHERE id=? AND ((state='scheduled' AND run_at<=?) OR
                    (state='dispatching' AND lease_until<=?))''',
                    (now+self.lease_seconds,now,row['id'],now,now))
                if cursor.rowcount:
                    task=db.execute('SELECT * FROM workflow_tasks WHERE id=?',(row['id'],)).fetchone()
                    self._record_transition(db,row['id'],'effect.claimed','dispatching',
                                            {'attempt':task['attempts']},created=now,from_state=row['state'])
                    claimed.append(self.clean(task))
        return claimed

    def defer(self,task_id,run_at,error=''):
        now=float(self.clock())
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT state FROM workflow_tasks WHERE id=?',(str(task_id),)).fetchone()
            changed=db.execute('''UPDATE workflow_tasks SET state='scheduled',run_at=?,lease_until=0,
                attempts=MAX(0,attempts-1),
                last_error=?,updated=? WHERE id=? AND state NOT IN ('completed','failed','cancelled')''',
                (float(run_at),str(error or '')[:500] or None,now,str(task_id))).rowcount
            if changed:self._record_transition(db,task_id,'effect.deferred','scheduled',{'retryAt':float(run_at),'reason':str(error or '')[:300]},created=now,from_state=old['state'] if old else None)
        return self.get(task_id)

    def dispatch_result(self,task_id,receipt,status):
        now=float(self.clock());status=int(status)
        receipt_json=json.dumps(receipt,ensure_ascii=False,separators=(',',':')) if receipt is not None else None
        if isinstance(receipt,dict) and receipt.get('deferred'):
            return self.defer(task_id,receipt.get('retryAt',now+30),receipt.get('message','目标会话暂不可执行'))
        if status>=500:
            return self.mark_uncertain(task_id,'网关返回服务错误，派发结果未知；正在用原请求标识核对')
        elif status>=400:state='failed'
        elif not isinstance(receipt,dict):state='reconciling'
        elif receipt.get('error'):state='failed'
        else:state={'queued':'queued','started':'running','accepted':'reconciling'}.get(receipt.get('mode'),'reconciling')
        turn=((receipt or {}).get('result') or {}).get('turn') or {}
        uncertain=now if state=='reconciling' and not turn.get('id') else None
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT state FROM workflow_tasks WHERE id=?',(str(task_id),)).fetchone()
            changed=db.execute('''UPDATE workflow_tasks SET state=?,receipt_json=?,turn_id=COALESCE(?,turn_id),
                lease_until=0,updated=?,last_error=?,uncertain_since=COALESCE(?,uncertain_since) WHERE id=? AND state='dispatching' ''',
                (state,receipt_json,turn.get('id'),now,
                 str((receipt or {}).get('error',{}).get('message',''))[:500] or None,uncertain,str(task_id))).rowcount
            if changed:self._record_transition(db,task_id,'effect.receipt',state,
                                                {'status':status,'mode':(receipt or {}).get('mode')},created=now,
                                                from_state=old['state'] if old else None)
        return self.get(task_id)

    def reconcile(self,task_id,receipt):
        if not isinstance(receipt,dict) or receipt.get('mode') not in ('queued','started','accepted'):
            return self.get(task_id)
        task=self.get(task_id)
        if not task or task['state'] in TERMINAL:return task
        state={'queued':'queued','started':'running','accepted':'reconciling'}[receipt['mode']]
        if task['state']=='running' and state!='running':return task
        turn=((receipt.get('result') or {}).get('turn') or {}).get('id')
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT state FROM workflow_tasks WHERE id=?',(str(task_id),)).fetchone()
            changed=db.execute('''UPDATE workflow_tasks SET state=?,receipt_json=?,turn_id=COALESCE(?,turn_id),
                updated=?,last_error=NULL,
                uncertain_since=CASE WHEN ? THEN COALESCE(uncertain_since,?) ELSE NULL END
                WHERE id=? AND state NOT IN ('completed','failed','cancelled')''',
                (state,json.dumps(receipt,ensure_ascii=False),turn,float(self.clock()),
                 1 if state=='reconciling' and not turn else 0,float(self.clock()),str(task_id))).rowcount
            if changed:self._record_transition(db,task_id,'receipt.reconciled',state,{'mode':receipt['mode']},
                                               created=float(self.clock()),from_state=old['state'] if old else None)
        return self.get(task_id)

    def mark_uncertain(self,task_id,error='派发结果未知，正在用原请求标识核对'):
        now=float(self.clock())
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT state FROM workflow_tasks WHERE id=?',(str(task_id),)).fetchone()
            changed=db.execute('''UPDATE workflow_tasks SET state='reconciling',lease_until=0,
                receipt_json=COALESCE(receipt_json,?),last_error=?,updated=?,
                uncertain_since=COALESCE(uncertain_since,?)
                WHERE id=? AND state IN ('dispatching','accepted','queued','reconciling')''',
                (json.dumps({'mode':'unknown','reconciliationRequired':True},ensure_ascii=False),
                 str(error or '')[:500],now,now,str(task_id))).rowcount
            if changed:self._record_transition(db,task_id,'effect.outcome_unknown','reconciling',
                                               {'reason':str(error or '')[:300]},created=now,
                                               from_state=old['state'] if old else None)
        return self.get(task_id)

    def review_uncertain(self,task_id,max_age=600,force=False):
        now=float(self.clock())
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT state FROM workflow_tasks WHERE id=?',(str(task_id),)).fetchone()
            changed=db.execute('''UPDATE workflow_tasks SET state='needs_review',lease_until=0,
                last_error='派发结果仍无法确认；为避免重复执行，已暂停自动处理',updated=?
                WHERE id=? AND state IN ('accepted','reconciling') AND uncertain_since IS NOT NULL
                  AND (? OR uncertain_since<=?)''',
                (now,str(task_id),1 if force else 0,now-max(0,float(max_age)))).rowcount
            if changed:self._record_transition(db,task_id,'reconciliation.expired','needs_review',
                                               {'thresholdSeconds':max(0,float(max_age))},created=now,
                                               from_state=old['state'] if old else None)
        return self.get(task_id)

    def advance_cursor(self,task_id,seq):
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE workflow_tasks SET event_cursor=?,updated=? WHERE id=? AND event_cursor<?',
                       (int(seq),float(self.clock()),str(task_id),int(seq)))

    def finish(self,task_id,state,error=''):
        if state not in TERMINAL:raise ValueError('invalid_terminal_state')
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            now=float(self.clock())
            old=db.execute('SELECT state FROM workflow_tasks WHERE id=?',(str(task_id),)).fetchone()
            changed=db.execute('''UPDATE workflow_tasks SET state=?,last_error=?,lease_until=0,updated=?,uncertain_since=NULL
                WHERE id=? AND state NOT IN ('completed','failed','cancelled')''',
                (state,str(error or '')[:500] or None,now,str(task_id))).rowcount
            if changed:self._record_transition(db,task_id,'turn.terminal',state,
                                               {'reason':str(error or '')[:300]},created=now,
                                               from_state=old['state'] if old else None)
        return self.get(task_id) if changed else self.get(task_id)

    def cancel(self,task_id):
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT state FROM workflow_tasks WHERE id=?',(str(task_id),)).fetchone()
            now=float(self.clock())
            changed=db.execute("UPDATE workflow_tasks SET state='cancelled',last_error=?,updated=?,uncertain_since=NULL "
                              "WHERE id=? AND state IN ('scheduled','needs_review') "
                              "AND (state!='needs_review' OR uncertain_since IS NULL)",
                              ('由用户取消',now,str(task_id))).rowcount
            if changed:self._record_transition(db,task_id,'command.cancelled','cancelled',{},created=now,
                                               from_state=old['state'] if old else None)
        task=self.get(task_id)
        if not changed and task and task['state']!='cancelled':raise ValueError('workflow_already_dispatched')
        return task

    def resume(self,task_id):
        current=self.get(task_id)
        if current and current.get('outcomeUnknown'):
            raise ValueError('workflow_outcome_unknown_requires_reconciliation')
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT state FROM workflow_tasks WHERE id=?',(str(task_id),)).fetchone()
            now=float(self.clock())
            changed=db.execute("UPDATE workflow_tasks SET state='scheduled',run_at=?,updated=?,last_error=NULL,"
                       "uncertain_since=NULL WHERE id=? AND state='needs_review' AND uncertain_since IS NULL",
                       (now,now,str(task_id))).rowcount
            if changed:self._record_transition(db,task_id,'command.resumed','scheduled',{},created=now,
                                               from_state=old['state'] if old else None)
        return self.get(task_id)

    def retry_reconciliation(self,task_id):
        now=float(self.clock())
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT state FROM workflow_tasks WHERE id=?',(str(task_id),)).fetchone()
            changed=db.execute("UPDATE workflow_tasks SET state='reconciling',updated=?,last_error=NULL,uncertain_since=? "
                       "WHERE id=? AND state='needs_review' AND uncertain_since IS NOT NULL",
                       (now,now,str(task_id))).rowcount
            if changed:self._record_transition(db,task_id,'command.reconcile_requested','reconciling',{},
                                               created=now,from_state=old['state'] if old else None)
        task=self.get(task_id)
        if task and task['state']!='reconciling':raise ValueError('workflow_not_uncertain')
        return task

    def active(self,limit=300):
        with closing(self.connect()) as db:
            rows=db.execute("SELECT * FROM workflow_tasks WHERE state IN "
                            "('accepted','queued','running','reconciling') ORDER BY updated LIMIT ?",(limit,)).fetchall()
        return [self.clean(row) for row in rows]


class WorkflowWorker:
    def __init__(self,store,dispatch,reconcile_events,interval=2.0,logger=None):
        self.store=store;self.dispatch=dispatch;self.reconcile_events=reconcile_events
        self.interval=float(interval);self.logger=logger or (lambda message:None)
        self.stop_event=threading.Event();self.thread=None;self.lock=threading.Lock()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():return
            self.thread=threading.Thread(target=self._run,name='workflow-worker',daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            try:self.tick()
            except Exception as exc:self.logger('workflow worker: '+type(exc).__name__)
            self.stop_event.wait(self.interval)

    def tick(self):
        for task in self.store.claim_due():
            try:
                receipt,status=self.dispatch(task)
                self.store.dispatch_result(task['id'],receipt,status)
            except Exception as exc:
                self.store.mark_uncertain(task['id'])
                self.logger('workflow dispatch deferred: '+type(exc).__name__)
        for task in self.store.active():
            try:self.reconcile_events(task)
            except Exception as exc:self.logger('workflow reconciliation deferred: '+type(exc).__name__)

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=self.interval+2)
