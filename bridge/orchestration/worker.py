from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import RLock
from typing import Any, Callable, Optional


class WorkerQueue:
    """Shared future queue; task-specific lifecycle logic stays in TaskRunner."""

    def __init__(self, max_workers: int = 4, *, executor: Optional[Any] = None):
        if int(max_workers) < 1:
            raise ValueError("max_workers must be positive")
        self._executor = executor or ThreadPoolExecutor(max_workers=int(max_workers), thread_name_prefix="bridge-worker")
        self._lock = RLock()
        self._futures: dict[str, Future[Any]] = {}

    def submit(self, task_id: str, callable_: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        with self._lock:
            existing = self._futures.get(task_id)
            if existing is not None and not existing.done():
                raise ValueError(f"task is already queued or running: {task_id}")
            future = self._executor.submit(callable_, *args, **kwargs)
            self._futures[task_id] = future
            return future

    def future(self, task_id: str) -> Optional[Future[Any]]:
        with self._lock:
            return self._futures.get(task_id)

    def cancel(self, task_id: str) -> bool:
        future = self.future(task_id)
        return bool(future and future.cancel())

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
