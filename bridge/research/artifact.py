from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal

from pydantic import Field, StrictStr, validator

from ..security import SecurityError, ensure_safe_relative_path
from ._base import ResearchModel, validate_identifier, validate_mapping


ArtifactType = Literal[
    "dataset",
    "processed_dataset",
    "feature",
    "checkpoint",
    "metric",
    "visualization",
    "report",
]
ARTIFACT_TYPES = frozenset(
    {
        "dataset",
        "processed_dataset",
        "feature",
        "checkpoint",
        "metric",
        "visualization",
        "report",
    }
)


class Artifact(ResearchModel):
    """A durable reference to a research output.

    ``path`` is intentionally project-relative.  The model stores metadata only;
    it does not read, download, or otherwise materialize the referenced file.
    """

    artifact_id: StrictStr = Field(min_length=1, max_length=128)
    task_id: StrictStr = Field(min_length=1, max_length=128)
    type: ArtifactType
    path: StrictStr = Field(min_length=1, max_length=4096)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    _validate_ids = validator("artifact_id", "task_id", allow_reuse=True)(
        lambda value, field: validate_identifier(value, field.name)
    )

    @validator("path")
    def validate_path(cls, value: str) -> str:
        try:
            safe_path = ensure_safe_relative_path(value)
        except SecurityError as exc:
            raise ValueError(str(exc)) from exc
        return safe_path.replace("\\", "/")

    @validator("metadata")
    def validate_metadata(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return validate_mapping(value, "metadata")
