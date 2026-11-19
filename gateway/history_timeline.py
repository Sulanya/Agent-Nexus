import json
from pathlib import Path


def _text(parts):
    return '\n'.join(part if isinstance(part, str) else str(part.get('text') or '')
                     for part in (parts or []) if isinstance(part, (str, dict)))


def _item(raw, timestamp):
    kind = raw.get('type')
    item = {'id': raw.get('id'), 'timestamp': timestamp}
    if not item['id']:
        return None
    if kind == 'UserMessage':
        item.update(type='userMessage', clientId=raw.get('client_id'), content=raw.get('content') or [])
        text = _text(item['content']).lstrip()
        if text.startswith(('# AGENTS.md instructions', '<environment_context>', '<user_instructions>')):
            return None
    elif kind == 'AgentMessage':
        item.update(type='agentMessage', text=_text(raw.get('content')), phase=raw.get('phase'))
    elif kind == 'Reasoning':
        item.update(type='reasoning', summary=raw.get('summary_text') or [], content=raw.get('raw_content') or [])
    elif kind == 'CommandExecution':
        command = raw.get('command') or ''
        duration = raw.get('duration') or {}
        item.update(type='commandExecution', command=' '.join(command) if isinstance(command, list) else command,
                    status=str(raw.get('status') or '').lower(), exitCode=raw.get('exit_code'),
                    aggregatedOutput=str(raw.get('aggregated_output') or '')[:24000],
                    durationMs=(duration.get('secs', 0)*1000 + duration.get('nanos', 0)/1e6)
                    if isinstance(duration, dict) else 0)
    elif kind == 'FileChange':
        changes = raw.get('changes') or []
        if isinstance(changes, dict):
            changes = [dict(value if isinstance(value, dict) else {}, path=path) for path, value in changes.items()]
        item.update(type='fileChange', status=raw.get('status'), changes=changes)
    elif kind == 'McpToolCall':
        item.update(type='mcpToolCall', server=raw.get('server'), tool=raw.get('tool'),
                    arguments=raw.get('arguments'), result=raw.get('result'), error=raw.get('error'))
    elif kind == 'ContextCompaction':
        item['type'] = 'contextCompaction'
    else:
        return None
    return item


def read_timeline_window(path, end=None, limit=80, max_scan_bytes=16*1024*1024):
    """Read bounded native completion events; None requests the legacy fallback."""
    with Path(path).open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
        ceiling = size if end is None else max(0, min(int(end), size))
        window = min(2*1024*1024, max_scan_bytes)
        while True:
            start = max(0, ceiling-window)
            stream.seek(start)
            if start:
                stream.readline()
            offset = stream.tell()
            records = []
            while stream.tell() < ceiling:
                line_offset = stream.tell()
                line = stream.readline(ceiling-line_offset)
                if not line.endswith(b'\n'):
                    break
                if b'"event_msg"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                payload = record.get('payload') or {}
                if payload.get('type') in ('item_completed', 'task_started', 'task_complete', 'turn_aborted'):
                    records.append((line_offset, record))
            rows = []
            statuses = {}
            for line_offset, record in records:
                payload = record['payload']
                turn_id = payload.get('turn_id')
                if not turn_id:
                    continue
                kind = payload.get('type')
                if kind == 'task_started':
                    statuses[turn_id] = 'inProgress'
                elif kind in ('task_complete', 'turn_aborted'):
                    statuses[turn_id] = 'interrupted' if kind == 'turn_aborted' else 'completed'
                elif kind == 'item_completed':
                    item = _item(payload.get('item') or {}, record.get('timestamp'))
                    if item:
                        rows.append((line_offset, turn_id, item))
            if len(rows) >= limit or start == 0 or window >= max_scan_bytes:
                break
            window = min(window*2, max_scan_bytes)
    if not rows:
        return None
    selected = rows[-limit:]
    next_end = selected[0][0]
    first_turn = selected[0][1]
    if not any(item['type'] == 'userMessage' and turn_id == first_turn for _, turn_id, item in selected):
        anchor = next((row for row in rows if row[1] == first_turn and row[2]['type'] == 'userMessage'), None)
        if anchor and anchor[0] < next_end:
            selected.insert(0, anchor)
    rows = selected
    turns = []
    by_turn = {}
    messages = []
    for line_offset, turn_id, item in rows:
        turn = by_turn.get(turn_id)
        if turn is None:
            turn = {'id': turn_id, 'status': statuses.get(turn_id), 'items': []}
            by_turn[turn_id] = turn
            turns.append(turn)
        if any(previous['id'] == item['id'] for previous in turn['items']):
            continue
        turn['items'].append(item)
        messages.append({'id': item['id'], 'item': item, 'turnId': turn_id, 'turnStatus': turn['status'],
                         'ts': item['timestamp'], 'off': line_offset})
    return {'turns': turns, 'msgs': messages, 'nextEnd': next_end,
            'hasMore': next_end > 0, 'size': size, 'timeline': True}
