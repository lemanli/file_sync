"""共享批次的文件调度器：每批一个 Future，文件由空闲线程共同消费。"""
from collections import deque
from concurrent.futures import Future
import threading


class CopyPool:
    def __init__(self, limit):
        self.limit = limit
        self.condition = threading.Condition()
        self.queue = deque()
        self.active = 0
        self.closed = False
        self.threads = []

    def __enter__(self):
        # 线程按提交时的并发上限懒创建，暂停修改后无需销毁队列。
        return self

    def submit(self, function, items, *, priority=False):
        future = Future()
        items = deque(items)
        if not items:
            future.set_result([])
            return future
        group = [function, items, future, len(items), [], None]
        with self.condition:
            if self.closed:
                raise RuntimeError('复制调度器已关闭')
            (self.queue.appendleft if priority else self.queue.append)(group)
            self._grow()
            self.condition.notify_all()
        return future

    def _grow(self):
        for index in range(len(self.threads), max(1, min(32, self.limit()))):
            thread = threading.Thread(target=self._worker, name=f'sync-copy-{index}')
            self.threads.append(thread)
            thread.start()

    def _worker(self):
        while True:
            with self.condition:
                self._grow()
                while not self.queue or self.active >= self.limit():
                    if self.closed and not self.queue:
                        return
                    # 恢复时即使没有新批次提交，也允许增加消费线程。
                    self.condition.wait(.1)
                    self._grow()
                group = self.queue[0]
                function, items, future = group[:3]
                item = items.popleft()
                if not items:
                    self.queue.popleft()
                self.active += 1
            events = []; error = None
            try:
                if not future.cancelled():
                    events = function([item])
            except BaseException as caught:
                error = caught
            with self.condition:
                self.active -= 1
                group[3] -= 1
                group[4].extend(events)
                if error is not None and group[5] is None:
                    group[5] = error
                if not group[3] and not future.cancelled():
                    if group[5] is not None:
                        future.set_exception(group[5])
                    else:
                        future.set_result(group[4])
                self.condition.notify_all()

    def __exit__(self, *args):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        # worker 可能在恢复后扩容，结束前持续等待全部线程。
        index = 0
        while index < len(self.threads):
            self.threads[index].join()
            index += 1
