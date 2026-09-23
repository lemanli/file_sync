"""目录扫描及策略契约，不依赖真实网络共享。"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))
from standalone.engine import synchronize

class StrategyFixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.src=self.root/'src';self.dst=self.root/'dst'
        self.src.mkdir();self.dst.mkdir();self.events=[]
    def sync(self,strategy,**kw):
        return synchronize(dict(source=str(self.src),target=str(self.dst),syncStrategy=strategy,maxRetries=0,**kw),self.events.append)

class StrategyTests(StrategyFixture,unittest.TestCase):
    def test_copy_all_overwrites_equal_metadata_without_target_enumeration(self):
        (self.src/'a').write_text('new');(self.dst/'a').write_text('old')
        for p in (self.src/'a',self.dst/'a'):os.utime(p,(1000,1000))
        original=os.scandir
        def scan(path):
            if not isinstance(path,int): self.assertNotEqual(Path(path),self.dst,'COPY_ALL 禁止枚举目标')
            return original(path)
        with patch('os.scandir',side_effect=scan): result=self.sync('COPY_ALL')
        self.assertEqual((self.dst/'a').read_text(),'new');self.assertEqual(result['copied'],1)
    def test_skip_existing_keeps_different_content(self):
        (self.src/'a').write_text('new value');(self.dst/'a').write_text('old')
        self.assertEqual(self.sync('SKIP_EXISTING')['skipped'],1)
        self.assertEqual((self.dst/'a').read_text(),'old')
    def test_compare_does_not_query_target_file_for_comparison(self):
        (self.src/'a').write_text('same');(self.dst/'a').write_text('same')
        for p in (self.src/'a',self.dst/'a'):os.utime(p,(1000,1000))
        original=Path.stat
        def query(path,*a,**kw):
            if path==self.dst/'a' and kw.get('follow_symlinks',True):
                self.fail('比较不得调用目标 Path.stat/exists/is_file')
            return original(path,*a,**kw)
        with patch.object(Path,'stat',query): result=self.sync('COMPARE_METADATA')
        self.assertEqual(result['skipped'],1)
    def test_mirror_deletes_extras(self):
        (self.src/'a').write_text('new');(self.dst/'extra').write_text('old')
        result=self.sync('MIRROR')
        self.assertFalse((self.dst/'extra').exists());self.assertEqual(result['deleted'],1)
    def test_spilled_directory_keeps_all_files_and_counts(self):
        for n in range(30):
            (self.src/str(n)).write_text(str(n))
            if n%2==0:(self.dst/str(n)).write_text('old')
        result=self.sync('SKIP_EXISTING',_snapshotLimit=2,scanWorkers=4)
        self.assertEqual((result['copied'],result['skipped']),(15,15))
        self.assertEqual(result['scanMetrics']['comparisonExistsCalls'],0)
    def test_copy_begins_before_later_directory_scan(self):
        import threading
        from standalone import directory_plan
        (self.src/'a').write_text('one');(self.src/'child').mkdir();(self.src/'child'/'b').write_text('two')
        copied=threading.Event();original=directory_plan.read_snapshot
        def read(path,*args,**kwargs):
            if path==self.src/'child':self.assertTrue(copied.wait(3),'必须在全树扫描结束前复制')
            return original(path,*args,**kwargs)
        def emit(event):
            if event.get('path')=='a' and event.get('action')=='copied':copied.set()
        with patch.object(directory_plan,'read_snapshot',side_effect=read):
            result=synchronize(dict(source=str(self.src),target=str(self.dst),syncStrategy='COPY_ALL'),emit)
        self.assertEqual(result['copied'],2)
    def test_mirror_error_keeps_target_only_file(self):
        (self.src/'bad').write_text('x');(self.dst/'bad').mkdir();(self.dst/'extra').write_text('keep')
        result=self.sync('MIRROR',continueOnError=True)
        self.assertTrue(result['deletionSkipped']);self.assertTrue((self.dst/'extra').exists())
    def test_copy_all_refuses_target_link(self):
        (self.src/'a').write_text('new');outside=self.root/'outside';outside.write_text('keep')
        try:(self.dst/'a').symlink_to(outside)
        except OSError:self.skipTest('平台未授权创建链接')
        with self.assertRaises(RuntimeError):self.sync('COPY_ALL')
        self.assertEqual(outside.read_text(),'keep')
    def test_skip_existing_does_not_stat_metadata(self):
        from standalone.scanner.portable import PortableScanner
        from standalone.scanner import ScanFields
        original=PortableScanner.scan;fields=[]
        def scan(scanner,path,requested,metrics):
            fields.append(requested);yield from original(scanner,path,requested,metrics)
        (self.src/'a').write_text('a');(self.dst/'a').write_text('b')
        with patch.object(PortableScanner,'scan',scan):self.sync('SKIP_EXISTING',scannerBackend='portable')
        self.assertEqual(fields,[ScanFields.NAME_TYPE,ScanFields.NAME_TYPE])
    def test_unsupported_source_ignored_and_target_retained_in_mirror(self):
        try:(self.src/'a').symlink_to('missing')
        except OSError:self.skipTest('平台未授权创建链接')
        (self.dst/'a').write_text('keep')
        result=self.sync('MIRROR')
        self.assertEqual(result['unsupportedSkipped'],1);self.assertTrue((self.dst/'a').exists())
    def test_ambiguous_names_fail_safely(self):
        (self.src/'Name').write_text('new');(self.dst/'name').write_text('old')
        with self.assertRaises(ValueError):self.sync('SKIP_EXISTING')
        self.assertEqual((self.dst/'name').read_text(),'old')

    def test_directory_ignore_applies_to_link_before_unsupported_policy(self):
        outside=self.root/'outside';outside.mkdir()
        try:
            (self.src/'ignored').symlink_to(outside,target_is_directory=True)
            (self.dst/'ignored').symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest('平台未授权创建链接')
        result=self.sync('MIRROR',ignorePatterns=['ignored/'],skipUnsupported=False)
        self.assertEqual(result['ignored'],1);self.assertEqual(result['failed'],0)
        self.assertTrue((self.dst/'ignored').is_symlink())

class ScannerTests(unittest.TestCase):
    def test_native_matches_portable_files_and_fields(self):
        from standalone.scanner import get_scanner,ScanFields,ScanMetrics
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'sub').mkdir()
            for n in range(5000):(root/f'文件-{n}').write_text(str(n))
            for fields in (ScanFields.NAME_TYPE,ScanFields.METADATA):
                expected={e.name:e for e in get_scanner('portable').scan(root,fields,ScanMetrics())}
                backends=['auto']
                if sys.platform=='darwin':backends.append('macos')
                if sys.platform=='win32':backends.extend(['windows_bulk','windows_find'])
                for backend in backends:
                    actual={e.name:e for e in get_scanner(backend).scan(root,fields,ScanMetrics())}
                    self.assertEqual(actual,expected,backend)
    def test_partial_native_fallback_discards_partial_snapshot(self):
        from standalone.scanner import DirectoryEntry,ScanFields,ScanMetrics,UnsupportedScanner
        from standalone.directory_plan import read_snapshot
        class Unsupported:
            def scan(self,*args):
                yield DirectoryEntry('ghost','file')
                raise UnsupportedScanner('not supported')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'real').write_text('x');metrics=ScanMetrics()
            with patch('standalone.directory_plan.get_scanner',return_value=Unsupported()):
                with read_snapshot(root,ScanFields.METADATA,'auto',metrics,'source',1) as result:
                    self.assertEqual([e.name for e in result],['real'])
            self.assertEqual(metrics.snapshot()['fallbacks'],1)
    def test_permission_error_does_not_fallback(self):
        from standalone.scanner import ScanFields,ScanMetrics
        from standalone.directory_plan import read_snapshot
        class Denied:
            def scan(self,*args):raise PermissionError('denied')
        with tempfile.TemporaryDirectory() as folder:
            with patch('standalone.directory_plan.get_scanner',return_value=Denied()):
                with self.assertRaises(PermissionError):read_snapshot(Path(folder),ScanFields.NAME_TYPE,'auto',ScanMetrics(),'source')

class StrategyAPITests(unittest.TestCase):
    def test_persistence_validation_and_copy_all_api_no_target_enumeration(self):
        import time
        from fastapi.testclient import TestClient
        from standalone.api import create_app
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';dst=root/'dst';src.mkdir();dst.mkdir()
            (src/'a').write_text('new');(dst/'a').write_text('old')
            headers={'X-Sync-Local':'1'}
            task=dict(name='策略',source=str(src),target=str(dst),syncStrategy='COPY_ALL',scanWorkers=4)
            original=os.scandir
            def scan(path):
                if not isinstance(path,int):self.assertNotEqual(Path(path),dst)
                return original(path)
            with TestClient(create_app(root/'state/db')) as client:
                response=client.post('/api/tasks',json=task,headers=headers)
                self.assertEqual(response.status_code,200,response.text);tid=response.json()['id']
                saved=client.get('/api/tasks').json()[0]
                self.assertEqual(saved['syncStrategy'],'COPY_ALL');self.assertEqual(saved['scanWorkers'],4)
                with patch('os.scandir',side_effect=scan):
                    client.post(f'/api/tasks/{tid}/run',headers=headers,json={})
                    deadline=time.monotonic()+5
                    while time.monotonic()<deadline:
                        row=client.get('/api/runs').json()[0]
                        if row['status']!='running':break
                        time.sleep(.02)
                self.assertEqual(row['status'],'success',row)
                for extra in (dict(direction='bidirectional'),dict(delete=True),dict(updateOnly=True),dict(scanWorkers=100)):
                    self.assertEqual(client.post('/api/tasks',json=dict(task,**extra),headers=headers).status_code,422)
                mirror=client.post('/api/tasks',json=dict(task,syncStrategy='MIRROR'),headers=headers)
                self.assertEqual(mirror.status_code,200,mirror.text)
                self.assertTrue(next(t for t in client.get('/api/tasks').json() if t['id']==mirror.json()['id'])['delete'])
    def test_affected_sqlite_runtime_serializes_parallel_api_connections(self):
        import sqlite3
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor
        from fastapi.testclient import TestClient
        from standalone.api import create_app
        counts={'active':0,'peak':0};lock=threading.Lock();original=sqlite3.connect
        class Observed(sqlite3.Connection):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs)
                with lock:
                    counts['active']+=1;counts['peak']=max(counts['peak'],counts['active'])
                time.sleep(.003)
            def close(self):
                try:super().close()
                finally:
                    with lock:counts['active']-=1
        def connect(*args,**kwargs):return original(*args,factory=Observed,**kwargs)
        with tempfile.TemporaryDirectory() as folder,patch('standalone.api.sqlite3.sqlite_version_info',(3,51,1)),patch('standalone.api.sqlite3.connect',side_effect=connect):
            with TestClient(create_app(Path(folder)/'db')) as client,ThreadPoolExecutor(max_workers=8) as pool:
                statuses=list(pool.map(lambda _:client.get('/api/tasks').status_code,range(32)))
            self.assertEqual(statuses,[200]*32)
            self.assertEqual(counts,{'active':0,'peak':1})

class PublicationRaceTests(StrategyFixture,unittest.TestCase):
    def test_skip_existing_does_not_overwrite_file_created_after_scan(self):
        import shutil
        (self.src/'a').write_text('source');original=shutil.copymode
        def changed(*args,**kwargs):
            (self.dst/'a').write_text('external writer')
            return original(*args,**kwargs)
        with patch('standalone.engine.shutil.copymode',side_effect=changed):
            result=self.sync('SKIP_EXISTING')
        self.assertEqual((self.dst/'a').read_text(),'external writer')
        self.assertEqual(result['skipped'],1)
    def test_source_alias_collision_rejected_before_any_operation(self):
        from standalone.directory_plan import read_snapshot
        from standalone.scanner import DirectoryEntry,ScanFields,ScanMetrics
        class Conflicting:
            def scan(self,*args):yield from [DirectoryEntry('Name','file'),DirectoryEntry('name','file')]
        with patch('standalone.directory_plan.get_scanner',return_value=Conflicting()):
            for limit in (1,100):
                with self.assertRaises(ValueError):
                    with read_snapshot(self.src,ScanFields.NAME_TYPE,'auto',ScanMetrics(),'source',limit):pass
    def test_directory_alias_does_not_merge(self):
        (self.src/'Name').mkdir();(self.src/'Name'/'a').write_text('new')
        (self.dst/'name').mkdir();(self.dst/'name'/'a').write_text('keep')
        with self.assertRaises(ValueError):self.sync('COMPARE_METADATA')
        self.assertEqual((self.dst/'name'/'a').read_text(),'keep')
    def test_windows_error_path_not_found_is_file_not_found(self):
        from standalone.scanner import windows
        self.assertTrue(hasattr(windows,'windows_error'))
        for code in (2,3):
            error=windows.windows_error(code,self.dst/'missing')
            self.assertIsInstance(error,FileNotFoundError)
            self.assertEqual(error.winerror,code)

    def test_native_backend_copies_new_subdirectory(self):
        import shutil
        (self.src/'child').mkdir();(self.src/'child'/'new').write_text('new')
        backends=['portable']
        if sys.platform=='win32':backends+=['windows_bulk','windows_find']
        if sys.platform=='darwin':backends+=['macos']
        for backend in backends:
            result=self.sync('COMPARE_METADATA',scannerBackend=backend)
            self.assertEqual(result['copied'],1,backend)
            self.assertEqual((self.dst/'child'/'new').read_text(),'new')
            shutil.rmtree(self.dst/'child')
