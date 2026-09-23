"""CPython scandir；属性只取一次，NAME/TYPE 不主动取大小时间。"""
import os
import stat
from .base import DirectoryEntry, ScanFields

class PortableScanner:
    name = 'scandir'
    def scan(self, path, fields, metrics):
        metrics.add('portableDirectories')
        with os.scandir(path) as entries:
            for entry in entries:
                # Windows junction 也必须视为链接，不进入重解析点。
                link = entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction())
                if link:
                    yield DirectoryEntry(entry.name, 'link'); continue
                if fields & (ScanFields.SIZE | ScanFields.MTIME):
                    info = entry.stat(follow_symlinks=False)
                    metrics.add('metadataCalls'); metrics.add('extraStatCount')
                    kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else 'special'
                    yield DirectoryEntry(entry.name, kind, info.st_size if kind=='file' and fields & ScanFields.SIZE else None,
                                         info.st_mtime_ns if kind=='file' and fields & ScanFields.MTIME else None)
                else:
                    kind = 'directory' if entry.is_dir(follow_symlinks=False) else 'file' if entry.is_file(follow_symlinks=False) else 'special'
                    yield DirectoryEntry(entry.name, kind)
