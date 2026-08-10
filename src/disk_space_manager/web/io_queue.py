"""One serialized queue for report scans and protected-folder disk I/O."""

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable


class HeavyIOQueue:
    """Serialize disk-heavy work so scans and Restic do not compete for I/O."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dsm-io")

    def submit(self, function: Callable[..., Any], *args: Any) -> Future:
        return self._executor.submit(function, *args)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
