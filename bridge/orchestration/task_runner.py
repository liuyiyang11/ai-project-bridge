from __future__ import annotations

import traceback
from typing import Any, Mapping, Optional, Protocol

from ..store.task_store import TaskStore
from .event_bus import TaskEventBus
from .state_machine import TaskStateMachine


class TaskHandler(Protocol):
    def execute(self, task: dict[str, Any]) -> Any:
        ...


class CodeTaskHandler:
    def __init__(self, executor: TaskHandler):
        self.executor = executor

    def execute(self, task: dict[str, Any]) -> Any:
        return self.executor.execute(task)


class TaskRunner:
    """Route one durable task to its handler and own failure conversion."""

    def __init__(self, *, store: TaskStore, handlers: Optional[Mapping[str, TaskHandler]] = None):
        self.store = store
        self.handlers = dict(handlers or {})

    def run(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        handler = self.handlers.get(str(task.get("task_type", "code")))
        if handler is None:
            raise ValueError(f"task type is not supported by the runtime: {task.get('task_type')}")
        bus = TaskEventBus(self.store, task_id)
        try:
            current = str(self.store.get_task(task_id).get("state", ""))
            if current == "QUEUED":
                bus.transition("QUEUED", "PREPARING", "worker accepted task")
            elif current != "PREPARING":
                raise RuntimeError(f"task is not runnable from {current}")
            handler.execute(task)
            current = str(self.store.get_task(task_id).get("state", ""))
            if current == "PREPARING":
                bus.transition("PREPARING", "RUNNING", "task execution started")
                current = "RUNNING"
            if current == "RUNNING":
                bus.transition("RUNNING", "WAITING_REVIEW", "task execution completed")
            return self.store.get_task(task_id)
        except Exception as exc:
            self._fail(task_id, exc)
            raise

    def _fail(self, task_id: str, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"[:2000]
        summary = " ".join(traceback.format_exc(limit=6).splitlines())[-4000:]
        bus = TaskEventBus(self.store, task_id)
        current = str(self.store.get_task(task_id).get("state", ""))
        if current != "FAILED" and TaskStateMachine.can_transition(current, "FAILED"):
            bus.transition(current, "FAILED", "task failed")
        bus.emit("error", {"message": message, "traceback": summary})
        self.store.update_task(task_id, last_error=f"{message}; traceback: {summary}")

