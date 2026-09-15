from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class TaskState(str, Enum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    WAITING_REVIEW = "WAITING_REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


TASK_TRANSITIONS: dict[str, set[str]] = {
    TaskState.QUEUED.value: {TaskState.PREPARING.value, TaskState.FAILED.value},
    TaskState.PREPARING.value: {
        TaskState.RUNNING.value,
        TaskState.FAILED.value,
        TaskState.INTERRUPTED.value,
        TaskState.UNKNOWN.value,
    },
    TaskState.RUNNING.value: {
        TaskState.WAITING_REVIEW.value,
        TaskState.FAILED.value,
        TaskState.INTERRUPTED.value,
        TaskState.UNKNOWN.value,
    },
    TaskState.WAITING_REVIEW.value: {TaskState.RUNNING.value, TaskState.COMPLETED.value},
    TaskState.INTERRUPTED.value: {TaskState.PREPARING.value, TaskState.FAILED.value},
    TaskState.FAILED.value: {TaskState.PREPARING.value},
    TaskState.UNKNOWN.value: {TaskState.RUNNING.value, TaskState.CANCELLED.value},
    TaskState.COMPLETED.value: set(),
    TaskState.CANCELLED.value: set(),
}


@dataclass
class TaskSnapshot:
    task_id: str
    project: str
    task_type: str = "code"
    state: str = TaskState.QUEUED.value
    created_at: str = ""
    updated_at: str = ""
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    worktree: Optional[str] = None
    events_file: str = "events.jsonl"
    artifact_manifest: str = "result.json"
    stage: str = "queued"
    changed_files: list[str] = field(default_factory=list)
    last_event_seq: int = 0
    instruction: str = ""
    acceptance: list[str] = field(default_factory=list)
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project": self.project,
            "task_type": self.task_type,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "worktree": self.worktree,
            "events_file": self.events_file,
            "artifact_manifest": self.artifact_manifest,
            "stage": self.stage,
            "changed_files": list(self.changed_files),
            "last_event_seq": self.last_event_seq,
            "instruction": self.instruction,
            "acceptance": list(self.acceptance),
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }

