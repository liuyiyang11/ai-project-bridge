from __future__ import annotations

from dataclasses import dataclass, field
import subprocess
from threading import Condition, Event, RLock
from typing import Any, Optional


DEFAULT_PROCESS_CLEANUP_TIMEOUT = 1.0


@dataclass
class ExperimentRuntime:
    """Cancellation and process lifetime for one experiment invocation."""

    task_id: str
    cancel_event: Event = field(default_factory=Event)
    process_attached: Event = field(default_factory=Event, repr=False)
    process: Optional[Any] = None


def _process_has_exited(process: Any) -> bool:
    poll = getattr(process, "poll", None)
    if not callable(poll):
        return False
    try:
        return poll() is not None
    except Exception:
        return False


def _wait_bounded(process: Any, timeout: float) -> bool:
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return _process_has_exited(process)
    try:
        wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return _process_has_exited(process)


def terminate_process_bounded(process: Any, *, timeout: float = DEFAULT_PROCESS_CLEANUP_TIMEOUT) -> None:
    """Terminate one direct child without waiting indefinitely."""

    if _process_has_exited(process):
        return
    terminate = getattr(process, "terminate", None)
    if callable(terminate):
        try:
            terminate()
        except Exception:
            pass
    if _wait_bounded(process, timeout):
        return
    kill = getattr(process, "kill", None)
    if callable(kill):
        try:
            kill()
        except Exception:
            return
    _wait_bounded(process, timeout)


class ExperimentRuntimeRegistry:
    """Own cancellation signals and direct child handles, not task state."""

    def __init__(self, *, cleanup_timeout: float = DEFAULT_PROCESS_CLEANUP_TIMEOUT):
        if float(cleanup_timeout) <= 0:
            raise ValueError("cleanup_timeout must be positive")
        self.cleanup_timeout = float(cleanup_timeout)
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._runtimes: dict[str, ExperimentRuntime] = {}
        self._pending_cancellations: set[str] = set()

    def register(self, task_id: str) -> ExperimentRuntime:
        with self._changed:
            if task_id in self._runtimes:
                raise ValueError(f"experiment runtime is already active: {task_id}")
            runtime = ExperimentRuntime(task_id=task_id)
            if task_id in self._pending_cancellations:
                runtime.cancel_event.set()
                self._pending_cancellations.discard(task_id)
            self._runtimes[task_id] = runtime
            self._changed.notify_all()
            return runtime

    def wait_for_runtime(self, task_id: str, *, timeout: Optional[float] = None) -> Optional[ExperimentRuntime]:
        with self._changed:
            if task_id not in self._runtimes:
                self._changed.wait_for(lambda: task_id in self._runtimes, timeout=timeout)
            return self._runtimes.get(task_id)

    def current(self, task_id: str) -> Optional[ExperimentRuntime]:
        with self._lock:
            return self._runtimes.get(task_id)

    def snapshot(self) -> dict[str, ExperimentRuntime]:
        with self._lock:
            return dict(self._runtimes)

    def attach_process(self, task_id: str, process: Any) -> None:
        with self._lock:
            runtime = self._runtimes.get(task_id)
            if runtime is None:
                should_cancel = True
            else:
                runtime.process = process
                runtime.process_attached.set()
                should_cancel = runtime.cancel_event.is_set()
        if should_cancel:
            terminate_process_bounded(process, timeout=self.cleanup_timeout)
            if runtime is None:
                raise RuntimeError(f"experiment runtime is no longer active: {task_id}")

    def request_cancel(self, task_id: str) -> bool:
        """Set cancellation under the registry lock, then clean up outside it."""

        with self._lock:
            runtime = self._runtimes.get(task_id)
            if runtime is None:
                self._pending_cancellations.add(task_id)
                return False
            runtime.cancel_event.set()
            process = runtime.process
        if process is not None:
            terminate_process_bounded(process, timeout=self.cleanup_timeout)
        return True

    def detach(self, task_id: str) -> Optional[ExperimentRuntime]:
        with self._lock:
            runtime = self._runtimes.pop(task_id, None)
            self._pending_cancellations.discard(task_id)
            return runtime
