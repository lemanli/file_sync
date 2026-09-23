"""本地与已挂载网络共享的临时文件同步；每个文件只交给一个工作线程。"""
from __future__ import annotations
from .control import RunInterrupted
import hashlib
import os
import shutil
import stat
import tempfile
import time
import threading
from contextlib import contextmanager, nullcontext, closing
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from .rules import IgnoreRules
from .comparison import compare, metadata, method_name, tolerance_ns
from .scanning import scan_tree
from .recovery import network_error, NetworkUnavailable


class UnsafePathError(RuntimeError):
    """根目录或路径边界失效，即使容错模式也必须停止。"""


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.digest()


def is_link(path):
    return path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction())


def roots(source, target):
    # 禁止源目标重叠和符号链接路径，避免递归复制或越界写入。
    for raw in (source, target):
        if not Path(raw).is_absolute():
            raise ValueError('请填写绝对目录路径')
        path = Path(raw).absolute()
        if any(is_link(p) for p in (path, *path.parents)):
            raise ValueError('目录及父目录不能是符号链接')
    src, dst = Path(source).resolve(), Path(target).resolve()
    if dst == Path(dst.anchor):
        raise ValueError('目标不能是磁盘根目录')
    if src == dst or src in dst.parents or dst in src.parents:
        raise ValueError('源目录与目标目录不能相同或互相包含')
    try:
        source_mode = src.stat().st_mode
    except OSError as error:
        raise ValueError('源目录无法读取：'+str(src)) from error
    if not stat.S_ISDIR(source_mode):
        raise ValueError('源目录不存在或不是目录')
    if dst.exists() and not dst.is_dir():
        raise ValueError('目标必须是目录')
    return src, dst


def synchronize(config, emit):
    config = dict(config)
    strategy = config.get('syncStrategy', 'AUTO')
    if strategy not in ('AUTO','COPY_ALL','SKIP_EXISTING','COMPARE_METADATA','MIRROR'):
        raise ValueError('未知同步策略')
    if strategy != 'AUTO':
        if config.get('direction') == 'bidirectional':
            raise ValueError('双向同步请使用兼容策略 AUTO')
        if config.get('updateOnly') or (config.get('delete') and strategy != 'MIRROR'):
            raise ValueError('新策略不能组合仅更新或独立删除选项')
        config['delete'] = strategy == 'MIRROR'
        config['comparisonMode'] = 'size_mtime'
        config['checksum'] = False
    if config.get("direction") == "bidirectional":
        from .two_way import synchronize_two_way
        return synchronize_two_way(config, emit)
    if strategy != 'AUTO':
        from .directory_plan import SpillSet
        from contextlib import ExitStack
        with ExitStack() as resources:
            for key in ('_excludedStore','_deletionStore'):
                config[key]=SpillSet(config.get('_snapshotLimit',10000))
                resources.callback(config[key].close)
            return _synchronize_one(config, emit)
    return _synchronize_one(config, emit)


