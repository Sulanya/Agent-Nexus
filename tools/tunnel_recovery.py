"""Recover this installation's quick tunnel, with no model or message sends."""
import argparse
import contextlib
import datetime
import json
import http.client
import ipaddress
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import urllib.parse

import psutil
import shutil

ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'_run'
EXE=Path(os.environ.get('AN_CLOUDFLARED') or shutil.which('cloudflared') or str(ROOT/'tools/cloudflared.exe'))
STATE=RUN/'tunnel-health.json'
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))
URL_PATTERN=re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
DNS_CACHE={}


def log(message):
    with (RUN/'tunnel-watchdog.log').open('a',encoding='utf-8') as stream:
        stream.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S ')+message+'\n')


def atomic_write(path,text):
    temp=path.with_name(path.name+'.tmp')
    temp.write_text(text,encoding='utf-8')
    os.replace(temp,path)


def request(url,token=None,timeout=8):
    headers={'X-Auth':token} if token else {}
    try:
        with OPENER.open(urllib.request.Request(url,headers=headers),timeout=timeout) as response:
            return response.status,response.read()
    except urllib.error.HTTPError as exc:
        return exc.code,exc.read(1024)
    except (OSError,TimeoutError) as exc:
        reason=getattr(exc,'reason',exc)
        host=urllib.parse.urlsplit(url).hostname or ''
        if isinstance(reason,socket.gaierror) and host.endswith('.trycloudflare.com'):
            return request_tunnel_via_dns(url,headers,timeout)
        return None,b''


def request_tunnel_via_dns(url,headers,timeout):
    # Corporate DNS can reject a valid quick tunnel. Resolve only this request
    # through DoH, preserving SNI and TLS verification; do not alter DNS/hosts.
    parsed=urllib.parse.urlsplit(url);host=parsed.hostname
    try:
        cached=DNS_CACHE.get(host)
        if not cached or cached[0]<time.time():
            resolver='https://doh.pub/dns-query?name='+urllib.parse.quote(host)+'&type=A'
            req=urllib.request.Request(resolver,headers={'Accept':'application/dns-json'})
            with OPENER.open(req,timeout=timeout) as response:answer=json.load(response)
            addresses=[str(ipaddress.IPv4Address(row['data'])) for row in answer.get('Answer',[]) if row.get('type')==1]
            if not addresses:return None,b''
            DNS_CACHE[host]=(time.time()+min(300,min(row.get('TTL',60) for row in answer['Answer'])),addresses)
        for address in DNS_CACHE[host][1]:
            connection=http.client.HTTPSConnection(host,timeout=timeout)
            connection._create_connection=lambda target,timeout=None,source_address=None:socket.create_connection(
                (address,target[1]),timeout,source_address)
            try:
                path=parsed.path or '/'
                if parsed.query:path+='?'+parsed.query
                connection.request('GET',path,headers=headers)
                response=connection.getresponse()
                return response.status,response.read()
            except OSError:continue
            finally:connection.close()
    except (OSError,ValueError,KeyError):pass
    return None,b''


def owned_processes():
    result=[]
    for process in psutil.process_iter(['pid','name']):
        if (process.info['name'] or '').lower()!='cloudflared.exe':continue
        try:
            if Path(process.exe()).resolve()!=EXE.resolve():continue
            args=process.cmdline()
            if 'http://127.0.0.1:8790' in args and 'tunnel' in args:result.append(process)
        except psutil.Error:continue
    return result


def inspect_process(process):
    ready=False;metric_start=None;port=None
    for connection in process.net_connections(kind='tcp'):
        if connection.status!='LISTEN' or connection.laddr.ip not in ('127.0.0.1','::1'):continue
        host='127.0.0.1' if connection.laddr.ip=='127.0.0.1' else '[::1]'
        code,body=request('http://'+host+':'+str(connection.laddr.port)+'/metrics',timeout=3)
        if code!=200:continue
        match=re.search(rb'^process_start_time_seconds ([^\r\n]+)',body,re.M)
        if not match:continue
        metric_start=float(match.group(1))
        if abs(metric_start-process.create_time())>2:continue
        port=connection.laddr.port
        ready=request('http://'+host+':'+str(port)+'/ready',timeout=3)[0]==200
        break
    return {'pid':process.pid,'createdAt':process.create_time(),'metricsPort':port,
            'metricsStartTime':metric_start,'ready':ready}


