"""Dispatch a Codex message once through Codex's durable native queue."""


def find_in_pages(fetch_page, client_id, max_pages=32):
    cursor=None
    seen_cursors=set()
    position=0
    length=0
    match=None
    match_position=0
    for _ in range(max_pages):
        response=fetch_page(cursor) or {}
        rows=response.get('data') or []
        for row in rows:
            position+=1
            if (match is None and isinstance(row,dict)
                    and row.get('clientUserMessageId')==client_id):
                match,match_position=row,position
        length+=len(rows)
        next_cursor=response.get('nextCursor')
        if not next_cursor:
            return match,match_position,length
        if next_cursor in seen_cursors:
            raise ValueError('queue_cursor_cycle')
        seen_cursors.add(next_cursor)
        cursor=next_cursor
    raise ValueError('queue_too_large')


def enqueue_once(app_server, thread_id, text, client_id, extra_input=None):
    """Submit once and report only evidence that currently exists.

    A successful queue/add acknowledgement confirms admission, not turn start.
    Unknown outcomes must be reconciled by client id, never submitted again.
    """
    result=app_server.queue_add(thread_id,text,client_id,extra_input=extra_input)
    queued_submission=(result or {}).get('queuedSubmission')
    queue_id=queued_submission.get('id') if isinstance(queued_submission,dict) else None
    try:
        match,position,length=app_server.queue_find_client_submission(thread_id,client_id)
    except Exception:
        match,position,length=None,0,0
    receipt={'clientUserMessageId':client_id,'queuedSubmission':queued_submission,
             'queueLen':length,'queueId':queue_id}
    if match is not None:
        receipt.update(mode='queued',position=position,queueId=match.get('id') or queue_id)
    else:
        receipt.update(mode='accepted',position=0)
    return receipt
