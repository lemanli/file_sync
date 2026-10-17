"""Windows 路径安全快查：普通路径只读属性，重解析点仍核验类型。"""
import ctypes
import os
import stat
import ntpath
import time

_last_error = getattr(ctypes, 'get_last_error', lambda: 0)

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


def path_role(path):
    drive, tail = ntpath.splitdrive(os.fspath(path))
    return 'shareRoot' if drive.startswith('\\\\') and tail in ('', '\\') else 'ancestor'


def sample(counts, path, kind, role, seconds, **extra):
    if counts is not None and counts.get('_details'):
        samples = counts.setdefault('_samples', [])
        if len(samples) < 4:
            samples.append(dict(path=os.fspath(path), query=kind, role=role, seconds=seconds, **extra))


def query(call, path, counts, kind, role, collect=True):
    """只在诊断开启时计时；异常照常传播，累计字段使用固定分类。"""
    if counts is None or not counts.get('_details'):
        return call()
    started = time.perf_counter()
    try:
        return call()
    finally:
        seconds = max(0, time.perf_counter()-started)
        prefix = kind+role[0].upper()+role[1:]
        counts[prefix+'Seconds'] = counts.get(prefix+'Seconds', 0)+seconds
        increment(counts, prefix+'Count')
        if collect and seconds >= .05:
            sample(counts, path, kind, role, seconds, reason='slow_query')


def windows_link(path, counts=None):
    # 不缓存安全结果；原生调用失败和重解析点仍走原有核验。
    details = counts is not None and counts.get('_details')
    role = path_role(path) if details else 'ancestor'
    if _get_attributes is not None:
        increment(counts, 'attributeQueries')
        def attributes_call():
            value = _get_attributes(os.fspath(path))
            # 必须紧随原生调用读取线程局部错误，不能等后续 lstat 覆盖。
            return value, _last_error() if value == 0xffffffff else 0
        key = 'attributes'+role[0].upper()+role[1:]+'Seconds'
        before = counts.get(key, 0) if details else 0
        attributes, error = query(attributes_call, path, counts, 'attributes', role, collect=False)
        seconds = counts.get(key, 0)-before if details else 0
        if attributes != 0xffffffff and not attributes & 0x400:
            if seconds >= .05:
                sample(counts, path, 'attributes', role, seconds, reason='slow_query')
            return False
        increment(counts, 'attributeFallbacks')
        if details:
            bucket = ('Reparse' if attributes != 0xffffffff else
                      {2:'NotFound',3:'PathNotFound',5:'AccessDenied',53:'BadNetworkPath',
                       67:'BadNetworkName',123:'InvalidName',206:'LongPath'}.get(error,'OtherError'))
            increment(counts, 'fallback'+bucket)
            sample(counts, path, 'attributes', role, seconds,
                   reason='reparse_point' if attributes != 0xffffffff else 'native_error', winError=error)
    increment(counts, 'lstatQueries')
    try:
        return link_info(query(path.lstat, path, counts, 'lstat', role))
    except FileNotFoundError:
        return False
