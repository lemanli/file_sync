"""文件边界的协作暂停：保留扫描位置，不挂起正在写入的临时文件。"""
from contextlib import contextmanager
import threading
import time


class RunInterrupted(Exception):
    pass


class RunControl:
    def __init__(self, config, changed):
        self.config = config
        self.changed = changed
        self.condition = threading.Condition(threading.RLock())
        self.requested = False
        self.closed = False
        self.active = 0
        self.acknowledged = False
        self.state = 'running'
        self.recovery = None

    def _set(self, state):
        if self.state != state:
            self.state = state
            self.changed(state)

    def pause(self):
        with self.condition:
            if self.closed:
                raise RunInterrupted('执行已结束')
            if self.requested:
                return
            self.acknowledged = False
            self.requested = True
            self._set('pausing')
            self.condition.notify_all()

    def resume(self):
        with self.condition:
            if self.closed:
                raise RunInterrupted('执行已结束')
            self.requested = False
            self._set('running')
            self.condition.notify_all()

    def checkpoint(self, main=True):
        with self.condition:
            if self.requested and main:
                self.acknowledged = True
                if not self.active:
                    self._set('paused')
            while self.requested and not self.closed:
                self.condition.wait()
            if self.closed:
                raise RunInterrupted('程序退出，执行已中断；重新运行将重新比较文件')

    @contextmanager
    def file(self, config):
        with self.condition:
            self.checkpoint(main=False)
            defaults = {'bandwidthLimit': 0, 'maxRetries': 2, 'retryInterval': 1, 'continueOnError': False}
            for key, default in defaults.items():
                config[key] = self.config.get(key, config.get(key, default))
            self.active += 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                if self.requested and self.acknowledged and not self.active and not self.closed:
                    self._set('paused')

    def wait_for_retry(self, error, attempt, limit, seconds, exhausted=False):
        """只有本组 worker 已全部退出后调用；管理员恢复会立即开始下一次尝试。"""
        with self.condition:
            self.recovery = dict(error=str(error), attempt=attempt, limit=limit,
                                 nextRetryAt=None if exhausted else time.time()+seconds,
                                 requiresResume=exhausted)
            if exhausted:
                self.requested = True
                self.acknowledged = True
                self._set('paused')
                self.checkpoint()
            else:
                self._set('retry_wait')
                deadline=time.monotonic()+seconds
                while not self.closed and self.state=='retry_wait':
                    remaining=deadline-time.monotonic()
                    if remaining<=0:
                        break
                    self.condition.wait(remaining)
                self.checkpoint()
                self._set('running')
            self.recovery = None

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
