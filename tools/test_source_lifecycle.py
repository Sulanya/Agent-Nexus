"""Isolated source lifecycle smoke; no real agent account or model calls."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]

def smoke():
    import psutil
    gateway_pid=None
    with tempfile.TemporaryDirectory(prefix='an-source-smoke-') as temp:
        base=Path(temp).resolve();app=base/'app';home=base/'home';home.mkdir()
        shutil.copytree(ROOT/'gateway',app/'gateway',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        shutil.copytree(ROOT/'ui',app/'ui')
        (app/'tools').mkdir();shutil.copy2(ROOT/'tools/dev.py',app/'tools/dev.py')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        (app/'an.config.json').write_text(json.dumps({'host':'127.0.0.1','port':port,'allowFullAccess':False,'tunnel':False}))
        for p in (home/'AppData/Local',home/'AppData/Roaming',home/'.codex'):
            p.mkdir(parents=True,exist_ok=True)
        env=os.environ.copy()
        for k in list(env):
            if re.search(r'KEY|TOKEN|SECRET|PASSWORD|CODEX|CLAUDE|WORKBUDDY|AGENTREMOTE|AN_ROUTER',k,re.I):env.pop(k,None)
        env.update(USERPROFILE=str(home),HOME=str(home),CODEX_HOME=str(home/'.codex'),
            APPDATA=str(home/'AppData/Roaming'),LOCALAPPDATA=str(home/'AppData/Local'),
            PATH=str(Path(sys.executable).parent),PYTHONUTF8='1')
        def run(action):
            r=subprocess.run([sys.executable,'-X','utf8',str(app/'tools/dev.py'),action],
                env=env,cwd=app,capture_output=True,text=True,encoding='utf-8',timeout=60)
            if r.returncode:raise RuntimeError('source '+action+' failed: '+r.stdout[:400]+r.stderr[:200])
            return json.loads(r.stdout)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(path,token=None):
            headers={'X-Auth':token} if token else {}
            try:
                with opener.open(urllib.request.Request('http://127.0.0.1:'+str(port)+path,headers=headers),timeout=8) as r:
                    return r.status,r.read()
            except urllib.error.HTTPError as r:return r.code,r.read()
        try:
            started=run('start');gateway_pid=started['pid']
            status=run('status');assert status['owned']
            again=run('start');assert again['state']=='already-running' and again['pid']==gateway_pid
            token=(app/'gateway/.secrets/token.txt').read_text().strip()
            code,_=request('/api/whoami');assert code==401
            code,body=request('/api/whoami',token);assert code==200 and json.loads(body)['role']=='full'
            for path in ('/api/health','/api/rpc/contract','/api/workflows','/api/threads?limit=1'):
                code,_=request(path,token);assert code==200,path
            code,body=request('/',token);assert code==200 and b'setupSidebarResize' in body
            before=psutil.Process(gateway_pid).children(recursive=True)
            stopped=run('stop');assert stopped['state']=='stopped'
            gateway_pid=None
            time.sleep(1)
            remaining=[p.pid for p in before if p.is_running()]
            assert not remaining,'temporary gateway child processes still running'
            print(json.dumps({'check':'isolated-source-lifecycle','ok':True,
                'checks':['start','same-pid-idempotent-start','owned-status','unauthenticated-401',
                          'authenticated-full-role','health','rpc-contract','workflow','threads','ui',
                          'exact-owned-stop','child-process-cleanup'],
                'usesRealAgent':False,'callsModel':False}))
            return True
        finally:
            if gateway_pid:
                with __import__('contextlib').suppress(psutil.NoSuchProcess):
                    process=psutil.Process(gateway_pid);children=process.children(recursive=True)
                    process.terminate()
                    for child in children:
                        with __import__('contextlib').suppress(psutil.NoSuchProcess):child.terminate()
                    psutil.wait_procs([process]+children,timeout=8)

if __name__=="__main__":sys.exit(0 if smoke() else 1)
