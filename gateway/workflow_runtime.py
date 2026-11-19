"""Workflow integration: metadata-only preflight, stable submission identity."""
import time


def build_instruction(task):
    text=task['instruction']
    if task['kind']=='handoff':
        source=task.get('source_thread_id') or ''
        engine=task.get('source_engine') or 'codex'
        text=('任务交接\n来源会话：'+engine+'/'+source+'\n'
              '以下是用户指定的交接说明；历史未自动附加。\n\n'+text)
    return text


def dispatch_task(task,ledger,preflight,submit,reconcile,clock=time.time):
    tid=task['target_thread_id'];cid=task['cid']
    # Reconciliation bypasses new admission checks: a timed-out request may
    # already be executing, even if the user later handed control to desktop.
    if (ledger.lookup(tid,cid) or {}).get('found'):
        return ledger.resolve(tid,cid,reconcile)
    reason=preflight(task)
    if reason:
        return {'deferred':True,'retryAt':clock()+30,'message':reason},202
    body={'engine':task['target_engine'],'text':build_instruction(task),
          'clientUserMessageId':cid}
    try:return ledger.run(tid,body,lambda:submit(tid,body),reconcile)
    except Exception:
        if (ledger.lookup(tid,cid) or {}).get('found'):
            receipt,status=ledger.resolve(tid,cid,reconcile)
            if status==409:return {'mode':'accepted','reconcileRequired':True},202
            return receipt,status
        raise


def observe_events(task,events):
    """Only a terminal event for this task's exact native turn can complete it."""
    turn_id=task.get('turn_id')
    high=int(task.get('event_cursor') or 0)
    terminal=None
    for event in events:
        seq=int(event.get('seq') or 0);high=max(high,seq)
        params=event.get('params') or {}
        tid=params.get('threadId') or event.get('tid')
        if tid!=task['target_thread_id']:continue
        turn=params.get('turn') or {}
        event_turn=turn.get('id') or params.get('turnId')
        if not turn_id or str(event_turn)!=str(turn_id):continue
        method=event.get('method') or ''
        status=str(turn.get('status') or '').lower()
        if method=='turn/failed' or (method=='turn/completed' and status in ('failed','interrupted','cancelled')):
            terminal=('failed','目标回合失败或被停止')
        elif method=='turn/completed':terminal=('completed','')
    return high,terminal
