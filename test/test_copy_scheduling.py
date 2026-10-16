"""批量不能限制实际文件并发；通过屏障验证真实重叠执行。"""
import sys
import threading
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))


class SchedulingTests(unittest.TestCase):
    def test_one_batch_uses_multiple_workers(self):
        from standalone.copy_pool import CopyPool
        barrier = threading.Barrier(4)
        def copy(items):
            barrier.wait(timeout=3)
            return items
        with CopyPool(lambda: 4) as pool:
            result = pool.submit(copy, list(range(4))).result(timeout=5)
        self.assertEqual(sorted(result), list(range(4)))

    def test_worker_limit_changes_without_recreating_pool(self):
        from standalone.copy_pool import CopyPool
        limit = [1]
        with CopyPool(lambda: limit[0]) as pool:
            self.assertEqual(pool.submit(lambda items: items, [1]).result(timeout=3), [1])
            limit[0] = 4
            barrier = threading.Barrier(4)
            def copy(items):
                barrier.wait(timeout=3)
                return items
            self.assertEqual(len(pool.submit(copy, [1, 2, 3, 4]).result(timeout=5)), 4)

    def test_exception_reaches_batch_future(self):
        from standalone.copy_pool import CopyPool
        def fail(items):
            raise ValueError('复制失败')
        with CopyPool(lambda: 2) as pool:
            with self.assertRaisesRegex(ValueError, '复制失败'):
                pool.submit(fail, [1, 2, 3]).result(timeout=3)

    def test_queued_batch_grows_after_resume_without_new_submission(self):
        from standalone.copy_pool import CopyPool
        limit = [1]
        entered = threading.Event(); release = threading.Event()
        barrier = threading.Barrier(3)
        def copy(items):
            if items == [0]:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('测试未释放首文件')
            else:
                barrier.wait(timeout=3)
            return items
        with CopyPool(lambda: limit[0]) as pool:
            future = pool.submit(copy, [0, 1, 2, 3])
            try:
                self.assertTrue(entered.wait(3))
                limit[0] = 3
            finally:
                release.set()
            self.assertEqual(sorted(future.result(timeout=5)), [0, 1, 2, 3])

    def test_mixed_files_in_large_batch_actually_copy_concurrently(self):
        import tempfile
        import os
        from unittest.mock import patch
        from standalone.engine import synchronize
        for strategy in ('AUTO', 'COPY_ALL', 'COMPARE_METADATA'):
            with self.subTest(strategy=strategy), tempfile.TemporaryDirectory() as folder:
                source = Path(folder).resolve() / 'source'; source.mkdir()
                target = Path(folder).resolve() / 'target'
                for n in range(4):
                    (source / str(n)).write_bytes(b'a' * (1024 * 1024 if n == 0 else 10))
                barrier = threading.Barrier(4)
                original = os.fsync
                def copied(*args, **kwargs):
                    barrier.wait(timeout=5)
                    return original(*args, **kwargs)
                with patch('standalone.engine.os.fsync', side_effect=copied):
                    result = synchronize(dict(source=str(source), target=str(target),
                                              syncStrategy=strategy, parallelism=4, batchFiles=500,
                                              largeThresholdMb=1, maxRetries=0), lambda event: None)
                self.assertEqual(result['copied'], 4)
                for path in source.iterdir():
                    self.assertEqual(path.read_bytes(), (target / path.name).read_bytes())

    def test_temporary_file_metadata_is_not_serialized_across_files(self):
        import shutil
        import tempfile
        from unittest.mock import patch
        from standalone.engine import synchronize
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder).resolve() / 'source'; source.mkdir()
            target = Path(folder).resolve() / 'target'
            for n in range(4):
                (source / str(n)).write_text(str(n))
            barrier = threading.Barrier(4)
            original = shutil.copymode
            def metadata(*args, **kwargs):
                barrier.wait(timeout=3)
                return original(*args, **kwargs)
            with patch('standalone.engine.shutil.copymode', side_effect=metadata):
                result = synchronize(dict(source=str(source), target=str(target),
                                          syncStrategy='COPY_ALL', parallelism=4, batchFiles=500,
                                          maxRetries=0), lambda event: None)
            self.assertEqual(result['copied'], 4)
            for path in source.iterdir():
                self.assertEqual(path.read_bytes(), (target / path.name).read_bytes())

    def test_different_files_publish_concurrently(self):
        import os
        import tempfile
        from unittest.mock import patch
        from standalone.engine import synchronize
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder).resolve() / 'source'; source.mkdir()
            target = Path(folder).resolve() / 'target'
            for n in range(4):
                (source / str(n)).write_text(str(n))
            barrier = threading.Barrier(4)
            original = os.replace
            def publish(*args, **kwargs):
                barrier.wait(timeout=3)
                return original(*args, **kwargs)
            with patch('standalone.engine.os.replace', side_effect=publish):
                result = synchronize(dict(source=str(source), target=str(target),
                                          syncStrategy='COPY_ALL', parallelism=4, batchFiles=500,
                                          maxRetries=0), lambda event: None)
            self.assertEqual(result['copied'], 4)

    def test_same_target_serializes_and_releases_lock_entries(self):
        from standalone.file_locks import FilePublishLocks
        locks = FilePublishLocks()
        attempted = threading.Event(); acquired = threading.Event()
        def publish():
            attempted.set()
            with locks.hold('A/é'):
                acquired.set()
        with locks.hold('a/e\u0301'):
            worker = threading.Thread(target=publish); worker.start()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(acquired.wait(.05), '等价目标路径不能并发发布')
        worker.join(2)
        self.assertTrue(acquired.is_set())
        self.assertEqual(locks._entries, {})
        with self.assertRaises(ValueError):
            with locks.hold('error'):
                raise ValueError('测试发布失败')
        self.assertEqual(locks._entries, {})
