"""Exercise real gateway route methods over a disposable loopback HTTP server."""
import ast
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from workflow_tasks import WorkflowTaskStore
from rpc_contract import RPC_VERSION, authorize_http, contract_document, negotiate_ws_version


class WorkflowApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkflowTaskStore(Path(self.temp.name)/'workflows.sqlite')
        self.known = True
        source = ast.parse(Path(__file__).with_name('remote_server.py').read_text(encoding='utf-8-sig'))
        handler = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name=='Handler')
        routes = [node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name in ('do_GET','do_POST','_json')]
        create = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name=='workflow_create')
        namespace = dict(json=json, time=time, urlparse=urlparse, parse_qs=parse_qs,
                         GUARD=None, TRIAL=None, RECOVERY_JOURNAL=None, log=lambda *_:None,
                         rpc_contract_document=contract_document,
                         authorize_http=authorize_http, WS_RPC_VERSION=RPC_VERSION,
                         negotiate_ws_version=negotiate_ws_version,
                         workflow_store=lambda:self.store,
                         workflow_thread=lambda *_: {'id':'known'} if self.known else None)
        exec(compile(ast.Module(body=[create]+routes, type_ignores=[]), str(Path(__file__).with_name('remote_server.py')), 'exec'), namespace)

        class TestHandler(BaseHTTPRequestHandler):
            log_message = lambda *_:None
            _auth_level = lambda self, _qs: {'owner':'full','viewer':'view'}.get(self.headers.get('X-Auth'))
            _auth_ok = lambda self, qs: bool(self._auth_level(qs))
            _auth_fail = lambda self, _path: self._json({'error':'unauthorized'},401)
            _trial_gate = lambda *_:False
            _view_guard = lambda *_:False
            _accepts_gzip = lambda *_:False
            _client_disconnected = lambda *_:False
            _ws = lambda self, version=RPC_VERSION: self._json({'negotiatedVersion':version},200)
            def _read_body(self):
                return json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))

        for name in ('do_GET','do_POST','_json'):
            setattr(TestHandler,name,namespace[name])
        self.server = ThreadingHTTPServer(('127.0.0.1',0),TestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:'+str(self.server.server_port)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self,path,body=None,role='owner'):
        headers={'X-Auth':role,'Content-Type':'application/json'}
        req=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with self.opener.open(req,timeout=5) as response:
                return response.status,json.load(response)
        except urllib.error.HTTPError as response:
            return response.code,json.load(response)

    def payload(self,request_id='api-stable'):
        return dict(requestId=request_id,kind='scheduled',targetThreadId='target',targetEngine='codex',
                    instruction='Run focused checks',runAt=time.time()+3600)

    def test_duplicate_conflict_and_retry_after_target_archived(self):
        payload=self.payload()
        status,result=self.request('/api/workflows',payload)
        self.assertEqual(status,201)
        task_id=result['task']['id']
        status,detail=self.request('/api/workflows/'+task_id)
        self.assertEqual(status,200)
        self.assertEqual(detail['events'][0]['event_type'],'command.accepted')
        self.known=False
        status,retry=self.request('/api/workflows',payload)
        self.assertEqual(status,201)
        self.assertEqual(retry['task']['id'],task_id)
        conflict=dict(payload,instruction='Changed instruction')
        self.assertEqual(self.request('/api/workflows',conflict)[0],409)
        status,result=self.request('/api/workflows/'+task_id+'/cancel',{})
        self.assertEqual((status,result['task']['state']),(200,'cancelled'))
        self.assertEqual(len(self.request('/api/workflows')[1]['data']),1)

    def test_auth_timezone_and_explicit_cross_engine_handoff(self):
        self.assertEqual(self.request('/api/workflows',role='none')[0],401)
        self.assertEqual(self.request('/api/workflows',self.payload(),role='viewer')[0],403)
        invalid=dict(self.payload(),runAt='2027-01-01T12:00')
        self.assertEqual(self.request('/api/workflows',invalid)[0],400)
        handoff=dict(self.payload('cross-engine'),kind='handoff',sourceThreadId='claude-source',
                     sourceEngine='claude')
        status,result=self.request('/api/workflows',handoff)
        self.assertEqual(status,201)
        self.assertEqual(result['task']['source_engine'],'claude')
        self.assertEqual(result['task']['target_engine'],'codex')
        self.assertEqual(result['task']['instruction'],handoff['instruction'])

    def test_rpc_contract_endpoint_is_authenticated_and_reports_separate_versions(self):
        status, result = self.request('/api/rpc/contract')
        self.assertEqual(status, 200)
        self.assertEqual(result['http']['apiVersion'], 1)
        self.assertEqual(result['websocket']['version'], 1)
        def has_credential_field(value):
            if isinstance(value, dict):
                return any(key.lower() in ('token', 'secret', 'api_key') or has_credential_field(item)
                           for key, item in value.items())
            if isinstance(value, list):
                return any(has_credential_field(item) for item in value)
            return False
        self.assertFalse(has_credential_field(result))
        self.assertEqual(self.request('/api/rpc/contract', role='none')[0], 401)

    def test_websocket_handshake_rejects_unsupported_version_before_upgrade(self):
        status, result = self.request('/api/ws?rpc=1')
        self.assertEqual(status, 200)
        self.assertEqual(result['negotiatedVersion'], RPC_VERSION)
        status, result = self.request('/api/ws?rpc=99')
        self.assertEqual(status, 426)
        self.assertEqual(result['error']['code'], 'rpc_version_unsupported')
        self.assertEqual(self.request('/api/ws')[1]['negotiatedVersion'], RPC_VERSION)


if __name__=='__main__':
    unittest.main()
