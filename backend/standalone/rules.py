"""单机忽略规则：相对路径匹配，目录剪枝与目标删除使用同一规则。"""
import fnmatch
import re
from functools import lru_cache
from pathlib import PurePosixPath


def validate_patterns(patterns):
    for raw in patterns:
        value = raw.strip()
        if not value or value.startswith('#'):
            continue
        if value.startswith('!') or '\\' in value or '..' in value.split('/'):
            raise ValueError('忽略规则不支持 ! 反选、反斜杠或 ..，请使用相对路径及 /')
    return patterns


@lru_cache(maxsize=512)
def path_regex(pattern):
    parts = pattern.split('/')
    pieces = []
    for index, part in enumerate(parts):
        if part == '**':
            pieces.append('.*' if index == len(parts)-1 else '(?:[^/]+/)*')
        else:
            # fnmatch 单段不跨目录，** 才匹配多层目录。
            pieces.append(''.join('[^/]*' if c == '*' else '[^/]' if c == '?' else re.escape(c) for c in part))
            if index < len(parts)-1:
                pieces.append('/')
    return re.compile('^' + ''.join(pieces) + '$')


class IgnoreRules:
    def __init__(self, patterns=()):
        validate_patterns(patterns)
        self.patterns = [p.strip() for p in patterns if p.strip() and not p.strip().startswith('#')]

    def matches(self, relative, is_dir=False):
        value = PurePosixPath(relative.as_posix())
        # 文件位于被忽略目录内时也受保护，尤其用于目标端删除。
        candidates = [(value.as_posix(), is_dir)]
        candidates.extend((p.as_posix(), True) for p in value.parents if p.as_posix() != '.')
        for raw in self.patterns:
            directory_only = raw.endswith('/')
            anchored = raw.startswith('/')
            pattern = raw.strip('/')
            if not pattern:
                continue
            for name, directory in candidates:
                if directory_only and not directory:
                    continue
                if '/' not in pattern and not anchored:
                    if fnmatch.fnmatchcase(name.rsplit('/',1)[-1], pattern):
                        return True
                elif path_regex(pattern).fullmatch(name):
                    return True
        return False
