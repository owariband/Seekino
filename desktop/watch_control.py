"""Connect a Qt stop action to the Gateway's active HTTP request."""
from threading import Lock

from gateway.usage import ModelCallCancelledError


class WatchCallControl:
    def __init__(self):
        self._lock = Lock()
        self._calls = set()
        self.cancelled = False

    def acquire(self, call):
        with self._lock:
            if self.cancelled:
                raise ModelCallCancelledError("已停止回答。")
            self._calls.add(call)

    def release(self, call):
        with self._lock:
            self._calls.discard(call)

    def cancel(self):
        with self._lock:
            self.cancelled = True
            calls = tuple(self._calls)
        for call in calls:
            call.cancel()
