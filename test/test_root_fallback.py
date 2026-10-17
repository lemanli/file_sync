"""不支持原生身份查询时，不能让每个文件重复支付探测成本。"""
import sys
from pathlib import Path
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone import root_identity as module


class RootFallbackTests(unittest.TestCase):
    def test_concurrent_first_probe_only_once_and_every_check_is_fresh(self):
        path=Mock();path.lstat.side_effect=lambda:object()
        calls=[]
        def unavailable(path,counts):
            calls.append(1);time.sleep(.02)
            counts['rootNativeFallbacks']=counts.get('rootNativeFallbacks',0)+1
            return path.lstat()
        reader=module.RootIdentityReader(unavailable)
        with ThreadPoolExecutor(max_workers=32) as pool:
            values=list(pool.map(lambda _:reader(path,{}),range(128)))
        self.assertEqual(len(calls),1)
        self.assertEqual(path.lstat.call_count,128)
        self.assertEqual(len({id(value) for value in values}),128)

    def test_supported_queries_remain_parallel(self):
        lock=threading.Lock();active=0;peak=0
        def supported(path,counts):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            time.sleep(.01)
            with lock:active-=1
            return object()
        reader=module.RootIdentityReader(supported)
        reader(Mock(),{})
        with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(lambda _:reader(Mock(),{}),range(32)))
        self.assertGreater(peak,1)

    def test_later_fallback_disables_only_this_reader(self):
        attempts=[]
        def probe(path,counts):
            attempts.append(1)
            if len(attempts)>1:
                counts['rootNativeFallbacks']=1
            return path.lstat()
        native=Mock(side_effect=probe);reader=module.RootIdentityReader(native);path=Mock()
        reader(path,{})  # 首次成功。
        reader(path,{})  # 运行中发现不兼容。
        counts={};reader(path,counts)
        self.assertEqual(counts['rootNativeBypassed'],1)
        self.assertEqual(counts['lstatQueries'],1)
        module.RootIdentityReader(native)(path,{})
        self.assertEqual(native.call_count,3)

    def test_fallback_reason_is_recorded(self):
        api=Mock();api.open.return_value=123;api.info.return_value=0;api.close.return_value=1
        path=Mock();path.__fspath__=Mock(return_value='C:\\root');counts={'_details':True}
        with patch.object(module,'_api',api),patch.object(module,'last_error',return_value=50):
            module.root_stat(path,counts)
        self.assertEqual(counts['rootFallbackAttributeTag'],1)
        self.assertEqual(counts['rootFallbackError50'],1)
        sample=counts['_samples'][0]
        self.assertEqual(sample['winError'],50)
        self.assertEqual(sample['stage'],'attributeTag')
        api.close.assert_called_once()

    def test_network_error_is_not_converted_to_fallback(self):
        reader=module.RootIdentityReader(Mock(side_effect=ConnectionError('断网')))
        path=Mock()
        with self.assertRaises(ConnectionError):reader(path,{})
        path.lstat.assert_not_called()
