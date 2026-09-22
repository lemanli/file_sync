"""浏览后台所在机器的目录；网络共享需由运行账户事先认证。"""
import os
import re
from pathlib import Path
from fastapi import HTTPException


def directory_roots():
    if os.name == 'nt':
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        return [dict(name=f'{chr(65+i)}:', path=f'{chr(65+i)}:\\') for i in range(26) if mask & (1 << i)]
    candidates = [Path.home().resolve(), Path('/'), Path('/Volumes'), Path('/mnt'), Path('/media')]
    return [dict(name=str(p), path=str(p)) for p in candidates if p.is_dir()]


def natural_key(item):
    # 分段按数字比较，名称相同时用原始名称和路径保证分页顺序稳定。
    parts = tuple((0, int(part)) if part.isdecimal() else (1, part.casefold())
                  for part in re.split(r'(\d+)', item['name']))
    return parts, item['name'], item['path']


def directory_page(entries, path, parent, offset, limit, query, sort):
    keyword = query.strip().casefold()
    entries = [entry for entry in entries if keyword in entry['name'].casefold()]
    entries.sort(key=natural_key)
    if sort == 'name_desc':
        entries.reverse()
    elif sort.startswith('modified_'):
        # 稳定排序让时间相同的目录保持名称自然顺序。
        entries.sort(key=lambda entry: entry.get('modified') or 0, reverse=sort.endswith('desc'))
    total = len(entries)
    page = entries[offset:offset+limit]
    return dict(path=path, parent=parent, directories=page, total=total,
                nextOffset=offset+len(page) if offset+len(page)<total else None)


def list_directories(raw, protected, offset=0, limit=200, query='', sort='name_asc'):
    if sort not in {'name_asc', 'name_desc', 'modified_asc', 'modified_desc'}:
        raise HTTPException(422, '不支持的目录排序方式')
    if not raw:
        entries = directory_roots()
        if sort.startswith('modified_'):
            for entry in entries:
                try:
                    entry['modified'] = Path(entry['path']).stat().st_mtime
                except OSError:
                    entry['modified'] = None
        return directory_page(entries, '', None, offset, limit, query, sort)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(422, '请输入绝对路径；网络共享填写完整 UNC 或挂载路径')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise HTTPException(422, '不支持符号链接目录，请输入真实路径')
    path = path.resolve()
    if path == protected or protected in path.parents:
        raise HTTPException(403, '程序数据库目录不可选择')
    try:
        entries = []
        keyword = query.strip().casefold()
        # 先搜索和排序整个当前层，再分页；不递归扫描子目录。
        with os.scandir(path) as iterator:
            for entry in iterator:
                if keyword not in entry.name.casefold():
                    continue
                if not entry.is_dir(follow_symlinks=False) or Path(entry.path) == protected:
                    continue
                item = dict(name=entry.name, path=entry.path)
                if sort.startswith('modified_'):
                    item['modified'] = entry.stat(follow_symlinks=False).st_mtime
                entries.append(item)
        return directory_page(entries, str(path), str(path.parent) if path.parent != path else None,
                              offset, limit, query, sort)
    except FileNotFoundError:
        raise HTTPException(404, '目录不存在或网络共享未连接')
    except NotADirectoryError:
        raise HTTPException(422, '请选择文件夹，不能选择文件')
    except PermissionError:
        raise HTTPException(403, '当前运行账户没有访问该目录的权限')
    except OSError as error:
        raise HTTPException(422, f'无法访问目录：{error}')
