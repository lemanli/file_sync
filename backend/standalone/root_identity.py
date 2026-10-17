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


class LegacyInformation(C.Structure):
    # FILETIME 是两个 DWORD，不能用会改变结构对齐的 uint64 替代。
    _fields_ = [('attributes', C.c_uint32), ('created', C.c_uint32 * 2),
                ('accessed', C.c_uint32 * 2), ('modified', C.c_uint32 * 2),
                ('volume', C.c_uint32), ('size_high', C.c_uint32),
                ('size_low', C.c_uint32), ('links', C.c_uint32),
                ('index_high', C.c_uint32), ('index_low', C.c_uint32)]


class NativeIdentityUnavailable(Exception):
    """接口或身份格式不可用；不代表网络错误或安全检查通过。"""


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
    legacy_call = kernel.GetFileInformationByHandle
    legacy_call.argtypes = [W.HANDLE, C.POINTER(LegacyInformation)]
    legacy_call.restype = W.BOOL
    _api = SimpleNamespace(open=open_call, info=info_call, close=close_call, legacy=legacy_call)


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


def legacy_root_stat(path, counts=None, capabilities=None):
    """兼容查询取得属性；保留可用的完整身份，仅在接口不支持时采用 64 位身份。"""
    if capabilities is None:
        capabilities = {}
    def unavailable(stage, code=0):
        increment(counts, 'rootLegacyUnavailable')
        increment(counts, 'rootLegacyFailure'+stage[0].upper()+stage[1:])
        if code:
            increment(counts, 'rootLegacyError'+str(code))
        sample(counts, path, 'identityBackend', 'targetRoot', 0,
               reason='legacy_root_unavailable', stage=stage, winError=code)
        raise NativeIdentityUnavailable(stage)
    if _api is None:
        unavailable('platform')
    increment(counts, 'rootLegacyQueries')
    handle = _api.open(native_path(path), 0x80, 3, None, 3, 0x02200000, None)
    if handle == C.c_void_p(-1).value or handle is None:
        code = last_error()
        if code in (1, 5, 32, 50, 87, 120):
            unavailable('open', code)
        raise win_error(code)
    try:
        value = LegacyInformation()
        if not _api.legacy(handle, C.byref(value)):
            code = last_error()
            if code in (1, 5, 32, 50, 87, 120):
                unavailable('query', code)
            raise win_error(code)
        tag = 0
        if value.attributes & 0x400:
            tagged = AttributeTag()
            if not _api.info(handle, 9, C.byref(tagged), C.sizeof(tagged)):
                code = last_error()
                if code in (1, 5, 32, 50, 87, 120):
                    unavailable('reparse', code)
                raise win_error(code)
            tag = tagged.tag
            if tag not in (0xA0000003, 0xA000000C):
                unavailable('reparse')
        inode = (value.index_high << 32) | value.index_low
        volume = value.volume
        # ReFS 等系统的 64 位编号不保证唯一。只要完整身份接口可用，就不能降为 64 位。
        if capabilities.get('id128', True):
            full = FileIdentity()
            increment(counts, 'rootCompatId128Queries')
            if _api.info(handle, 18, C.byref(full), C.sizeof(full)):
                inode = int.from_bytes(bytes(full.identity), 'little')
                volume = full.volume
            else:
                code = last_error()
                if code not in (1, 50, 87, 120):
                    raise win_error(code)
                capabilities['id128'] = False
                increment(counts, 'rootCompatId128Unsupported')
                increment(counts, 'rootCompatId128Error'+str(code))
        if not inode or not volume:
            unavailable('incomplete')
        mode = stat.S_IFDIR if value.attributes & 0x10 else stat.S_IFREG
        if tag == 0xA000000C:
            mode = stat.S_IFLNK
        return SimpleNamespace(st_dev=volume, st_ino=inode,
                               st_mode=mode, st_reparse_tag=tag)
    finally:
        if not _api.close(handle):
            raise win_error(last_error())


class RootIdentityReader:
    """每组选择查询接口；身份不缓存，兼容接口先与完整查询结果校准。"""
    def __init__(self, native=None, legacy=None):
        self.native = native or root_stat
        self.capabilities = {}
        selected = legacy if legacy is not None else (legacy_root_stat if native is None else None)
        self.legacy = (lambda path, counts: legacy_root_stat(path, counts, self.capabilities)) if selected is legacy_root_stat else selected
        self.mode = None
        self.probe_lock = threading.Lock()
        self.compat_lock = threading.Lock()
        self.compat_tried = False

    @staticmethod
    def _identity(info):
        return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode),
                getattr(info, 'st_reparse_tag', 0))

    def _calibrate(self, path, counts, baseline):
        if self.legacy is None or self.compat_tried:
            return baseline
        with self.compat_lock:
            if self.compat_tried:
                return baseline
            self.compat_tried = True
            try:
                current = self.legacy(path, counts)
            except NativeIdentityUnavailable:
                return baseline
            # 必须比较完整整数，不能将 128 位 inode 或卷身份截断后接受。
            if (baseline.st_ino and baseline.st_dev and
                    self._identity(current) == self._identity(baseline)):
                self.mode = 'legacy'
                increment(counts, 'rootLegacySelected')
                return current
            increment(counts, 'rootLegacyMismatch')
            # 不匹配可能是目录发生变化；重新使用完整查询，交由原安全判断处理。
            increment(counts, 'lstatQueries')
            return path.lstat()

    def _native(self, path, counts):
        previous = counts.get('rootNativeFallbacks', 0)
        try:
            result = self.native(path, counts)
        finally:
            if counts.get('rootNativeFallbacks', 0) > previous and self.mode != 'legacy':
                self.mode = False
        if counts.get('rootNativeFallbacks', 0) > previous:
            return self._calibrate(path, counts, result)
        if self.mode is None:
            self.mode = True
        return result

    def __call__(self, path, counts=None):
        if counts is None:
            counts = {}
        if self.mode is None:
            with self.probe_lock:
                if self.mode is None:
                    return self._native(path, counts)
        if self.mode == 'legacy':
            try:
                return self.legacy(path, counts)
            except NativeIdentityUnavailable:
                self.mode = False
        if self.mode is False:
            increment(counts, 'rootNativeBypassed')
            increment(counts, 'lstatQueries')
            return path.lstat()
        return self._native(path, counts)
