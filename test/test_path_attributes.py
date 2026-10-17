"""路径属性快查必须保留链接、异常和不覆盖发布语义。"""
import sys
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))


class PathAttributesTests(unittest.TestCase):
    def test_plain_attributes_avoid_full_stat(self):
        from standalone import path_attributes as module
        path=Mock();path.__fspath__=Mock(return_value='C:\\folder')
        counts={}
        with patch.object(module, '_get_attributes', return_value=0x10):
            self.assertFalse(module.windows_link(path, counts))
        path.lstat.assert_not_called()
        self.assertEqual(counts['attributeQueries'],1)

    def test_reparse_points_and_errors_use_existing_semantics(self):
        from standalone import path_attributes as module
        import stat
        for attr, mode, tag, expected in [(0x400,stat.S_IFDIR,0xA0000003,True),
                                         (0x400,stat.S_IFLNK,0,True),
                                         (0x400,stat.S_IFREG,0x9000001A,False),
                                         (0xffffffff,stat.S_IFDIR,0,False)]:
            path=Mock();path.__fspath__=Mock(return_value='C:\\folder')
            path.lstat.return_value=SimpleNamespace(st_mode=mode,st_reparse_tag=tag)
            with patch.object(module,'_get_attributes',return_value=attr):
                self.assertEqual(module.windows_link(path),expected)
            path.lstat.assert_called_once()
        path.lstat.side_effect=PermissionError('拒绝访问')
        with patch.object(module,'_get_attributes',return_value=0xffffffff):
            with self.assertRaises(PermissionError):module.windows_link(path)
        path.lstat.side_effect=FileNotFoundError()
        with patch.object(module,'_get_attributes',return_value=0xffffffff):
            self.assertFalse(module.windows_link(path))

    def test_skip_existing_rename_has_no_missing_temp_cleanup(self):
        from standalone.engine import synchronize
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';src.mkdir();(src/'a').write_text('data')
            attempts=[];unlink=Path.unlink
            def tracked(path,*args,**kwargs):
                if path.name.startswith('.sync-'):attempts.append(path)
                return unlink(path,*args,**kwargs)
            with patch.object(Path,'unlink',tracked):
                result=synchronize(dict(source=str(src),target=str(root/'dst'),syncStrategy='SKIP_EXISTING'),lambda e:None)
            self.assertEqual(result['copied'],1)
            self.assertEqual(attempts,[])

    def test_hardlink_fallback_still_cleans_temp(self):
        from standalone.engine import synchronize
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';src.mkdir();(src/'a').write_text('data')
            with patch('standalone.publication.C.CDLL',return_value=object()):
                result=synchronize(dict(source=str(src),target=str(root/'dst'),syncStrategy='SKIP_EXISTING'),lambda e:None)
            self.assertEqual(result['copied'],1)
            self.assertEqual([p.name for p in (root/'dst').iterdir()],['a'])

    def test_path_metrics_export_separately_from_scan(self):
        from standalone.engine import synchronize
        from standalone.progress import Progress
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';src.mkdir();dst=root/'dst';dst.mkdir();(src/'a').write_text('data')
            progress=Progress(1)
            synchronize(dict(source=str(src),target=str(dst),syncStrategy='COPY_ALL',_progress=progress.update),lambda e:None)
            metrics=progress.snapshot()['pathCheckMetrics']
            self.assertGreaterEqual(metrics['targetChecks'],2)
            self.assertGreaterEqual(metrics['rootIdentityQueries'],2)
            self.assertEqual(progress.diagnostics()['pathCheckMetrics'],metrics)

    def test_root_replacement_before_publish_is_rejected(self):
        from standalone.engine import synchronize, UnsafePathError
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';src.mkdir();dst=root/'dst';dst.mkdir();(src/'a').write_text('data')
            changed=False
            def progress(kind,**data):
                nonlocal changed
                if kind=='file' and data.get('phase')=='target_check' and not changed:
                    changed=True
                    dst.rename(root/'old-dst');dst.mkdir()
            with self.assertRaises(UnsafePathError):
                synchronize(dict(source=str(src),target=str(dst),syncStrategy='COPY_ALL',_progress=progress,maxRetries=0),lambda e:None)
            self.assertFalse((dst/'a').exists())

    def test_windows_native_attributes_on_real_paths(self):
        import os
        if os.name != 'nt':self.skipTest('需要原生 Windows')
        from standalone.path_attributes import windows_link
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);file=root/'文件';file.write_text('data')
            for path in (root,file):
                counts={}
                self.assertFalse(windows_link(path,counts))
                self.assertEqual(counts,{'attributeQueries':1})
            self.assertFalse(windows_link(root/'missing'))
