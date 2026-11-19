"""Native project assignment with durable read-back, without changing cwd."""
import time
import threading
import hashlib
from pathlib import Path

_thread_locks=[threading.Lock() for _ in range(64)]


def create_native_project(home,rpc,path,name=None):
    from desktop_projects import DesktopProjects,path_key
    root=Path(path)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('项目目录不存在或不是绝对路径')
    root=str(root.resolve())
    catalog=DesktopProjects(home,ttl=0)
    for project in catalog.data()['projects']:
        if path_key(project.get('path'))==path_key(root):return project
    key='an-project:'+hashlib.sha256(path_key(root).encode()).hexdigest()
    response=rpc('project/create',{'name':str(name or Path(root).name),'roots':[{'path':root}],
                                 'idempotencyKey':key},timeout=12)
    if not isinstance(response,dict) or response.get('error'):
        raise RuntimeError('原生项目创建未确认，请重试核对同一目录')
    result=response.get('result') or response
    project_id=(result.get('project') or {}).get('id')
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        for project in catalog.data()['projects']:
            if project.get('id')==project_id and path_key(project.get('path'))==path_key(root):return project
        time.sleep(.05)
    raise RuntimeError('原生项目持久化回读未确认，请重试核对同一目录')


def assign_thread(request, catalog, thread_id, project_id, timeout=3):
    lock=_thread_locks[hash(thread_id)%len(_thread_locks)]
    with lock:
        return _assign_thread(request,catalog,thread_id,project_id,timeout)


def _assign_thread(request, catalog, thread_id, project_id, timeout):
    data=catalog.data()
    if project_id is not None and project_id not in data['byId']:
        raise ValueError('桌面端工程已变化，请刷新后重试。')
    native=data.get('nativeThreads',{}).get(thread_id)
    if native is None:
        raise ValueError('会话尚未落盘，请发送首条消息后再移动。')
    if native.get('project_id')!=project_id:
        # Current Codex treats null as "field omitted"; the empty string is
        # the native clear operation and persists SQL NULL (isolated probe).
        reply=request('thread/metadata/update',{'threadId':thread_id,'projectId':project_id or ''},timeout=25)
        if reply.get('error'):
            raise RuntimeError('Codex 未确认工程修改，请刷新核对后重试。')
    deadline=time.monotonic()+timeout
    while True:
        catalog.invalidate()
        observed=catalog.data()
        row=observed.get('nativeThreads',{}).get(thread_id) or {}
        projected=catalog.apply({'id':thread_id,'cwd':row.get('cwd')},observed) if row else {}
        if row.get('project_id')==project_id and row and projected.get('projectId')==project_id:
            return {'ok':True,'projectId':project_id,'catalogRevision':observed.get('revision'),
                    'cwd':row.get('cwd'),'persisted':True}
        if time.monotonic()>=deadline:
            raise RuntimeError('工程修改已请求，但持久化回读未一致；请刷新核对，勿重复新建。')
        time.sleep(.1)
