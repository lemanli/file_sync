"""比较日志应展示真实数据，快速比较不得计算哈希。"""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone.engine import synchronize

class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name).resolve();self.a=root/'a';self.b=root/'b';self.a.mkdir();self.b.mkdir();self.events=[]
        (self.a/'file').write_text('abc');(self.b/'file').write_text('xyz')
        for p in (self.a/'file',self.b/'file'):os.utime(p,(1000,1000))
    def sync(self,**kw):
        return synchronize(dict(source=str(self.a),target=str(self.b),**kw),self.events.append)
    def test_fast_mode_reports_metadata_without_hash(self):
        with patch('standalone.engine.digest',side_effect=AssertionError('不应计算哈希')):
            self.sync()
        detail=self.events[-1]['comparison']
        self.assertEqual(detail['method'],'size_mtime')
        self.assertEqual(detail['source']['size'],3)
        self.assertEqual(detail['target']['mtimeNs'],'1000000000000')
        self.assertNotIn('sha256',detail['source'])
    def test_hash_mode_reports_before_and_written_hash(self):
        self.sync(checksum=True)
        event=self.events[-1];self.assertEqual(event['action'],'copied')
        self.assertEqual(event['comparison']['source']['sha256'],hashlib.sha256(b'abc').hexdigest())
        self.assertEqual(event['comparison']['target']['sha256'],hashlib.sha256(b'xyz').hexdigest())
        self.assertEqual(event['verification']['writtenSha256'],hashlib.sha256(b'abc').hexdigest())
    def test_hash_preview_does_not_claim_written_hash(self):
        self.sync(checksum=True,dryRun=True)
        self.assertIn('sha256',self.events[-1]['comparison']['source'])
        self.assertNotIn('verification',self.events[-1])
        self.assertEqual((self.b/'file').read_text(),'xyz')
    def test_bidirectional_conflict_has_comparison(self):
        self.sync(checksum=True,direction='bidirectional')
        event=next(e for e in self.events if e['action']=='conflict')
        self.assertNotEqual(event['comparison']['source']['sha256'],event['comparison']['target']['sha256'])
