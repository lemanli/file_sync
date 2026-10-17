"""根目录原生身份快查：共享读取、逐次核验和失败回退。"""
import ctypes
import errno
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))


class RootIdentityTests(unittest.TestCase):
    def api(self, module, *, tag=0, volume=123, identity=(1<<100)+42):
        api=Mock();api.open.return_value=1234;api.close.return_value=1
        def info(handle,kind,pointer,size):
            if kind==9:
                value=ctypes.cast(pointer,ctypes.POINTER(module.AttributeTag)).contents
                value.attributes=0x10 | (0x400 if tag else 0);value.tag=tag
            elif kind==18:
                value=ctypes.cast(pointer,ctypes.POINTER(module.FileIdentity)).contents
                value.volume=volume
                value.identity[:]=identity.to_bytes(16,'little')
            else:self.fail(f'不应查询其他属性 {kind}')
            return 1
        api.info.side_effect=info
        return api

    def test_shared_open_queries_only_type_and_full_identity(self):
        from standalone import root_identity as module
        api=self.api(module)
        with patch.object(module,'_api',api):
            first=module.root_stat(Path('root'))
            second=module.root_stat(Path('root'))
        self.assertEqual(first.st_dev,123)
        self.assertEqual(first.st_ino,(1<<100)+42)
        self.assertTrue(stat.S_ISDIR(first.st_mode))
        self.assertEqual(api.open.call_count,2,'每次重新打开当前路径，不能缓存身份')
        self.assertEqual(api.open.call_args.args[1],0x80)
        self.assertEqual(api.open.call_args.args[2],3,'并行读取，禁止当前核验期间删除或重命名')
        self.assertEqual(api.open.call_args.args[5],0x02200000)
        self.assertEqual(api.close.call_count,2)
        self.assertEqual(second.st_ino,first.st_ino)

    def test_symlink_and_junction_tags_are_preserved(self):
        from standalone import root_identity as module
        from standalone.path_attributes import link_info
        for tag in (0xA0000003,0xA000000C):
            with patch.object(module,'_api',self.api(module,tag=tag)):
                self.assertTrue(link_info(module.root_stat(Path('root'))))

    def test_network_error_closes_handle_and_is_not_silently_retried(self):
        from standalone import root_identity as module
        api=self.api(module);api.info.side_effect=None;api.info.return_value=0
        error=OSError(errno.EIO,'网络故障')
        path=Mock();path.__fspath__=Mock(return_value='C:\\root')
        with patch.object(module,'_api',api),patch.object(module,'last_error',return_value=64),patch.object(module,'win_error',return_value=error):
            with self.assertRaises(OSError):module.root_stat(path)
        api.close.assert_called_once_with(1234)
        path.lstat.assert_not_called()

    def test_unsupported_api_falls_back_and_closes_handle(self):
        from standalone import root_identity as module
        api=self.api(module);api.info.side_effect=None;api.info.return_value=0
        path=Mock();path.__fspath__=Mock(return_value='C:\\root');counts={}
        with patch.object(module,'_api',api),patch.object(module,'last_error',return_value=50):
            self.assertIs(module.root_stat(path,counts),path.lstat.return_value)
        api.close.assert_called_once_with(1234)
        self.assertEqual(counts['rootNativeFallbacks'],1)
        self.assertEqual(counts['lstatQueries'],1)

    def test_failed_open_does_not_close_invalid_handle(self):
        from standalone import root_identity as module
        api=self.api(module);api.open.return_value=ctypes.c_void_p(-1).value
        path=Mock();path.__fspath__=Mock(return_value='C:\\root')
        with patch.object(module,'_api',api),patch.object(module,'last_error',return_value=64),patch.object(module,'win_error',return_value=OSError()):
            with self.assertRaises(OSError):module.root_stat(path)
        api.close.assert_not_called()
        path.lstat.assert_not_called()

    def test_non_windows_uses_portable_lstat(self):
        from standalone import root_identity as module
        path=Mock();path.__fspath__=Mock(return_value='C:\\root')
        with patch.object(module,'_api',None):
            self.assertIs(module.root_stat(path),path.lstat.return_value)

    @unittest.skipUnless(os.name=='nt','需要原生 Windows')
    def test_native_identity_matches_python_and_detects_replacement(self):
        from standalone.root_identity import root_stat
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'root';root.mkdir()
            before=root.lstat();native=root_stat(root)
            self.assertEqual((native.st_dev,native.st_ino),(before.st_dev,before.st_ino))
            root.rename(root.with_name('old'));root.mkdir()
            after=root_stat(root)
            self.assertNotEqual((native.st_dev,native.st_ino),(after.st_dev,after.st_ino))

    def test_long_paths_and_existing_prefix(self):
        from standalone.root_identity import native_path
        local='C:\\'+'x'*260
        unc='\\\\nas\\share\\'+'x'*260
        self.assertEqual(native_path(local),'\\\\?\\'+local)
        self.assertEqual(native_path(unc),'\\\\?\\UNC\\'+unc[2:])
        self.assertEqual(native_path('\\\\?\\'+local),'\\\\?\\'+local)

    def test_non_link_reparse_uses_python_semantics(self):
        from standalone import root_identity as module
        api=self.api(module,tag=0x9000001A)
        path=Mock();path.__fspath__=Mock(return_value='C:\\root')
        with patch.object(module,'_api',api):
            self.assertIs(module.root_stat(path),path.lstat.return_value)
        api.close.assert_called_once()

    def test_engine_uses_fresh_native_identity_before_publish(self):
        import standalone.engine as engine
        class WindowsOs:
            name='nt'
            def __getattr__(self,name):return getattr(os,name)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();src=root/'src';src.mkdir();dst=root/'dst';dst.mkdir();(src/'a').write_text('data')
            calls=[]
            def root_query(path,counts):
                calls.append(path)
                return path.lstat()
            with patch.object(engine,'os',WindowsOs()),patch.object(engine,'root_stat',side_effect=root_query):
                result=engine.synchronize(dict(source=str(src),target=str(dst),syncStrategy='COPY_ALL'),lambda e:None)
            self.assertEqual(result['copied'],1)
            self.assertEqual(calls,[dst,dst])

    @unittest.skipUnless(os.name=='nt','需要原生 Windows')
    def test_native_parallel_queries_and_handle_release(self):
        from concurrent.futures import ThreadPoolExecutor
        from standalone.root_identity import root_stat
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'root';root.mkdir();expected=root.lstat()
            def check(_):
                current=root_stat(root)
                return current.st_dev,current.st_ino
            with ThreadPoolExecutor(max_workers=32) as pool:
                self.assertEqual(list(pool.map(check,range(128))),[(expected.st_dev,expected.st_ino)]*128)
            root.rename(root.with_name('renamed'))  # 若漏关不共享删除的句柄，此处会失败。
