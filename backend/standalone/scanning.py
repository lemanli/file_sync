"""流式目录预检：保留 DirEntry 类型信息，不为普通文件重复调用 lstat。"""
import os
from pathlib import Path


def scan_tree(root, visit, onerror, directory_started=lambda path:None):
    """visit 返回真才进入子目录；每次仅打开一个目录，不保存文件列表。"""
    root=Path(root)
    pending=[(root,Path())]
    while pending:
        base,relative_dir=pending.pop()
        directory_started(base)
        # 目录在枚举和进入之间可能被替换成链接，不能跟随新链接。
        if base.is_symlink():
            raise ValueError('扫描目录已被替换为符号链接：'+str(base))
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    relative=relative_dir/entry.name
                    if visit(entry,relative):
                        pending.append((Path(entry.path),relative))
        except OSError as error:
            onerror(error)
