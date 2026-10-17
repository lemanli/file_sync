"""路径诊断计时、错误分类与抽样上限回归。"""
import sys
from pathlib import Path
import stat
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone import path_attributes as module
from standalone.progress import Progress


class PathDiagnosticsTests(unittest.TestCase):
    def test_shared_root_failure_records_error_before_fallback(self):
        path=Mock();path.__fspath__=Mock(return_value='\\\\nas\\share\\')
        path.lstat.return_value=SimpleNamespace(st_mode=stat.S_IFDIR)
        counts={'_details':True}
        with patch.object(module,'_get_attributes',return_value=0xffffffff), patch.object(module,'_last_error',return_value=53,create=True), patch.object(module.time,'perf_counter',side_effect=[1,1.2,2,2.3]):
            self.assertFalse(module.windows_link(path,counts))
        self.assertEqual(counts['fallbackBadNetworkPath'],1)
        self.assertAlmostEqual(counts['attributesShareRootSeconds'],.2)
        self.assertAlmostEqual(counts['lstatShareRootSeconds'],.3)
        sample=counts['_samples'][0]
        self.assertEqual(sample['winError'],53)
        self.assertEqual(sample['role'],'shareRoot')
        self.assertEqual(sample['reason'],'native_error')

    def test_disabled_details_do_not_read_clock_or_collect_paths(self):
        path=Mock();path.__fspath__=Mock(return_value='C:\\folder')
        with patch.object(module,'_get_attributes',return_value=16),patch.object(module.time,'perf_counter') as clock:
            counts={};module.windows_link(path,counts)
        clock.assert_not_called()
        self.assertNotIn('_samples',counts)

    def test_samples_and_error_categories_are_bounded(self):
        progress=Progress(1)
        for n in range(100):
            progress.update('path_metrics',targetChecks=1,samples=[dict(path=str(n),seconds=.2)])
        snap=progress.snapshot()
        self.assertEqual(snap['pathCheckMetrics']['targetChecks'],100)
        self.assertLessEqual(len(snap['pathCheckSamples']),8)
        self.assertTrue(snap['pathCheckSamples'])
        self.assertEqual(progress.diagnostics()['pathCheckSamples'],snap['pathCheckSamples'])

    def test_root_identity_timing_preserves_exceptions(self):
        counts={'_details':True}
        with patch.object(module.time,'perf_counter',side_effect=[1,1.4]):
            with self.assertRaises(PermissionError):
                module.query(lambda: (_ for _ in ()).throw(PermissionError()),Path('/root'),counts,'identity','targetRoot')
        self.assertAlmostEqual(counts['identityTargetRootSeconds'],.4)

    def test_details_follow_task_setting_and_export(self):
        import tempfile
        from standalone.engine import synchronize
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';src.mkdir();(src/'a').write_text('data')
            for enabled in (False,True):
                dst=root/str(enabled);dst.mkdir();progress=Progress(1)
                synchronize(dict(source=str(src),target=str(dst),syncStrategy='COPY_ALL',debugPerformance=enabled,_progress=progress.update),lambda e:None)
                metrics=progress.diagnostics()['pathCheckMetrics']
                self.assertNotIn('_details',metrics)
                self.assertEqual('identityTargetRootSeconds' in metrics,enabled)
                if enabled:self.assertEqual(metrics['identityTargetRootCount'],metrics['rootIdentityQueries'])

    def test_per_check_sample_storage_is_bounded(self):
        path=Mock();path.__fspath__=Mock(return_value='C:\\folder')
        path.lstat.return_value=SimpleNamespace(st_mode=stat.S_IFDIR)
        counts={'_details':True}
        with patch.object(module,'_get_attributes',return_value=0xffffffff),patch.object(module,'_last_error',return_value=99999):
            for _ in range(50):module.windows_link(path,counts)
        self.assertEqual(counts['fallbackOtherError'],50)
        self.assertLessEqual(len(counts['_samples']),4)
