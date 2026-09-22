from contextlib import closing
"""跨文件系统比较与时间保留：所有操作限定在临时目录。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone.engine import synchronize
from standalone.api import Task

class FileTimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name).resolve();self.a=root/'a';self.b=root/'b';self.a.mkdir();self.b.mkdir();self.events=[]
        (self.a/'file').write_text('abc');(self.b/'file').write_text('xyz')
        os.utime(self.a/'file',ns=(1000000000000,1000000000000))
        os.utime(self.b/'file',ns=(1000000000000,1001900000000))
    def sync(self,**kw):
        return synchronize(dict(source=str(self.a),target=str(self.b),maxRetries=0,**kw),self.events.append)
    def test_new_api_defaults_and_legacy_checksum(self):
        task=Task(name='new',source=str(self.a),target=str(self.b))
        self.assertEqual(task.comparisonMode,'size');self.assertTrue(task.skipUnsupported)
        self.assertTrue(task.preserveTime);self.assertEqual(task.timeToleranceSeconds,2)
        self.assertEqual(Task(name='old',source=str(self.a),target=str(self.b),checksum=False).comparisonMode,'size_mtime')
        self.assertEqual(Task(name='old',source=str(self.a),target=str(self.b),checksum=True).comparisonMode,'sha256')
    def test_size_mode_ignores_time_and_never_hashes(self):
        with patch('standalone.engine.digest',side_effect=AssertionError('不应算哈希')):
            result=self.sync(comparisonMode='size')
        self.assertEqual(result['skipped'],1);self.assertEqual((self.b/'file').read_text(),'xyz')
        self.assertEqual(self.events[-1]['comparison']['method'],'size')
    def test_size_time_tolerance_boundary(self):
        self.assertEqual(self.sync(comparisonMode='size_mtime',timeToleranceSeconds=2)['skipped'],1)
        self.assertEqual(self.sync(comparisonMode='size_mtime',timeToleranceSeconds=1)['copied'],1)
    def test_hash_ignores_timestamp_and_detects_same_size_change(self):
        result=self.sync(comparisonMode='sha256')
        self.assertEqual(result['copied'],1)
        self.assertIn('verification',self.events[-1])
    def test_preserved_timestamp_read_back(self):
        (self.b/'file').unlink()
        self.sync(comparisonMode='size',preserveTime=True,timeToleranceSeconds=0)
        self.assertEqual((self.b/'file').stat().st_mtime_ns,(self.a/'file').stat().st_mtime_ns)
        self.assertTrue(self.events[-1]['timestamps']['preserved'])
        self.assertEqual(self.events[-1]['timestamps']['differenceNs'],'0')
    def test_write_time_option_does_not_restore_source_time(self):
        (self.b/'file').unlink()
        self.sync(comparisonMode='size',preserveTime=False)
        self.assertGreater((self.b/'file').stat().st_mtime_ns,2000000000000)
        self.assertFalse(self.events[-1]['timestamps']['preserved'])
    def test_default_ignores_source_link(self):
        (self.a/'link').symlink_to('missing')
        result=self.sync(comparisonMode='size')
        self.assertEqual(result['unsupportedSkipped'],1)
    def test_newer_protection_uses_tolerance(self):
        result=self.sync(comparisonMode='sha256',updateOnly=True,timeToleranceSeconds=2)
        self.assertEqual(result['copied'],1)
    def test_two_way_newer_does_not_use_tiny_time_difference(self):
        result=self.sync(direction='bidirectional',comparisonMode='sha256',conflictPolicy='newer',timeToleranceSeconds=2)
        self.assertEqual(result['conflicts'],1)
        self.assertEqual((self.a/'file').read_text(),'abc')
    def test_time_precision_outside_tolerance_keeps_original_target(self):
        original=os.utime
        def rounded(path,*args,**kwargs):
            if Path(path).name.startswith('.sync-'):
                return original(path,ns=(1000000000000,1003000000000))
            return original(path,*args,**kwargs)
        with patch('standalone.engine.os.utime',side_effect=rounded):
            with self.assertRaises(RuntimeError):
                self.sync(comparisonMode='sha256',preserveTime=True,timeToleranceSeconds=2)
        self.assertEqual((self.b/'file').read_text(),'xyz')

    def test_exact_tolerance_and_skip_preserves_existing_time(self):
        os.utime(self.b/'file',ns=(1000000000000,1002000000000))
        self.assertEqual(self.sync(comparisonMode='size_mtime',timeToleranceSeconds=2)['skipped'],1)
        self.assertEqual((self.b/'file').stat().st_mtime_ns,1002000000000)

    def test_explicit_mode_wins_and_invalid_tolerance_rejected(self):
        from pydantic import ValidationError
        base=dict(name='new',source=str(self.a),target=str(self.b))
        self.assertFalse(Task(**base,comparisonMode='size',checksum=True).checksum)
        for value in [-1,61,float('nan'),float('inf')]:
            with self.assertRaises(ValidationError):
                Task(**base,timeToleranceSeconds=value)

    def test_timestamp_failure_pauses_delete(self):
        (self.b/'extra').write_text('keep')
        with patch('standalone.engine.os.utime',side_effect=PermissionError('禁止设置时间')):
            result=self.sync(comparisonMode='sha256',preserveTime=True,continueOnError=True,delete=True)
        self.assertEqual(result['failed'],1)
        self.assertTrue((self.b/'extra').exists())
        self.assertEqual((self.b/'file').read_text(),'xyz')

    def test_preview_never_sets_timestamps(self):
        with patch('standalone.engine.os.utime',side_effect=AssertionError('演练不能写时间')):
            self.assertEqual(self.sync(comparisonMode='sha256',dryRun=True)['copied'],1)
        self.assertNotIn('timestamps',self.events[-1])
        self.assertEqual((self.b/'file').read_text(),'xyz')

    def test_legacy_edit_keeps_newer_protection(self):
        from fastapi.testclient import TestClient
        from standalone.api import create_app
        import json
        import sqlite3
        database=Path(self.tmp.name)/'db'/'legacy.sqlite3'
        app=create_app(database)
        legacy=dict(name='old',source=str(self.a),target=str(self.b),checksum=True,updateOnly=True,maxRetries=0)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('INSERT INTO tasks(config) VALUES(?)',(json.dumps(legacy),))
        with TestClient(app) as client:
            saved=client.get('/api/tasks').json()[0]
            saved['name']='renamed'
            response=client.put('/api/tasks/1',json=saved,headers={'X-Sync-Local':'1'})
            self.assertEqual(response.status_code,200,response.text)
            updated=client.get('/api/tasks').json()[0]
        self.assertEqual(updated['timeToleranceSeconds'],0)
        result=synchronize(updated,self.events.append)
        self.assertEqual(result['skipped'],1)
        self.assertEqual((self.b/'file').read_text(),'xyz')
