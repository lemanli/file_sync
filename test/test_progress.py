"""实时进度不依赖完整日志读取，测试仅使用临时目录。"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone.progress import Progress
from standalone.engine import synchronize
from standalone.api import create_app
from fastapi.testclient import TestClient


class ProgressTests(unittest.TestCase):
    def test_bounded_and_snapshot_isolation(self):
        p=Progress(1)
        for i in range(10000):
            p.update('file',path=str(i),size=10,written=0,phase='comparing')
            p.update('file_done')
        self.assertEqual(p.snapshot()['activeFiles'],[])
        self.assertEqual(p.snapshot()['finished'],10000)
        a=p.snapshot();a['counts']['failed']=999
        self.assertEqual(p.snapshot()['counts']['failed'],0)
        for _ in range(1000):
            p.snapshot()
        self.assertLessEqual(len(p.samples),12)

    def test_engine_counts_and_no_progress_events(self):
        with tempfile.TemporaryDirectory() as temp:
            a=Path(temp).resolve()/'a';b=Path(temp).resolve()/'b';a.mkdir()
            for i in range(13):(a/str(i)).write_text('sample')
            p=Progress(1);p.update('group',index=1);events=[]
            result=synchronize(dict(source=str(a),target=str(b),_progress=p.update,comparisonMode='size',batchFiles=3),events.append)
            snapshot=p.snapshot()
            self.assertEqual(snapshot['totalFiles'],13)
            self.assertEqual(snapshot['finished'],13)
            self.assertEqual(snapshot['writtenBytes'],78)
            self.assertEqual(snapshot['activeFiles'],[])
            self.assertEqual(len(events),result['copied'])

    def test_live_api_large_file_and_final_statistics(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();a=root/'a';a.mkdir();(a/'large').write_bytes(b'x'*(3*1024*1024))
            with TestClient(create_app(root/'db'/'sync.sqlite3')) as client:
                headers={'X-Sync-Local':'1'}
                task=client.post('/api/tasks',headers=headers,json=dict(name='progress',source=str(a),target=str(root/'b'),parallelism=1,bandwidthLimit=1024)).json()
                client.post(f"/api/tasks/{task['id']}/run",headers=headers,json={})
                snapshots=[]
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    run=client.get('/api/runs').json()[0]
                    if run['status']!='running':break
                    if run.get('progress'):snapshots.append(run['progress'])
                    time.sleep(.05)
                self.assertEqual(run['status'],'success')
                self.assertTrue(any(p['writtenBytes']>0 and p['activeFiles'] for p in snapshots))
                self.assertTrue(any(p['bytesPerSecond']>0 for p in snapshots))
                self.assertTrue(any(p['totalFiles']==1 and p['finished']==0 for p in snapshots))
                import json
                result=json.loads(run['result'])
                self.assertEqual(result['progress']['counts']['copied'],1)
                self.assertEqual(result['progress']['writtenBytes'],3*1024*1024)
                self.assertNotIn('progress',run)
                self.assertEqual(len(client.get(f"/api/runs/{run['id']}/events").json()),4)

    def test_failure_keeps_counters(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();a=root/'a';a.mkdir();(a/'file').write_text('data')
            with TestClient(create_app(root/'db'/'sync.sqlite3')) as client:
                h={'X-Sync-Local':'1'}
                task=client.post('/api/tasks',headers=h,json=dict(name='failure',source=str(a),target=str(root/'b'),maxRetries=0)).json()
                with patch('standalone.engine.os.utime',side_effect=PermissionError('test')):
                    client.post(f"/api/tasks/{task['id']}/run",headers=h,json={})
                    for _ in range(200):
                        run=client.get('/api/runs').json()[0]
                        if run['status']!='running':break
                        time.sleep(.01)
                self.assertEqual(run['status'],'failed')
                import json
                self.assertEqual(json.loads(run['result'])['progress']['counts']['failed'],1)
