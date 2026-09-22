"""使用隔离临时目录验证单机同步，不接触用户目录。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from standalone.engine import synchronize

class CopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name).resolve() / '源 目录'
        self.dst = Path(self.tmp.name).resolve() / '目标'
        self.src.mkdir()
        self.dst.mkdir()
        self.events = []

    def run_copy(self, **options):
        return synchronize({'source': str(self.src), 'target': str(self.dst), **options}, self.events.append)

    def test_copy_incremental_checksum_and_delete(self):
        (self.src / '中文.txt').write_text('hello')
        self.assertEqual(self.run_copy()['copied'], 1)
        self.assertEqual(self.run_copy()['skipped'], 1)
        target = self.dst / '中文.txt'
        stamp = target.stat().st_mtime_ns
        target.write_text('world')
        os.utime(target, ns=(stamp, stamp))
        self.assertEqual(self.run_copy(checksum=True)['copied'], 1)
        (self.dst / 'extra').write_text('extra')
        self.run_copy(delete=True)
        self.assertFalse((self.dst / 'extra').exists())
        self.assertEqual(list(self.dst.glob('.sync-*')), [])

    def test_overlap_and_symlink_rejected(self):
        with self.assertRaises(ValueError):
            self.run_copy(target=str(self.src / 'child'))
        (self.src / 'link').symlink_to(self.dst, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_copy(skipUnsupported=False)

    def test_dry_run_and_protect_newer(self):
        (self.src / 'a').write_text('new')
        self.run_copy(dryRun=True)
        self.assertFalse((self.dst / 'a').exists())
        (self.dst / 'a').write_text('protected')
        os.utime(self.dst / 'a', (2000000000, 2000000000))
        self.assertEqual(self.run_copy(updateOnly=True)['skipped'], 1)

    def test_small_batch_all_files(self):
        for n in range(35):
            (self.src / str(n)).write_bytes(bytes([n]) * 100)
        result = self.run_copy(parallelism=4, batchFiles=3, largeThresholdMb=1)
        self.assertEqual(result['copied'], 35)
        self.assertEqual(len(list(self.dst.iterdir())), 35)

if __name__ == '__main__':
    unittest.main()

class APITests(unittest.TestCase):
    def test_persistence_execute_and_local_boundary(self):
        import time
        from fastapi.testclient import TestClient
        from standalone.api import create_app
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            src = root / 'src'
            src.mkdir()
            (src / 'hello').write_text('hello')
            db = root / 'state/state.sqlite3'
            headers = {'X-Sync-Local': '1'}
            with TestClient(create_app(db)) as client:
                self.assertEqual(client.post('/api/tasks', json={}).status_code, 403)
                self.assertEqual(client.get('/api/tasks', headers={'Host': 'evil.example'}).status_code, 403)
                self.assertEqual(client.get('/api/tasks', headers={'Origin': 'https://evil.example'}).status_code, 403)
                self.assertEqual(client.get('/').status_code, 200)
                blocked = client.post('/api/tasks', headers=headers, json={'name':'危险目标','source':str(src),'target':str(db.parent)})
                self.assertEqual(blocked.status_code, 422)
                response = client.post('/api/tasks', headers=headers, json={'name':'测试','source':str(src),'target':str(root / 'dst')})
                self.assertEqual(response.status_code, 200, response.text)
                task = response.json()['id']
                response = client.post(f'/api/tasks/{task}/run', headers=headers, json={'dryRun':False})
                self.assertEqual(response.status_code, 202)
                for _ in range(100):
                    row = client.get('/api/runs').json()[0]
                    if row['status'] != 'running':
                        break
                    time.sleep(.02)
                self.assertEqual(row['status'], 'success', row)
                self.assertEqual((root / 'dst/hello').read_text(), 'hello')
                self.assertEqual(len(client.get(f"/api/runs/{row['id']}/events").json()), 4)
            with TestClient(create_app(db)) as client:
                self.assertEqual(len(client.get('/api/tasks').json()), 1)
                self.assertEqual(client.get('/api/runs').json()[0]['status'], 'success')

class FailureTests(unittest.TestCase):
    setUp = CopyTests.setUp
    run_copy = CopyTests.run_copy
    def test_failed_copy_keeps_extra_target_and_logs_failure(self):
        (self.src / 'a').write_text('content')
        (self.dst / 'a').mkdir()
        (self.dst / 'extra').write_text('must remain')
        with self.assertRaises(RuntimeError):
            self.run_copy(delete=True, maxRetries=0)
        self.assertTrue((self.dst / 'extra').exists())
        self.assertEqual(self.events[-1]['action'], 'failed')

class SourceLossTests(unittest.TestCase):
    setUp = CopyTests.setUp
    def test_source_lost_before_delete_aborts(self):
        (self.src / 'a').write_text('a')
        (self.dst / 'extra').write_text('keep')
        def emit(event):
            if event['action'] == 'copied':
                self.src.rename(self.src.with_name('moved'))
        with self.assertRaises((RuntimeError, FileNotFoundError)):
            synchronize(dict(source=str(self.src),target=str(self.dst),delete=True), emit)
        self.assertTrue((self.dst / 'extra').exists())

class UnsupportedEntryTests(unittest.TestCase):
    setUp = CopyTests.setUp
    run_copy = CopyTests.run_copy

    def test_rejected_entry_reports_exact_path(self):
        link = self.src / 'python-link'
        link.symlink_to('missing-python')
        with self.assertRaisesRegex(ValueError, 'python-link'):
            self.run_copy(skipUnsupported=False)
        self.assertEqual(self.events[-1]['action'], 'failed')
        self.assertEqual(self.events[-1]['path'], 'python-link')

    def test_explicit_skip_copies_files_preserves_excluded_targets(self):
        (self.src / 'normal.txt').write_text('content')
        (self.src / 'broken-link').symlink_to('missing')
        (self.src / 'linked-directory').symlink_to(self.dst, target_is_directory=True)
        (self.dst / 'broken-link').write_text('preserve excluded target')
        (self.dst / 'linked-directory').mkdir()
        (self.dst / 'linked-directory' / 'keep').write_text('keep')
        (self.dst / 'extra').write_text('remove')
        result = self.run_copy(skipUnsupported=True, delete=True)
        self.assertEqual(result['copied'], 1)
        self.assertEqual(result['skipped'], 2)
        self.assertEqual(result['unsupportedSkipped'], 2)
        self.assertEqual((self.dst / 'normal.txt').read_text(), 'content')
        self.assertTrue((self.dst / 'broken-link').exists())
        self.assertTrue((self.dst / 'linked-directory' / 'keep').exists())
        self.assertFalse((self.dst / 'extra').exists())
        skipped = [e for e in self.events if e['action']=='skipped']
        self.assertEqual(len(skipped), 2)
        self.assertTrue(all(e['reason'] for e in skipped))

    def test_skip_never_follows_destination_link(self):
        outside = self.src.parent / 'outside'
        outside.mkdir()
        (self.dst / 'link').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '目标.*link'):
            self.run_copy(skipUnsupported=True, delete=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_special_file_skip_dry_run(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('当前平台不支持 FIFO')
        os.mkfifo(self.src / 'pipe')
        (self.src / 'ordinary').write_text('data')
        result = self.run_copy(skipUnsupported=True, dryRun=True, delete=True)
        self.assertEqual(result['unsupportedSkipped'], 1)
        self.assertEqual(result['copied'], 1)
        self.assertFalse((self.dst / 'ordinary').exists())
