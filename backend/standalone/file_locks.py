"""目标文件粒度的发布锁；仅保存正在使用或等待中的路径。"""
from contextlib import contextmanager
import threading
import unicodedata


class FilePublishLocks:
    def __init__(self):
        self._guard = threading.Lock()
        self._entries = {}

    @contextmanager
    def hold(self, path):
        # 保守合并大小写和 Unicode 等价路径，不增加文件系统查询。
        key = unicodedata.normalize('NFD', str(path)).casefold()
        with self._guard:
            entry = self._entries.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._entries[key]
