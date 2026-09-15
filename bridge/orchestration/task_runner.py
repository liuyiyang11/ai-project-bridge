from __future__ import annotations

import traceback
from typing import Any, Mapping, Optional, Protocol

from ..store.task_store import TaskStore
from .event_bus import TaskEventBus
from .models import TaskResult
from .state_machine import TaskStateMachine


class TaskHandler(Protocol):
    def execute(self, task: dict[str, Any]) -> TaskResult:
        ...


class CodeTaskHandler:
    def __init__(self, executor: TaskHandler):
        self.executor = executor

    def execute(self, task: dict[str, Any]) -> TaskResult:
        return self.executor.execute(task)


class TaskRunner:
    """Route one durable task to its handler and own failure conversion."""

    _RESULT_LIFECYCLE_FIELDS = frozenset(
        {
            "state",
            "status",
            "stage",
            "current_action",
            "last_transition_reason",
            "review_ready",
            "event_seq",
            "last_event_seq",
            "last_error",
        }
    )

    def __init__(self, *, store: TaskStore, handlers: Optional[Mapping[str, TaskHandler]] = None):
        self.store = store
        self.handlers = dict(handlers or {})

    def run(self, task_id: str, *, recovery: bool = False, continuation: bool = False) -> TaskResult:
        task = self.store.get_task(task_id)
        bus = TaskEventBus(self.store, task_id)
        try:
            handler = self.handlers.get(str(task.get("task_type", "code")))
            if handler is None:
                raise ValueError(f"task type is not supported by the runtime: {task.get('task_type')}")
            current = str(self.store.get_task(task_id).get("state", ""))
            if current == "QUEUED":
                bus.transition("QUEUED", "PREPARING", "worker accepted task")
                current = "PREPARING"
            elif current == "RUNNING" and (recovery or continuation):
                pass
            elif current != "PREPARING":
                raise RuntimeError(f"task is not runnable from {current}")
            if current == "PREPARING":
                bus.transition("PREPARING", "RUNNING", "task execution started")
            if self._is_interrupted(task_id):
                return self._interrupted_result()
            result = handler.execute(task)
            if not isinstance(result, TaskResult):
                raise TypeError("task handlers must return TaskResult")
            self._persist_result(task_id, result)
            # This is not outcome inference: an explicit control transition
            # can race a handler, and must win over its late result.
            if self._is_interrupted(task_id):
                return self._interrupted_result()
            if not result.success:
                if recovery:
                    return self._mark_unknown(task_id, RuntimeError(result.message or "task handler reported failure"))
                self._fail(
                    task_id,
                    RuntimeError(result.message or "task handler reported failure"),
                    message=result.message or "task handler reported failure",
                )
                return result
            if result.review_ready:
                bus.transition("RUNNING", "WAITING_REVIEW", result.message or "task execution completed", updates={"review_ready": True})
            return result
        except Exception as exc:
            if self._is_interrupted(task_id):
                return self._interrupted_result()
            if recovery:
                return self._mark_unknown(task_id, exc)
            self._fail(task_id, exc)
            raise

    def _persist_result(self, task_id: str, result: TaskResult) -> None:
        if result.metadata:
            metadata = dict(result.metadata)
            reserved = self._RESULT_LIFECYCLE_FIELDS.intersection(metadata)
            if reserved:
                names = ", ".join(sorted(reserved))
                raise ValueError(f"task result metadata cannot replace lifecycle fields: {names}")
            self.store.update_task(task_id, **metadata)
        if not result.artifacts:
            return
        manifest = self.store.save_artifacts(task_id, list(result.artifacts))
        bus = TaskEventBus(self.store, task_id)
        for artifact in manifest:
            bus.emit(
                "artifact_created",
                {
                    "path": artifact.get("path"),
                    "size": artifact.get("size", artifact.get("bytes")),
                    "kind": artifact.get("kind"),
                },
            )

    def _mark_unknown(self, task_id: str, error: Exception) -> TaskResult:
        message = f"recovery could not safely resume task: {type(error).__name__}: {error}"[:2000]
        bus = TaskEventBus(self.store, task_id)
        current = str(self.store.get_task(task_id).get("state", ""))
        if current != "UNKNOWN" and TaskStateMachine.can_transition(current, "UNKNOWN"):
            bus.transition(current, "UNKNOWN", "recovery resume could not be verified")
        bus.emit("error", {"message": message, "previous_state": current})
        self.store.update_task(task_id, last_error=message)
        return TaskResult(success=False, message=message)

    def _is_interrupted(self, task_id: str) -> bool:
        return str(self.store.get_task(task_id).get("state", "")) == "INTERRUPTED"

    @staticmethod
    def _interrupted_result() -> TaskResult:
        return TaskResult(success=True, review_ready=False, message="task interrupted")

    def _fail(self, task_id: str, error: Exception, *, message: Optional[str] = None) -> None:
        message = (message or f"{type(error).__name__}: {error}")[:2000]
        formatted = traceback.format_exc(limit=6)
        summary = "" if formatted.strip() == "NoneType: None" else " ".join(formatted.splitlines())[-4000:]
        bus = TaskEventBus(self.store, task_id)
        current = str(self.store.get_task(task_id).get("state", ""))
        already_failed = current == "FAILED"
        if not already_failed and TaskStateMachine.can_transition(current, "FAILED"):
            bus.transition(current, "FAILED", "task failed")
        if not already_failed:
            bus.emit("error", {"message": message, "traceback": summary})
        last_error = f"{message}; traceback: {summary}" if summary else message
        self.store.update_task(task_id, last_error=last_error)
