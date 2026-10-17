"""减少重复远程调用，同时保留发布阶段可观测性。"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone.engine import synchronize


class PublishOverheadTests(unittest.TestCase):
    def test_successful_replace_does_not_unlink_missing_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';src.mkdir();(src/'a').write_text('data')
            original=Path.unlink;attempts=[]
            def unlink(path,*args,**kwargs):
                if path.name.startswith('.sync-'):attempts.append(path)
                return original(path,*args,**kwargs)
            phases=[]
            def progress(kind,**data):
                if kind=='file':phases.append(data['phase'])
            with patch.object(Path,'unlink',unlink):
                result=synchronize(dict(source=str(src),target=str(root/'dst'),syncStrategy='COPY_ALL',_progress=progress),lambda event:None)
            self.assertEqual(result['copied'],1)
            self.assertEqual(attempts,[],'成功重命名后不应再次访问不存在的临时路径')
            for phase in ('path_check','source_check','target_check','renaming','logging'):
                self.assertIn(phase,phases)

    def test_failed_replace_still_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';src.mkdir();(src/'a').write_text('data')
            with patch('standalone.engine.os.replace',side_effect=PermissionError('模拟发布失败')):
                with self.assertRaises(RuntimeError):
                    synchronize(dict(source=str(src),target=str(root/'dst'),syncStrategy='COPY_ALL',maxRetries=0),lambda event:None)
            self.assertEqual(list((root/'dst').iterdir()),[])

    def test_windows_link_query_combines_symlink_and_junction(self):
        import stat
        from types import SimpleNamespace
        from unittest.mock import Mock
        from standalone.engine import windows_link
        for mode,tag,expected in ((stat.S_IFDIR,0,False),(stat.S_IFLNK,0,True),(stat.S_IFDIR,0xA0000003,True)):
            path=SimpleNamespace(lstat=Mock(return_value=SimpleNamespace(st_mode=mode,st_reparse_tag=tag)))
            self.assertEqual(windows_link(path),expected)
            path.lstat.assert_called_once()

    def test_debug_log_is_saved_and_filterable(self):
        import time
        from fastapi.testclient import TestClient
        from standalone.api import create_app
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';src.mkdir();(src/'a').write_text('data')
            headers={'X-Sync-Local':'1'}
            with TestClient(create_app(root/'state/db')) as client:
                tid=client.post('/api/tasks',headers=headers,json=dict(name='诊断',source=str(src),target=str(root/'dst'),debugPerformance=True)).json()['id']
                rid=client.post(f'/api/tasks/{tid}/run',headers=headers,json={}).json()['runId']
                for _ in range(100):
                    run=client.get('/api/runs').json()[0]
                    if run['status'] not in ('running','queued'):break
                    time.sleep(.02)
                self.assertEqual(run['status'],'success',run)
                events=client.get(f'/api/runs/{rid}/events',params={'action':'performance_debug'}).json()
                self.assertTrue(events)
                self.assertIn('phaseTimings',str(events))
                self.assertIn('renaming',str(events))
                exported=client.get(f'/api/runs/{rid}/diagnostics').json()
                self.assertEqual(exported['runId'],rid)
                self.assertIn('pythonVersion',exported)
                self.assertIn('renaming',exported['progress']['phaseTimings'])
                self.assertTrue(exported['recentDiagnostics'])
                self.assertEqual(client.get('/api/runs/999999/diagnostics').status_code,404)

    def test_phase_totals_and_slow_samples_are_bounded(self):
        from standalone.progress import Progress
        with patch('standalone.progress.time.monotonic',return_value=100) as clock:
            progress=Progress(1)
            for n in range(20):
                clock.return_value=100+n*2
                progress.update('file',path=str(n),phase='renaming',size=1,written=1)
                clock.return_value+=2
                progress.update('file_done')
            report=progress.diagnostics()
            self.assertEqual(report['phaseTimings']['renaming']['seconds'],40)
            self.assertLessEqual(len(report['slowFiles']),5)
            self.assertEqual(progress.diagnostics()['slowFiles'],[])
            self.assertEqual(progress.file_timings,{})
