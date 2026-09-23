"""固定数据目录、跨进程独占锁，以及包含 WAL 数据的旧库迁移。"""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile


class DatabaseLock:
    """锁由操作系统持有；进程被关闭后自动释放，不用手工删锁文件。"""
    def __init__(self, database):
        self.path = Path(str(Path(database).resolve()) + '.lock')
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise RuntimeError('数据目录正在被另一个新版程序使用，请先关闭它：' + str(self.path.parent)) from error
        self.stream = stream
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None


def resolve_path(root, value):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(root) / path).resolve()


def database_path(root, config):
    settings = config.get('database', {})
    directory = settings.get('directory')
    if directory:
        return resolve_path(root, directory) / 'file_sync.sqlite3'
    return resolve_path(root, settings.get('path') or 'data/standalone/file_sync.sqlite3')


def interrupt_records(connection, reason):
    """恢复历史状态，并逐执行保留原因；不把中断任务当作成功。"""
    rows = connection.execute("SELECT id FROM runs WHERE status IN ('running','pausing','paused','retry_wait')").fetchall()
    stamp = datetime.now(timezone.utc).isoformat()
    for row in rows:
        run_id = row[0]
        connection.execute("UPDATE runs SET status='interrupted',ended=?,error=? WHERE id=?", (stamp, reason, run_id))
        connection.execute('INSERT INTO events(run_id,created,detail) VALUES(?,?,?)',
                           (run_id, stamp, json.dumps(dict(action='control', reason=reason), ensure_ascii=False)))
    return len(rows)


def migrate(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source.is_dir():
        source = source / 'file_sync.sqlite3'
    if not source.is_file():
        raise ValueError('旧数据库不存在：' + str(source))
    if source == destination or destination.exists():
        raise ValueError('目标数据库已存在，拒绝覆盖；共用目录无需迁移，直接启动即可')
    # 新版进程持有此锁时拒绝迁移；旧版没有锁，首次切换仍需先关闭旧程序。
    with DatabaseLock(source), DatabaseLock(destination):
        if destination.exists():
            raise ValueError('目标数据库已存在，拒绝覆盖')
        fd, name = tempfile.mkstemp(prefix='.migration-', suffix='.sqlite3', dir=destination.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as old, closing(sqlite3.connect(temporary)) as new:
                old.backup(new)
                tables = {r[0] for r in new.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'tasks', 'runs', 'events'} <= tables:
                    raise ValueError('来源不是受支持的单机任务数据库')
                if new.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('旧库完整性检查未通过')
                interrupted = interrupt_records(new, '迁移时原记录未结束，已在副本标为中断；重新运行将重新比较文件')
                new.commit()
                counts = {table: new.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('tasks','runs','events')}
                counts['interrupted'] = interrupted
                new.execute('PRAGMA journal_mode=DELETE')
            # 原子发布；目标意外出现时也不覆盖。
            os.link(temporary, destination)
            return counts
        finally:
            temporary.unlink(missing_ok=True)
            for suffix in ('-wal', '-shm', '-journal'):
                Path(str(temporary)+suffix).unlink(missing_ok=True)


def prepare_database(root, config):
    destination = database_path(root, config)
    source = config.get('database', {}).get('migrate_from')
    if destination.exists():
        print('复用数据：' + str(destination), flush=True)
    elif source:
        counts = migrate(resolve_path(root, source), destination)
        print('旧库迁移完成：' + str(counts), flush=True)
    else:
        print('使用数据目录：' + str(destination.parent), flush=True)
    return destination
