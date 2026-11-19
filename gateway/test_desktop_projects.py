import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from desktop_projects import DesktopProjects


class DesktopProjectMembershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.catalog = DesktopProjects(self.home, ttl=0)
        self.host = 'local:' + str(self.home)
        self.video = r'C:\work\VideoWorkspace'
        self.extended_video = chr(92)*2 + '?' + chr(92) + self.video
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db, db:
            db.executescript('''
                CREATE TABLE projects (id TEXT PRIMARY KEY,name TEXT,position INTEGER);
                CREATE TABLE project_roots (project_id TEXT,position INTEGER,path TEXT);
                CREATE TABLE threads (id TEXT PRIMARY KEY,project_id TEXT,archived INTEGER,
                                      cwd TEXT,title TEXT,name TEXT,is_pinned INTEGER);
            ''')
            # An earlier project also includes this directory as a secondary
            # root; the video's primary root must win directory fallback.
            db.executemany('INSERT INTO projects VALUES (?,?,?)',
                           [('reverse','EngineeringWorkspace',0),('video','VideoWorkspace',1)])
            db.executemany('INSERT INTO project_roots VALUES (?,?,?)',
                           [('reverse',0,r'C:\work\EngineeringWorkspace'),
                            ('reverse',1,self.video),('video',0,self.video)])
            db.execute('INSERT INTO threads VALUES (?,NULL,0,?,?,?,0)',
                       ('exampleproject-2',self.extended_video,'ExampleProject-2','ExampleProject-2'))
        self.state = {'app-server-projects-migration-by-host': {
            self.host: {'projectsMigrated': True, 'threadAssignmentsMigrated': False,
                        'pendingThreadAssignmentIds': []}}}
        self.write_state()

    def tearDown(self):
        self.temp.cleanup()

    def write_state(self):
        (self.home / '.codex-global-state.json').write_text(
            json.dumps(self.state,ensure_ascii=False),encoding='utf-8')
        self.catalog.invalidate()

    def projected(self):
        return self.catalog.apply({'id':'exampleproject-2','engine':'codex',
                                   'cwd':self.extended_video,'archived':0})

    def test_catalog_migrated_but_assignments_incomplete_uses_primary_directory(self):
        row = self.projected()
        self.assertEqual(row['projectId'],'video')
        self.assertEqual(row['projectName'],'VideoWorkspace')
        self.assertEqual(row['projectGroupKey'],'project:video')
        self.assertEqual(row['projectMembershipSource'],'directory')
        self.assertEqual(row['cwd'],self.extended_video)
        self.assertEqual(row['archived'],0)

    def test_complete_membership_migration_makes_null_authoritative(self):
        self.state['app-server-projects-migration-by-host'][self.host]['threadAssignmentsMigrated']=True
        self.write_state()
        self.assertIsNone(self.projected()['projectId'])
        self.assertEqual(self.projected()['projectMembershipSource'],'projectless')

    def test_explicit_unassigned_thread_does_not_use_directory_fallback(self):
        self.state['projectless-thread-ids']=['exampleproject-2']
        self.write_state()
        self.assertIsNone(self.projected()['projectId'])

    def test_native_nonnull_assignment_overrides_matching_cwd(self):
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db, db:
            db.execute("UPDATE threads SET project_id='reverse' WHERE id='exampleproject-2'")
        self.assertEqual(self.projected()['projectId'],'reverse')
        self.assertEqual(self.projected()['projectMembershipSource'],'desktop')

    def test_pending_member_retains_legacy_assignment_after_global_completion(self):
        self.state['app-server-projects-migration-by-host'][self.host].update(
            threadAssignmentsMigrated=True,pendingThreadAssignmentIds=['exampleproject-2'])
        self.state['thread-project-assignments']={'exampleproject-2':{'projectKind':'local','projectId':'reverse'}}
        self.write_state()
        self.assertEqual(self.projected()['projectId'],'reverse')

    def test_legacy_assignment_ignored_after_membership_migration_completes(self):
        self.state['app-server-projects-migration-by-host'][self.host]['threadAssignmentsMigrated']=True
        self.state['thread-project-assignments']={'exampleproject-2':{'projectKind':'local','projectId':'reverse'}}
        self.write_state()
        self.assertIsNone(self.projected()['projectId'])

    def test_revision_changes_when_membership_migration_completes(self):
        before=self.catalog.data()['revision']
        self.state['app-server-projects-migration-by-host'][self.host]['threadAssignmentsMigrated']=True
        self.write_state()
        self.assertNotEqual(before,self.catalog.data()['revision'])


if __name__=='__main__':
    unittest.main()
