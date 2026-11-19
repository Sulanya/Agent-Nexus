"""Portable, local-first source launcher. Private links never go to stdout."""
import argparse
import contextlib
import html
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser

ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'_run'
OWNER_FILE=RUN/'source-owner.json'
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))


def config():
    path=ROOT/'an.config.json'
    cfg=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    cfg['port']=int(os.environ.get('AGENTREMOTE_PORT') or cfg.get('port') or 8790)
    cfg['host']=os.environ.get('AGENTREMOTE_HOST') or cfg.get('host') or '127.0.0.1'
    if not 1<=cfg['port']<=65535:raise ValueError('Port outside 1..65535')
    return cfg


def request(path,cfg=None,auth=False,base=None):
    cfg=cfg or config()
    url=(base or ('http://127.0.0.1:'+str(cfg['port']))).rstrip('/')+path
    headers={}
    if auth:
        token=(ROOT/'gateway/.secrets/token.txt').read_text().strip()
        headers['X-Auth']=token
    with OPENER.open(urllib.request.Request(url,headers=headers),timeout=5) as response:
        return json.load(response)


def health(cfg):
    try:
        result=request('/api/health',cfg)
        return result if result.get('ok') and result.get('service')=='agent-nexus' else None
    except Exception:return None


def owns(h):
    import psutil
    try:
        proc=psutil.Process(int(h['pid']))
        args=proc.cmdline()
        script=str((ROOT/'gateway/remote_server.py').resolve())
        return any(os.path.normcase(str(Path(arg).resolve()))==os.path.normcase(script)
                   for arg in args if not arg.startswith('-'))
    except (psutil.Error,KeyError,ValueError,OSError):return False


