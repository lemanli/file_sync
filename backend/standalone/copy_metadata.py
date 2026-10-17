"""临时文件只读取长度和修改时间，避免为内容校验查询完整文件身份。"""
import ctypes as C
import os
from types import SimpleNamespace
from .root_identity import native_path


class FileAttributes(C.Structure):
    _fields_ = [('attributes', C.c_uint32), ('created', C.c_uint32 * 2),
                ('accessed', C.c_uint32 * 2), ('modified', C.c_uint32 * 2),
                ('size_high', C.c_uint32), ('size_low', C.c_uint32)]


_query = None
_last_error = getattr(C, 'get_last_error', lambda: 0)
_win_error = getattr(C, 'WinError', lambda code: OSError(code, 'Windows 属性查询失败'))
if os.name == 'nt':
    from ctypes import wintypes as W
    _query = C.WinDLL('kernel32', use_last_error=True).GetFileAttributesExW
    _query.argtypes = [W.LPCWSTR, C.c_int, C.POINTER(FileAttributes)]
    _query.restype = W.BOOL


class CopyMetadataReader:
    """每个目录组记住接口可用性，不缓存文件属性。"""
    def __init__(self):
        self.native = _query

    def __call__(self, path):
        native = self.native
        if native is None:
            return path.stat()
        value = FileAttributes()
        if not native(native_path(path), 0, C.byref(value)):
            code = _last_error()
            if code in (1, 5, 32, 50, 87, 120):
                # 接口不兼容只尝试一次；其他在途线程可能各完成一次调用。
                self.native = None
                return path.stat()
            raise _win_error(code)
        if value.attributes & 0x400:
            # 重解析点仍沿用 Python stat 的行为，不把链接属性误当目标属性。
            return path.stat()
        ticks = (value.modified[1] << 32) | value.modified[0]
        return SimpleNamespace(st_size=(value.size_high << 32) | value.size_low,
                               st_mtime_ns=(ticks - 116444736000000000) * 100)
