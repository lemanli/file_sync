"""最小字段契约；计数是本进程可观测 API 调用，不等同网络报文。"""
from dataclasses import dataclass
from enum import IntFlag
import threading

class ScanFields(IntFlag):
    NAME = 1
    TYPE = 2
    SIZE = 4
    MTIME = 8
    NAME_TYPE = NAME | TYPE
    METADATA = NAME | TYPE | SIZE | MTIME

@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    name: str
    kind: str
    size: int | None = None
    mtime_ns: int | None = None

    @property
    def is_dir(self): return self.kind == 'directory'
    @property
    def is_file(self): return self.kind == 'file'

class ScanMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = dict(sourceEntries=0, targetEntries=0, sourceDirectories=0,
                           targetDirectories=0, metadataCalls=0, extraStatCount=0,
                           comparisonExistsCalls=0, nativeCalls=0, portableDirectories=0,
                           fallbacks=0, directoryQueueLength=0, copyQueueLength=0)
    def add(self, key, value=1):
        with self.lock: self.values[key] = self.values.get(key, 0) + value
    def set(self, key, value):
        with self.lock: self.values[key] = value
    def snapshot(self):
        with self.lock: return dict(self.values)

class UnsupportedScanner(OSError):
    """仅能力不支持时回退；网络与权限错误必须交给任务重试。"""
