import ast
import unittest
from pathlib import Path
from thread_visibility import is_internal_thread

class ThreadVisibilityTests(unittest.TestCase):
    def test_subagents_hidden_even_when_named_or_pinned(self):
        for row in [{'threadSource':'subagent','name':'review','pinned':True},
                    {'source':'{"subagent":{"thread_spawn":{"parent_thread_id":"p"}}}'},
                    {'source':{'subagent':{'other':'guardian'}}}, {'source':'subagent'}]:
            self.assertTrue(is_internal_thread(row))

    def test_real_chats_empty_drafts_and_exec_are_retained(self):
        for row in [{'source':'vscode','threadSource':'user'}, {'source':'exec'},
                    {'source':'cli'}, {'name':'','preview':'','pendingTranscript':True},
                    {'source':'malformed'}]:
            self.assertFalse(is_internal_thread(row))

    def test_production_list_does_not_bypass_filter_with_all(self):
        source=ast.parse(Path(__file__).with_name('remote_server.py').read_text(encoding='utf-8-sig'))
        fn=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='threads_snapshot_payload')
        class Catalog:
            def data(self):return {'nativeThreads':{},'projects':[]}
        ns={'_index_snapshot':lambda:{'threads':[{'id':'child','source':{'subagent':{}},'visible':False},
                                                {'id':'user','source':'vscode','visible':True}]},
            'desktop_project_catalog':lambda:Catalog(),'is_internal_thread':is_internal_thread}
        # Reach the returned projection with a minimal, deterministic shell.
        import time,copy
        ns.update(time=time,copy=copy,queue_counts=lambda:{},AS=type('AS',(),{'resumed':set()})(),
                  history_has_items=lambda _:False,thread_group_apply=lambda *_:None,
                  _index_lock_fields=lambda *_:(True,False,None),runtime_status_payload=lambda *_a,**_k:{},
                  _indexer_health=lambda _:{'stale':False},log=lambda *_:None)
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<list>','exec'),ns)
        for show_all in (False,True):
            result=ns['threads_snapshot_payload'](show_all=show_all)
            self.assertEqual([r['id'] for r in result['data']],['user'])

if __name__=='__main__':unittest.main()
