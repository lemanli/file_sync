"""有界的实时进度：只保存计数及活动线程，不积累文件历史。"""
import threading
import time


class Progress:
    def __init__(self, groups):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.groups = groups
        self.group = 0
        self.phase = 'queued'
        self.scanned = self.discovered = self.finished = self.written = 0
        self.total = None
        self.scan_complete = False
        self.counts = dict(copied=0, skipped=0, failed=0, ignored=0, deleted=0, conflicts=0)
        self.active = {}
        self.samples = [(self.started, 0, 0)]

    def update(self, kind, **data):
        with self.lock:
            if kind == 'group':
                self.group = data['index']
                self.phase = 'scanning'
                self.discovered = self.finished = 0
                self.total = None
                self.scan_complete = False
                self.active.clear()
            elif kind == 'scan':
                self.scanned += 1
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
                self.active[threading.get_ident()] = data
            elif kind == 'written':
                self.written += data['delta']
                self.active[threading.get_ident()] = dict(path=data['path'], size=data['size'], written=data['written'], phase='copying')
            elif kind == 'clear_file':
                self.active.pop(threading.get_ident(), None)
            elif kind == 'file_done':
                self.active.pop(threading.get_ident(), None)
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
                self.samples.append((current, self.written, processed))
            while len(self.samples) > 2 and self.samples[1][0] < current - 10:
                self.samples.pop(0)
            first = self.samples[0]
            seconds = max(current - first[0], .001)
            return dict(phase=self.phase, mappingIndex=self.group, mappingCount=self.groups,
                        scannedEntries=self.scanned, discovered=self.discovered, finished=self.finished,
                        totalFiles=self.total, scanComplete=self.scan_complete, counts=dict(self.counts), writtenBytes=self.written,
                        bytesPerSecond=(self.written-first[1])/seconds,
                        filesPerSecond=(processed-first[2])/seconds,
                        elapsedSeconds=current-self.started,
                        activeFiles=[dict(value) for value in self.active.values()])