def last_log(path):
    try:
        with path.open('rb') as stream:
            stream.seek(max(0,path.stat().st_size-65536))
            return stream.read().decode('utf-8',errors='replace')
    except OSError:return ''


def terminal_registration_error(text):
    # A generic network outage must not rotate the user's URL. A registration
    # explicitly rejected by Cloudflare cannot recover with the old tunnel ID.
    registered=max(text.rfind('Registered tunnel connection'),text.rfind('registered tunnel connection'))
    terminal=max(text.rfind('Unauthorized: Tunnel not found'),text.rfind('Unauthorized: tunnel not found'))
    return terminal>=0 and terminal>registered


def recovery_reason(ready,public_ok,terminal,failures):
    if public_ok:return None
    if not ready and terminal:return 'registration_invalid'
    if not ready and failures>=3:return 'unready_repeatedly'
    return None


def read_state():
    try:return json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}


def publish(url,process,previous):
    token=(ROOT/'gateway/.secrets/token.txt').read_text().strip()
    view_path=ROOT/'gateway/.secrets/view_token.txt'
    view=view_path.read_text().strip() if view_path.exists() else ''
    # Existing private connection files are the only place full auth links go.
    links='FULL : '+url+'/?k='+token+'\nVIEW : '+url+'/?k='+view+'\n'
    old_links=(RUN/'LINKS.txt').read_text(encoding='utf-8') if (RUN/'LINKS.txt').exists() else ''
    atomic_write(RUN/'LINKS.txt',links)
    atomic_write(RUN/'TUNNEL-URL.txt',url+'\n')
    if old_links!=links or not (RUN/'OWNER-CONNECT.html').exists():
        import importlib.util
        spec=importlib.util.spec_from_file_location('an_owner_entry',ROOT/'tools/open-owner.py')
        entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
        entry.refresh(open_browser=False)
    status={'ok':True,'state':'ready','pid':process.pid,'createdAt':process.create_time(),
            'url':url,'previousUrl':previous,'failures':0,'updatedAt':time.time()}
    atomic_write(STATE,json.dumps(status,indent=2))
    return status


def replace_tunnel(process,health,reason):
    # Verify health/metrics belong to this exact process before termination.
    # There is no bulk taskkill and the gateway/Codex processes stay untouched.
    if not health.get('metricsPort') or health.get('pid')!=process.pid:
        raise RuntimeError('tunnel process has no verified local health endpoint')
    if abs(health['metricsStartTime']-process.create_time())>2:
        raise RuntimeError('tunnel health identity changed')
    if process not in owned_processes():raise RuntimeError('tunnel owner changed')
    log('隧道恢复：已核对 metrics PID '+str(process.pid)+'，原因 '+reason)
    process.terminate();process.wait(timeout=15)


def start_tunnel(previous):
    if owned_processes():raise RuntimeError('another tunnel is already running')
    stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    logfile=RUN/('cloudflared-'+stamp+'.log')
    output=(RUN/('cloudflared-'+stamp+'.out.log')).open('ab')
    try:
        process=subprocess.Popen([str(EXE),'tunnel','--url','http://127.0.0.1:8790',
            '--protocol','http2','--logfile',str(logfile)],cwd=ROOT,
            stdin=subprocess.DEVNULL,stdout=output,stderr=output,
            creationflags=subprocess.CREATE_NO_WINDOW|subprocess.DETACHED_PROCESS)
    finally:output.close()
    pending={'ok':False,'state':'starting','pid':process.pid,'url':None,'previousUrl':previous,
             'logfile':str(logfile),'lastRestartAt':time.time(),'updatedAt':time.time()}
    atomic_write(STATE,json.dumps(pending,indent=2))
    log('隧道启动 pid='+str(process.pid)+'；等待真实公网健康检查')
    url=''
    for attempt in range(45):
        if process.poll() is not None:raise RuntimeError('new tunnel exited')
        matches=URL_PATTERN.findall(last_log(logfile))
        if matches:url=matches[-1]
        if url:
            code,body=request(url+'/api/health',timeout=4)
            if code==200:
                try:remote=json.loads(body)
                except ValueError:remote={}
                if remote.get('ok') and remote.get('service')=='agent-nexus':
                    result=publish(url,process,previous)
                    result.update(logfile=str(logfile),lastRestartAt=pending['lastRestartAt'])
                    atomic_write(STATE,json.dumps(result,indent=2))
                    log('公网隧道已恢复 '+url+'；新入口写入本机连接文件')
                    return result
        time.sleep(1)
    pending.update(state='pending_verification',candidateUrl=url or None,updatedAt=time.time())
    atomic_write(STATE,json.dumps(pending,indent=2))
    raise RuntimeError('new tunnel is running but public health is not yet verified')


