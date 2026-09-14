from __future__ import annotations

import re
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, root_validator, validator

from .security import SecurityError, ensure_safe_project_name, ensure_safe_relative_path


class TaskParseError(ValueError):
    """Raised when an Issue body is not a valid Bridge task."""


class BridgeTask(BaseModel):
    class Config:
        extra = "forbid"
        anystr_strip_whitespace = True

    version: Literal[1]
    task_type: Literal["code", "presentation", "experiment-review"]
    project: str
    title: str = Field(min_length=1, max_length=200)
    goal: str | None = None
    instructions: str | None = None
    acceptance: list[str] = Field(default_factory=list)
    command_id: str | None = None
    brief: str | None = None
    slides_spec: str | None = None
    assets_dir: str | None = None
    template: str | None = None

    @validator("project")
    def validate_project(cls, value: str) -> str:
        try:
            return ensure_safe_project_name(value)
        except SecurityError as exc:
            raise ValueError(str(exc)) from exc

    @validator("acceptance")
    def validate_acceptance(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("acceptance entries must be non-empty strings")
        return value

    @validator("command_id")
    def validate_command_id(cls, value: str | None) -> str | None:
        if value is not None and (not value or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in value)):
            raise ValueError("command_id must be a safe identifier")
        return value

    @root_validator
    def validate_task_specific_fields(cls, values: dict) -> dict:
        task_type = values.get("task_type")
        paths = {
            "brief": values.get("brief"),
            "slides_spec": values.get("slides_spec"),
            "assets_dir": values.get("assets_dir"),
            "template": values.get("template"),
        }
        for name, value in paths.items():
            if value is not None:
                try:
                    ensure_safe_relative_path(value)
                except SecurityError as exc:
                    raise ValueError(f"{name} must be a project-relative path: {exc}") from exc
        if task_type == "experiment-review" and not values.get("command_id"):
            raise ValueError("experiment-review task requires command_id")
        if task_type == "presentation" and not any((values.get("brief"), values.get("slides_spec"), values.get("instructions"))):
            raise ValueError("presentation task requires brief, slides_spec, or instructions")
        return values


_TASK_BLOCK = re.compile(r"<!--\s*AI_BRIDGE_TASK\s*-->\s*```yaml\s*\r?\n(.*?)\r?\n?```", re.IGNORECASE | re.DOTALL)
_REWORK_BLOCK = re.compile(r"<!--\s*AI_BRIDGE_REWORK\s*-->\s*```yaml\s*\r?\n(.*?)\r?\n?```", re.IGNORECASE | re.DOTALL)


def parse_task_body(body: str) -> BridgeTask:
    if "AI_BRIDGE_TASK" not in body:
        raise TaskParseError("Issue body is missing AI_BRIDGE_TASK marker")
    matches = _TASK_BLOCK.findall(body)
    if len(matches) != 1:
        raise TaskParseError("Issue body must contain exactly one YAML task block")
    try:
        raw = yaml.safe_load(matches[0])
        if not isinstance(raw, dict):
            raise TaskParseError("task YAML must be a mapping")
        return BridgeTask.parse_obj(raw)
    except TaskParseError:
        raise
    except (yaml.YAMLError, ValidationError, TypeError, ValueError) as exc:
        raise TaskParseError(f"invalid task schema: {exc}") from exc


def parse_rework_comment(body: str) -> str:
    if "AI_BRIDGE_REWORK" not in body:
        raise TaskParseError("comment is missing AI_BRIDGE_REWORK marker")
    matches = _REWORK_BLOCK.findall(body)
    if len(matches) != 1:
        raise TaskParseError("rework comment must contain exactly one YAML block")
    try:
        raw = yaml.safe_load(matches[0])
    except yaml.YAMLError as exc:
        raise TaskParseError(f"invalid rework YAML: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"instruction"} or not isinstance(raw.get("instruction"), str) or not raw["instruction"].strip():
        raise TaskParseError("rework YAML must contain only a non-empty instruction")
    return raw["instruction"].strip()