def start(cfg):
    RUN.mkdir(parents=True,exist_ok=True)
    with open(RUN/'source-launch.lock','a+b') as lock:
        try:
            if os.name=='nt':
                import msvcrt
                lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise RuntimeError('Another source launcher is starting this instance')
        h=health(cfg)
        if h:
            if not owns(h):raise RuntimeError('Port belongs to another AN installation; choose another port')
            print(json.dumps({'ok':True,'state':'already-running','pid':h['pid']}));return
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1',cfg['port']))==0:
                raise RuntimeError('Port is occupied; refusing to launch another process')
        executable=Path(sys.executable)
        if os.name=='nt':
            executable=Path(getattr(sys,'_base_executable',sys.executable)).with_name('pythonw.exe')
            if not executable.is_file():raise RuntimeError('Base pythonw.exe is required for hidden Windows startup')
        env=os.environ.copy()
        env.update(AGENTREMOTE_HOST=cfg['host'],AGENTREMOTE_PORT=str(cfg['port']),PYTHONUTF8='1')
        # A base pythonw does not inherit venv site-packages automatically.
        if os.name=='nt' and sys.prefix!=sys.base_prefix:
            site=str(Path(sys.prefix)/'Lib/site-packages')
            env['PYTHONPATH']=site+(os.pathsep+env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        with open(RUN/'gateway.out.log','ab') as out,open(RUN/'gateway.err.log','ab') as err:
            proc=subprocess.Popen([str(executable),'-X','utf8',str(ROOT/'gateway/remote_server.py')],
                cwd=ROOT,env=env,stdout=out,stderr=err,stdin=subprocess.DEVNULL,
                creationflags=flags,start_new_session=os.name!='nt')
        OWNER_FILE.write_text(json.dumps({'pid':proc.pid,'root':str(ROOT),'port':cfg['port']}))
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            h=health(cfg)
            if h and int(h['pid'])==proc.pid and owns(h):
                print(json.dumps({'ok':True,'state':'running','pid':proc.pid}));return
            if proc.poll() is not None:raise RuntimeError('Gateway exited; inspect local _run/gateway.err.log')
            time.sleep(.5)
        raise RuntimeError('Gateway health not confirmed; inspect local logs before attempting another start')


def stop(cfg):
    h=health(cfg)
    if not h:raise RuntimeError('Health unavailable; refusing to guess which PID to terminate')
    if not owns(h):raise RuntimeError('Health PID does not belong to this source tree')
    status=request('/api/status',cfg,auth=True)
    busy=int(status.get('runningTurns') or 0)+int(status.get('pendingApprovals') or 0)
    for engine in ('claude','workbuddy'):
        info=status.get(engine) or {}
        busy+=int(info.get('running') or 0)+int(info.get('pending') or 0)
    for engine in ('codex','claude','workbuddy'):
        rows=request('/api/threads?engine='+engine+'&all=1&showAuto=1&limit=0',cfg,auth=True).get('data') or []
        busy+=sum(int(row.get('queueLen') or 0) for row in rows)
    if busy:raise RuntimeError('Active turns, approvals or queued messages exist; finish or stop them in the UI first')
    import psutil
    proc=psutil.Process(int(h['pid']))
    # Do not stop unrelated engines. Only stop descendants of this verified
    # owned gateway after validating the health PID again.
    children=proc.children(recursive=True)
    if not owns(h):raise RuntimeError('Gateway ownership changed during stop')
    proc.terminate()
    with contextlib.suppress(psutil.TimeoutExpired):proc.wait(timeout=10)
    for child in children:
        with contextlib.suppress(psutil.NoSuchProcess):child.terminate()
    psutil.wait_procs(children,timeout=5)
    if proc.is_running():raise RuntimeError('Gateway did not stop; no broad process kill attempted')
    print(json.dumps({'ok':True,'state':'stopped','pid':h['pid']}))


def connect(cfg,base=None,open_browser=False):
    h=health(cfg)
    if not h or not owns(h):raise RuntimeError('Start this source instance first')
    base=(base or ('http://127.0.0.1:'+str(cfg['port']))).rstrip('/')
    url=urllib.parse.urlsplit(base)
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('Supply only an HTTP(S) base URL, with no credentials, query or fragment')
    role=request('/api/whoami',cfg,auth=True,base=base).get('role')
    if role!='full':raise RuntimeError('Authenticated full-access request was not confirmed at this URL')
    owner=(ROOT/'gateway/.secrets/token.txt').read_text().strip()
    view_path=ROOT/'gateway/.secrets/view_token.txt'
    viewer=view_path.read_text().strip() if view_path.exists() else ''
    link=base+'/?k='+urllib.parse.quote(owner,safe='')
    readonly=base+'/?k='+urllib.parse.quote(viewer,safe='') if viewer else ''
    qr=''
    try:
        import qrcode
        import base64,io
        image=qrcode.make(link);buffer=io.BytesIO();image.save(buffer,format='PNG')
        qr='<img alt="Private access QR" src="data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode()+'">'
    except ImportError:pass
    RUN.mkdir(parents=True,exist_ok=True)
    page=RUN/'OWNER-CONNECT.html'
    page.write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>Agent Nexus private connection</title><h1>Private connection</h1>'
        '<p>This page contains credentials. Do not publish or screenshot it.</p><p><a href="'+html.escape(link,quote=True)+'">'
        'Open AN with full access</a></p>'+qr+'<p><a href="'+html.escape(readonly,quote=True)+'">Read-only access</a></p>',encoding='utf-8')
    if os.name!='nt':page.chmod(0o600)
    if open_browser:webbrowser.open(link)
    print(json.dumps({'ok':True,'privatePage':str(page),'opened':open_browser}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','status','stop','open','connect'])
    parser.add_argument('--base-url')
    args=parser.parse_args();cfg=config()
    if args.action=='start':start(cfg)
    elif args.action=='status':
        h=health(cfg);print(json.dumps({'ok':bool(h),'owned':owns(h) if h else False,'health':h}))
    elif args.action=='stop':stop(cfg)
    else:connect(cfg,args.base_url,open_browser=args.action=='open')


if __name__=='__main__':
    try:main()
    except Exception as exc:
        # Do not echo exception URLs or request objects containing tokens.
        print(json.dumps({'ok':False,'errorType':type(exc).__name__,
                          'message':str(exc) if isinstance(exc,(RuntimeError,ValueError)) else 'See local diagnostic logs'}))
        sys.exit(1)
