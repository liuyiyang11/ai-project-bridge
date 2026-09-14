from __future__ import annotations

import re
from typing import Any, Callable, Optional, Type

from pydantic import BaseModel, ValidationError

from ..orchestration.router import TaskRequest
from ..orchestration.supervisor import TaskSupervisor
from .schemas import (
    EmptyInput,
    StartCodeInput,
    StartExperimentInput,
    StartPresentationInput,
    TaskArtifactsInput,
    TaskControlInput,
    TaskEventsInput,
    TaskStatusInput,
)


class McpToolError(ValueError):
    """A safe, caller-facing MCP tool validation or execution error."""


class BridgeMcpTools:
    """The nine local Bridge tools exposed by the stdio adapter."""

    _SCHEMAS: dict[str, Type[BaseModel]] = {
        "bridge_list_projects": EmptyInput,
        "bridge_codex_catalog": EmptyInput,
        "bridge_start_code_task": StartCodeInput,
        "bridge_start_experiment_review": StartExperimentInput,
        "bridge_start_presentation_task": StartPresentationInput,
        "bridge_task_status": TaskStatusInput,
        "bridge_task_events": TaskEventsInput,
        "bridge_control_task": TaskControlInput,
        "bridge_task_artifacts": TaskArtifactsInput,
    }

    def __init__(self, config: Any = None, *, supervisor: Optional[TaskSupervisor] = None):
        if supervisor is None and config is None:
            raise ValueError("config or supervisor is required")
        self.supervisor = supervisor or TaskSupervisor(config)

    @classmethod
    def definitions(cls) -> list[dict[str, Any]]:
        descriptions = {
            "bridge_list_projects": "List registered projects and public capabilities.",
            "bridge_codex_catalog": "List runtime Codex models and supported reasoning efforts.",
            "bridge_start_code_task": "Start a code task in an isolated registered-project worktree.",
            "bridge_start_experiment_review": "Run a registered deterministic experiment review against a trusted candidate.",
            "bridge_start_presentation_task": "Start a presentation task in an isolated registered-project worktree.",
            "bridge_task_status": "Read safe status for a Bridge task.",
            "bridge_task_events": "Read a bounded incremental safe event stream for a Bridge task.",
            "bridge_control_task": "Steer, interrupt, continue, or accept a Bridge task.",
            "bridge_task_artifacts": "Read a bounded artifact manifest for a Bridge task.",
        }
        return [
            {"name": name, "description": descriptions[name], "inputSchema": schema.schema()}
            for name, schema in cls._SCHEMAS.items()
        ]

    def call(self, name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        schema = self._SCHEMAS.get(name)
        if schema is None:
            raise McpToolError(f"unknown Bridge tool: {name}")
        try:
            value = schema.parse_obj(arguments or {})
        except ValidationError as exc:
            raise McpToolError(self._safe_error(str(exc))) from exc
        try:
            handler = getattr(self, name)
            return handler(value)
        except McpToolError:
            raise
        except Exception as exc:
            raise McpToolError(self._safe_error(str(exc))) from exc

    def bridge_list_projects(self, value: EmptyInput) -> dict[str, Any]:
        return {"projects": self.supervisor.list_projects()}

    def bridge_codex_catalog(self, value: EmptyInput) -> dict[str, Any]:
        return self.supervisor.codex_catalog()

    def bridge_start_code_task(self, value: StartCodeInput) -> dict[str, Any]:
        return self.supervisor.start_code_task(
            value.project,
            value.instruction,
            value.acceptance,
            model=value.model,
            reasoning_effort=value.reasoning_effort,
        )

    def bridge_start_experiment_review(self, value: StartExperimentInput) -> dict[str, Any]:
        return self.supervisor.start_experiment_review(
            value.project,
            value.command_id,
            source_task_id=value.source_task_id,
            review=value.review,
        )

    def bridge_start_presentation_task(self, value: StartPresentationInput) -> dict[str, Any]:
        return self.supervisor.start_presentation_task(
            value.project,
            value.instruction,
            brief=value.brief,
            slides_spec=value.slides_spec,
            assets_dir=value.assets_dir,
            template=value.template,
            renderer=value.renderer,
            model=value.model,
            reasoning_effort=value.reasoning_effort,
        )

    def bridge_task_status(self, value: TaskStatusInput) -> dict[str, Any]:
        return self.supervisor.task_status(value.task_id)

    def bridge_task_events(self, value: TaskEventsInput) -> dict[str, Any]:
        events = self.supervisor.task_events(value.task_id, after_seq=value.after_seq, limit=value.limit)
        return {"task_id": value.task_id, "events": events}

    def bridge_control_task(self, value: TaskControlInput) -> dict[str, Any]:
        return self.supervisor.control_task(value.task_id, value.action, value.instruction)

    def bridge_task_artifacts(self, value: TaskArtifactsInput) -> dict[str, Any]:
        return {"task_id": value.task_id, "artifacts": self.supervisor.task_artifacts(value.task_id, kind=value.kind, limit=value.limit)}

    @staticmethod
    def _safe_error(message: str) -> str:
        # Do not reflect Windows drive paths or common POSIX absolute paths in
        # MCP errors.  The caller only needs the validation category.
        value = re.sub(r"[A-Za-z]:\\[^\n\r,)]+", "<absolute-path>", message)
        value = re.sub(r"(?<![A-Za-z0-9_])/(?:[^\s,)]+/)*[^\s,)]+", "<absolute-path>", value)
        return value[:2000]
