"""同步前目标权限检查；真实运行使用可清理的临时文件，演练不写入。"""
import os
import tempfile
from pathlib import Path


def check_destination(path, dry_run=False):
    destination = Path(path)
    probe_dir = destination
    # 未创建的目标检查最近存在父目录，不提前创建正式目录树。
    while True:
        try:
            probe_dir.stat()
            break
        except FileNotFoundError:
            if probe_dir == probe_dir.parent:
                raise ValueError(f'目标没有可访问的父目录：{destination}')
            probe_dir = probe_dir.parent
        except OSError as error:
            raise ValueError(f'无法访问目标目录：{probe_dir}；{error}') from error
    if not probe_dir.is_dir():
        raise ValueError(f'目标路径不是目录：{probe_dir}')
    if any(p.is_symlink() for p in (probe_dir, *probe_dir.parents)):
        raise ValueError(f'目标路径包含符号链接：{probe_dir}')
    try:
        with os.scandir(probe_dir) as entries:
            next(entries, None)
    except OSError as error:
        raise ValueError(f'目标目录不可读取：{probe_dir}；{error}') from error
    if dry_run:
        if not os.access(probe_dir, os.W_OK | os.X_OK):
            raise ValueError(f'目标目录权限检查未通过：{probe_dir}')
        return dict(path=str(destination), checkedDirectory=str(probe_dir), writeVerified=False,
                    reason='演练：目录可读取，权限检查通过；未实际写入，写权限尚未验证')
    temp = None
    try:
        fd, temp = tempfile.mkstemp(prefix='.sync-access-', dir=probe_dir)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(b'file-sync access check\n')
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise ValueError(f'目标目录无法创建或写入临时文件：{probe_dir}；{error}') from error
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError as error:
                raise ValueError(f'目标目录无法清理权限检查文件：{temp}；{error}') from error
    return dict(path=str(destination), checkedDirectory=str(probe_dir), writeVerified=True,
                reason='目标目录读写检查通过：临时文件创建、写入、清理成功' if probe_dir == destination else '目标尚不存在，已验证现有父目录可读取及创建、写入、清理临时文件')
