from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Protocol

from ..experiments.runtime import ExperimentRuntimeRegistry
from ..public_errors import public_error_from_exception
from ..security import validate_unicode_scalars
from ..store.task_store import TaskStore
from .event_bus import TaskEventBus
from .models import TaskResult
from .state_machine import TaskStateMachine


logger = logging.getLogger(__name__)


class TaskHandler(Protocol):
    def execute(self, task: dict[str, Any]) -> TaskResult:
        ...


class CodeTaskHandler:
    def __init__(self, executor: TaskHandler):
        self.executor = executor

    def execute(self, task: dict[str, Any]) -> TaskResult:
        return self.executor.execute(task)


class ExperimentTaskHandler:
    """Adapter that keeps experiment execution below the shared TaskRunner."""

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
            "error_code",
        }
    )

    def __init__(
        self,
        *,
        store: TaskStore,
        handlers: Optional[Mapping[str, TaskHandler]] = None,
        experiment_runtime: Optional[ExperimentRuntimeRegistry] = None,
    ):
        self.store = store
        self.handlers = dict(handlers or {})
        self.experiment_runtime = experiment_runtime

    def run(self, task_id: str, *, recovery: bool = False, continuation: bool = False) -> TaskResult:
        task = self.store.get_task(task_id)
        bus = TaskEventBus(self.store, task_id)
        runtime_registered = False
        try:
            if str(task.get("task_type", "code")) == "experiment" and self.experiment_runtime is not None:
                self.experiment_runtime.register(task_id)
                runtime_registered = True
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
            if self._is_interrupted(task_id):
                return self._interrupted_result()
            result_artifacts = list(result.artifacts) if result.artifacts else []
            validate_unicode_scalars(
                {
                    "message": result.message,
                    "metadata": result.metadata,
                    "artifacts": self.store._bounded_artifacts(result_artifacts),
                }
            )
            persisted = self._persist_result(task_id, result)
            # This is not outcome inference: an explicit control transition
            # can race a handler, and must win over its late result.
            if self._is_interrupted(task_id):
                return self._interrupted_result()
            if not persisted:
                # Another lifecycle owner already moved the task away from
                # RUNNING.  Do not apply a late handler outcome to that state.
                return result
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
        finally:
            if runtime_registered and self.experiment_runtime is not None:
                self.experiment_runtime.detach(task_id)

    def _persist_result(self, task_id: str, result: TaskResult) -> bool:
        metadata: Optional[dict[str, Any]] = None
        if result.metadata:
            metadata = dict(result.metadata)
            reserved = self._RESULT_LIFECYCLE_FIELDS.intersection(metadata)
            if reserved:
                names = ", ".join(sorted(reserved))
                raise ValueError(f"task result metadata cannot replace lifecycle fields: {names}")
        bus = TaskEventBus(self.store, task_id)
        return bus.persist_result_if_active(
            metadata=metadata,
            artifacts=list(result.artifacts) if result.artifacts else None,
        )

    def _mark_unknown(self, task_id: str, error: Exception) -> TaskResult:
        public = public_error_from_exception(error, context="recovery")
        logger.error(
            "task recovery could not be verified task_id=%s error_type=%s",
            task_id,
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        bus = TaskEventBus(self.store, task_id)
        current = str(self.store.get_task(task_id).get("state", ""))
        if current != "UNKNOWN" and TaskStateMachine.can_transition(current, "UNKNOWN"):
            bus.transition(current, "UNKNOWN", "recovery resume could not be verified")
        bus.emit("error", {**public.as_dict(), "previous_state": current})
        self.store.update_task(task_id, last_error=public.message, error_code=public.error_code)
        return TaskResult(success=False, message=public.message)

    def _is_interrupted(self, task_id: str) -> bool:
        return str(self.store.get_task(task_id).get("state", "")) == "INTERRUPTED"

    @staticmethod
    def _interrupted_result() -> TaskResult:
        return TaskResult(success=True, review_ready=False, message="task interrupted")

    def _fail(self, task_id: str, error: Exception, *, message: Optional[str] = None) -> None:
        public = public_error_from_exception(error, context="task")
        logger.error(
            "task failed task_id=%s error_type=%s",
            task_id,
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        bus = TaskEventBus(self.store, task_id)
        current = str(self.store.get_task(task_id).get("state", ""))
        already_failed = current == "FAILED"
        if not already_failed and TaskStateMachine.can_transition(current, "FAILED"):
            bus.transition(current, "FAILED", "task failed")
        if not already_failed:
            bus.emit("error", public.as_dict())
        self.store.update_task(task_id, last_error=public.message, error_code=public.error_code)
