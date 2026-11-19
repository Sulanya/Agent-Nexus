def receipt_from_events(events, tid, cid):
    for event in reversed(events):
        params = event.get('params') or {}
        if (params.get('threadId') or event.get('tid')) != tid:
            continue
        item = params.get('item') or {}
        client_id = item.get('clientId') or item.get('client_id') or item.get('clientUserMessageId')
        if item.get('type') == 'userMessage' and client_id == cid:
            turn_id = params.get('turnId')
            return ({'mode':'started', 'reconciled':True, 'clientUserMessageId':cid,
                     'result':{'turn':{'id':turn_id}}}, 200)
        for row in event.get('ev') or []:
            if event.get('tid') == tid and row.get('k') == 'user' and row.get('clientId') == cid:
                return ({'mode':'started','reconciled':True,'clientUserMessageId':cid,
                         'result':{'turn':{'id':row.get('turnId')}}},200)
    return None


def project_items(events, tid, limit=100, max_bytes=512*1024):
    rows = {}
    for event in events:
        params = event.get('params') or {}
        if params.get('threadId') != tid:
            continue
        if event.get('method') == 'item/commandExecution/outputDelta':
            item_id = params.get('itemId')
            if item_id and item_id in rows and not rows[item_id]['complete']:
                rows[item_id]['item']['aggregatedOutput'] = rows[item_id]['item'].get('aggregatedOutput','')+str(params.get('delta') or '')
                rows[item_id]['lastSeq'] = event['seq']
            continue
        method = event.get('method') or ''
        item = params.get('item') or {}
        item_id = params.get('itemId') or item.get('id')
        if not item_id:
            continue
        row = rows.setdefault(item_id, {'seq':event['seq'], 'lastSeq':event['seq'],
                  'turnId':params.get('turnId'), 'complete':False, 'prefixKnown':False, 'item':{'id':item_id}})
        row['lastSeq'] = event['seq']
        if params.get('turnId'):
            row['turnId'] = params['turnId']
        if method in ('item/started', 'item/completed'):
            row['item'] = dict(item)
            row['complete'] = method == 'item/completed'
            row['prefixKnown'] = True
        elif method == 'item/agentMessage/delta' and not row['complete']:
            row['item']['type'] = 'agentMessage'
            row['item']['text'] = row['item'].get('text', '') + str(params.get('delta') or '')
        elif method in ('item/reasoning/textDelta', 'item/reasoning/summaryTextDelta') and not row['complete']:
            row['item']['type'] = 'reasoning'
            field = 'summary' if 'summaryText' in method else 'content'
            existing = row['item'].get(field) or []
            text = ''.join(part if isinstance(part,str) else part.get('text','') for part in existing)
            row['item'][field] = [text+str(params.get('delta') or '')]
    import json
    output = []
    used = 0
    for row in reversed(list(rows.values())):
        if not row['turnId'] or not row['item'].get('type'):
            continue
        size = len(json.dumps(row, ensure_ascii=False).encode('utf-8'))
        if used+size > max_bytes or len(output) >= limit:
            break
        output.append(row)
        used += size
    return list(reversed(output))
