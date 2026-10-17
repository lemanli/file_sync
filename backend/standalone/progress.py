"""有界的实时进度：只保存计数及活动线程，不积累文件历史。"""
import threading
import time
from collections import deque


class Progress:
    def __init__(self, groups):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.groups = groups
        self.group = 0
        self.phase = 'queued'
        self.scanned = self.discovered = self.finished = self.written = 0
        self.scan_path = self.scan_side = None
        self.total = None
        self.scan_complete = False
        self.counts = dict(copied=0, skipped=0, failed=0, ignored=0, deleted=0, conflicts=0)
        self.active = {}
        self.phase_totals = {}
        self.file_timings = {}
        self.slow_files = deque(maxlen=5)
        self.sample_window = self.started
        self.sample_count = 0
        self.scan_metrics = {}
        self.path_check_metrics = {}
        self.path_check_samples = deque(maxlen=8)
        self.path_sample_window = self.started
        self.path_sample_count = 0
        self.samples = [(self.started, 0, 0, 0)]

    def _record_phase(self, identity, current):
        active = self.active.get(identity)
        if not active:
            return
        phase = active['phase']
        seconds = max(0, current-active.get('phaseStarted', current))
        total = self.phase_totals.setdefault(phase, dict(seconds=0, count=0, maxSeconds=0))
        total['seconds'] += seconds
        total['count'] += 1
        total['maxSeconds'] = max(total['maxSeconds'], seconds)
        timings = self.file_timings.setdefault(identity, {})
        timings[phase] = timings.get(phase, 0)+seconds

    def diagnostics(self):
        snapshot = self.snapshot()
        with self.lock:
            samples = list(self.slow_files)
            self.slow_files.clear()
        return dict(phaseTimings=snapshot['phaseTimings'], slowFiles=samples,
                    activeFiles=snapshot['activeFiles'], counts=snapshot['counts'],
                    elapsedSeconds=snapshot['elapsedSeconds'], scanMetrics=snapshot['scanMetrics'], pathCheckMetrics=snapshot['pathCheckMetrics'], pathCheckSamples=snapshot['pathCheckSamples'])

    def update(self, kind, **data):
        with self.lock:
            if kind == 'group':
                self.group = data['index']
                self.phase = 'scanning'
                self.discovered = self.finished = 0
                self.total = None
                self.scan_complete = False
                self.active.clear()
                self.file_timings.clear()
            elif kind == 'path_metrics':
                current = time.monotonic()
                if current-self.path_sample_window >= 10:
                    self.path_sample_window = current
                    self.path_sample_count = 0
                for item in data.pop('samples', []):
                    if self.path_sample_count < 8:
                        self.path_check_samples.append(dict(item, elapsedSeconds=current-self.started))
                        self.path_sample_count += 1
                for key, value in data.items():
                    self.path_check_metrics[key] = self.path_check_metrics.get(key, 0) + value
            elif kind == 'metrics':
                self.scan_metrics = dict(data)
            elif kind == 'scan':
                self.scanned += data.get('count', 1)
                self.scan_path = data.get('path', self.scan_path)
                self.scan_side = data.get('side', self.scan_side)
            elif kind == 'discovered':
                self.discovered += 1
            elif kind == 'total':
                self.total = data['value']
            elif kind == 'scan_done':
                self.total = self.discovered
                self.scan_complete = True
            elif kind == 'phase':
                self.phase = data['phase']
            elif kind == 'file':
                self._record_phase(threading.get_ident(), time.monotonic())
                self.active[threading.get_ident()] = dict(data, phaseStarted=time.monotonic())
            elif kind == 'written':
                self.written += data['delta']
                previous = self.active.get(threading.get_ident(), {})
                phase = 'flushing' if previous.get('phase') == 'flushing' else 'copying'
                if previous.get('phase') != phase:
                    self._record_phase(threading.get_ident(), time.monotonic())
                started = previous.get('phaseStarted', time.monotonic()) if previous.get('phase') == phase else time.monotonic()
                self.active[threading.get_ident()] = dict(path=data['path'], size=data['size'], written=data['written'], phase=phase, phaseStarted=started)
            elif kind == 'clear_file':
                self.active.pop(threading.get_ident(), None)
            elif kind == 'file_done':
                identity = threading.get_ident(); current = time.monotonic()
                self._record_phase(identity, current)
                active = self.active.pop(identity, None)
                timings = self.file_timings.pop(identity, {})
                if current-self.sample_window >= 10:
                    self.sample_window = current; self.sample_count = 0
                if active and sum(timings.values()) >= 1 and self.sample_count < 5:
                    self.slow_files.append(dict(path=active['path'], seconds=sum(timings.values()), phases=timings))
                    self.sample_count += 1
                self.finished += 1
            elif kind == 'event':
                action = data['action']
                key = 'conflicts' if action == 'conflict' else action
                if key in self.counts:
                    self.counts[key] += 1

    def snapshot(self):
        with self.lock:
            current = time.monotonic()
            processed = self.counts['copied'] + self.counts['skipped'] + self.counts['failed'] + self.counts['conflicts']
            # 至多保留约十秒采样，频繁 API 请求不会导致无界增长。
            if current - self.samples[-1][0] >= 1:
                self.samples.append((current, self.written, processed, self.scanned))
            while len(self.samples) > 2 and self.samples[1][0] < current - 10:
                self.samples.pop(0)
            first = self.samples[0]
            seconds = max(current - first[0], .001)
            return dict(phaseTimings={key:dict(value) for key,value in self.phase_totals.items()},
                        scanMetrics=dict(self.scan_metrics),
                        pathCheckMetrics=dict(self.path_check_metrics),
                        pathCheckSamples=[dict(item) for item in self.path_check_samples],
                        sourceScanRate=self.scan_metrics.get('sourceEntries',0)/max(current-self.started,.001),
                        targetScanRate=self.scan_metrics.get('targetEntries',0)/max(current-self.started,.001),
                        directoriesPerSecond=(self.scan_metrics.get('sourceDirectories',0)+self.scan_metrics.get('targetDirectories',0))/max(current-self.started,.001),
                        phase=self.phase, mappingIndex=self.group, mappingCount=self.groups,
                        scanPath=self.scan_path, scanSide=self.scan_side,
                        scanEntriesPerSecond=(self.scanned-first[3])/seconds,
                        scannedEntries=self.scanned, discovered=self.discovered, finished=self.finished,
                        totalFiles=self.total, scanComplete=self.scan_complete, counts=dict(self.counts), writtenBytes=self.written,
                        bytesPerSecond=(self.written-first[1])/seconds,
                        filesPerSecond=(processed-first[2])/seconds,
                        elapsedSeconds=current-self.started,
                        activeFiles=[dict({k:v for k,v in value.items() if k != 'phaseStarted'},
                                          phaseSeconds=max(0,current-value.get('phaseStarted',current)))
                                     for value in self.active.values()])
