"""Persist desktop control intent and verify native subscription unloading."""
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class HandoffPolicy:
    def __init__(self,path):
        self.path=Path(path)
        self.lock=threading.RLock()
        self.stripes=[threading.RLock() for _ in range(64)]
        try:
            self.desktop=set(json.loads(self.path.read_text(encoding='utf-8')).get('desktopThreads',[]))
        except FileNotFoundError:
            self.desktop=set()

    def is_desktop(self,tid):
        with self.lock:return tid in self.desktop

    @contextmanager
    def guard(self,tid):
        with self.stripes[hash(tid)%len(self.stripes)]:yield

    def set_desktop(self,tid,value=True):
        with self.lock:
            desired=set(self.desktop)
            if value:desired.add(tid)
            else:desired.discard(tid)
            if desired==self.desktop:return
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temp=self.path.with_suffix('.tmp')
            temp.write_text(json.dumps({'version':1,'desktopThreads':sorted(desired)}),encoding='utf-8')
            os.replace(temp,self.path)
            self.desktop=desired


def native_loaded(request,tid):
    cursor=None
    while True:
        params={'limit':100}
        if cursor:params['cursor']=cursor
        result=request('thread/loaded/list',params,timeout=5)
        if any((item.get('id') if isinstance(item,dict) else item)==tid for item in result.get('data',[])):
            return True
        next_cursor=result.get('nextCursor')
        if not next_cursor:return False
        if next_cursor==cursor:raise RuntimeError('loaded list cursor did not advance')
        cursor=next_cursor


def unsubscribe_verified(request,tid,timeout=4):
    # Always use the native server, even if AN's resumed set is empty.
    result=request('thread/unsubscribe',{'threadId':tid},timeout=15)
    deadline=time.monotonic()+timeout
    while native_loaded(request,tid):
        if time.monotonic()>=deadline:return False
        time.sleep(.1)
    return result.get('status') in ('unsubscribed','notLoaded','notSubscribed')
