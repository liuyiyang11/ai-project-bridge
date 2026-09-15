from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class TaskRequest:
    task_type: str
    project: str
    instruction: str = ""
    acceptance: Optional[List[str]] = None
    task_id: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    command_id: Optional[str] = None
    source_task_id: Optional[str] = None
    review: Any = None
    brief: Optional[str] = None
    slides_spec: Optional[str] = None
    assets_dir: Optional[str] = None
    template: Optional[str] = None
    renderer: str = "auto"
    # Private, already-validated transport context.  It is persisted with the
    # task so a worker/recovery pass can reconstruct the transport adapter
    # without making the transport own task lifecycle state.
    metadata: Optional[dict[str, Any]] = None


class TaskRouter:
    """Map validated task types to the supervisor's executor paths."""

    TASK_TYPES = frozenset({"code", "experiment", "experiment-review", "presentation"})

    @classmethod
    def validate_type(cls, task_type: str) -> str:
        if task_type not in cls.TASK_TYPES:
            raise ValueError(f"unsupported task type: {task_type}")
        return task_type

    @classmethod
    def route(cls, request: TaskRequest) -> str:
        return cls.validate_type(request.task_type)
