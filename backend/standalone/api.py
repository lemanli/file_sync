"""免登录本机 API：任务持久化、独立后台线程、运行日志分页。"""
from __future__ import annotations
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from .engine import roots, synchronize
from .folders import list_directories
from .rules import validate_patterns
from .comparison import method_name
from . import __version__
from .progress import Progress
from .preflight import check_destination


def now():
    return datetime.now(timezone.utc).isoformat()


class PathMapping(BaseModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)


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
        validate_patterns(self.ignorePatterns)
        if self.direction == 'bidirectional' and self.delete:
            raise ValueError('双向合并不支持删除，请关闭删除选项')
        return self
    direction: Literal['one_way', 'bidirectional'] = 'one_way'
    conflictPolicy: Literal['skip', 'newer'] = 'skip'
    ignorePatterns: list[str] = Field(default_factory=list, max_length=200)
    continueOnError: bool = False
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
    live_lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sync-task')

    @contextmanager
    def connect():
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
        db.execute("UPDATE runs SET status='interrupted',ended=?,error='程序上次退出时任务未完成' WHERE status='running'", (now(),))

    @asynccontextmanager
    async def lifespan(app):
        yield
        # 等待正在写入的文件完成，避免进程退出时后台线程丢失最终状态。
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
        return await call_next(request)

    def validate_paths(source, target):
        src, dst = roots(source, target)
        protected = database.parent
        for path in (src, dst):
            if path == protected or path in protected.parents or protected in path.parents:
                raise ValueError('同步目录不能包含或位于程序的 SQLite 数据目录')
        return src, dst

    def validate_task(config):
        task = Task.model_validate(config)
        mappings = task.pathMappings
        paths = [validate_paths(m.source, m.target) for m in mappings]
        def overlaps(a, b):
            return a == b or a in b.parents or b in a.parents
        # 目标不得影响任何其他源，也不能与其他目标相互覆盖。
        for i, (src, dst) in enumerate(paths):
            for j, (other_src, other_dst) in enumerate(paths):
                if i != j and (overlaps(dst, other_src) or overlaps(dst, other_dst)
                               or (task.direction == 'bidirectional' and overlaps(src, other_src))):
                    raise ValueError(f'第 {i+1} 组目标与第 {j+1} 组源或目标重叠')
        return [m.model_dump() for m in mappings]

    @app.get('/api/directories')
    def directories(path: str = '', offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500),
                    query: str = Query('', max_length=255),
                    sort: Literal['name_asc', 'name_desc', 'modified_asc', 'modified_desc'] = 'name_asc'):
        return list_directories(path, database.parent, offset, limit, query, sort)

    @app.get('/')
    def index():
        return FileResponse(Path(__file__).with_name('index.html'))

    @app.get('/api/health')
    def health():
        return {'releaseVersion': __version__, 'mode': 'standalone', 'database': 'sqlite', 'version': 1, 'configSchemaVersion': 2, 'comparisonDetailsVersion': 1, 'timeOptionsVersion': 1, 'progressVersion': 1, 'logQueryVersion': 1}

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
        with connect() as db:
            if db.execute('UPDATE tasks SET config=? WHERE id=?', (task.model_dump_json(), task_id)).rowcount == 0:
                raise HTTPException(404, '任务不存在')
        return {'ok': True}

    @app.delete('/api/tasks/{task_id}')
    def remove(task_id: int):
        with connect() as db:
            if db.execute('DELETE FROM tasks WHERE id=?', (task_id,)).rowcount == 0:
                raise HTTPException(404, '任务不存在')
        return {'ok': True}

    def execute(run_id, config):
        progress = Progress(len(config['pathMappings']))
        with live_lock:
            live[run_id] = progress
        buffer = []
        def flush():
            if buffer:
                with connect() as db:
                    db.executemany('INSERT INTO events(run_id,created,detail) VALUES(?,?,?)', buffer)
                buffer.clear()
        def emit(event):
            progress.update('event', action=event.get('action'))
            buffer.append((run_id, now(), json.dumps(event, ensure_ascii=False)))
            if len(buffer) >= 100:
                flush()
        try:
            summaries = []
            # 所有目录组先检查，再开始扫描/复制；双向模式两侧都要写入。
            progress.update('phase', phase='checking')
            for index, mapping in enumerate(config['pathMappings'], 1):
                destinations = [mapping['target']]
                if config.get('direction') == 'bidirectional':
                    destinations.append(mapping['source'])
                for destination in destinations:
                    try:
                        detail = check_destination(destination, config['dryRun'])
                        emit(dict(detail, action='preflight', mappingIndex=index))
                    except Exception as error:
                        emit(dict(action='mapping_failed', mappingIndex=index, path=destination, error=str(error)))
                        raise
            flush()
            for index, mapping in enumerate(config['pathMappings'], 1):
                progress.update('group', index=index)
                def mapping_emit(event):
                    emit(dict(event, mappingIndex=index, source=mapping['source'], target=mapping['target']))
                try:
                    mapping_emit(dict(action='mapping_started', comparisonMethod=method_name(config)))
                    summary = synchronize(dict(config, **mapping, _progress=progress.update), mapping_emit)
                    summaries.append(dict(summary, **mapping, mappingIndex=index))
                    mapping_emit(dict(action='mapping_completed', result=summary))
                    flush()
                except Exception as error:
                    mapping_emit(dict(action='mapping_failed', error=str(error)))
                    raise
            result = {key: sum(r[key] for r in summaries) for key in ('copied', 'skipped', 'deleted', 'bytes', 'scanned', 'durationSeconds', 'unsupportedSkipped', 'ignored', 'failed', 'conflicts')}
            result.update(dryRun=config['dryRun'], mappings=summaries, progress=progress.snapshot())
            has_issues = result['failed'] > 0 or result['conflicts'] > 0
            status = ('preview_partial' if config['dryRun'] else 'partial') if has_issues else ('preview' if config['dryRun'] else 'success')
            flush()
            with connect() as db:
                db.execute('UPDATE runs SET status=?,ended=?,result=? WHERE id=?',
                           (status, now(), json.dumps(result), run_id))
        except Exception as error:
            flush()
            with connect() as db:
                db.execute("UPDATE runs SET status='failed',ended=?,error=?,result=? WHERE id=?", (now(), str(error), json.dumps(dict(progress=progress.snapshot())), run_id))
        finally:
            with live_lock:
                live.pop(run_id, None)
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
                config = json.loads(row['config'])
                try:
                    config['pathMappings'] = validate_task(config)
                except ValueError as error:
                    raise HTTPException(422, str(error))
                config['dryRun'] = options.dryRun
                cursor = db.execute('INSERT INTO runs(task_id,status,started,snapshot) VALUES(?,?,?,?)',
                                    (task_id, 'running', now(), json.dumps(config)))
                run_id = cursor.lastrowid
            pool.submit(execute, run_id, config)
            return {'runId': run_id}
        except Exception:
            gate.release()
            raise

    @app.get('/api/runs')
    def runs():
        with connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 100')]
        with live_lock:
            for row in rows:
                if row['id'] in live:
                    row['progress'] = live[row['id']].snapshot()
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
        allowed = {'copied','skipped','ignored','failed','conflict','deleted','mapping_started','mapping_completed','mapping_failed','deletion_skipped','preflight'}
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
