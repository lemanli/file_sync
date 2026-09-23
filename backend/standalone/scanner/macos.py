"""Darwin getattrlistbulk：4 字节打包布局，只请求必要属性。"""
import ctypes as C
import errno
import os
import struct
from .base import DirectoryEntry, ScanFields, UnsupportedScanner

class AttrList(C.Structure):
    _fields_ = [('count',C.c_uint16),('reserved',C.c_uint16),('common',C.c_uint32),
                ('volume',C.c_uint32),('directory',C.c_uint32),('file',C.c_uint32),('fork',C.c_uint32)]

class MacOSScanner:
    name = 'getattrlistbulk'
    def scan(self, path, fields, metrics):
        libc = C.CDLL(None, use_errno=True)
        try: call = libc.getattrlistbulk
        except AttributeError as error: raise UnsupportedScanner('getattrlistbulk 不可用') from error
        call.argtypes = [C.c_int,C.POINTER(AttrList),C.c_void_p,C.c_size_t,C.c_uint64]
        call.restype = C.c_int
        common = 0x80000000 | 0x20000000 | 1 | 8
        if fields & ScanFields.MTIME: common |= 0x400
        attrs = AttrList(5,0,common,0,0,0x200 if fields & ScanFields.SIZE else 0,0)
        buffer = C.create_string_buffer(256*1024)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            while True:
                count = call(fd,C.byref(attrs),buffer,len(buffer),8)
                metrics.add('nativeCalls')
                if count < 0:
                    code = C.get_errno()
                    cls = UnsupportedScanner if code in (errno.ENOTSUP,errno.ENOSYS,errno.EINVAL) else OSError
                    raise cls(code,os.strerror(code),os.fspath(path))
                if not count: return
                raw = buffer.raw; offset = 0
                for _ in range(count):
                    length = struct.unpack_from('=I',raw,offset)[0]
                    end = offset+length
                    if length < 40 or end > len(raw): raise UnsupportedScanner('无效 bulk 记录长度')
                    returned = struct.unpack_from('=5I',raw,offset+4)
                    error = struct.unpack_from('=I',raw,offset+24)[0]
                    if error: raise OSError(error,os.strerror(error),os.fspath(path))
                    ref = offset+28
                    delta,name_length = struct.unpack_from('=iI',raw,ref)
                    start = ref+delta
                    if name_length < 1 or start < offset or start+name_length > end:
                        raise UnsupportedScanner('无效 bulk 文件名偏移')
                    name = os.fsdecode(raw[start:start+name_length-1])
                    kind_code = struct.unpack_from('=I',raw,offset+36)[0]
                    if returned[0] & (1|8) != (1|8): raise UnsupportedScanner('缺少名称或类型')
                    cursor = offset+40; mtime = size = None
                    if fields & ScanFields.MTIME:
                        if cursor+16 > end: raise UnsupportedScanner('缺少时间字段')
                        sec,ns = struct.unpack_from('=qq',raw,cursor);cursor+=16
                        if not returned[0] & 0x400: raise UnsupportedScanner('文件系统不返回修改时间')
                        mtime = sec*1_000_000_000+ns
                    if fields & ScanFields.SIZE:
                        if cursor+8 > end: raise UnsupportedScanner('缺少大小字段')
                        size = struct.unpack_from('=q',raw,cursor)[0]
                        if kind_code == 1 and not returned[3] & 0x200: raise UnsupportedScanner('文件系统不返回文件大小')
                    yield DirectoryEntry(name,{1:'file',2:'directory',5:'link'}.get(kind_code,'special'),size if kind_code==1 else None,mtime if kind_code==1 else None)
                    offset = end
        finally: os.close(fd)


class MacOSAutoScanner:
    name = 'macos-auto'
    def scan(self,path,fields,metrics):
        # 本地基准 NAME/TYPE 使用 scandir 更快；大小/时间才使用 bulk。
        from .portable import PortableScanner
        scanner = MacOSScanner() if fields & (ScanFields.SIZE|ScanFields.MTIME) else PortableScanner()
        yield from scanner.scan(path,fields,metrics)
