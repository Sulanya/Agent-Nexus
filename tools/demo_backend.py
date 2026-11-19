"""Ephemeral read-only fixture backend for full-frontend screenshots. No agent access."""
import base64
import hashlib
import json
from pathlib import Path
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
NOW=1805515200
THREADS=[]
for i,(name,pid) in enumerate([('完善首页响应式布局','website'),('修复导航与键盘操作','website'),
                             ('整理安装说明','docs'),('检查导出流程','docs')]):
    THREADS.append(dict(id='demo-'+str(i),name=name,title=name,preview=name,engine='codex',
        cwd='/demo/'+pid,projectId=pid,projectGroupKey='project:'+pid,
        projectName='示例 · '+('网站项目' if pid=='website' else '文档工具'),
        projectPath='/demo/'+pid,projectMembershipSource='desktop',
        updatedAt=NOW-i*1800,createdAt=NOW-86400,recencyAt=NOW-i*1800,
        model='demo-model',reasoningEffort='medium',writable=True,running=False,
        loaded=True,queueLen=0,archived=False,status={'type':'idle'}))
TURNS=[{'id':'demo-turn','status':'completed','items':[
    {'id':'u1','type':'userMessage','content':[{'type':'text','text':'帮我检查首页在电脑、平板和手机上的布局。保留现有设计，修复窄屏溢出，并说明改动。'}]},
    {'id':'a1','type':'agentMessage','text':'我先检查页面布局和断点，再调整窄屏下的导航、卡片与操作区。'},
    {'id':'c1','type':'commandExecution','command':'npm run build','status':'completed',
     'exitCode':0,'durationMs':820,'aggregatedOutput':'✓ Build completed\n✓ Responsive layout checks passed'},
    {'id':'a2','type':'agentMessage','text':'### 布局已调整\n\n这次修改沿用现有样式，主要处理了三个地方：\n\n- **电脑：** 保留双栏布局和完整操作区。\n- **平板：** 缩小侧栏，给正文留出阅读空间。\n- **手机：** 切换单栏，避免横向溢出。\n\n构建检查已通过，可以继续补充要求。\n\n> 演示数据：项目、对话与执行结果均为虚构。'}]}]


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def handle(self):
        try:super().handle()
        except (ConnectionResetError,BrokenPipeError,TimeoutError):pass
    def log_message(self,*args):pass
    def send_json(self,value,status=200):
        data=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self):
        path=urlsplit(self.path).path
        if path=='/':
            data=(ROOT/'ui/index.html').read_bytes()
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        if path=='/api/ws':return self.websocket()
        if path=='/favicon.ico':return self.send_json({},404)
        from sys import path as module_path
        if str(ROOT/'gateway') not in module_path:module_path.insert(0,str(ROOT/'gateway'))
        if path=='/api/rpc/contract':
            from rpc_contract import contract_document
            return self.send_json(contract_document())
        if path=='/api/threads':
            projects=[{'id':pid,'name':name,'path':'/demo/'+pid,'roots':[{'path':'/demo/'+pid}],'position':i}
                      for i,(pid,name) in enumerate([('website','示例 · 网站项目'),('docs','示例 · 文档工具')])]
            return self.send_json({'data':THREADS,'total':len(THREADS),'projects':projects,'stale':False,'catalogRevision':'demo'})
        if path.startswith('/api/threads/'):
            if path.endswith('/recovery'):return self.send_json({'items':[],'data':[]})
            if path.endswith('/queue'):return self.send_json({'queue':[],'data':[],'queueLen':0})
            if path.endswith('/state'):return self.send_json({'running':False,'writable':True,'queueLen':0,'status':{'type':'idle'}})
            tid=path.split('/')[3];row=next((r for r in THREADS if r['id']==tid),THREADS[0])
            return self.send_json({'thread':dict(row,turns=TURNS,hasMore=False)})
        if path=='/api/whoami':return self.send_json({'role':'full'})
        if path=='/api/health':return self.send_json({'ok':True,'service':'agent-nexus-demo','wsContractVersion':1})
        if path=='/api/status':return self.send_json({'ok':True,'appserver':'ready','runningTurns':0,'pendingApprovals':0,'resumed':1})
        if path=='/api/models':return self.send_json({'models':[{'id':'demo-model','label':'演示模型','supportedReasoningEfforts':[{'reasoningEffort':'medium','description':'标准'}]}],'efforts':['low','medium','high'],'defaultModel':'demo-model','defaultEffort':'medium','provider':'demo'})
        if path=='/api/trial':return self.send_json({'enabled':False,'expired':False,'state':'disabled'})
        if path=='/api/settings':return self.send_json({'ok':True,'model':'demo-model','effort':'medium'})
        return self.send_json({'ok':True,'data':[],'pending':[],'roots':[],'projects':[],'enabled':False})
    def do_POST(self):
        # Fixture server cannot mutate the filesystem, call a model or reach a production service.
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        self.send_json({'error':{'code':'demo_readonly','message':'演示后端不执行操作'}},403)
    def websocket(self):
        key=self.headers.get('Sec-WebSocket-Key','')
        accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
        payload=json.dumps({'kind':'gateway','status':'ready'}).encode()
        self.wfile.write(bytes([0x81,len(payload)])+payload);self.wfile.flush()
        self.connection.settimeout(60)
        while not self.server.stopping.is_set():
            try:
                header=self.rfile.read(2)
                if len(header)<2:break
                op=header[0]&15;length=header[1]&127
                if length==126:length=struct.unpack('>H',self.rfile.read(2))[0]
                if length==127:length=struct.unpack('>Q',self.rfile.read(8))[0]
                if length>65536:break
                mask=self.rfile.read(4) if header[1]&128 else None
                data=self.rfile.read(length)
                if op==8:break
            except (OSError,TimeoutError):break


def start():
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True;server.stopping=threading.Event()
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server
