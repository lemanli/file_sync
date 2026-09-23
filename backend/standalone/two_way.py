"""双向合并：先比较两端初始状态，再执行单一方向的文件计划，不传播删除。"""
import os
import stat
import time
from pathlib import Path
from .rules import IgnoreRules
from .recovery import network_error, NetworkUnavailable
from .comparison import compare, metadata, method_name, tolerance_ns


def fingerprint(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return (info.st_mode, info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino)


def synchronize_two_way(config, emit):
    from .engine import roots, digest, _synchronize_one
    if config.get('delete'):
        raise ValueError('双向合并不支持传播删除，请关闭删除选项')
    left, right = roots(config['source'], config['target'])
    # 两侧都可能被写入，反向根路径也必须满足同一安全约束。
    if right.exists():
        roots(str(right), str(left))
    progress = config.get('_progress', lambda *args, **kwargs: None)
    progress('phase', phase='planning')
    start = time.monotonic()
    totals = dict(copied=0, skipped=0, deleted=0, bytes=0, scanned=0,
                  unsupportedSkipped=0, ignored=0, failed=0, conflicts=0)
    rules = IgnoreRules(config.get('ignorePatterns', ()))
    blocked = set()
    identities = {p: (p.stat().st_dev,p.stat().st_ino) for p in (left,right) if p.exists()}
    def check_roots():
        for path, identity in identities.items():
            info = path.stat()
            if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or (info.st_dev,info.st_ino) != identity:
                raise RuntimeError(f'双向同步根目录已失效或被替换：{path}')
    def failure(relative, error, side):
        if config.get('continueOnError') and network_error(error):
            raise NetworkUnavailable(str(error)) from error
        blocked.add(relative)
        totals['failed'] += 1
        emit(dict(action='failed',path=str(relative),error=str(error),bytes=0,direction=side))
        if not config.get('continueOnError'):
            raise error
    def scan(root, side):
        items = {}
        if not root.exists():
            return items
        def scan_error(error):
            if Path(error.filename)==root:
                raise error
            failure(Path(error.filename).relative_to(root), error, side)
        for base, directories, files in os.walk(root, onerror=scan_error):
            for name in directories + files:
                if config.get('_control'):
                    config['_control'].checkpoint()
                progress('scan')
                entry = Path(base)/name
                relative = entry.relative_to(root)
                if rules.matches(relative, name in directories):
                    blocked.add(relative)
                    if name in directories:
                        directories.remove(name)
                    totals['ignored'] += 1
                    emit(dict(action='ignored',path=str(relative),reason='匹配忽略规则',direction=side,bytes=0))
                    continue
                try:
                    info = fingerprint(entry)
                    if info is None:
                        raise FileNotFoundError(f'扫描期间条目消失：{entry}')
                    if not (stat.S_ISREG(info[0]) or stat.S_ISDIR(info[0])):
                        if not config.get('skipUnsupported', True):
                            raise ValueError(f'双向目录包含不支持的链接或特殊文件：{entry}')
                        blocked.add(relative)
                        if name in directories:
                            directories.remove(name)
                        totals['skipped'] += 1
                        totals['unsupportedSkipped'] += 1
                        emit(dict(action='skipped',path=str(relative),reason='双向排除链接或特殊文件及另一侧对应路径',bytes=0,direction=side))
                        continue
                    items[relative] = info
                except OSError as error:
                    failure(relative,error,side)
                    if name in directories:
                        directories.remove(name)
        return items
    a,b = scan(left,'source_to_target'),scan(right,'target_to_source')
    plans = [set(),set()]
    expected = [{},{}]
    def is_blocked(path):
        return path in blocked or any(p in blocked for p in path.parents)
    def conflict(path, reason, comparison=None):
        blocked.add(path)
        totals['conflicts'] += 1
        emit(dict(action='conflict',path=str(path),bytes=0,reason=reason,direction='both',comparison=comparison))
    # 父目录先处理，类型冲突时整个子树都不能被后续计划覆盖。
    for relative in sorted(a.keys() | b.keys(), key=lambda p:(len(p.parts),str(p))):
        if config.get('_control'):
            config['_control'].checkpoint()
        if is_blocked(relative):
            continue
        x,y = a.get(relative),b.get(relative)
        if x and y and stat.S_ISDIR(x[0]) != stat.S_ISDIR(y[0]):
            conflict(relative,'两端文件与目录类型不同，保留两边内容')
            continue
        if (x and stat.S_ISDIR(x[0])) or (y and stat.S_ISDIR(y[0])):
            continue
        progress('scan')
        totals['scanned'] += 1
        direction = None
        if x is None:
            direction = 1
        elif y is None:
            direction = 0
        else:
            try:
                progress('file', path=str(relative), size=x[1], written=0, phase='comparing')
                equal, detail = compare(left/relative,right/relative,metadata(x[1],x[2]),metadata(y[1],y[2]),method_name(config),digest,tolerance_ns(config))
            except OSError as error:
                failure(relative,error,'both')
                continue
            if equal:
                totals['skipped'] += 1
                emit(dict(action='skipped',path=str(relative),bytes=0,reason=detail['reason'],comparison=detail,direction='both'))
            elif config.get('conflictPolicy','skip') == 'newer' and abs(x[2] - y[2]) > tolerance_ns(config):
                direction = 0 if x[2]>y[2] else 1
            else:
                conflict(relative,'同名文件比较结果不同，保留两边内容；时间差在容差内也不覆盖',detail)
        if direction is not None:
            plans[direction].add(relative)
            expected[direction][relative] = (x,y) if direction==0 else (y,x)
    progress('clear_file')
    check_roots()
    for index,(source,target,items) in enumerate(((left,right,a),(right,left,b))):
        if not source.exists():
            # 目标原本不存在的演练不创建目录，也没有反向文件计划。
            if index==1 and not plans[index]:
                continue
            raise RuntimeError(f'双向源目录已失效：{source}')
        allowed_dirs = {p for p,info in items.items() if stat.S_ISDIR(info[0]) and not is_blocked(p)}
        direction = 'source_to_target' if index==0 else 'target_to_source'
        check_roots()
        def directional_emit(event):
            emit(dict(event,direction=direction))
        result = _synchronize_one(dict(config,source=str(source),target=str(target),updateOnly=False,
                                       _skipPaths=blocked,_allowedFiles=plans[index],_allowedDirs=allowed_dirs,
                                       _expected=expected[index]),directional_emit)
        for key in ('copied','skipped','deleted','bytes','unsupportedSkipped','ignored','failed'):
            totals[key] += result[key]
    check_roots()
    totals.update(durationSeconds=round(time.monotonic()-start,4),dryRun=config.get('dryRun',False),deletionSkipped=False)
    return totals
