"""常用规则、双向合并和容错均使用独立临时目录。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from standalone.engine import synchronize
from standalone.rules import IgnoreRules

class RuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.a=self.root/'a';self.b=self.root/'b'
        self.a.mkdir();self.b.mkdir();self.events=[]
    def sync(self,**kw):
        return synchronize(dict(source=str(self.a),target=str(self.b),maxRetries=0,**kw),self.events.append)
    def test_glob_semantics(self):
        rules=IgnoreRules(['.git/','*.tmp','/build/','docs/**/*.log'])
        for value in ['deep/.git/config','x/a.tmp','build/keep.txt','docs/a.log','docs/x/a.log']:
            self.assertTrue(rules.matches(Path(value)),value)
        for value in ['x/build/keep.txt','docs/a.txt','a.tmp2']:
            self.assertFalse(rules.matches(Path(value)),value)
    def test_ignored_files_and_parent_survive_mirror(self):
        (self.a/'normal').write_text('new');(self.a/'secret.tmp').write_text('ignore')
        (self.a/'.git').mkdir();(self.a/'.git'/'link').symlink_to('missing')
        (self.b/'old').mkdir();(self.b/'old'/'keep.tmp').write_text('keep')
        (self.b/'extra').write_text('remove')
        result=self.sync(ignorePatterns=['*.tmp','.git/'],delete=True)
        self.assertEqual(result['copied'],1);self.assertEqual(result['ignored'],2)
        self.assertTrue((self.b/'old'/'keep.tmp').exists());self.assertFalse((self.b/'extra').exists())
        self.assertFalse((self.b/'secret.tmp').exists())
    def test_continue_errors_returns_partial_and_disables_delete(self):
        (self.a/'good').write_text('copy');(self.a/'bad').write_text('fail')
        (self.b/'bad').mkdir();(self.b/'extra').write_text('keep')
        result=self.sync(continueOnError=True,delete=True)
        self.assertEqual(result['failed'],1);self.assertEqual(result['copied'],1)
        self.assertTrue((self.b/'extra').exists());self.assertTrue(result['deletionSkipped'])
    def test_bidirectional_merge_preserves_conflicts_and_deletions(self):
        (self.a/'left').write_text('a');(self.b/'right').write_text('b')
        (self.a/'same').write_text('left');(self.b/'same').write_text('right')
        result=self.sync(direction='bidirectional')
        self.assertEqual((self.a/'right').read_text(),'b');self.assertEqual((self.b/'left').read_text(),'a')
        self.assertEqual((self.a/'same').read_text(),'left');self.assertEqual((self.b/'same').read_text(),'right')
        self.assertEqual(result['conflicts'],1)
    def test_bidirectional_newer_and_equal_time_conflict(self):
        import os
        (self.a/'file').write_text('new');(self.b/'file').write_text('old')
        os.utime(self.a/'file',(2000,2000));os.utime(self.b/'file',(1000,1000))
        self.sync(direction='bidirectional',conflictPolicy='newer')
        self.assertEqual((self.b/'file').read_text(),'new')
        (self.b/'file').write_text('different');os.utime(self.b/'file',(2000,2000))
        result=self.sync(direction='bidirectional',conflictPolicy='newer')
        self.assertEqual(result['conflicts'],1);self.assertEqual((self.a/'file').read_text(),'new')
    def test_bidirectional_preview_missing_target(self):
        (self.a/'file').write_text('content');self.b.rmdir()
        result=self.sync(direction='bidirectional',dryRun=True)
        self.assertEqual(result['copied'],1);self.assertFalse(self.b.exists())
    def test_bidirectional_reject_delete(self):
        with self.assertRaises(ValueError):self.sync(direction='bidirectional',delete=True)
    def test_bidirectional_skips_link_on_either_side(self):
        (self.a/'left').write_text('a');(self.b/'link').symlink_to('missing')
        result=self.sync(direction='bidirectional',skipUnsupported=True)
        self.assertEqual((self.b/'left').read_text(),'a');self.assertTrue((self.b/'link').is_symlink())
        self.assertGreaterEqual(result['unsupportedSkipped'],1)
    def test_directory_creation_error_can_continue(self):
        (self.a/'bad').mkdir();(self.a/'bad'/'x').write_text('no')
        (self.a/'ok').write_text('yes');(self.b/'bad').write_text('collision')
        result=self.sync(continueOnError=True)
        self.assertGreaterEqual(result['failed'],1);self.assertEqual((self.b/'ok').read_text(),'yes')

    def test_single_star_never_crosses_directory(self):
        rules=IgnoreRules(['docs/*.log'])
        self.assertTrue(rules.matches(Path('docs/a.log')))
        self.assertFalse(rules.matches(Path('docs/deep/a.log')))
    def test_bidirectional_preview_matches_execution_counts(self):
        (self.a/'left').write_text('a');(self.b/'right').write_text('b')
        (self.a/'conflict').write_text('a');(self.b/'conflict').write_text('bb')
        preview=self.sync(direction='bidirectional',dryRun=True)
        actual=self.sync(direction='bidirectional')
        for key in ['copied','skipped','conflicts','bytes']:
            self.assertEqual(preview[key],actual[key],key)
    def test_plan_change_does_not_overwrite_new_target(self):
        from standalone import engine
        (self.a/'left').write_text('planned')
        original=engine._synchronize_one
        touched=False
        def changed(config,emit):
            nonlocal touched
            if not touched:
                touched=True
                (self.b/'left').write_text('external change')
            return original(config,emit)
        with patch('standalone.engine._synchronize_one',side_effect=changed):
            result=self.sync(direction='bidirectional',continueOnError=True)
        self.assertEqual(result['failed'],1)
        self.assertEqual((self.b/'left').read_text(),'external change')
    def test_scan_error_protects_deletion(self):
        import os
        (self.a/'good').write_text('yes');(self.a/'closed').mkdir()
        (self.b/'extra').write_text('keep')
        original=os.walk
        def walk(top,*args,**kwargs):
            if Path(top)==self.a:
                kwargs['onerror'](PermissionError(13,'denied',str(self.a/'closed')))
            yield from original(top,*args,**kwargs)
        with patch('standalone.engine.os.walk',side_effect=walk):
            result=self.sync(continueOnError=True,delete=True)
        self.assertGreaterEqual(result['failed'],1)
        self.assertTrue((self.b/'extra').exists())
    def test_delete_error_stops_remaining_deletions(self):
        (self.b/'extra1').write_text('1');(self.b/'extra2').write_text('2')
        with patch('standalone.engine.safe_remove',side_effect=PermissionError('delete denied')) as unlink:
            result=self.sync(continueOnError=True,delete=True)
        self.assertEqual(unlink.call_count,1)
        self.assertEqual(result['failed'],1)
        self.assertTrue(result['deletionSkipped'])

    def test_missing_planned_file_is_reported(self):
        from standalone import engine
        (self.a/'gone').write_text('planned')
        original=engine._synchronize_one
        def removed(config,emit):
            if (self.a/'gone').exists():
                (self.a/'gone').unlink()
            return original(config,emit)
        with patch('standalone.engine._synchronize_one',side_effect=removed):
            result=self.sync(direction='bidirectional',continueOnError=True)
        self.assertEqual(result['failed'],1)
        self.assertTrue(any('消失' in event.get('error','') for event in self.events))

    def test_delete_parent_link_swap_cannot_delete_outside(self):
        import os
        from standalone import engine
        if os.open not in os.supports_dir_fd:
            self.skipTest('当前平台没有目录描述符删除能力')
        outside=self.root/'outside';outside.mkdir();(outside/'file').write_text('must remain')
        (self.b/'child').mkdir();(self.b/'child/file').write_text('old')
        original=engine.safe_remove
        def swap(root,relative,is_directory,identity):
            (self.b/'child').rename(self.b/'saved')
            (self.b/'child').symlink_to(outside,target_is_directory=True)
            return original(root,relative,is_directory,identity)
        with patch('standalone.engine.safe_remove',side_effect=swap):
            with self.assertRaises(engine.UnsafePathError):
                self.sync(delete=True,continueOnError=True)
        self.assertEqual((outside/'file').read_text(),'must remain')

    def test_safety_error_stops_queued_copy(self):
        from standalone.engine import UnsafePathError
        (self.a/'bad').write_text('bad');(self.a/'good').write_text('good')
        original=Path.is_symlink
        def link(path):
            return True if path==self.a/'bad' else original(path)
        with patch.object(Path,'is_symlink',link):
            with self.assertRaises(UnsafePathError):
                self.sync(continueOnError=True,parallelism=1,batchFiles=1)
        self.assertFalse((self.b/'good').exists())

    def test_same_metadata_comparison_contract(self):
        import os
        (self.a/'same').write_text('abc');(self.b/'same').write_text('xyz')
        for path in (self.a/'same',self.b/'same'):os.utime(path,(1000,1000))
        self.assertEqual(self.sync(direction='bidirectional')['skipped'],1)
        self.assertEqual(self.sync(direction='bidirectional',checksum=True)['conflicts'],1)

    def test_main_thread_safety_error_stops_two_active_workers(self):
        import os
        import threading
        from standalone.engine import UnsafePathError
        for name in ('a','b'):(self.a/name).write_text('content')
        (self.a/'child').mkdir();(self.b/'child').mkdir()
        outside=self.root/'outside';outside.mkdir()
        barrier=threading.Barrier(3)
        original_walk=os.walk;original_fsync=os.fsync;original_event=threading.Event
        captured={}
        def event_factory(*args,**kwargs):
            event=original_event(*args,**kwargs)
            captured.setdefault('stop',event)
            return event
        def fsync(fd):
            barrier.wait(timeout=5)
            captured['stop'].wait(timeout=5)
            original_fsync(fd)
        def walk(top,*args,**kwargs):
            # 预检已使用 scandir，这里针对执行遍历时的路径替换。
            mutate=Path(top)==self.a
            for row in original_walk(top,*args,**kwargs):
                if mutate and Path(row[0])==self.a/'child':
                    barrier.wait(timeout=5)
                    (self.b/'child').rmdir()
                    (self.b/'child').symlink_to(outside,target_is_directory=True)
                yield row
        with patch('standalone.engine.threading.Event',side_effect=event_factory), patch('standalone.engine.os.fsync',side_effect=fsync), patch('standalone.engine.os.walk',side_effect=walk):
            with self.assertRaises(UnsafePathError):
                self.sync(continueOnError=True,parallelism=2,batchFiles=1)
        self.assertFalse((self.b/'a').exists());self.assertFalse((self.b/'b').exists())

class RuleAPITests(unittest.TestCase):
    def setUp(self):
        RuleTests.setUp(self)
        from fastapi.testclient import TestClient
        from standalone.api import create_app
        self.client=TestClient(create_app(self.root/'state/db.sqlite3'))
        self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.headers={'X-Sync-Local':'1'}
    def task(self,**extra):
        response=self.client.post('/api/tasks',headers=self.headers,json=dict(name='规则测试',source=str(self.a),target=str(self.b),**extra))
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['id']
    def execute_task(self,tid,options=None):
        import time
        response=self.client.post(f'/api/tasks/{tid}/run',headers=self.headers,json=options or {})
        self.assertEqual(response.status_code,202,response.text)
        for _ in range(100):
            row=self.client.get('/api/runs').json()[0]
            if row['status']!='running':return row
            time.sleep(.02)
        self.fail('任务超时')
    def test_persist_rules_direct_run_and_partial_status(self):
        import json
        (self.a/'good').write_text('yes');(self.a/'bad').write_text('no');(self.b/'bad').mkdir()
        tid=self.task(continueOnError=True,ignorePatterns=['*.tmp'],maxRetries=0)
        saved=self.client.get('/api/tasks').json()[0]
        self.assertEqual(saved['ignorePatterns'],['*.tmp']);self.assertTrue(saved['continueOnError'])
        row=self.execute_task(tid)
        self.assertEqual(row['status'],'partial',row)
        self.assertEqual(json.loads(row['result'])['failed'],1)
        self.assertTrue((self.b/'good').exists())
    def test_conflict_preview_is_not_success(self):
        (self.a/'x').write_text('a');(self.b/'x').write_text('bb')
        row=self.execute_task(self.task(direction='bidirectional'),{'dryRun':True})
        self.assertEqual(row['status'],'preview_partial',row)
    def test_bidir_overlapping_sources_and_delete_rejected(self):
        for extra in [dict(delete=True),dict(pathMappings=[dict(source=str(self.a),target=str(self.b)),dict(source=str(self.a),target=str(self.root/'c'))])]:
            response=self.client.post('/api/tasks',headers=self.headers,json=dict(name='bad',source=str(self.a),target=str(self.b),direction='bidirectional',**extra))
            self.assertEqual(response.status_code,422,response.text)

    def test_continue_to_next_mapping_after_file_error(self):
        import json
        second=self.root/'second';second.mkdir();(second/'ok').write_text('yes')
        (self.a/'bad').write_text('fail');(self.b/'bad').mkdir()
        tid=self.task(continueOnError=True,maxRetries=0,pathMappings=[dict(source=str(self.a),target=str(self.b)),dict(source=str(second),target=str(self.root/'second-backup'))])
        row=self.execute_task(tid)
        self.assertEqual(row['status'],'partial',row)
        self.assertEqual(len(json.loads(row['result'])['mappings']),2)
        self.assertEqual((self.root/'second-backup/ok').read_text(),'yes')
