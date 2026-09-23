"""CPython scandir 已调用 libc readdir/getdents，避免重复封装系统调用。
DT_UNKNOWN 下类型判断可能隐式 stat；不能将显式计数当作全部内核调用。
"""
from .portable import PortableScanner
class LinuxScanner(PortableScanner):
    name = 'linux-scandir'
