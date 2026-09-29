"""One serialized queue for report scans and protected-folder disk I/O."""

from concurrent.futures import Future, ThreadPoolExecutor
from threading import RLock
from typing import Any, Callable


class HeavyIOQueue:
    """Serialize disk-heavy work so scans and Restic do not compete for I/O."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="coldshelf-io")
        self._lock = RLock()
        self._pending = 0

    def submit(self, function: Callable[..., Any], *args: Any) -> Future:
        with self._lock:
            self._pending += 1
            try:
                future = self._executor.submit(function, *args)
            except BaseException:
                self._pending -= 1
                raise
            future.add_done_callback(self._finished)
            return future

    def _finished(self, _future: Future) -> None:
        with self._lock:
            self._pending -= 1

    def has_pending_work(self) -> bool:
        with self._lock:
            return self._pending > 0

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
