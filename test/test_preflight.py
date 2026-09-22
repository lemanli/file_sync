"""读写探测必须先于所有目录组同步，且演练不写盘。"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone.preflight import check_destination
from standalone.api import create_app
from fastapi.testclient import TestClient

class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
    def test_write_probe_cleanup_and_missing_target(self):
        self.assertTrue(check_destination(self.root)['writeVerified'])
        self.assertEqual(list(self.root.iterdir()),[])
        result=check_destination(self.root/'missing'/'child')
        self.assertEqual(result['checkedDirectory'],str(self.root))
        self.assertFalse((self.root/'missing').exists())
        self.assertEqual(list(self.root.iterdir()),[])
    def test_dry_run_never_writes(self):
        with patch('standalone.preflight.tempfile.mkstemp',side_effect=AssertionError('不允许写入')):
            self.assertFalse(check_destination(self.root,True)['writeVerified'])
    def test_read_and_write_errors_are_specific(self):
        with patch('standalone.preflight.os.scandir',side_effect=PermissionError('read denied')):
            with self.assertRaisesRegex(ValueError,'不可读取'):check_destination(self.root)
        with patch('standalone.preflight.tempfile.mkstemp',side_effect=PermissionError('write denied')):
            with self.assertRaisesRegex(ValueError,'无法创建或写入'):check_destination(self.root)
    def test_all_groups_checked_before_any_sync(self):
        a=self.root/'a';b=self.root/'b';a.mkdir();b.mkdir()
        with TestClient(create_app(self.root/'db'/'file.sqlite3')) as client:
            headers={'X-Sync-Local':'1'}
            task=client.post('/api/tasks',headers=headers,json=dict(name='two',pathMappings=[dict(source=str(a),target=str(self.root/'x')),dict(source=str(b),target=str(self.root/'y'))])).json()
            with patch('standalone.api.check_destination',side_effect=[dict(reason='ok'),ValueError('目标不可写')]) as probe,patch('standalone.api.synchronize') as sync:
                client.post(f"/api/tasks/{task['id']}/run",headers=headers,json={})
                for _ in range(200):
                    run=client.get('/api/runs').json()[0]
                    if run['status']!='running':break
                    time.sleep(.01)
                self.assertEqual(run['status'],'failed')
                self.assertIn('目标不可写',run['error'])
                self.assertEqual(probe.call_count,2);sync.assert_not_called()
            report=client.get(f"/api/runs/{run['id']}/report").json()
            self.assertEqual(report['counts']['mapping_failed'],1)
