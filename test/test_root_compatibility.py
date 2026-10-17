"""NAS 兼容身份查询必须先校准，不能截断身份或重复失败探测。"""
import ctypes as C
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone import root_identity as module


def info(ino=123):
    return SimpleNamespace(st_mode=stat.S_IFDIR,st_dev=17,st_ino=ino,st_reparse_tag=0)


class RootCompatibilityTests(unittest.TestCase):
    def test_fallback_selects_matching_compatibility_reader(self):
        baseline=info();path=Mock();path.lstat.return_value=baseline
        def unavailable(path,counts):
            counts['rootNativeFallbacks']=1
            return path.lstat()
        native=Mock(side_effect=unavailable);legacy=Mock(return_value=baseline)
        reader=module.RootIdentityReader(native,legacy=legacy)
        for _ in range(100):self.assertIs(reader(path,{}),baseline)
        self.assertEqual(native.call_count,1)
        self.assertEqual(path.lstat.call_count,1)
        self.assertEqual(legacy.call_count,100)

    def test_mismatched_or_zero_identity_does_not_enable_compatibility(self):
        for baseline,candidate in ((info((1<<80)+123),info(123)),(info(0),info(0)),(info(123),info(124))):
            path=Mock();path.lstat.return_value=baseline
            def unavailable(path,counts):counts['rootNativeFallbacks']=1;return baseline
            legacy=Mock(return_value=candidate)
            reader=module.RootIdentityReader(unavailable,legacy=legacy)
            reader(path,{})
            for _ in range(10):reader(path,{})
            self.assertEqual(legacy.call_count,1)
            self.assertGreaterEqual(path.lstat.call_count,10)

    def test_compatibility_queries_remain_fresh_and_do_not_mask_replacement(self):
        baseline=info();path=Mock();path.lstat.return_value=baseline
        def unavailable(path,counts):counts['rootNativeFallbacks']=1;return baseline
        legacy=Mock(side_effect=[baseline,info(456)])
        reader=module.RootIdentityReader(unavailable,legacy=legacy)
        self.assertEqual(reader(path,{}).st_ino,123)
        self.assertEqual(reader(path,{}).st_ino,456)

    def test_later_unsupported_switches_to_portable_without_reprobing(self):
        baseline=info();path=Mock();path.lstat.return_value=baseline
        def unavailable(path,counts):counts['rootNativeFallbacks']=1;return baseline
        legacy=Mock(side_effect=[baseline,module.NativeIdentityUnavailable('不支持')])
        reader=module.RootIdentityReader(unavailable,legacy=legacy)
        for _ in range(10):reader(path,{})
        self.assertEqual(legacy.call_count,2)

    def test_network_failure_does_not_switch_to_success(self):
        path=Mock()
        def unavailable(path,counts):counts['rootNativeFallbacks']=1;return info()
        reader=module.RootIdentityReader(unavailable,legacy=Mock(side_effect=ConnectionError('断网')))
        with self.assertRaises(ConnectionError):reader(path,{})
        path.lstat.assert_not_called()

    def test_legacy_abi_and_single_query(self):
        self.assertEqual(C.sizeof(module.LegacyInformation),52)
        api=Mock();api.open.return_value=321;api.close.return_value=1
        def fill(handle,pointer):
            value=C.cast(pointer,C.POINTER(module.LegacyInformation)).contents
            value.attributes=16;value.volume=17;value.index_high=1;value.index_low=123
            return 1
        api.legacy.side_effect=fill;api.info.return_value=0
        caps={}
        with patch.object(module,'_api',api),patch.object(module,'last_error',return_value=50):
            value=module.legacy_root_stat(Path('root'),{},caps)
            api.info.reset_mock()
            value=module.legacy_root_stat(Path('root'),{},caps)
        self.assertEqual(value.st_ino,(1<<32)+123)
        self.assertEqual(value.st_dev,17)
        self.assertEqual(api.legacy.call_count,2);api.info.assert_not_called();self.assertEqual(api.close.call_count,2)

    @unittest.skipUnless(os.name=='nt','需要原生 Windows')
    def test_real_compatibility_is_either_matching_or_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'root';root.mkdir();baseline=root.lstat()
            def fallback(path,counts):counts['rootNativeFallbacks']=1;return path.lstat()
            reader=module.RootIdentityReader(fallback,legacy=module.legacy_root_stat)
            for _ in range(5):
                value=reader(root,{})
                self.assertEqual((value.st_dev,value.st_ino),(baseline.st_dev,baseline.st_ino))
            root.rename(root.with_name('old'));root.mkdir()
            value=reader(root,{})
            self.assertNotEqual((value.st_dev,value.st_ino),(baseline.st_dev,baseline.st_ino))

    def test_legacy_network_failure_closes_handle(self):
        api=Mock();api.open.return_value=123;api.close.return_value=1;api.legacy.return_value=0
        with patch.object(module,'_api',api),patch.object(module,'last_error',return_value=64),patch.object(module,'win_error',return_value=ConnectionError('断网')):
            with self.assertRaises(ConnectionError):module.legacy_root_stat(Path('root'),{})
        api.close.assert_called_once_with(123)

    def test_legacy_symlink_requires_and_preserves_tag(self):
        api=Mock();api.open.return_value=123;api.close.return_value=1
        def fill(handle,pointer):
            value=C.cast(pointer,C.POINTER(module.LegacyInformation)).contents
            value.attributes=0x410;value.volume=17;value.index_low=123
            return 1
        def tagged(handle,kind,pointer,size):
            if kind==18:return 0
            self.assertEqual(kind,9)
            value=C.cast(pointer,C.POINTER(module.AttributeTag)).contents
            value.tag=0xA000000C
            return 1
        api.legacy.side_effect=fill;api.info.side_effect=tagged
        with patch.object(module,'_api',api),patch.object(module,'last_error',return_value=50):value=module.legacy_root_stat(Path('root'),{})
        self.assertTrue(stat.S_ISLNK(value.st_mode))
        api.close.assert_called_once()

    def test_calibrated_reader_remains_parallel(self):
        import threading,time
        from concurrent.futures import ThreadPoolExecutor
        baseline=info();active=0;peak=0;lock=threading.Lock()
        def unavailable(path,counts):counts['rootNativeFallbacks']=1;return baseline
        def compatible(path,counts):
            nonlocal active,peak
            with lock:active+=1;peak=max(active,peak)
            time.sleep(.01)
            with lock:active-=1
            return info()
        reader=module.RootIdentityReader(unavailable,legacy=compatible);reader(Mock(),{})
        with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(lambda _:reader(Mock(),{}),range(32)))
        self.assertGreater(peak,1)

    def test_legacy_keeps_full_identity_when_available(self):
        api=Mock();api.open.return_value=123;api.close.return_value=1
        def fill(handle,pointer):
            value=C.cast(pointer,C.POINTER(module.LegacyInformation)).contents
            value.attributes=16;value.volume=17;value.index_low=123
            return 1
        def full(handle,kind,pointer,size):
            self.assertEqual(kind,18)
            value=C.cast(pointer,C.POINTER(module.FileIdentity)).contents
            value.volume=(1<<40)+17;value.identity[:]=((1<<100)+123).to_bytes(16,'little')
            return 1
        api.legacy.side_effect=fill;api.info.side_effect=full
        with patch.object(module,'_api',api):value=module.legacy_root_stat(Path('root'),{})
        self.assertEqual(value.st_dev,(1<<40)+17)
        self.assertEqual(value.st_ino,(1<<100)+123)
        api.close.assert_called_once()
