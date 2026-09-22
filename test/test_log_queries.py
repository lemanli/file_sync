"""日志筛选必须覆盖整次执行，报告和翻页不得依赖前端当前页。"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone.api import create_app
from fastapi.testclient import TestClient

class LogQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'db.sqlite3'
        app=create_app(self.db)
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO runs(id,task_id,status,started,snapshot) VALUES(1,1,'failed','now',?)",(json.dumps({'name':'测试','dryRun':False}),))
            events=[dict(action='copied',path=f'目录/file-{i}.txt',bytes=10,mappingIndex=1) for i in range(1201)]
            events += [dict(action='skipped',path='100%_ok.txt',bytes=0,mappingIndex=2),dict(action='ignored',path='.git',bytes=0,mappingIndex=2),dict(action='mapping_failed',error='failed',mappingIndex=2)]
            db.executemany('INSERT INTO events(run_id,created,detail) VALUES(1,?,?)',[('now',json.dumps(e)) for e in events])
        self.client=TestClient(app);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)

    def query(self,**params):
        r=self.client.get('/api/runs/1/logs',params=params);self.assertEqual(r.status_code,200,r.text);return r.json()

    def test_full_search_status_and_literal_wildcards(self):
        result=self.query(action='copied',q='file-1200')
        self.assertEqual(result['total'],1)
        self.assertIn('1200',result['items'][0]['detail'])
        self.assertEqual(self.query(q='%_')['total'],1)
        self.assertEqual(self.query(action='copied',mapping=2)['total'],0)
        self.assertEqual(self.query(q="' OR 1=1 --")['total'],0)

    def test_total_jump_order_and_boundaries(self):
        page=self.query(page=12,page_size=100,action='copied')
        self.assertEqual((page['total'],page['pages'],len(page['items'])),(1201,13,100))
        self.assertEqual(self.query(page=999,action='copied')['page'],13)
        self.assertEqual(self.query(action='copied',order='desc')['items'][0]['id'],1201)
        self.assertEqual(self.client.get('/api/runs/1/logs?page=0').status_code,422)
        self.assertEqual(self.client.get('/api/runs/1/logs?action=invalid').status_code,422)
        self.assertEqual(self.client.get('/api/runs/999/logs').status_code,404)

    def test_failed_run_report_and_legacy_endpoint(self):
        data=self.client.get('/api/runs/1/report').json()
        self.assertEqual(data['counts']['copied'],1201)
        self.assertEqual(data['counts']['ignored'],1)
        self.assertEqual(data['copiedBytes'],12010)
        self.assertEqual(data['totalEvents'],1204)
        self.assertFalse(data['partial'])
        self.assertEqual(len(self.client.get('/api/runs/1/events').json()),500)

    def test_legacy_single_mapping(self):
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO events(run_id,created,detail) VALUES(1,'now',?)",(json.dumps(dict(action='copied',path='legacy.txt',bytes=7)),))
        self.assertEqual(self.query(mapping=1,q='legacy')['total'],1)
        self.assertEqual(self.query(mapping=2,q='legacy')['total'],0)

    def test_safety_abort_keeps_prior_success_in_report(self):
        import time
        import os
        from unittest.mock import patch
        from standalone.engine import UnsafePathError
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();source=root/'source';source.mkdir()
            (source/'first').write_text('one');(source/'second').write_text('two')
            headers={'X-Sync-Local':'1'}
            task=self.client.post('/api/tasks',headers=headers,json=dict(name='safety',source=str(source),target=str(root/'target'),parallelism=1,batchFiles=100,maxRetries=0)).json()
            original=os.replace;calls=[]
            def replace(a,b):
                calls.append(str(b))
                if len(calls)==2:raise UnsafePathError('模拟同批后续路径失效')
                return original(a,b)
            with patch('standalone.engine.os.replace',side_effect=replace):
                response=self.client.post(f"/api/tasks/{task['id']}/run",headers=headers,json={})
                run_id=response.json()['runId']
                for _ in range(300):
                    run=self.client.get('/api/runs').json()[0]
                    if run['status']!='running':break
                    time.sleep(.01)
            self.assertEqual(run['status'],'failed')
            data=self.client.get(f'/api/runs/{run_id}/report').json()
            self.assertEqual(data['counts']['copied'],1)
            self.assertEqual(data['copiedBytes'],3)
            self.assertEqual(len(list((root/'target').iterdir())),1)
