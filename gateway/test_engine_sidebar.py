import ast
import json
import tempfile
import unittest
import threading
import time
from pathlib import Path

SOURCE=ast.parse(Path(__file__).with_name('remote_server.py').read_text(encoding='utf-8-sig'))

class EngineSidebarTests(unittest.TestCase):
    def test_internal_claude_agents_are_not_sidebar_threads(self):
        cls=next(n for n in SOURCE.body if isinstance(n,ast.ClassDef) and n.name=='ClaudeAS')
        fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='list_threads')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);normal=root/'chat.jsonl';agent=root/'subagents'/'child.jsonl';named=root/'agent-old.jsonl'
            namespace={'claude_session_meta':lambda p:{'cwd':temp,'preview':'hello'},'time':time,
                       'thread_has_image':lambda _:False}
            exec(compile(ast.Module(body=[fn],type_ignores=[]),'<list_threads>','exec'),namespace)
            class Engine:
                ENGINE='claude'
                SOURCE='test'
                archived=set()
                lock=threading.Lock()
                pending={}
                queues={};turns={};_pending_session_meta={}
                def _cfg(self):return {}
                def sessions_map(self):return {'chat':normal,'child':agent,'agent-old':named}
                def session_registry(self):return {}
                def is_running(self,_):return False
                def thread_model(self,_):return ('test',None)
                def _apply_cfg(self,t,_cfg):return t
            rows=namespace['list_threads'](Engine())
            self.assertEqual([r['id'] for r in rows['data']],['chat'])
            engine=Engine();engine.pending={'draft':{'cwd':temp,'created':100}}
            rows=namespace['list_threads'](engine)['data']
            draft=next(r for r in rows if r['id']=='draft')
            self.assertTrue(draft['pendingTranscript'])

    def test_workbuddy_desktop_title_updates_without_transcript_change(self):
        names=('claude_session_meta','_claude_text_of')
        nodes=[n for n in SOURCE.body if isinstance(n,ast.FunctionDef) and n.name in names]
        desktop={'title':'Before','cwd':'native-root'}
        ns={'Path':Path,'json':json,'_claude_meta_cache':{},'_is_wb_path':lambda _:True,
            'wb_desktop_db_meta':lambda:{'session':dict(desktop)},'USER_HOME':Path('C:/Users/example-user'),
            '_wb_clean_text':lambda t:t,'_claude_img_ref':lambda _:False,'_wb_usage_of':lambda _:None}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<metadata>','exec'),ns)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'session.jsonl'
            path.write_text(json.dumps({'type':'message','role':'user','content':'Original prompt'}),encoding='utf-8')
            self.assertEqual(ns['claude_session_meta'](path)['name'],'Before')
            desktop['title']='Renamed in desktop'
            self.assertEqual(ns['claude_session_meta'](path)['name'],'Renamed in desktop')

if __name__=='__main__':unittest.main()
