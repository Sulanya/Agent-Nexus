import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from desktop_project_commands import create_native_project

class ProjectCreationTests(unittest.TestCase):
    def test_existing_root_does_not_call_rpc(self):
        with tempfile.TemporaryDirectory() as temp:
            project={'id':'existing','path':str(Path(temp).resolve())}
            with patch('desktop_projects.DesktopProjects') as catalog:
                catalog.return_value.data.return_value={'projects':[project]}
                self.assertEqual(create_native_project(temp,lambda *_a,**_k:self.fail('unexpected RPC'),temp),project)

    def test_create_readback_and_idempotency_key(self):
        with tempfile.TemporaryDirectory() as temp:
            project={'id':'created','path':str(Path(temp).resolve())}
            calls=[]
            def rpc(method,params,**kwargs):
                calls.append((method,params));return {'result':{'project':{'id':'created'}}}
            with patch('desktop_projects.DesktopProjects') as catalog:
                catalog.return_value.data.side_effect=[{'projects':[]},{'projects':[project]}]
                self.assertEqual(create_native_project(temp,rpc,temp),project)
            self.assertEqual(calls[0][0],'project/create')
            self.assertTrue(calls[0][1]['idempotencyKey'].startswith('an-project:'))

    def test_missing_directory_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):create_native_project(temp,None,str(Path(temp)/'missing'))

if __name__=='__main__':unittest.main()
