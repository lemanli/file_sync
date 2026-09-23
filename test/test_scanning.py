"""扫描优化必须减少系统调用，同时保留失败、忽略和进度语义。"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone.engine import synchronize
from standalone.progress import Progress


class ScanningTests(unittest.TestCase):
    def test_preflight_does_not_lstat_each_regular_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';src.mkdir()
            for n in range(40):(src/str(n)).write_text('data')
            phase={'value':None};calls=[];original=Path.lstat
            def progress(kind,**data):
                if kind=='phase':phase['value']=data['phase']
            def lstat(path,*args,**kwargs):
                if phase['value']=='scanning' and path.parent==src:calls.append(path)
                return original(path,*args,**kwargs)
            with patch.object(Path,'lstat',lstat):
                result=synchronize(dict(source=str(src),target=str(root/'dst'),_progress=progress,dryRun=True),lambda event:None)
            self.assertEqual(result['copied'],40)
            self.assertEqual(calls,[],'普通文件预检不应重复查询 lstat')

    def test_batched_scan_progress_and_rate(self):
        progress=Progress(1)
        progress.update('scan',count=128,path='/source/sub',side='source')
        snapshot=progress.snapshot()
        self.assertEqual(snapshot['scannedEntries'],128)
        self.assertEqual(snapshot['scanPath'],'/source/sub')
        self.assertEqual(snapshot['scanSide'],'source')
        self.assertGreater(snapshot['scanEntriesPerSecond'],0)
