"""SKIP_EXISTING 的原子不覆盖发布，避免扫描后新文件被覆盖。"""
import ctypes as C
import errno
import os
import sys
from enum import IntEnum


class Publication(IntEnum):
    EXISTS = 0
    LINKED = 1  # 硬链接发布后仍需删除临时名称。
    RENAMED = 2  # 重命名已消耗临时名称，无需额外远程删除。


def publish_if_absent(temporary,target):
    """返回已存在、硬链接或重命名结果；不支持时安全失败，绝不降级为覆盖。"""
    try:
        if os.name == 'nt':
            # Windows rename 不允许替换已有名称。
            os.rename(temporary,target)
            return Publication.RENAMED
        libc=C.CDLL(None,use_errno=True)
        if sys.platform=='darwin' and hasattr(libc,'renamex_np'):
            call=libc.renamex_np;call.argtypes=[C.c_char_p,C.c_char_p,C.c_uint];call.restype=C.c_int
            result=call(os.fsencode(temporary),os.fsencode(target),4)  # RENAME_EXCL
        elif sys.platform.startswith('linux') and hasattr(libc,'renameat2'):
            call=libc.renameat2;call.argtypes=[C.c_int,C.c_char_p,C.c_int,C.c_char_p,C.c_uint];call.restype=C.c_int
            result=call(-100,os.fsencode(temporary),-100,os.fsencode(target),1)  # AT_FDCWD / RENAME_NOREPLACE
        else:result=None
        if result==0:return Publication.RENAMED
        if result is not None:
            code=C.get_errno()
            if code not in (errno.ENOSYS,errno.ENOTSUP,errno.EINVAL):
                raise OSError(code,os.strerror(code),os.fspath(target))
        # 临时文件与目标在同一目录；硬链接创建也是原子不覆盖。
        os.link(temporary,target,follow_symlinks=False)
        return Publication.LINKED
    except FileExistsError:
        return Publication.EXISTS