def check_and_recover():
    code,body=request('http://127.0.0.1:8790/api/health',timeout=5)
    if code!=200:return {'ok':False,'state':'gateway_unavailable'}
    state=read_state()
    previous=(RUN/'TUNNEL-URL.txt').read_text().strip() if (RUN/'TUNNEL-URL.txt').exists() else ''
    processes=owned_processes()
    if len(processes)>1:raise RuntimeError('multiple matching tunnels; refusing ambiguous recovery')
    if not processes:return start_tunnel(previous)
    process=processes[0];health=inspect_process(process)
    logfile=Path(state.get('logfile') or RUN/'cloudflared.log')
    if state.get('pid')!=process.pid:
        args=process.cmdline()
        if '--logfile' in args:logfile=Path(args[args.index('--logfile')+1])
    url=state.get('candidateUrl') if state.get('pid')==process.pid else None
    # The persisted URL can belong to a prior quick-tunnel process. Prefer
    # the URL emitted by the verified running process's own log.
    try:
        registered_urls=URL_PATTERN.findall(logfile.read_text(encoding='utf-8',errors='replace'))
    except OSError:
        registered_urls=[]
    if registered_urls:
        candidate=registered_urls[-1]
        candidate_code,candidate_body=request(candidate+'/api/health',timeout=8)
        try:candidate_health=json.loads(candidate_body)
        except ValueError:candidate_health={}
        if candidate_code==200 and candidate_health.get('ok') and candidate_health.get('service')=='agent-nexus':
            url=candidate
    url=url or previous
    remote_code,remote_body=request(url+'/api/health',timeout=8) if url else (None,b'')
    try:remote=json.loads(remote_body)
    except ValueError:remote={}
    public_ok=remote_code==200 and remote.get('ok') and remote.get('service')=='agent-nexus'
    if public_ok:
        result=publish(url,process,previous if url!=previous else state.get('previousUrl'))
        result.update(logfile=str(logfile),lastRestartAt=state.get('lastRestartAt',0))
        atomic_write(STATE,json.dumps(result,indent=2))
        return result
    failures=(state.get('failures',0) if state.get('pid')==process.pid else 0)+1
    reason=recovery_reason(health['ready'],False,terminal_registration_error(last_log(logfile)),failures)
    if reason:
        # Distinguish service loss from this machine losing general connectivity.
        network_ok=request('https://api.trycloudflare.com',timeout=6)[0] is not None
        cooldown=time.time()-state.get('lastRestartAt',0)<120
        if network_ok and not cooldown:
            replace_tunnel(process,health,reason)
            return start_tunnel(previous)
    result={**state,**health,'ok':False,'state':'degraded','url':previous,'failures':failures,
            'logfile':str(logfile),'reason':reason,'updatedAt':time.time()}
    atomic_write(STATE,json.dumps(result,indent=2))
    log('公网不可用；ready='+str(health['ready'])+'，失败次数='+str(failures)+'，恢复原因='+str(reason))
    return result


@contextlib.contextmanager
def singleton():
    import msvcrt
    with (RUN/'tunnel-recovery.lock').open('a+b') as guard:
        guard.seek(0)
        try:msvcrt.locking(guard.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            yield False;return
        try:yield True
        finally:
            guard.seek(0);msvcrt.locking(guard.fileno(),msvcrt.LK_UNLCK,1)


def main():
    with singleton() as acquired:
        if not acquired:return {'ok':False,'state':'recovery_already_running'}
        return check_and_recover()


if __name__=='__main__':
    try:
        result=main();print(json.dumps(result));raise SystemExit(0 if result.get('ok') else 1)
    except Exception as exc:
        log('恢复失败：'+type(exc).__name__)
        print(json.dumps({'ok':False,'error':type(exc).__name__}))
        raise SystemExit(1)
