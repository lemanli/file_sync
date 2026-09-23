"""平台选择与显式基准选项；回退在完整目录快照层执行。"""
import sys
from .base import DirectoryEntry, ScanFields, ScanMetrics, UnsupportedScanner
from .portable import PortableScanner

def get_scanner(backend='auto'):
    if backend == 'portable': return PortableScanner()
    if sys.platform == 'darwin' and backend in ('auto','macos'):
        from .macos import MacOSScanner, MacOSAutoScanner
        return MacOSAutoScanner() if backend=='auto' else MacOSScanner()
    if sys.platform == 'win32' and backend in ('auto','windows_bulk','windows_find'):
        from .windows import WindowsScanner
        # 未实测前 auto 使用成熟的 CPython 枚举；A/B 可显式基准。
        if backend == 'auto': return PortableScanner()
        return WindowsScanner(backend)
    if sys.platform.startswith('linux') and backend in ('auto','linux'):
        from .linux import LinuxScanner
        return LinuxScanner()
    if backend != 'auto': raise ValueError('当前平台不支持扫描后端：'+backend)
    return PortableScanner()
