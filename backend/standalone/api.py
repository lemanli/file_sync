"""免登录本机 API：任务持久化、独立后台线程、运行日志分页。"""
from __future__ import annotations
import json
import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from .engine import roots, synchronize
from .folders import list_directories
from .rules import validate_patterns
from .comparison import method_name
from . import __version__
from .progress import Progress
from .preflight import check_destination
from .control import RunControl, RunInterrupted
from .storage import interrupt_records
from .recovery import network_error


def now():
    return datetime.now(timezone.utc).isoformat()


class PathMapping(BaseModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    includeSourceName: bool = False


class Task(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # 保留旧字段以读取已保存的单目录任务，新任务使用映射列表。
    source: str | None = None
    target: str | None = None
    pathMappings: list[PathMapping] | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode='after')
    def normalize_mappings(self):
        if self.pathMappings is None:
            if not self.source or not self.target:
                raise ValueError('请至少配置一组源目录和目标目录')
            self.pathMappings = [PathMapping(source=self.source, target=self.target)]
        if self.comparisonMode is None and 'checksum' in self.model_fields_set and 'timeToleranceSeconds' not in self.model_fields_set:
            self.timeToleranceSeconds = 0
        if self.comparisonMode is None:
            self.comparisonMode = ('sha256' if self.checksum else 'size_mtime') if 'checksum' in self.model_fields_set else 'size'
        self.checksum = self.comparisonMode == 'sha256'
        if self.syncStrategy != 'AUTO':
            if self.direction == 'bidirectional':
                raise ValueError('双向合并请使用兼容策略 AUTO')
            if self.updateOnly or (self.delete and self.syncStrategy != 'MIRROR'):
                raise ValueError('新策略不能组合仅更新或独立删除选项')
            self.delete = self.syncStrategy == 'MIRROR'
            self.comparisonMode = 'size_mtime'
            self.checksum = False
        validate_patterns(self.ignorePatterns)
        if self.direction == 'bidirectional' and self.delete:
            raise ValueError('双向合并不支持删除，请关闭删除选项')
        return self
    direction: Literal['one_way', 'bidirectional'] = 'one_way'
    syncStrategy: Literal['AUTO', 'COPY_ALL', 'SKIP_EXISTING', 'COMPARE_METADATA', 'MIRROR'] = 'AUTO'
    scanWorkers: int = Field(default=1, ge=1, le=32)
    scannerBackend: Literal['auto','portable','macos','linux','windows_bulk','windows_find'] = 'auto'
    conflictPolicy: Literal['skip', 'newer'] = 'skip'
    ignorePatterns: list[str] = Field(default_factory=list, max_length=200)
    continueOnError: bool = False
    networkRetryCount: int = Field(default=3, ge=0, le=100)
    networkRetryMinutes: float = Field(default=3, ge=.01, le=1440, allow_inf_nan=False)
    parallelism: int = Field(default=4, ge=1, le=32)
    batchFiles: int = Field(default=100, ge=1, le=1000)
    largeThresholdMb: int = Field(default=512, ge=1)
    skipUnsupported: bool = True
    comparisonMode: Literal['size', 'size_mtime', 'sha256'] | None = None
    preserveTime: bool = True
    timeToleranceSeconds: float = Field(default=2, ge=0, le=60, allow_inf_nan=False)
    checksum: bool = False
    delete: bool = False
    updateOnly: bool = False
    bandwidthLimit: int = Field(default=0, ge=0)
    maxRetries: int = Field(default=2, ge=0, le=10)
    retryInterval: int = Field(default=1, ge=0, le=60)


class RunOptions(BaseModel):
    dryRun: bool = False


def create_app(database):
    database = Path(database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    gate = threading.Lock()
    live = {}
    controls = {}
    live_lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sync-task')

    # SQLite 3.51.0/3.51.1 Unix 打开/关闭锁序缺陷；仅这些版本串行短事务。
    database_access = threading.RLock() if sqlite3.sqlite_version_info in ((3,51,0),(3,51,1)) else nullcontext()

    @contextmanager
    def connect():
        with database_access:
            yield from connect_unlocked()

    def connect_unlocked():
        connection = sqlite3.connect(database, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL,
                status TEXT NOT NULL, started TEXT NOT NULL, ended TEXT, snapshot TEXT NOT NULL,
                result TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL,
                created TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
            CREATE INDEX IF NOT EXISTS events_run_action ON events(run_id,json_extract(detail,'$.action'),id);
        ''')
        interrupt_records(db, '程序上次退出时任务未完成，已标为中断；重新运行将重新比较文件')

    @asynccontextmanager
    async def lifespan(app):
        yield
        # 等待正在写入的文件完成，避免进程退出时后台线程丢失最终状态。
        with live_lock:
            for control in controls.values():
                control.close()
        pool.shutdown(wait=True)

    app = FastAPI(title='文件同步·单机版', version=__version__, lifespan=lifespan)
    app.mount('/assets', StaticFiles(directory=Path(__file__).with_name('assets')), name='standalone-assets')

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        host = request.url.hostname
        if host not in {'localhost', '127.0.0.1', '::1', 'testserver'}:
            return JSONResponse({'detail': '单机版仅允许本机访问'}, status_code=403)
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.headers.get('host'):
            return JSONResponse({'detail': '拒绝跨站请求'}, status_code=403)
        if request.method not in {'GET', 'HEAD'} and request.headers.get('x-sync-local') != '1':
            return JSONResponse({'detail': '缺少本机请求标识'}, status_code=403)
        response = await call_next(request)
        # 本机管理页及接口不能沿用升级前缓存，避免旧脚本误判接口版本。
        response.headers['Cache-Control'] = 'no-store'
        return response

    def validate_paths(source, target):
        src, dst = roots(source, target)
        protected = database.parent
        for path in (src, dst):
            if path == protected or path in protected.parents or protected in path.parents:
                raise ValueError('同步目录不能包含或位于程序的 SQLite 数据目录')
        return src, dst

    def validate_task(config):
        task = Task.model_validate(config)
        mappings = []
        for item in task.pathMappings:
            mapping = item.model_dump()
            if item.includeSourceName:
                name = Path(item.source).resolve().name
                if not name:
                    raise ValueError('源为文件系统根目录时无法包含目录名，请选择子目录')
                mapping['configuredTarget'] = item.target
                mapping['target'] = str(Path(item.target) / name)
            mappings.append(mapping)
        paths = [validate_paths(m['source'], m['target']) for m in mappings]
        def overlaps(a, b):
            return a == b or a in b.parents or b in a.parents
        # 单向合并允许共享目标；删除和双向模式要求各组写入范围独立。
        for i, (src, dst) in enumerate(paths):
            for j, (other_src, other_dst) in enumerate(paths):
                if i == j:
                    continue
                if overlaps(dst, other_src):
                    raise ValueError(f'第 {i+1} 组实际目标与第 {j+1} 组源重叠，可能改写同步源')
                if overlaps(dst, other_dst) and (task.delete or task.direction == 'bidirectional'):
                    raise ValueError(f'第 {i+1} 组与第 {j+1} 组实际目标重叠；请关闭删除并使用单向同步，或包含源目录名以分开目标')
                if task.direction == 'bidirectional' and overlaps(src, other_src):
                    raise ValueError(f'第 {i+1} 组与第 {j+1} 组源重叠，不支持双向同步')
        return mappings

    @app.get('/api/directories')
    def directories(path: str = '', offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500),
                    query: str = Query('', max_length=255),
                    sort: Literal['name_asc', 'name_desc', 'modified_asc', 'modified_desc'] = 'name_asc'):
        return list_directories(path, database.parent, offset, limit, query, sort)

    @app.get('/')
    def index():
        directory = Path(__file__).parent
        html = (directory / 'index.html').read_text(encoding='utf-8')
        for name in ('app.js', 'app.css'):
            fingerprint = hashlib.sha256((directory / 'assets' / name).read_bytes()).hexdigest()[:16]
            html = html.replace('/assets/' + name + '?v=__ASSET_VERSION__', '/assets/' + name + '?v=' + fingerprint)
        return HTMLResponse(html)

    @app.get('/api/health')
    def health():
        return {'releaseVersion': __version__, 'mode': 'standalone', 'database': 'sqlite', 'version': 1, 'configSchemaVersion': 3, 'taskControlVersion': 1, 'networkRecoveryVersion': 1, 'scanArchitectureVersion': 1, 'comparisonDetailsVersion': 1, 'timeOptionsVersion': 1, 'progressVersion': 1, 'logQueryVersion': 1}

    @app.get('/api/tasks')
    def tasks():
        with connect() as db:
            return [dict(id=r['id'], **json.loads(r['config'])) for r in db.execute('SELECT * FROM tasks ORDER BY id DESC')]

    @app.post('/api/tasks')
    def add(task: Task):
        try:
            validate_task(task.model_dump())
        except ValueError as error:
            raise HTTPException(422, str(error))
        with connect() as db:
            cursor = db.execute('INSERT INTO tasks(config) VALUES(?)', (task.model_dump_json(),))
            return {'id': cursor.lastrowid}

    @app.put('/api/tasks/{task_id}')
    def edit(task_id: int, task: Task):
        try:
            validate_task(task.model_dump())
        except ValueError as error:
            raise HTTPException(422, str(error))
        with live_lock:
            control = next((c for c in controls.values() if c.task_id == task_id), None)
        def save():
            with connect() as db:
                if db.execute('UPDATE tasks SET config=? WHERE id=?', (task.model_dump_json(), task_id)).rowcount == 0:
                    raise HTTPException(404, '任务不存在')
        if control:
            with control.condition:
                if control.state != 'paused' or control.closed:
                    raise HTTPException(409, '请先暂停任务，并等待正在处理的文件完成')
                old = Task.model_validate(control.raw).model_dump()
                # 旧版 source/target 是目录组的兼容别名，网页不再提交它们。
                task.source, task.target = old['source'], old['target']
                new = task.model_dump()
                original = old['pathMappings']
                if len(new['pathMappings']) < len(original) or new['pathMappings'][:len(original)] != original:
                    raise HTTPException(409, '暂停任务不能删除、修改或重排已有目录组，只能追加目录组')
                allowed = {'name', 'bandwidthLimit', 'maxRetries', 'retryInterval', 'continueOnError', 'parallelism', 'batchFiles', 'largeThresholdMb', 'networkRetryCount', 'networkRetryMinutes', 'pathMappings'}
                if any(old[k] != new[k] for k in old.keys() - allowed):
                    raise HTTPException(409, '暂停时仅可调整限速、重试、遇错继续、并发和批量参数；同步规则需结束后修改')
                effective = validate_task(new)
                with connect() as db:
                    db.execute('UPDATE tasks SET config=? WHERE id=?', (task.model_dump_json(), task_id))
                    snapshot = dict(control.config)
                    snapshot.update({k: new[k] for k in allowed - {'pathMappings'}})
                    snapshot['pathMappings'] = effective
                    db.execute('UPDATE runs SET snapshot=? WHERE id=?', (json.dumps(snapshot), control.run_id))
                    db.execute('INSERT INTO events(run_id,created,detail) VALUES(?,?,?)', (control.run_id, now(), json.dumps(dict(action='config_updated', reason='暂停期间修改参数或追加目录组', config=new), ensure_ascii=False)))
                control.config.update({k: new[k] for k in allowed - {'pathMappings'}})
                control.config['pathMappings'].extend(effective[len(original):])
                control.raw = new
        else:
            save()
        return {'ok': True}

    @app.delete('/api/tasks/{task_id}')
    def remove(task_id: int):
        with live_lock:
            if any(c.task_id == task_id for c in controls.values()):
                raise HTTPException(409, '执行中的任务不能删除')
        with connect() as db:
            if db.execute('DELETE FROM tasks WHERE id=?', (task_id,)).rowcount == 0:
                raise HTTPException(404, '任务不存在')
        return {'ok': True}

    def execute(run_id, config):
        with live_lock:
            control = controls[run_id]
        progress = Progress(len(config['pathMappings']))
        with live_lock:
            live[run_id] = progress
        buffer = []
        event_totals = dict(copied=0, skipped=0, deleted=0, ignored=0, failed=0, conflicts=0, bytes=0)
        buffer_lock = threading.RLock()
        def flush():
            with buffer_lock:
                if buffer:
                    with connect() as db:
                        db.executemany('INSERT INTO events(run_id,created,detail) VALUES(?,?,?)', buffer)
                    buffer.clear()
        control.flush = flush
        def emit(event):
            with buffer_lock:
                progress.update('event', action=event.get('action'))
                key = 'conflicts' if event.get('action') == 'conflict' else event.get('action')
                if key in event_totals:
                    event_totals[key] += 1
                if key == 'copied':
                    event_totals['bytes'] += event.get('bytes', 0)
                buffer.append((run_id, now(), json.dumps(event, ensure_ascii=False)))
                if len(buffer) >= 100:
                    flush()
        def with_network_retry(operation, mapping_index):
            attempt = 0
            while True:
                control.checkpoint()
                try:
                    return operation()
                except Exception as error:
                    if not config.get('continueOnError') or not network_error(error):
                        raise
                    limit = config.get('networkRetryCount', 3)
                    exhausted = attempt >= limit
                    if not exhausted:
                        attempt += 1
                    seconds = config.get('networkRetryMinutes', 3) * 60
                    emit(dict(action='network_retry_exhausted' if exhausted else 'network_retry',
                              mappingIndex=mapping_index, error=str(error), retryCount=attempt,
                              retryLimit=limit, waitSeconds=None if exhausted else seconds,
                              reason='重试次数用尽，保持暂停，请管理员检查网络后恢复' if exhausted else '网络或存储暂不可用，等待后重新比较本目录组，已完成文件按规则跳过'))
                    flush()
                    control.wait_for_retry(error, attempt, limit, seconds, exhausted)
                    progress.update('group', index=mapping_index)
                    if exhausted:
                        attempt = 0

        try:
            summaries = []
            # 所有目录组先检查，再开始扫描/复制；双向模式两侧都要写入。
            progress.update('phase', phase='checking')
            for index, mapping in enumerate(config['pathMappings'], 1):
                control.checkpoint()
                destinations = [mapping['target']]
                if config.get('direction') == 'bidirectional':
                    destinations.append(mapping['source'])
                for destination in destinations:
                    try:
                        detail = with_network_retry(lambda: check_destination(destination, config['dryRun'], read_directory=config.get('syncStrategy') != 'COPY_ALL'), index)
                        emit(dict(detail, action='preflight', mappingIndex=index))
                    except Exception as error:
                        emit(dict(action='mapping_failed', mappingIndex=index, path=destination, error=str(error)))
                        raise
            flush()
            checked_count = len(config['pathMappings'])
            index = 0
            while True:
                control.checkpoint()
                if index >= len(config['pathMappings']):
                    break
                mapping = config['pathMappings'][index]
                index += 1
                progress.groups = len(config['pathMappings'])
                # 追加目录也必须在开始该组前完成权限检查。
                if index > checked_count:
                    for destination in [mapping['target']] + ([mapping['source']] if config.get('direction') == 'bidirectional' else []):
                        emit(dict(with_network_retry(lambda: check_destination(destination, config['dryRun'], read_directory=config.get('syncStrategy') != 'COPY_ALL'), index), action='preflight', mappingIndex=index))
                progress.update('group', index=index)
                def mapping_emit(event):
                    emit(dict(event, mappingIndex=index, source=mapping['source'], target=mapping['target']))
                try:
                    mapping_emit(dict(action='mapping_started', comparisonMethod=method_name(config)))
                    summary = with_network_retry(lambda: synchronize(dict(config, **mapping, _progress=progress.update, _control=control), mapping_emit), index)
                    summaries.append(dict(summary, **mapping, mappingIndex=index))
                    mapping_emit(dict(action='mapping_completed', result=summary))
                    flush()
                except Exception as error:
                    mapping_emit(dict(action='mapping_failed', error=str(error)))
                    raise
            control.close()
            result = {key: sum(r[key] for r in summaries) for key in ('copied', 'skipped', 'deleted', 'bytes', 'scanned', 'durationSeconds', 'unsupportedSkipped', 'ignored', 'failed', 'conflicts')}
            result.update(event_totals)
            result['durationSeconds'] = progress.snapshot()['elapsedSeconds']
            result.update(dryRun=config['dryRun'], mappings=summaries, progress=progress.snapshot())
            has_issues = result['failed'] > 0 or result['conflicts'] > 0
            status = ('preview_partial' if config['dryRun'] else 'partial') if has_issues else ('preview' if config['dryRun'] else 'success')
            flush()
            with connect() as db:
                db.execute('UPDATE runs SET status=?,ended=?,result=? WHERE id=?',
                           (status, now(), json.dumps(result), run_id))
        except RunInterrupted as error:
            flush()
            with connect() as db:
                db.execute("UPDATE runs SET status='interrupted',ended=?,error=?,result=? WHERE id=?", (now(), str(error), json.dumps(dict(progress=progress.snapshot())), run_id))
        except Exception as error:
            flush()
            with connect() as db:
                db.execute("UPDATE runs SET status='failed',ended=?,error=?,result=? WHERE id=?", (now(), str(error), json.dumps(dict(progress=progress.snapshot())), run_id))
        finally:
            with live_lock:
                live.pop(run_id, None)
                controls.pop(run_id, None)
            control.close()
            gate.release()

    @app.post('/api/tasks/{task_id}/run', status_code=202)
    def run(task_id: int, options: RunOptions):
        if not gate.acquire(blocking=False):
            raise HTTPException(409, '已有任务执行中，请等待完成后再运行')
        try:
            with connect() as db:
                row = db.execute('SELECT config FROM tasks WHERE id=?', (task_id,)).fetchone()
                if not row:
                    raise HTTPException(404, '任务不存在')
                config = Task.model_validate(json.loads(row['config'])).model_dump()
                try:
                    config['pathMappings'] = validate_task(config)
                except ValueError as error:
                    raise HTTPException(422, str(error))
                config['dryRun'] = options.dryRun
                cursor = db.execute('INSERT INTO runs(task_id,status,started,snapshot) VALUES(?,?,?,?)',
                                    (task_id, 'running', now(), json.dumps(config)))
                run_id = cursor.lastrowid
            def changed(status):
                if status == 'paused' and hasattr(control, 'flush'):
                    control.flush()
                with connect() as db:
                    db.execute('UPDATE runs SET status=? WHERE id=?', (status, run_id))
                    db.execute('INSERT INTO events(run_id,created,detail) VALUES(?,?,?)', (run_id, now(), json.dumps(dict(action='control', reason={'pausing':'正在等待当前文件完成', 'paused':'已暂停，可修改参数或追加目录', 'running':'恢复执行', 'retry_wait':'网络或存储异常，等待定时重试'}[status]))))
            control = RunControl(config, changed)
            control.task_id, control.run_id = task_id, run_id
            control.raw = Task.model_validate(json.loads(row['config'])).model_dump()
            with live_lock:
                controls[run_id] = control
            pool.submit(execute, run_id, config)
            return {'runId': run_id}
        except Exception:
            gate.release()
            raise

    @app.post('/api/runs/{run_id}/pause')
    def pause(run_id: int):
        with live_lock:
            control = controls.get(run_id)
        if control is None:
            raise HTTPException(409, '该执行已结束或程序已重启，不能暂停')
        try:
            control.pause()
        except RunInterrupted as error:
            raise HTTPException(409, str(error))
        return {'status': control.state}

    @app.post('/api/runs/{run_id}/resume')
    def resume(run_id: int):
        with live_lock:
            control = controls.get(run_id)
        if control is None:
            raise HTTPException(409, '程序重启后请重新运行任务；已完成文件按比较规则跳过')
        try:
            control.resume()
        except RunInterrupted as error:
            raise HTTPException(409, str(error))
        return {'status': control.state}

    @app.get('/api/runs')
    def runs():
        with connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 100')]
        with live_lock:
            for row in rows:
                if row['id'] in live:
                    row['progress'] = live[row['id']].snapshot()
                if row['id'] in controls:
                    row['recovery'] = controls[row['id']].recovery
        return rows

    @app.get('/api/runs/{run_id}/report')
    def report(run_id: int):
        # 从已持久化日志汇总，失败/中断的任务也能看到已完成的工作。
        with connect() as db:
            run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if run is None:
                raise HTTPException(404, '执行记录不存在')
            rows = db.execute("SELECT json_extract(detail,'$.action') AS action,COUNT(*) AS count,COALESCE(SUM(CASE WHEN json_extract(detail,'$.action')='copied' THEN json_extract(detail,'$.bytes') ELSE 0 END),0) AS bytes FROM events WHERE run_id=? GROUP BY json_extract(detail,'$.action')", (run_id,)).fetchall()
            counts = {row['action']: row['count'] for row in rows}
            copied_bytes = sum(row['bytes'] for row in rows if row['action'] == 'copied')
            return dict(run=dict(run), counts=counts, copiedBytes=copied_bytes, totalEvents=sum(counts.values()),
                        partial=run['status']=='running')

    @app.get('/api/runs/{run_id}/logs')
    def query_logs(run_id: int, page: int = Query(1, ge=1),
                   page_size: int = Query(100, ge=1, le=500),
                   action: str = Query('', max_length=40),
                   q: str = Query('', max_length=256),
                   mapping: int | None = Query(None, ge=1),
                   order: Literal['asc', 'desc'] = 'asc'):
        allowed = {'copied','skipped','ignored','failed','conflict','deleted','mapping_started','mapping_completed','mapping_failed','deletion_skipped','preflight','control','config_updated','network_retry','network_retry_exhausted'}
        if action and action not in allowed:
            raise HTTPException(422, '不支持的日志状态')
        clauses, values = ['run_id=?'], [run_id]
        if action:
            clauses.append("json_extract(detail,'$.action')=?")
            values.append(action)
        if q.strip():
            # 搜索整次执行的文件路径；百分号和下划线按字面匹配。
            escaped = q.strip().replace('!', '!!').replace('%', '!%').replace('_', '!_')
            clauses.append("COALESCE(json_extract(detail,'$.path'),'') LIKE ? ESCAPE '!'")
            values.append('%'+escaped+'%')
        if mapping is not None:
            clauses.append("COALESCE(json_extract(detail,'$.mappingIndex'),CASE WHEN (SELECT COALESCE(json_array_length(json_extract(snapshot,'$.pathMappings')),1) FROM runs WHERE id=events.run_id)=1 THEN 1 END)=?")
            values.append(mapping)
        where = ' AND '.join(clauses)
        with connect() as db:
            if db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone() is None:
                raise HTTPException(404, '执行记录不存在')
            # 同一读事务内计算总数与分页，避免运行中新增日志造成分页结果不一致。
            db.execute('BEGIN')
            total = db.execute('SELECT COUNT(*) FROM events WHERE '+where, values).fetchone()[0]
            pages = max(1, (total+page_size-1)//page_size)
            page = min(page, pages)
            rows = db.execute('SELECT * FROM events WHERE '+where+' ORDER BY id '+order.upper()+' LIMIT ? OFFSET ?', (*values,page_size,(page-1)*page_size)).fetchall()
            return dict(items=[dict(row) for row in rows], total=total, page=page, pages=pages, pageSize=page_size)

    @app.get('/api/runs/{run_id}/events')
    def events(run_id: int, after: int = 0):
        with connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 500', (run_id, after))]

    return app