def _synchronize_one(config, emit):
    # 已完成文件立即交给日志缓冲，避免同批后续安全异常丢失成功记录。
    callback, event_lock = emit, threading.Lock()
    def emit(event):
        with event_lock:
            callback(event)
    src, dst = roots(config['source'], config['target'])
    source_identity = (src.stat().st_dev, src.stat().st_ino)
    target_identity = (dst.stat().st_dev, dst.stat().st_ino) if dst.exists() else None
    stop = threading.Event()
    publish_lock = threading.Lock()
    network_failure = []
    def raise_network(error):
        if config.get('continueOnError') and network_error(error):
            if not network_failure:
                network_failure.append(error)
            stop.set()
            raise NetworkUnavailable(str(error)) from error

    def check_source():
        try:
            current = src.stat()
            valid = not is_link(src) and src.is_dir() and (current.st_dev, current.st_ino) == source_identity
        except OSError as error:
            raise_network(error)
            valid = False
        if not valid:
            raise UnsafePathError('源目录已失效或被替换，终止同步及删除')
    def check_target(path):
        if any(is_link(p) for p in (path, *path.parents)):
            raise UnsafePathError(f'目标路径出现符号链接：{path}')
        if target_identity is not None:
            try:
                current = dst.stat()
                valid = dst.is_dir() and (current.st_dev, current.st_ino) == target_identity
            except OSError as error:
                raise_network(error)
                valid = False
            if not valid:
                raise UnsafePathError('目标根目录已失效或被替换，终止同步及删除')
    def check_stop():
        if stop.is_set():
            if network_failure:
                raise NetworkUnavailable(str(network_failure[0])) from network_failure[0]
            raise UnsafePathError('其他线程发现安全错误，停止本组后续写入')


    report_progress = config.get('_progress', lambda *args, **kwargs: None)
    def progress(kind, **data):
        if kind in ('scan', 'discovered', 'phase') and config.get('_control'):
            config['_control'].checkpoint()
        report_progress(kind, **data)
    progress('phase', phase='scanning')
    dry = config.get('dryRun', False)
    parallelism = max(1, min(32, config.get('parallelism', 4)))
    batch_size = max(1, min(1000, config.get('batchFiles', 100)))
    threshold = config.get('largeThresholdMb', 512) * 1024 * 1024
    result = dict(copied=0, skipped=0, deleted=0, bytes=0, scanned=0, unsupportedSkipped=0, ignored=0, failed=0, conflicts=0)
    started = time.monotonic()
    # 源端显式选择后可跳过不支持的条目；目标端仍拒绝链接，避免越界写入。
    excluded = config.get('_excludedStore', set())
    for path in config.get('_skipPaths', ()): excluded.add(path)
    rules = IgnoreRules(config.get('ignorePatterns', ()))
    failures = []
    def failed(relative, error):
        event = dict(path=str(relative), action='failed', bytes=0, error=str(error))
        if not failures: failures.append(event)
        result['failed'] += 1
        emit(event)

    def walk_error(error):
        raise_network(error)
        if not config.get('continueOnError'):
            raise error
        path = Path(error.filename)
        if path in (src, dst):
            raise error
        relative = path.relative_to(src if path.is_relative_to(src) else dst)
        excluded.add(relative)
        failed(relative, error)

    optimized = config.get('syncStrategy', 'AUTO') != 'AUTO'
    from .scanner import ScanMetrics
    metrics = ScanMetrics()
    planned_files = 0
    for root in (() if optimized else (src, dst)):
        if not root.exists():
            continue
        scan_count = 0
        scan_path = str(root)
        scan_tick = time.monotonic()
        side = 'source' if root == src else 'target'
        def scan_update(force=False):
            nonlocal scan_count, scan_tick
            if force or scan_count >= 128 or time.monotonic()-scan_tick >= .2:
                progress('scan', count=scan_count, path=scan_path, side=side)
                scan_count = 0
                scan_tick = time.monotonic()
        def directory_started(path):
            nonlocal scan_path
            scan_path = str(path)
            scan_update(True)
        def inspect_entry(entry, relative):
            nonlocal planned_files, scan_count
            scan_count += 1
            scan_update()
            if excluded and (relative in excluded or any(parent in excluded for parent in relative.parents)):
                return False
            try:
                is_link = entry.is_symlink()
                is_directory = entry.is_dir(follow_symlinks=False)
                # 链接只为目录忽略规则判断类型，不进入其指向的目录。
                ignored_directory = is_directory or (is_link and entry.is_dir())
                if rules.matches(relative, ignored_directory):
                    if root == src:
                        excluded.add(relative)
                        result['ignored'] += 1
                        emit(dict(path=str(relative), action='ignored', bytes=0, reason='匹配忽略规则'))
                    return False
                if is_directory and not is_link:
                    return True
                if not is_link and entry.is_file(follow_symlinks=False):
                    if root == src:
                        planned_files += 1
                    return False
            except OSError as error:
                raise_network(error)
                if not config.get('continueOnError'):
                    raise
                excluded.add(relative)
                failed(relative,error)
                return False
            kind = '符号链接' if is_link else '特殊文件'
            if root == src and config.get('skipUnsupported', True):
                excluded.add(relative)
                result['skipped'] += 1
                result['unsupportedSkipped'] += 1
                result['scanned'] += 1
                emit(dict(path=str(relative), action='skipped', bytes=0,
                          reason=f'按任务配置跳过源{kind}，未复制其内容', entryType=kind))
                return False
            label = '源' if root == src else '目标'
            reason = f'{label}目录包含{kind}：{entry.path}'
            if root == src:
                reason += '；如无需复制该项，可在同步规则中启用“跳过源目录中的符号链接和特殊文件”'
            emit(dict(path=str(relative), action='failed', bytes=0, error=reason, entryType=kind))
            raise ValueError(reason)
        try:
            scan_tree(root,inspect_entry,walk_error,directory_started)
        finally:
            scan_update(True)
    if not optimized and config.get('direction') != 'bidirectional':
        progress('total', value=planned_files)
    if not dry:
        dst.mkdir(parents=True, exist_ok=True)
        if target_identity is None:
            target_identity = (dst.stat().st_dev, dst.stat().st_ino)

    @contextmanager
    def source_stream(path, before):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            current = os.fstat(stream.fileno())
            if (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns) != (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns):
                raise RuntimeError(f'源文件在打开期间被替换：{path}')
            yield stream

    def copy_one(item):
        if optimized:
            relative, scanned_source, scanned_target = item
        else:
            relative = item
        check_stop()
        if optimized and config['syncStrategy'] != 'COPY_ALL':
            left = metadata(scanned_source.size, scanned_source.mtime_ns) if scanned_source.size is not None else None
            right = metadata(scanned_target.size, scanned_target.mtime_ns) if scanned_target and scanned_target.size is not None else None
            if config['syncStrategy'] == 'SKIP_EXISTING' and scanned_target:
                return dict(path=str(relative), action='skipped', bytes=0, reason='目标目录枚举中已存在同名条目')
            if scanned_target and not scanned_target.is_file:
                raise ValueError(f'目标类型冲突：{relative}')
            if config['syncStrategy'] != 'SKIP_EXISTING':
                equal, detail = compare(src/relative,dst/relative,left,right,'size_mtime',digest,tolerance_ns(config))
                if equal:
                    return dict(path=str(relative),action='skipped',bytes=0,comparison=detail,reason=detail['reason'])

        source, target = src / relative, dst / relative
        check_target(target)
        # 发布前再次检查路径，避免写入已被替换为链接的目录。
        for path in (source, target):
            if any(is_link(p) for p in (path, *path.parents)):
                raise UnsafePathError(f'复制路径出现符号链接：{path}')
        expected = config.get('_expected', {}).get(relative)
        if expected is not None:
            from .two_way import fingerprint
            if (fingerprint(source), fingerprint(target)) != expected:
                raise RuntimeError(f'文件在双向规划后已发生变化：{relative}')
        before = source.stat()
        if not stat.S_ISREG(before.st_mode):
            raise UnsafePathError(f'源不再是普通文件：{relative}')
        old = None
        progress('file', path=str(relative), size=before.st_size, written=0, phase='comparing')
        left = metadata(before.st_size, before.st_mtime_ns)
        right = None
        verification = None
        timestamps = None
        if not optimized and target.exists():
            if not target.is_file():
                raise ValueError(f'目标类型冲突：{relative}')
            old = target.stat()
            right = metadata(old.st_size, old.st_mtime_ns)
            if config.get('updateOnly', False) and old.st_mtime_ns - before.st_mtime_ns > tolerance_ns(config):
                reason = '目标修改时间较新，按保护规则跳过；未计算哈希'
                detail = dict(method=method_name(config), source=left, target=right, reason=reason)
                return dict(path=str(relative), action='skipped', bytes=0, comparison=detail, reason=reason)
        if optimized:
            right = metadata(scanned_target.size,scanned_target.mtime_ns) if scanned_target and scanned_target.size is not None else None
            detail = dict(method=config['syncStrategy'],source=left,target=right,
                          reason='直接覆盖复制' if config['syncStrategy']=='COPY_ALL' else '目标不存在或目录快照属性不同')
            equal = False
        else:
            equal, detail = compare(source, target, left, right, method_name(config), digest, tolerance_ns(config))
        if equal:
            return dict(path=str(relative), action='skipped', bytes=0, comparison=detail, reason=detail['reason'])
        if not dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix='.sync-', dir=target.parent)
            temp = Path(temp_name)
            try:
                # 大文件流式写入，不拆文件。带宽限制按工作线程分摊。
                limit = config.get('bandwidthLimit', 0) * 1024 / parallelism
                tick, written = time.monotonic(), 0
                reported, reported_at = 0, tick
                with os.fdopen(fd, 'wb') as output, source_stream(source, before) as stream:
                    try:
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            check_stop()
                            output.write(block)
                            written += len(block)
                            if reported == 0 or time.monotonic() - reported_at >= .2:
                                progress('written', path=str(relative), size=before.st_size, written=written, delta=written-reported)
                                reported, reported_at = written, time.monotonic()
                            if limit:
                                stop.wait(max(0, written / limit - (time.monotonic() - tick)))
                        output.flush()
                        os.fsync(output.fileno())
                    finally:
                        progress('written', path=str(relative), size=before.st_size, written=written, delta=written-reported)
                after = source.stat()
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    raise RuntimeError(f'复制期间源文件发生变化：{relative}')
                if temp.stat().st_size != before.st_size:
                    raise RuntimeError(f'文件长度不一致：{relative}')
                if method_name(config) == 'sha256':
                    progress('file', path=str(relative), size=before.st_size, written=written, phase='verifying')
                    source_hash, written_hash = digest(source).hex(), digest(temp).hex()
                    if source_hash != written_hash:
                        raise RuntimeError(f'哈希校验失败：{relative}')
                    verification = dict(sourceSha256=source_hash, writtenSha256=written_hash)
                    detail['source']['sha256'] = source_hash
                with publish_lock:
                    check_stop()
                    check_source()
                    check_target(target)
                    if expected is not None:
                        from .two_way import fingerprint
                        if fingerprint(source) != expected[0] or fingerprint(target) != expected[1]:
                            raise RuntimeError(f'源或目标在复制期间发生变化：{relative}')
                    written_stat = temp.stat()
                    # 仅复制权限；时间在下方按任务配置单独写入并校验。
                    # copystat 还会复制 BSD 标志，macOS 文件同步到 SMB 时可能报 EINVAL。
                    shutil.copymode(source, temp)
                    preserve = config.get('preserveTime', True)
                    # 使用复制前快照；读取源文件可能改变访问时间。
                    times = (before.st_atime_ns, before.st_mtime_ns) if preserve else (written_stat.st_atime_ns, written_stat.st_mtime_ns)
                    os.utime(temp, ns=times)
                    stored_mtime = temp.stat().st_mtime_ns
                    difference = abs(stored_mtime - before.st_mtime_ns)
                    timestamps = dict(preserved=preserve, sourceMtimeNs=str(before.st_mtime_ns),
                                      targetMtimeNs=str(stored_mtime), differenceNs=str(difference),
                                      toleranceSeconds=tolerance_ns(config)/1_000_000_000,
                                      withinTolerance=difference <= tolerance_ns(config))
                    if preserve and difference > tolerance_ns(config):
                        raise RuntimeError(f'目标文件系统无法在时间容差内保留源修改时间：{relative}（差 {difference/1_000_000_000:g} 秒）')
                    check_stop()
                    if optimized and config['syncStrategy']=='SKIP_EXISTING':
                        from .publication import publish_if_absent
                        if not publish_if_absent(temp,target):
                            return dict(path=str(relative),action='skipped',bytes=0,
                                        reason='目标在目录枚举后出现，原子不覆盖发布已保留该目标')
                    else:
                        os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
        event = dict(path=str(relative), action='copied', bytes=before.st_size, comparison=detail, reason=detail['reason'])
        if timestamps is not None:
            event['timestamps'] = timestamps
        if verification is not None:
            event['verification'] = verification
        return event

    def batch(items):
        events = []
        for item in items:
            relative = item[0] if optimized else item
            with config['_control'].file(config) if config.get('_control') else nullcontext():
                for attempt in range(config.get('maxRetries', 2) + 1):
                    try:
                        event = copy_one(item)
                    except RunInterrupted:
                        raise
                    except UnsafePathError:
                        stop.set()
                        raise
                    except Exception as error:
                        raise_network(error)
                        if attempt < config.get('maxRetries', 2):
                            time.sleep(config.get('retryInterval', 1))
                            continue
                        event = dict(path=str(relative), action='failed', bytes=0, error=str(error))
                    event['retries'] = attempt
                    # 日志失败不能重试已经成功发布的文件。
                    emit(event)
                    events.append(event)
                    progress('file_done')
                    break
        return events

    def collect(done):
        for future in done:
            try:
                while config.get('_control') and not future.done():
                    config['_control'].checkpoint()
                    wait([future], timeout=.2)
                events = future.result()
            except UnsafePathError:
                stop.set()
                for remaining in pending:
                    remaining.cancel()
                raise
            for event in events:
                if event['action'] == 'failed':
                    if not failures: failures.append(event)
                    result['failed'] += 1
                    continue
                result[event['action']] += 1
                result['bytes'] += event['bytes']

    def planned_walk():
        from .directory_plan import directory_pairs
        def accept(relative, is_dir):
            return not (relative in excluded or any(p in excluded for p in relative.parents) or rules.matches(relative,is_dir))
        def checkpoint():
            check_stop()
            if config.get('_control'): config['_control'].checkpoint()
            snapshot=metrics.snapshot()
            progress('metrics', **snapshot)
            scanned=snapshot['sourceEntries']+snapshot['targetEntries']
            progress('scan',count=scanned-result.get('_lastScan',0),path=snapshot.get('currentScanPath'),side=snapshot.get('currentScanSide'))
            result['_lastScan']=scanned
        plan_config = dict(config, _rules=rules)
        with closing(directory_pairs(src,dst,plan_config,metrics,accept,walk_error,checkpoint)) as pairs:
            for relative_dir,source_entries,target_entries in pairs:
                def files():
                    for entry in source_entries:
                        relative = relative_dir/entry.name
                        ignored_directory=entry.is_dir or (entry.kind=='link' and (src/relative).is_dir())
                        if not accept(relative,ignored_directory):
                            if rules.matches(relative,ignored_directory):
                                result['ignored'] += 1
                                emit(dict(path=str(relative),action='ignored',bytes=0,reason='匹配忽略规则'))
                            continue
                        if entry.is_dir: continue
                        if not entry.is_file:
                            if not config.get('skipUnsupported',True):
                                raise ValueError('源包含符号链接或特殊文件：'+str(relative))
                            result['skipped'] += 1;result['unsupportedSkipped'] += 1;result['scanned'] += 1
                            excluded.add(relative)
                            emit(dict(path=str(relative),action='skipped',bytes=0,reason='按配置跳过符号链接或特殊文件'))
                            continue
                        yield relative,entry,target_entries.get(entry.name) if config['syncStrategy']!='COPY_ALL' else None
                snapshot=metrics.snapshot()
                progress('scan',count=snapshot['sourceEntries']+snapshot['targetEntries']-result.get('_lastScan',0),path=str(src/relative_dir),side='source')
                result['_lastScan']=snapshot['sourceEntries']+snapshot['targetEntries']
                yield str(src/relative_dir),[],files()

    progress('phase', phase='syncing')
    seen_files, seen_directories = set(), set()
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        pending, small = set(), []
        try:
            walker = planned_walk() if optimized else os.walk(src, onerror=walk_error)
            for base, directories, files in walker:
                check_stop()
                relative_dir = Path(base).relative_to(src)
                if '_allowedDirs' in config:
                    seen_directories.add(relative_dir)
                directories[:] = [name for name in directories if relative_dir / name not in excluded]
                if '_allowedDirs' in config:
                    directories[:] = [name for name in directories if relative_dir / name in config['_allowedDirs']]
                try:
                    directory_target = dst / relative_dir
                    if any(is_link(p) for p in (directory_target, *directory_target.parents)):
                        raise UnsafePathError(f'目录路径出现符号链接：{directory_target}')
                    if not optimized and directory_target.exists() and not directory_target.is_dir():
                        raise FileExistsError(f'目标目录位置已存在普通文件：{directory_target}')
                    if not dry:
                        directory_target.mkdir(parents=True, exist_ok=True)
                except OSError as error:
                    raise_network(error)
                    if not config.get('continueOnError'):
                        raise
                    failed(relative_dir, error)
                    if optimized: excluded.add(relative_dir)
                    directories[:] = []
                    continue
                for name in files:
                    relative = name[0] if optimized else relative_dir / name
                    work_item = name if optimized else relative
                    if relative in excluded or ('_allowedFiles' in config and relative not in config['_allowedFiles']):
                        continue
                    if '_allowedFiles' in config:
                        seen_files.add(relative)
                    result['scanned'] += 1
                    progress('discovered')
                    try:
                        is_large = False if optimized else (src / relative).stat().st_size >= threshold
                    except OSError as error:
                        raise_network(error)
                        if not config.get('continueOnError'):
                            raise
                        failed(relative, error)
                        progress('file_done')
                        continue
                    if is_large:
                        pending.add(pool.submit(batch, [work_item]))
                    else:
                        small.append(work_item)
                        if len(small) >= batch_size:
                            pending.add(pool.submit(batch, small))
                            small = []
                    metrics.set('copyQueueLength',len(pending)*batch_size+len(small))
                    if len(pending) >= parallelism * 2:
                        done = set()
                        while not done:
                            if config.get('_control'):
                                config['_control'].checkpoint()
                            done, pending = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
                        collect(done)
                # 每个目录完成即提交尾批；扫描后续目录时复制可以同时进行。
                if optimized and small:
                    pending.add(pool.submit(batch,small));small=[]
                if optimized and len(pending) >= parallelism*2:
                    done=set()
                    while not done:
                        if config.get('_control'):config['_control'].checkpoint()
                        done,pending=wait(pending,timeout=.2,return_when=FIRST_COMPLETED)
                    collect(done)
            if small:
                pending.add(pool.submit(batch, small))
            if config.get('direction') != 'bidirectional':
                progress('scan_done')
            collect(pending)
        except BaseException as error:
            if config.get('_control') and not network_error(error):
                config['_control'].close()
            # 主扫描线程的安全错误也必须在等待线程池退出前通知所有 worker。
            stop.set()
            for future in pending:
                future.cancel()
            raise
        finally:
            if optimized and 'walker' in locals(): walker.close()
    for relative in (config.get('_allowedFiles', set()) - seen_files) | (config.get('_allowedDirs', set()) - seen_directories):
        failed(relative, RuntimeError(f'计划条目在执行前消失或无法访问：{relative}'))
    if failures and not config.get('continueOnError'):
        raise RuntimeError(f"{result['failed']} 个文件失败，首个：{failures[0]['path']}：{failures[0]['error']}")
    progress('phase', phase='finalizing')
    # 所有复制成功后才删除，worker 不独立执行目录删除。
    check_source()
    result['deletionSkipped'] = bool(failures and config.get('delete'))
    if result['deletionSkipped']:
        emit(dict(action='deletion_skipped', reason='扫描或复制失败，本组不执行删除'))
    if config.get('delete', False) and not failures:
        planned_deletions = config.get('_deletionStore',set())
        stop_delete = False
        def delete_walk_error(error):
            nonlocal stop_delete
            raise_network(error)
            if not config.get('continueOnError'):
                raise error
            error_path=Path(error.filename) if error.filename else dst
            failed(error_path.relative_to(src if error_path.is_relative_to(src) else dst), error)
            stop_delete = True
            result['deletionSkipped'] = True
            emit(dict(action='deletion_skipped', reason='删除扫描失败，停止本组剩余删除'))
        def deletion_items():
            if optimized:
                from .directory_plan import mirror_candidates
                def check():
                    check_source();check_stop()
                    if config.get('_control'):config['_control'].checkpoint()
                    progress('metrics',**metrics.snapshot())
                try:
                    yield from mirror_candidates(src,dst,config,metrics,excluded,rules,check)
                except OSError as error:
                    delete_walk_error(error)
            else:
                for base,directories,files in os.walk(dst,topdown=False,onerror=delete_walk_error):
                    if stop_delete:break
                    for name in files+directories:
                        yield (Path(base)/name).relative_to(dst),name in directories
        with closing(deletion_items()) as candidates:
            for relative,is_directory in candidates:
                if stop_delete:break
                target=dst/relative
                if (relative in excluded or any(parent in excluded for parent in relative.parents)
                        or rules.matches(relative, is_directory)):
                    continue
                if config.get('_control'):
                    config['_control'].checkpoint()
                check_source()
                check_target(target)
                if not (src / relative).exists():
                    # 含被忽略内容的父目录保留；只删除已经为空的目录。
                    try:
                        if is_directory:
                            if dry:
                                # 演练中已计划删除的子项仍在磁盘上，使用计划集合判断。
                                children = target.iterdir()
                                if any(p.relative_to(dst) not in planned_deletions for p in children):
                                    continue
                            elif any(target.iterdir()):
                                continue
                        if not dry:
                            with config['_control'].file(config) if config.get('_control') else nullcontext():
                                safe_remove(dst, relative, is_directory, target_identity)
                        if dry: planned_deletions.add(relative)
                        result['deleted'] += 1
                        emit(dict(path=str(relative), action='deleted', bytes=0))
                    except OSError as error:
                        raise_network(error)
                        failed(relative, error)
                        if not config.get('continueOnError'):
                            raise
                        stop_delete = True
                        result['deletionSkipped'] = True
                        emit(dict(action='deletion_skipped', reason='删除失败，停止本组剩余删除'))
                        break
    check_source()
    result.pop('_lastScan',None)
    if optimized:
        metrics.set('copyQueueLength',0)
        result['scanMetrics']=metrics.snapshot()
        progress('metrics',**metrics.snapshot())
    result['durationSeconds'] = round(time.monotonic() - started, 4)
    result['dryRun'] = dry
    return result


def safe_remove(root, relative, is_directory, identity):
    if os.open in os.supports_dir_fd and os.unlink in os.supports_dir_fd and os.rmdir in os.supports_dir_fd:
        descriptors = []
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            current = os.open(root, flags)
            descriptors.append(current)
            info = os.fstat(current)
            if (info.st_dev,info.st_ino) != identity:
                raise UnsafePathError('删除前目标根目录被替换')
            for part in relative.parts[:-1]:
                current = os.open(part, flags, dir_fd=current)
                descriptors.append(current)
            if is_directory:
                os.rmdir(relative.name, dir_fd=current)
            else:
                os.unlink(relative.name, dir_fd=current)
        except OSError as error:
            import errno
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise UnsafePathError(f'删除路径被替换或包含链接：{relative}') from error
            raise
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
    else:
        # 不支持 dir_fd 的平台使用路径链复核；Windows 需避免外部程序并发改名目录。
        target = root / relative
        if any(is_link(p) for p in (target,*target.parents)):
            raise UnsafePathError(f'删除路径出现链接：{target}')
        current = root.stat()
        if (current.st_dev,current.st_ino) != identity:
            raise UnsafePathError('删除前目标根目录被替换')
        target.rmdir() if is_directory else target.unlink()
