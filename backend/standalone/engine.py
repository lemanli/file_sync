"""本地与已挂载网络共享的临时文件同步；每个文件只交给一个工作线程。"""
from __future__ import annotations
import hashlib
import os
import shutil
import stat
import tempfile
import time
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from .rules import IgnoreRules
from .comparison import compare, metadata, method_name, tolerance_ns


class UnsafePathError(RuntimeError):
    """根目录或路径边界失效，即使容错模式也必须停止。"""


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.digest()


def roots(source, target):
    # 禁止源目标重叠和符号链接路径，避免递归复制或越界写入。
    for raw in (source, target):
        if not Path(raw).is_absolute():
            raise ValueError('请填写绝对目录路径')
        path = Path(raw).absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('目录及父目录不能是符号链接')
    src, dst = Path(source).resolve(), Path(target).resolve()
    if dst == Path(dst.anchor):
        raise ValueError('目标不能是磁盘根目录')
    if src == dst or src in dst.parents or dst in src.parents:
        raise ValueError('源目录与目标目录不能相同或互相包含')
    if not src.is_dir():
        raise ValueError('源目录不存在或不是目录')
    if dst.exists() and not dst.is_dir():
        raise ValueError('目标必须是目录')
    return src, dst


def synchronize(config, emit):
    if config.get("direction") == "bidirectional":
        from .two_way import synchronize_two_way
        return synchronize_two_way(config, emit)
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
    def check_source():
        try:
            current = src.stat()
            valid = not src.is_symlink() and src.is_dir() and (current.st_dev, current.st_ino) == source_identity
        except OSError:
            valid = False
        if not valid:
            raise UnsafePathError('源目录已失效或被替换，终止同步及删除')
    def check_target(path):
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise UnsafePathError(f'目标路径出现符号链接：{path}')
        if target_identity is not None:
            try:
                current = dst.stat()
                valid = dst.is_dir() and (current.st_dev, current.st_ino) == target_identity
            except OSError:
                valid = False
            if not valid:
                raise UnsafePathError('目标根目录已失效或被替换，终止同步及删除')
    def check_stop():
        if stop.is_set():
            raise UnsafePathError('其他线程发现安全错误，停止本组后续写入')


    progress = config.get('_progress', lambda *args, **kwargs: None)
    progress('phase', phase='scanning')
    dry = config.get('dryRun', False)
    parallelism = max(1, min(32, config.get('parallelism', 4)))
    batch_size = max(1, min(1000, config.get('batchFiles', 100)))
    threshold = config.get('largeThresholdMb', 512) * 1024 * 1024
    result = dict(copied=0, skipped=0, deleted=0, bytes=0, scanned=0, unsupportedSkipped=0, ignored=0, failed=0, conflicts=0)
    started = time.monotonic()
    # 源端显式选择后可跳过不支持的条目；目标端仍拒绝链接，避免越界写入。
    excluded = set(config.get('_skipPaths', ()))
    rules = IgnoreRules(config.get('ignorePatterns', ()))
    failures = []
    def failed(relative, error):
        event = dict(path=str(relative), action='failed', bytes=0, error=str(error))
        failures.append(event)
        result['failed'] += 1
        emit(event)

    def walk_error(error):
        if not config.get('continueOnError'):
            raise error
        path = Path(error.filename)
        if path in (src, dst):
            raise error
        relative = path.relative_to(src if path.is_relative_to(src) else dst)
        excluded.add(relative)
        failed(relative, error)

    planned_files = 0
    for root in (src, dst):
        if not root.exists():
            continue
        for base, directories, files in os.walk(root, onerror=walk_error):
            for name in directories + files:
                progress('scan')
                entry = Path(base) / name
                relative = entry.relative_to(root)
                if relative in excluded or any(parent in excluded for parent in relative.parents):
                    if name in directories:
                        directories.remove(name)
                    continue
                if rules.matches(relative, name in directories):
                    if name in directories:
                        directories.remove(name)
                    if root == src:
                        excluded.add(relative)
                        result['ignored'] += 1
                        emit(dict(path=str(relative), action='ignored', bytes=0, reason='匹配忽略规则'))
                    continue
                try:
                    mode = entry.lstat().st_mode
                except OSError as error:
                    if not config.get('continueOnError'):
                        raise
                    excluded.add(relative)
                    if name in directories:
                        directories.remove(name)
                    failed(relative, error)
                    continue
                if stat.S_ISREG(mode) or stat.S_ISDIR(mode):
                    if root == src and stat.S_ISREG(mode):
                        planned_files += 1
                    continue
                relative = entry.relative_to(root)
                kind = '符号链接' if stat.S_ISLNK(mode) else '特殊文件'
                if root == src and config.get('skipUnsupported', True):
                    excluded.add(relative)
                    if name in directories:
                        directories.remove(name)
                    result['skipped'] += 1
                    result['unsupportedSkipped'] += 1
                    result['scanned'] += 1
                    emit(dict(path=str(relative), action='skipped', bytes=0,
                              reason=f'按任务配置跳过源{kind}，未复制其内容', entryType=kind))
                else:
                    side = '源' if root == src else '目标'
                    reason = f'{side}目录包含{kind}：{entry}'
                    if root == src:
                        reason += '；如无需复制该项，可在同步规则中启用“跳过源目录中的符号链接和特殊文件”'
                    emit(dict(path=str(relative), action='failed', bytes=0, error=reason, entryType=kind))
                    raise ValueError(reason)
    if config.get('direction') != 'bidirectional':
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

    def copy_one(relative):
        check_stop()
        source, target = src / relative, dst / relative
        check_target(target)
        # 发布前再次检查路径，避免写入已被替换为链接的目录。
        for path in (source, target):
            if any(p.is_symlink() for p in (path, *path.parents)):
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
        if target.exists():
            if not target.is_file():
                raise ValueError(f'目标类型冲突：{relative}')
            old = target.stat()
            right = metadata(old.st_size, old.st_mtime_ns)
            if config.get('updateOnly', False) and old.st_mtime_ns - before.st_mtime_ns > tolerance_ns(config):
                reason = '目标修改时间较新，按保护规则跳过；未计算哈希'
                detail = dict(method=method_name(config), source=left, target=right, reason=reason)
                return dict(path=str(relative), action='skipped', bytes=0, comparison=detail, reason=reason)
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
                    shutil.copystat(source, temp)
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
        for relative in items:
            for attempt in range(config.get('maxRetries', 2) + 1):
                try:
                    event = copy_one(relative)
                except UnsafePathError:
                    stop.set()
                    raise
                except Exception as error:
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
                events = future.result()
            except UnsafePathError:
                stop.set()
                for remaining in pending:
                    remaining.cancel()
                raise
            for event in events:
                if event['action'] == 'failed':
                    failures.append(event)
                    result['failed'] += 1
                    continue
                result[event['action']] += 1
                result['bytes'] += event['bytes']

    progress('phase', phase='syncing')
    seen_files, seen_directories = set(), set()
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        pending, small = set(), []
        try:
            for base, directories, files in os.walk(src, onerror=walk_error):
                check_stop()
                relative_dir = Path(base).relative_to(src)
                seen_directories.add(relative_dir)
                directories[:] = [name for name in directories if relative_dir / name not in excluded]
                if '_allowedDirs' in config:
                    directories[:] = [name for name in directories if relative_dir / name in config['_allowedDirs']]
                try:
                    directory_target = dst / relative_dir
                    if any(p.is_symlink() for p in (directory_target, *directory_target.parents)):
                        raise UnsafePathError(f'目录路径出现符号链接：{directory_target}')
                    if directory_target.exists() and not directory_target.is_dir():
                        raise FileExistsError(f'目标目录位置已存在普通文件：{directory_target}')
                    if not dry:
                        directory_target.mkdir(parents=True, exist_ok=True)
                except OSError as error:
                    if not config.get('continueOnError'):
                        raise
                    failed(relative_dir, error)
                    directories[:] = []
                    continue
                for name in files:
                    relative = relative_dir / name
                    if relative in excluded or ('_allowedFiles' in config and relative not in config['_allowedFiles']):
                        continue
                    seen_files.add(relative)
                    result['scanned'] += 1
                    progress('discovered')
                    try:
                        is_large = (src / relative).stat().st_size >= threshold
                    except OSError as error:
                        if not config.get('continueOnError'):
                            raise
                        failed(relative, error)
                        progress('file_done')
                        continue
                    if is_large:
                        pending.add(pool.submit(batch, [relative]))
                    else:
                        small.append(relative)
                        if len(small) >= batch_size:
                            pending.add(pool.submit(batch, small))
                            small = []
                    if len(pending) >= parallelism * 2:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        collect(done)
            if small:
                pending.add(pool.submit(batch, small))
            if config.get('direction') != 'bidirectional':
                progress('scan_done')
            collect(pending)
        except BaseException:
            # 主扫描线程的安全错误也必须在等待线程池退出前通知所有 worker。
            stop.set()
            for future in pending:
                future.cancel()
            raise
    for relative in (config.get('_allowedFiles', set()) - seen_files) | (config.get('_allowedDirs', set()) - seen_directories):
        failed(relative, RuntimeError(f'计划条目在执行前消失或无法访问：{relative}'))
    if failures and not config.get('continueOnError'):
        raise RuntimeError(f"{len(failures)} 个文件失败，首个：{failures[0]['path']}：{failures[0]['error']}")
    progress('phase', phase='finalizing')
    # 所有复制成功后才删除，worker 不独立执行目录删除。
    check_source()
    result['deletionSkipped'] = bool(failures and config.get('delete'))
    if result['deletionSkipped']:
        emit(dict(action='deletion_skipped', reason='扫描或复制失败，本组不执行删除'))
    if config.get('delete', False) and not failures:
        planned_deletions = set()
        stop_delete = False
        def delete_walk_error(error):
            nonlocal stop_delete
            if not config.get('continueOnError'):
                raise error
            failed(Path(error.filename).relative_to(dst), error)
            stop_delete = True
            result['deletionSkipped'] = True
            emit(dict(action='deletion_skipped', reason='删除扫描失败，停止本组剩余删除'))
        for base, directories, files in os.walk(dst, topdown=False, onerror=delete_walk_error):
            if stop_delete:
                break
            for name in files + directories:
                target = Path(base) / name
                relative = target.relative_to(dst)
                if (relative in excluded or any(parent in excluded for parent in relative.parents)
                        or rules.matches(relative, name in directories)):
                    continue
                check_source()
                check_target(target)
                if not (src / relative).exists():
                    # 含被忽略内容的父目录保留；只删除已经为空的目录。
                    try:
                        if name in directories:
                            if dry:
                                # 演练中已计划删除的子项仍在磁盘上，使用计划集合判断。
                                children = list(target.iterdir())
                                if any(p.relative_to(dst) not in planned_deletions for p in children):
                                    continue
                            elif any(target.iterdir()):
                                continue
                        if not dry:
                            safe_remove(dst, relative, name in directories, target_identity)
                        planned_deletions.add(relative)
                        result['deleted'] += 1
                        emit(dict(path=str(relative), action='deleted', bytes=0))
                    except OSError as error:
                        failed(relative, error)
                        if not config.get('continueOnError'):
                            raise
                        stop_delete = True
                        result['deletionSkipped'] = True
                        emit(dict(action='deletion_skipped', reason='删除失败，停止本组剩余删除'))
                        break
    check_source()
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
        if any(p.is_symlink() for p in (target,*target.parents)):
            raise UnsafePathError(f'删除路径出现链接：{target}')
        current = root.stat()
        if (current.st_dev,current.st_ino) != identity:
            raise UnsafePathError('删除前目标根目录被替换')
        target.rmdir() if is_directory else target.unlink()
