from __future__ import annotations

import re
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, StrictInt, StrictStr, validator

from ..security import SecurityError, ensure_safe_relative_path


def _safe_task_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError("task_id must be a safe identifier")
    return value


class StrictModel(BaseModel):
    class Config:
        extra = "forbid"
        anystr_strip_whitespace = True


class EmptyInput(StrictModel):
    pass


class StartCodeInput(StrictModel):
    project: StrictStr = Field(min_length=1, max_length=128)
    instruction: StrictStr = Field(min_length=1, max_length=50000)
    acceptance: List[StrictStr] = Field(default_factory=list, max_items=100)
    model: Optional[StrictStr] = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: Optional[StrictStr] = Field(default=None, min_length=1, max_length=100)


class StartExperimentInput(StrictModel):
    project: StrictStr = Field(min_length=1, max_length=128)
    command_id: StrictStr = Field(min_length=1, max_length=128)
    source_task_id: Optional[StrictStr] = Field(default=None, max_length=128)
    review: Any = None

    @validator("source_task_id")
    def validate_source_task_id(cls, value: Optional[str]) -> Optional[str]:
        return _safe_task_id(value) if value is not None else None


class StartPresentationInput(StrictModel):
    project: StrictStr = Field(min_length=1, max_length=128)
    instruction: StrictStr = Field(min_length=1, max_length=50000)
    brief: Optional[StrictStr] = None
    slides_spec: Optional[StrictStr] = None
    assets_dir: Optional[StrictStr] = None
    template: Optional[StrictStr] = None
    renderer: Literal["auto", "wps", "powerpoint", "libreoffice"] = "auto"
    model: Optional[StrictStr] = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: Optional[StrictStr] = Field(default=None, min_length=1, max_length=100)

    @validator("brief", "slides_spec", "assets_dir", "template")
    def validate_project_path(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            try:
                return ensure_safe_relative_path(value)
            except SecurityError as exc:
                raise ValueError(str(exc)) from exc
        return value


class TaskStatusInput(StrictModel):
    task_id: StrictStr = Field(min_length=1, max_length=128)

    _safe_id = validator("task_id", allow_reuse=True)(_safe_task_id)


class TaskEventsInput(StrictModel):
    task_id: StrictStr = Field(min_length=1, max_length=128)
    after_seq: StrictInt = Field(default=0, ge=0)
    limit: StrictInt = Field(default=100, ge=1, le=1000)

    _safe_id = validator("task_id", allow_reuse=True)(_safe_task_id)


class TaskControlInput(StrictModel):
    task_id: StrictStr = Field(min_length=1, max_length=128)
    action: Literal["steer", "interrupt", "continue", "accept"]
    instruction: Optional[StrictStr] = Field(default=None, max_length=50000)

    _safe_id = validator("task_id", allow_reuse=True)(_safe_task_id)


class TaskArtifactsInput(StrictModel):
    task_id: StrictStr = Field(min_length=1, max_length=128)
    kind: Optional[StrictStr] = Field(default=None, max_length=100)
    limit: StrictInt = Field(default=100, ge=1, le=1000)

    _safe_id = validator("task_id", allow_reuse=True)(_safe_task_id)
