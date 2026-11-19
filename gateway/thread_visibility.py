"""Separate user/automation chats from implementation-only child threads."""
import json


def is_internal_thread(row):
    if str(row.get('threadSource') or row.get('thread_source') or '').lower()=='subagent':
        return True
    source=row.get('source')
    if isinstance(source,str):
        if source.lower()=='subagent':return True
        try:source=json.loads(source)
        except (ValueError,TypeError):return False
    return isinstance(source,dict) and 'subagent' in source
