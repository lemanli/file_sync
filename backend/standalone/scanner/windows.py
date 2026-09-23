"""Windows 目录批量枚举 A/B；reparse point 一律不跟随。"""
import ctypes as C
from ctypes import wintypes as W
import os
import errno
import struct
from .base import DirectoryEntry, ScanFields, UnsupportedScanner

EPOCH = 116444736000000000

def entry(name, attributes, size, ticks, fields):
    kind = 'link' if attributes & 0x400 else 'directory' if attributes & 0x10 else 'file'
    return DirectoryEntry(name,kind,size if kind=='file' and fields & ScanFields.SIZE else None,
                          (ticks-EPOCH)*100 if kind=='file' and fields & ScanFields.MTIME else None)

def windows_error(code,path):
    if hasattr(C,'WinError'):
        error=C.WinError(code)
        error.filename=os.fspath(path)
    else:
        # 非 Windows 上也能验证错误分类契约。
        mapped={2:errno.ENOENT,3:errno.ENOENT,5:errno.EACCES,267:errno.ENOTDIR,80:errno.EEXIST,183:errno.EEXIST}
        error=OSError(mapped.get(code,errno.EIO),'Windows error '+str(code),os.fspath(path))
    if code in (1,50,87,120,124):error=UnsupportedScanner(error.errno,str(error),os.fspath(path))
    error.winerror=code
    return error


class FindData(C.Structure):
    _fields_ = [('attributes',W.DWORD),('creation',W.FILETIME),('access',W.FILETIME),
                ('write',W.FILETIME),('high',W.DWORD),('low',W.DWORD),
                ('reserved0',W.DWORD),('reserved1',W.DWORD),('name',W.WCHAR*260),('alternate',W.WCHAR*14)]

class WindowsScanner:
    def __init__(self, backend='windows_bulk'):
        self.name = backend
    def scan(self,path,fields,metrics):
        kernel = C.WinDLL('kernel32',use_last_error=True)
        close = kernel.CloseHandle;close.argtypes=[W.HANDLE];close.restype=W.BOOL
        def fail():
            code = C.get_last_error()
            raise windows_error(code,path)
        if self.name == 'windows_find':
            first=kernel.FindFirstFileExW;first.argtypes=[W.LPCWSTR,C.c_int,C.c_void_p,C.c_int,C.c_void_p,W.DWORD];first.restype=W.HANDLE
            next_entry=kernel.FindNextFileW;next_entry.argtypes=[W.HANDLE,C.POINTER(FindData)];next_entry.restype=W.BOOL
            finish=kernel.FindClose;finish.argtypes=[W.HANDLE];finish.restype=W.BOOL
            data=FindData();metrics.add('nativeCalls')
            handle=first(os.path.join(os.fspath(path),'*'),1,C.byref(data),0,None,2)
            if handle == C.c_void_p(-1).value:
                # 根目录已由上层确认存在；ERROR_FILE_NOT_FOUND 表示空匹配。
                if C.get_last_error()==2: return
                fail()
            try:
                while True:
                    if data.name not in ('.','..'):
                        yield entry(data.name,data.attributes,(data.high<<32)|data.low,
                                    (data.write.dwHighDateTime<<32)|data.write.dwLowDateTime,fields)
                    metrics.add('nativeCalls')
                    if not next_entry(handle,C.byref(data)):
                        if C.get_last_error()==18:return
                        fail()
            finally:finish(handle)
            return
        create=kernel.CreateFileW;create.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE];create.restype=W.HANDLE
        query=kernel.GetFileInformationByHandleEx;query.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD];query.restype=W.BOOL
        handle=create(os.fspath(path),1,7,None,3,0x02000000|0x00200000,None)
        if handle == C.c_void_p(-1).value:fail()
        buffer=C.create_string_buffer(256*1024)
        try:
            while True:
                metrics.add('nativeCalls')
                if not query(handle,14,buffer,len(buffer)):
                    if C.get_last_error()==18:return
                    fail()
                raw=buffer.raw;offset=0
                while True:
                    if offset+68>len(raw):raise UnsupportedScanner('目录记录越界')
                    following=struct.unpack_from('<I',raw,offset)[0]
                    ticks=struct.unpack_from('<q',raw,offset+24)[0]
                    size=struct.unpack_from('<q',raw,offset+40)[0]
                    attributes,length=struct.unpack_from('<II',raw,offset+56)
                    end=offset+following if following else len(raw)
                    if length%2 or offset+68+length>end or end>len(raw):raise UnsupportedScanner('目录名称越界')
                    name=raw[offset+68:offset+68+length].decode('utf-16-le','surrogatepass')
                    if name not in ('.','..'):yield entry(name,attributes,size,ticks,fields)
                    if not following:break
                    if following<68:raise UnsupportedScanner('目录记录偏移无效')
                    offset+=following
        finally:close(handle)
