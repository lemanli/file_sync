"""一次性、按目录、有界的扫描流水线；临时表不是持久历史索引。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unicodedata
from .scanner import get_scanner, ScanFields, DirectoryEntry, UnsupportedScanner
from .scanner.portable import PortableScanner


def alias(name):
    return unicodedata.normalize('NFD', name).casefold()


class Snapshot:
    """小目录内存哈希；超限后落盘，防止千万条目录项占满内存。"""
    def __init__(self, limit=10000, reject_aliases=False):
        self.reject_aliases=reject_aliases
        self.limit=limit; self.items={}; self.aliases={}; self.db=None; self.temp=None
    def add(self, entry):
        if self.reject_aliases:
            if self.db is None:previous=self.aliases.get(alias(entry.name))
            else:
                row=self.db.execute('SELECT name FROM entries WHERE alias=? LIMIT 1',(os.fsencode(alias(entry.name)),)).fetchone()
                previous=os.fsdecode(row[0]) if row else None
            if previous is not None and previous!=entry.name:
                raise ValueError('源目录存在大小写或 Unicode 等价名称，无法安全映射目标：'+previous+' / '+entry.name)
        if self.db is None and len(self.items)>=self.limit:
            self.temp=tempfile.TemporaryDirectory(prefix='file-sync-scan-')
            self.db=sqlite3.connect(str(Path(self.temp.name)/'entries.sqlite'),check_same_thread=False)
            self.db.execute('PRAGMA journal_mode=OFF');self.db.execute('PRAGMA synchronous=OFF')
            self.db.execute('PRAGMA cache_size=-2048')
            self.db.execute('CREATE TABLE entries(name BLOB PRIMARY KEY, alias BLOB, detail TEXT)')
            self.db.execute('CREATE INDEX aliases ON entries(alias)')
            for old in self.items.values():self._insert(old)
            self.items.clear();self.aliases.clear()
        if self.db is None:
            self.items[entry.name]=entry;self.aliases[alias(entry.name)]=entry.name
        else:self._insert(entry)
    def _insert(self, entry):
        self.db.execute('INSERT OR REPLACE INTO entries VALUES(?,?,?)',
                        (os.fsencode(entry.name),os.fsencode(alias(entry.name)),json.dumps([entry.name,entry.kind,entry.size,entry.mtime_ns])))
    def get(self,name):
        if self.db is None:
            exact=self.items.get(name); ambiguous=alias(name) in self.aliases
        else:
            row=self.db.execute('SELECT detail FROM entries WHERE name=?',(os.fsencode(name),)).fetchone()
            exact=DirectoryEntry(*json.loads(row[0])) if row else None
            ambiguous=False if exact else self.db.execute('SELECT 1 FROM entries WHERE alias=? LIMIT 1',(os.fsencode(alias(name)),)).fetchone() is not None
        if exact is None and ambiguous:
            # SMB 服务端大小写/Unicode 规则不可从客户端平台推断，禁止猜测导致错误跳过/覆盖。
            raise ValueError('源目标存在大小写或 Unicode 等价名称，请统一名称或使用兼容策略：'+name)
        return exact
    def __iter__(self):
        if self.db is None:yield from self.items.values()
        else:
            for row in self.db.execute('SELECT detail FROM entries'):yield DirectoryEntry(*json.loads(row[0]))
    def close(self):
        if self.db is not None:self.db.close();self.db=None
        if self.temp is not None:self.temp.cleanup();self.temp=None
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


def read_snapshot(path,fields,backend,metrics,side,limit=10000,check=lambda:None):
    scanner=get_scanner(backend)
    while True:
        result=Snapshot(limit,reject_aliases=side=='source')
        count=0
        try:
            # 不跟随扫描期间被替换的目录链接；发布时仍再次复核。
            if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
                raise ValueError('扫描目录被替换成链接：'+str(path))
            count=0
            metrics.set('currentScanPath',str(path));metrics.set('currentScanSide',side)
            for entry in scanner.scan(path,fields,metrics):
                if count%128==0:check()
                result.add(entry);count+=1
                if count%128==0: metrics.add(side+'Entries',128)
            metrics.add(side+'Entries',count%128);metrics.add(side+'Directories')
            return result
        except UnsupportedScanner:
            metrics.add(side+'Entries',-(count//128)*128)
            result.close()
            if isinstance(scanner,PortableScanner):raise
            metrics.add('fallbacks');scanner=PortableScanner()
        except BaseException:
            result.close();raise


def directory_pairs(src,dst,config,metrics,accept,onerror,check):
    """并发单位为目录，磁盘目录队列+至多 workers 个在途快照。"""
    strategy=config['syncStrategy']
    fields=ScanFields.NAME_TYPE if strategy in ('COPY_ALL','SKIP_EXISTING') else ScanFields.METADATA
    workers=config.get('scanWorkers',1)
    backend=config.get('scannerBackend','auto')
    limit=config.get('_snapshotLimit',10000)
    def scan(relative):
        source=target=None
        try:
            # worker 检查关闭，不等待暂停；主线程负责暂停，避免暂停与 worker 池互等。
            control=config.get('_control')
            def cancelled():
                if control and control.closed:
                    from .control import RunInterrupted
                    raise RunInterrupted('任务已停止')
            source=read_snapshot(src/relative,fields,backend,metrics,'source',limit,cancelled)
            target=Snapshot(limit)
            if strategy!='COPY_ALL':
                try:
                    target=read_snapshot(dst/relative,fields,backend,metrics,'target',limit,cancelled)
                except FileNotFoundError as error:
                    from .recovery import network_error
                    if network_error(error):raise
            return source,target
        except BaseException:
            if source:source.close()
            if target:target.close()
            raise
    with tempfile.TemporaryDirectory(prefix='file-sync-dirs-') as folder, ExitStack() as stack:
        queue=sqlite3.connect(str(Path(folder)/'queue.sqlite'));stack.callback(queue.close)
        queue.execute('PRAGMA journal_mode=OFF');queue.execute('PRAGMA synchronous=OFF')
        queue.execute('PRAGMA cache_size=-1024');queue.execute('CREATE TABLE queue(id INTEGER PRIMARY KEY,path BLOB)')
        queue.execute('INSERT INTO queue(path) VALUES(?)',(b'.',));queued=1
        pool=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='sync-scan')
        pending=[]
        try:
            while queued or pending:
                check()
                while queued and len(pending)<workers:
                    row=queue.execute('SELECT id,path FROM queue ORDER BY id LIMIT 1').fetchone()
                    queue.execute('DELETE FROM queue WHERE id=?',(row[0],));queued-=1
                    relative=Path(os.fsdecode(row[1]));pending.append((relative,pool.submit(scan,relative)))
                metrics.set('directoryQueueLength',queued+len(pending))
                relative,future=pending[0]
                while not future.done():
                    from concurrent.futures import wait
                    wait([future],timeout=.2);check()
                pending.pop(0)
                try: source,target=future.result()
                except OSError as error:
                    onerror(error);continue
                with source,target:
                    # 目标中的链接/特殊文件仍是安全错误。COPY_ALL 仅在写入路径检查。
                    for entry in target:
                        if entry.kind in ('link','special') and not config.get('_rules').matches(relative/entry.name,entry.kind=='link' and (dst/relative/entry.name).is_dir()):
                            raise ValueError('目标目录包含链接或特殊文件：'+str(dst/relative/entry.name))
                    if strategy!='COPY_ALL':
                        for entry in source:
                            if entry.is_dir and accept(relative/entry.name,True):target.get(entry.name)
                    yield relative,source,target
                    for entry in source:
                        child=relative/entry.name
                        if entry.is_dir and accept(child,True):
                            queue.execute('INSERT INTO queue(path) VALUES(?)',(os.fsencode(child),));queued+=1
        finally:
            for _,future in pending:future.cancel()
            pool.shutdown(wait=True,cancel_futures=True)
            for _,future in pending:
                if not future.cancelled() and future.exception() is None:
                    for snapshot in future.result():snapshot.close()
            metrics.set('directoryQueueLength',0)

class SpillSet:
    """忽略路径/删除演练集合也受内存上限约束。"""
    def __init__(self,limit=10000):self.snapshot=Snapshot(limit);self.count=0
    def add(self,path):
        if path not in self:self.snapshot.add(DirectoryEntry(str(path),'file'));self.count+=1
    def __contains__(self,path):
        name=str(path);db=self.snapshot.db
        if db is None:return name in self.snapshot.items
        return db.execute('SELECT 1 FROM entries WHERE name=?',(os.fsencode(name),)).fetchone() is not None
    def __bool__(self):return self.count>0
    def close(self):self.snapshot.close()


def mirror_candidates(src,dst,config,metrics,excluded,rules,check):
    """先完整生成磁盘删除计划，扫描全部成功后再自底向上执行。
    此阶段不保存整棵树的 Python 对象；目录错误中止计划，尚未开始删除。
    """
    with tempfile.TemporaryDirectory(prefix='file-sync-delete-') as folder:
        db=sqlite3.connect(str(Path(folder)/'plan.sqlite'))
        try:
            db.execute('PRAGMA journal_mode=OFF');db.execute('PRAGMA synchronous=OFF');db.execute('PRAGMA cache_size=-2048')
            db.execute('CREATE TABLE dirs(id INTEGER PRIMARY KEY,path BLOB)')
            db.execute('CREATE TABLE candidates(path BLOB,is_dir INTEGER,depth INTEGER)')
            db.execute('INSERT INTO dirs(path) VALUES(?)',(b'.',))
            while True:
                check()
                row=db.execute('SELECT id,path FROM dirs ORDER BY id LIMIT 1').fetchone()
                if row is None:break
                db.execute('DELETE FROM dirs WHERE id=?',(row[0],))
                relative=Path(os.fsdecode(row[1]))
                with ExitStack() as stack:
                    target=stack.enter_context(read_snapshot(dst/relative,ScanFields.NAME_TYPE,config.get('scannerBackend','auto'),metrics,'target',check=check))
                    try:source=read_snapshot(src/relative,ScanFields.NAME_TYPE,config.get('scannerBackend','auto'),metrics,'source',check=check)
                    except FileNotFoundError as error:
                        from .recovery import network_error
                        if network_error(error) or relative==Path():raise
                        source=Snapshot()
                    stack.enter_context(source)
                    for entry in target:
                        path=relative/entry.name
                        ignored_directory=entry.is_dir or (entry.kind=='link' and (dst/path).is_dir())
                        if path in excluded or any(p in excluded for p in path.parents) or rules.matches(path,ignored_directory):continue
                        if entry.kind in ('link','special'):raise ValueError('删除前发现目标链接或特殊文件：'+str(path))
                        if entry.is_dir:db.execute('INSERT INTO dirs(path) VALUES(?)',(os.fsencode(path),))
                        if source.get(entry.name) is None:
                            db.execute('INSERT INTO candidates VALUES(?,?,?)',(os.fsencode(path),entry.is_dir,len(path.parts)))
            db.execute('CREATE INDEX deepest_first ON candidates(depth DESC)')
            for path,is_dir in db.execute('SELECT path,is_dir FROM candidates ORDER BY depth DESC'):
                yield Path(os.fsdecode(path)),bool(is_dir)
        finally:db.close()
