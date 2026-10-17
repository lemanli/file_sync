"""大小快照不因时间差复制，不逐文件探测目标。"""
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from standalone.engine import synchronize


class SizeSnapshotTests(unittest.TestCase):
    def test_size_only_uses_snapshot_without_target_stat(self):
        for strategy in ('AUTO', 'COMPARE_SIZE'):
            with self.subTest(strategy=strategy), tempfile.TemporaryDirectory() as folder:
                root=Path(folder).resolve();src=root/'src';dst=root/'dst';src.mkdir();dst.mkdir()
                (src/'a').write_text('new');(dst/'a').write_text('old')
                os.utime(src/'a',(1000,1000));os.utime(dst/'a',(2000,2000))
                original=Path.stat
                def query(path,*args,**kwargs):
                    if path==dst/'a' and kwargs.get('follow_symlinks',True):
                        self.fail('快照比较不能逐文件 stat 目标')
                    return original(path,*args,**kwargs)
                with patch.object(Path,'stat',query):
                    result=synchronize(dict(source=str(src),target=str(dst),syncStrategy=strategy,
                                            comparisonMode='size',maxRetries=0),lambda event:None)
                self.assertEqual(result['skipped'],1)
                self.assertIn('scanMetrics',result)
                self.assertEqual((dst/'a').read_text(),'old')

    def test_memory_budget_spills_and_releases(self):
        from standalone.directory_plan import Snapshot, SnapshotBudget
        from standalone.scanner import DirectoryEntry, ScanMetrics
        metrics=ScanMetrics();budget=SnapshotBudget(4096,metrics)
        with Snapshot(limit=100000,budget=budget) as first, Snapshot(limit=100000,budget=budget) as second:
            for n in range(20):
                (first if n%2 else second).add(DirectoryEntry(str(n),'file',n,None))
            self.assertIsNotNone(first.db)
            self.assertEqual(len(list(first))+len(list(second)),20)
            self.assertLessEqual(budget.used,4096)
        self.assertEqual(budget.used,0)

    def test_auto_size_mirror_copies_changes_and_deletes_extras(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';dst=root/'dst';src.mkdir();dst.mkdir()
            (src/'small').write_text('new value');(dst/'small').write_text('old')
            (src/'large').write_bytes(b'x'*(2*1024*1024));(dst/'extra').write_text('extra')
            result=synchronize(dict(source=str(src),target=str(dst),comparisonMode='size',delete=True,
                                    largeThresholdMb=1,parallelism=4,maxRetries=0),lambda event:None)
            self.assertEqual((result['copied'],result['deleted']),(2,1))
            self.assertFalse((dst/'extra').exists())
            self.assertEqual((dst/'large').read_bytes(),(src/'large').read_bytes())

    def test_size_scanner_does_not_request_mtime(self):
        from standalone.scanner import ScanFields
        from standalone import directory_plan
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';dst=root/'dst';src.mkdir();dst.mkdir()
            (src/'a').write_text('a');seen=[];original=directory_plan.read_snapshot
            def read(path,fields,*args,**kwargs):
                seen.append(fields)
                return original(path,fields,*args,**kwargs)
            with patch.object(directory_plan,'read_snapshot',side_effect=read):
                synchronize(dict(source=str(src),target=str(dst),comparisonMode='size'),lambda event:None)
            self.assertTrue(seen)
            self.assertTrue(all(fields==ScanFields.NAME_TYPE_SIZE for fields in seen))

    def test_api_persists_memory_and_size_strategy(self):
        from fastapi.testclient import TestClient
        from standalone.api import create_app
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();(root/'source').mkdir()
            with TestClient(create_app(root/'state/db')) as client:
                task=dict(name='快照',source=str(root/'source'),target=str(root/'target'),syncStrategy='COMPARE_SIZE',scanMemoryMb=1024)
                response=client.post('/api/tasks',json=task,headers={'X-Sync-Local':'1'})
                self.assertEqual(response.status_code,200,response.text)
                saved=client.get('/api/tasks').json()[0]
                self.assertEqual((saved['scanMemoryMb'],saved['comparisonMode']),(1024,'size'))
                self.assertEqual(client.get('/api/health').json()['configSchemaVersion'],4)
                task['scanMemoryMb']=0
                self.assertEqual(client.post('/api/tasks',json=task,headers={'X-Sync-Local':'1'}).status_code,422)
