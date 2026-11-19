"""Render the complete frontend against a synthetic HTTP/WebSocket fixture server."""
import argparse
from pathlib import Path
import os
import shutil
import socket
import subprocess
import tempfile
from demo_backend import start
ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser',default=os.environ.get('AN_EDGE_PATH') or shutil.which('chromium') or 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    args=parser.parse_args();out=ROOT/'docs/images';out.mkdir(parents=True,exist_ok=True)
    server=start()
    try:
        with tempfile.TemporaryDirectory(prefix='an-complete-demo-') as temp:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0));debug_port=probe.getsockname()[1]
            flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
            proc=subprocess.Popen([args.browser,'--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--hide-scrollbars','--remote-debugging-address=127.0.0.1','--remote-debugging-port='+str(debug_port),'--user-data-dir='+str(Path(temp)/'browser'),'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)
            try:
                subprocess.run(['node',str(ROOT/'tools/capture_demo.cjs'),str(debug_port),'http://127.0.0.1:'+str(server.server_port),str(out)],check=True,timeout=80)
            finally:
                import psutil
                try:
                    parent=psutil.Process(proc.pid);children=parent.children(recursive=True);parent.terminate()
                    for child in children:
                        try:child.terminate()
                        except psutil.NoSuchProcess:pass
                    psutil.wait_procs([parent]+children,timeout=5)
                except psutil.NoSuchProcess:pass
    finally:
        server.stopping.set();server.shutdown();server.server_close()

if __name__=='__main__':main()
