"""Windows 根目录身份快查：逐次按路径打开，只查询类型、重解析标记和完整身份。"""
import ctypes as C
import os
import ntpath
import stat
import sys
import threading
from types import SimpleNamespace
from .path_attributes import increment, sample


class AttributeTag(C.Structure):
    _fields_ = [('attributes', C.c_uint32), ('tag', C.c_uint32)]


class FileIdentity(C.Structure):
    _fields_ = [('volume', C.c_uint64), ('identity', C.c_ubyte * 16)]


last_error = getattr(C, 'get_last_error', lambda: 0)
win_error = getattr(C, 'WinError', lambda code: OSError(code, 'Windows API 查询失败'))
_api = None
# Python 3.12 的 stat 支持完整 128 位 inode；旧 Python 沿用原查询，避免身份格式不一致。
if os.name == 'nt' and sys.version_info >= (3, 12):
    from ctypes import wintypes as W
    kernel = C.WinDLL('kernel32', use_last_error=True)
    open_call = kernel.CreateFileW
    open_call.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    open_call.restype = W.HANDLE
    info_call = kernel.GetFileInformationByHandleEx
    info_call.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD]
    info_call.restype = W.BOOL
    close_call = kernel.CloseHandle
    close_call.argtypes = [W.HANDLE]
    close_call.restype = W.BOOL
    _api = SimpleNamespace(open=open_call, info=info_call, close=close_call)


def native_path(path):
    text = os.fspath(path)
    # 已由引擎解析为绝对路径；仅为长路径补充 Win32 扩展前缀。
    if len(text) >= 248 and not text.startswith(('\\\\?\\', '\\\\.\\')):
        if text.startswith('\\\\'):
            return '\\\\?\\UNC\\' + text[2:]
        if ntpath.isabs(text):
            return '\\\\?\\' + text
    return text


def root_stat(path, counts=None):
    def fallback(stage, code=0):
        increment(counts, 'rootNativeFallbacks')
        increment(counts, 'rootFallback'+stage[0].upper()+stage[1:])
        if code:
            increment(counts, 'rootFallbackError'+str(code))
        sample(counts, path, 'identityBackend', 'targetRoot', 0,
               reason='native_root_fallback', stage=stage, winError=code)
        increment(counts, 'lstatQueries')
        return path.lstat()
    if _api is None:
        return fallback('unavailable')
    increment(counts, 'rootNativeQueries')
    # FILE_READ_ATTRIBUTES；允许并行读写，不允许核验期间删除/重命名根目录。
    # BACKUP_SEMANTICS | OPEN_REPARSE_POINT：打开目录且不跟随根目录链接。
    handle = _api.open(native_path(path), 0x80, 3, None, 3, 0x02200000, None)
    if handle == C.c_void_p(-1).value or handle is None:
        code = last_error()
        if code in (1, 5, 32, 50, 87, 120):
            # 保留 Python 对权限/共享限制的兼容处理；回退也必须返回真实身份。
            return fallback('open', code)
        raise win_error(code)
    unsupported = False
    fallback_stage, fallback_code = None, 0
    try:
        tag = AttributeTag()
        identity = FileIdentity()
        for kind, value in ((9, tag), (18, identity)):
            if not _api.info(handle, kind, C.byref(value), C.sizeof(value)):
                code = last_error()
                if code in (1, 50, 87, 120):
                    unsupported = True
                    fallback_stage = 'attributeTag' if kind == 9 else 'fileId'
                    fallback_code = code
                    break
                raise win_error(code)
        # 非链接重解析点由 Python 保持原有文件系统专用语义。
        if tag.attributes & 0x400 and tag.tag not in (0xA0000003, 0xA000000C):
            unsupported = True
            fallback_stage, fallback_code = 'reparse', 0
        if not unsupported:
            mode = stat.S_IFDIR if tag.attributes & 0x10 else stat.S_IFREG
            if tag.tag == 0xA000000C:
                mode = stat.S_IFLNK
            result = SimpleNamespace(st_mode=mode, st_dev=identity.volume,
                                     st_ino=int.from_bytes(bytes(identity.identity), 'little'),
                                     st_reparse_tag=tag.tag)
    finally:
        # 包括查询异常和回退在内，每次都关闭句柄；不会跨文件缓存身份或句柄。
        if not _api.close(handle):
            raise win_error(last_error())
    return fallback(fallback_stage, fallback_code) if unsupported else result


class RootIdentityReader:
    """每个目录组独立选择查询方式；只记兼容性，不缓存任何目录身份。"""
    def __init__(self, native=None):
        self.native = native or root_stat
        self.mode = None
        self.probe_lock = threading.Lock()

    def _native(self, path, counts):
        previous = counts.get('rootNativeFallbacks', 0)
        try:
            result = self.native(path, counts)
        finally:
            if counts.get('rootNativeFallbacks', 0) > previous:
                self.mode = False
        # 另一线程可能已发现不兼容，不能被较早启动的成功查询重新启用。
        if self.mode is None:
            self.mode = True
        return result

    def __call__(self, path, counts=None):
        if counts is None:
            counts = {}
        if self.mode is None:
            # 首次只让一个线程探测，避免 32 个 worker 同时重复失败。
            with self.probe_lock:
                if self.mode is None:
                    return self._native(path, counts)
        if self.mode is False:
            increment(counts, 'rootNativeBypassed')
            increment(counts, 'lstatQueries')
            return path.lstat()
        # 正常核验与回退 lstat 均不持有探测锁，独立文件继续并行。
        return self._native(path, counts)
