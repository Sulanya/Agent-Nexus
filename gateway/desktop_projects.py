import copy
import hashlib
import json
import ntpath
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path


def path_key(value):
    value = str(value or '').replace('/', '\\')
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value)) if value else ''


class DesktopProjects:
    def __init__(self, home, ttl=.25):
        self.home = Path(home)
        self.ttl = ttl
        self.lock = threading.RLock()
        self.cached = None
        self.checked = 0

    def data(self):
        with self.lock:
            return self._read_data()

    def _read_data(self):
        with self.lock:
            if self.cached is not None and time.monotonic()-self.checked < self.ttl:
                return self.cached
        try:
            state = json.loads((self.home/'.codex-global-state.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            if self.cached is not None:
                return self.cached
            state = {}
        host = 'local:'+str(self.home)
        all_mappings = state.get('app-server-project-id-by-legacy-project-id-by-host') or {}
        mapping = next((value for key, value in all_mappings.items() if path_key(key[6:]) == path_key(self.home) and key.startswith('local:')), {})
        projects = []
        memberships = {}
        native_threads = {}
        native_catalog = False
        databases = list(self.home.glob('state_*.sqlite'))
        if databases:
            database = max(databases, key=lambda path:path.stat().st_mtime_ns)
            try:
                with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
                    db.execute('BEGIN')
                    roots = {}
                    for project_id, position, path in db.execute('SELECT project_id,position,path FROM project_roots ORDER BY position'):
                        roots.setdefault(project_id, []).append(path)
                    for project_id, name, position in db.execute('SELECT id,name,position FROM projects ORDER BY position,id'):
                        paths = roots.get(project_id) or []
                        projects.append(self.project(project_id, name, paths, position))
                    native_catalog = True
                    memberships = dict(db.execute('SELECT id,project_id FROM threads WHERE project_id IS NOT NULL'))
                    columns = {row[1] for row in db.execute('PRAGMA table_info(threads)')}
                    wanted = [key for key in ('id','project_id','is_pinned','archived','cwd','title','name','source','rollout_path',
                              'updated_at','recency_at','thread_source') if key in columns]
                    native_threads = {str(row[0]):dict(zip(wanted,row)) for row in
                                      db.execute('SELECT '+','.join(wanted)+' FROM threads')}
            except sqlite3.Error:
                if self.cached is not None:
                    return self.cached
        if not native_catalog:
            legacy = state.get('local-projects') or {}
            order = [key for key in state.get('project-order') or [] if key in legacy]
            order += [key for key in legacy if key not in order]
            for position, project_id in enumerate(order):
                project = legacy[project_id]
                paths = project.get('rootPaths') or []
                if paths:
                    projects.append(self.project(mapping.get(project_id, project_id), project.get('name') or ntpath.basename(paths[0]), paths, position))
        by_id = {project['id']:project for project in projects}
        projectless = set(state.get('projectless-thread-ids') or [])
        migration=next((value for key,value in (state.get('app-server-projects-migration-by-host') or {}).items()
                        if key.startswith('local:') and path_key(key[6:])==path_key(self.home)),{})
        pending=set(migration.get('pendingThreadAssignmentIds') or [])
        # Project catalog migration and per-thread membership migration are
        # separate desktop phases. SQL NULL is definitive only after the
        # membership phase; until then desktop still falls back to cwd.
        native_authority=bool(migration.get('threadAssignmentsMigrated'))
        hosts = state.get('thread-project-membership-host-ids') or {}
        for tid, assignment in (state.get('thread-project-assignments') or {}).items():
            if tid in memberships or not isinstance(assignment, dict):
                continue
            if native_authority and tid in native_threads and tid not in pending:
                continue
            if assignment.get('projectKind') != 'local' or hosts.get(tid, 'local') not in ('local', host):
                continue
            legacy_id = assignment.get('projectId')
            canonical = mapping.get(legacy_id, legacy_id)
            if canonical in by_id:
                memberships[tid] = canonical
        orders = {mapping.get(key,key):[str(x) for x in value] for key,value in
                  (state.get('sidebar-project-thread-orders') or {}).items() if isinstance(value,list)}
        value = {'projects':projects, 'byId':by_id, 'memberships':memberships,
                 'projectless':projectless, 'nativeThreads':native_threads, 'threadOrders':orders,
                 'nativeAuthority':native_authority,'pendingMigration':sorted(pending),
                 'pinned':[str(x) for x in state.get('pinned-thread-ids') or []]}
        value['revision'] = hashlib.sha256(json.dumps({**value,'projectless':sorted(projectless)},
            sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()[:20]
        with self.lock:
            self.cached = value
            self.checked = time.monotonic()
        return value

    @staticmethod
    def project(project_id, name, paths, position):
        primary=paths[0] if paths else ''
        return {'id':project_id, 'name':name, 'path':primary, 'rootPaths':paths,
                'dirName':ntpath.basename(primary), 'position':position,
                'extra':[{'path':path,'name':ntpath.basename(path)} for path in paths[1:]]}

    def projects(self):
        return copy.deepcopy(self.data()['projects'])

    def invalidate(self):
        with self.lock:
            self.checked=0
            self.signature=None

    def apply(self, thread, catalog=None):
        if not isinstance(thread, dict) or thread.get('engine', 'codex') != 'codex':
            return thread
        if 'cwdOriginal' in thread:
            thread['cwd'] = thread['cwdOriginal']
        thread.pop('moved', None)
        data = self.data() if catalog is None else catalog
        tid = thread.get('id')
        native = data.get('nativeThreads',{}).get(tid) or {}
        project_id = data['memberships'].get(tid)
        if not native and not project_id:
            project_id = thread.get('projectId')
        explicit = bool(project_id)
        authoritative_null=bool(native and data.get('nativeAuthority') and tid not in data.get('pendingMigration',[]))
        if not project_id and tid not in data['projectless'] and not authoritative_null:
            cwd = path_key(thread.get('cwd'))
            candidates = []
            for project in data['projects']:
                for position, root in enumerate(project['rootPaths']):
                    key = path_key(root)
                    if key and cwd and (cwd == key or cwd.startswith(key+'\\')):
                        candidates.append((len(key), position == 0, -project['position'], project['id']))
            if candidates:
                project_id = max(candidates)[3]
        project = data['byId'].get(project_id)
        thread['projectId'] = project['id'] if project else None
        thread['projectName'] = project['name'] if project else ''
        thread['projectRoot'] = project['path'] if project else ''
        thread['projectRootPaths'] = project['rootPaths'] if project else []
        thread['projectPosition'] = project['position'] if project else None
        thread['projectGroupKey'] = 'project:'+project['id'] if project else 'projectless'
        thread['projectMembershipSource'] = 'desktop' if explicit else ('projectless' if tid in data['projectless'] or authoritative_null else 'directory')
        thread['pinned'] = bool(native.get('is_pinned') or tid in data.get('pinned',[]))
        order=data.get('threadOrders',{}).get(project_id,[])
        thread['desktopThreadPosition'] = order.index(tid) if tid in order else None
        return thread
