"""Windows 路径安全快查：普通路径只读属性，重解析点仍核验类型。"""
import ctypes
import os
import stat

_get_attributes = None
if os.name == 'nt':
    from ctypes import wintypes
    _get_attributes = ctypes.WinDLL('kernel32', use_last_error=True).GetFileAttributesW
    _get_attributes.argtypes = [wintypes.LPCWSTR]
    _get_attributes.restype = wintypes.DWORD


def link_info(info):
    return stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0) == 0xA0000003


def increment(counts, key):
    if counts is not None:
        counts[key] = counts.get(key, 0) + 1


def windows_link(path, counts=None):
    # 不缓存安全检查结果。GetFileAttributesW 返回链接本身的属性。
    # 网络共享根、长路径或服务端不支持时由 lstat 保留原有异常语义。
    if _get_attributes is not None:
        increment(counts, 'attributeQueries')
        attributes = _get_attributes(os.fspath(path))
        if attributes != 0xffffffff and not attributes & 0x400:
            return False
        increment(counts, 'attributeFallbacks')
    increment(counts, 'lstatQueries')
    try:
        return link_info(path.lstat())
    except FileNotFoundError:
        return False
