"""暂停必须在文件边界生效，且修改不能改变既有同步范围。"""
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from fastapi.testclient import TestClient
from standalone.api import create_app
from standalone.control import RunControl, RunInterrupted


class ControlTests(unittest.TestCase):
    def test_close_wakes_paused_waiter(self):
        control=RunControl({},lambda status:None)
        control.pause()
        result=[]
        def wait():
            try:control.checkpoint()
            except RunInterrupted:result.append('interrupted')
        thread=threading.Thread(target=wait);thread.start()
        control.close();thread.join(2)
        self.assertEqual(result,['interrupted'])

    def test_pause_edit_append_resume(self):
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';src.mkdir()
            for name in ['a','b','c']:(src/name).write_text(name)
            extra=root/'extra';extra.mkdir();(extra/'d').write_text('d')
            dst=root/'dst';headers={'X-Sync-Local':'1'}
            entered=threading.Event();release=threading.Event()
            original=shutil.copymode
            def copying(*args,**kwargs):
                if not entered.is_set():
                    entered.set()
                    if not release.wait(5):raise TimeoutError('测试未释放文件')
                return original(*args,**kwargs)
            with TestClient(create_app(root/'state/db')) as client, patch('standalone.engine.shutil.copymode',side_effect=copying):
                task=dict(name='暂停测试',source=str(src),target=str(dst),parallelism=1,batchFiles=1,syncStrategy=getattr(self,'strategy','AUTO'))
                tid=client.post('/api/tasks',json=task,headers=headers).json()['id']
                rid=client.post(f'/api/tasks/{tid}/run',json={},headers=headers).json()['runId']
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertEqual(client.post(f'/api/runs/{rid}/pause',json={},headers=headers).json()['status'],'pausing')
                    self.assertEqual(client.put(f'/api/tasks/{tid}',json=task,headers=headers).status_code,409)
                    release.set()
                    for _ in range(100):
                        run=client.get('/api/runs').json()[0]
                        if run['status']=='paused':break
                        time.sleep(.02)
                    self.assertEqual(run['status'],'paused',run)
                    before=list(dst.iterdir());time.sleep(.1)
                    self.assertEqual(list(dst.iterdir()),before)
                    saved=client.get('/api/tasks').json()[0]
                    bad=dict(saved,pathMappings=[dict(source=str(src),target=str(root/'other'))])
                    self.assertEqual(client.put(f'/api/tasks/{tid}',json=bad,headers=headers).status_code,409)
                    self.assertEqual(client.delete(f'/api/tasks/{tid}',headers=headers).status_code,409)
                    saved.pop('source',None);saved.pop('target',None)
                    saved['bandwidthLimit']=1024
                    saved['pathMappings'].append(dict(source=str(extra),target=str(root/'extra-backup')))
                    response=client.put(f'/api/tasks/{tid}',json=saved,headers=headers)
                    self.assertEqual(response.status_code,200,response.text)
                    client.post(f'/api/runs/{rid}/resume',json={},headers=headers)
                    for _ in range(200):
                        run=client.get('/api/runs').json()[0]
                        if run['status'] not in ('running','pausing','paused'):break
                        time.sleep(.02)
                    self.assertEqual(run['status'],'success',run)
                    self.assertEqual((root/'extra-backup/d').read_text(),'d')
                    self.assertEqual(len(list(dst.iterdir())),3)
                    events=client.get(f'/api/runs/{rid}/events',params={'action':'config_updated'}).json()
                    self.assertTrue(events)
                finally:
                    release.set()
                    client.post(f'/api/runs/{rid}/resume',json={},headers=headers)


class MigrationTests(unittest.TestCase):
    def test_backup_wal_and_no_overwrite(self):
        spec=importlib.util.spec_from_file_location('migration',Path(__file__).resolve().parents[1]/'scripts/migrate_data.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'old.db';dest=root/'new/db'
            with closing(sqlite3.connect(source)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript("CREATE TABLE tasks(config TEXT);CREATE TABLE runs(id INTEGER PRIMARY KEY,status TEXT,ended TEXT,error TEXT);CREATE TABLE events(run_id INTEGER,created TEXT,detail TEXT);INSERT INTO tasks VALUES('{}');INSERT INTO runs(id,status) VALUES(1,'success');INSERT INTO events(detail) VALUES('test');")
                result=module.migrate(source,dest)
                self.assertEqual(result,dict(tasks=1,runs=1,events=1,interrupted=0))
                with self.assertRaises(ValueError):module.migrate(source,dest)
                db.execute("UPDATE runs SET status='paused'");db.commit()
                recovered=module.migrate(source,root/'recovered.db')
                self.assertEqual(recovered['interrupted'],1)
                self.assertEqual(db.execute('SELECT status FROM runs').fetchone()[0],'paused')
                with closing(sqlite3.connect(root/'recovered.db')) as recovered_db:
                    self.assertEqual(recovered_db.execute('SELECT status FROM runs').fetchone()[0],'interrupted')
                    self.assertEqual(recovered_db.execute('SELECT COUNT(*) FROM events').fetchone()[0],2)


class DirectoryControlTests(ControlTests):
    strategy = 'COMPARE_METADATA'
