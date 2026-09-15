from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import Field, StrictStr, validator

from ._base import ResearchModel, validate_identifier, validate_mapping
from .artifact import Artifact
from .metric import MetricRecord


class Hypothesis(ResearchModel):
    """The reason for running an experiment."""

    hypothesis_id: StrictStr = Field(min_length=1, max_length=128)
    statement: StrictStr = Field(min_length=1, max_length=10000)
    experiment_id: Optional[StrictStr] = Field(default=None, max_length=128)
    rationale: Optional[StrictStr] = Field(default=None, max_length=10000)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    _validate_hypothesis_id = validator("hypothesis_id", allow_reuse=True)(
        lambda value, field: validate_identifier(value, field.name)
    )
    _validate_experiment_id = validator("experiment_id", allow_reuse=True)(
        lambda value, field: validate_identifier(value, field.name) if value is not None else value
    )

    @validator("metadata")
    def validate_metadata(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return validate_mapping(value, "metadata")

class ExperimentObservation(ResearchModel):
    """What was done and what the experiment produced."""

    observation_id: StrictStr = Field(min_length=1, max_length=128)
    experiment_id: StrictStr = Field(min_length=1, max_length=128)
    task_id: Optional[StrictStr] = Field(default=None, max_length=128)
    action: StrictStr = Field(min_length=1, max_length=10000)
    result: Dict[str, Any] = Field(default_factory=dict)
    metrics: List[MetricRecord] = Field(default_factory=list)
    artifacts: List[Artifact] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    _validate_ids = validator(
        "observation_id", "experiment_id", "task_id", allow_reuse=True
    )(
        lambda value, field: validate_identifier(value, field.name) if value is not None else value
    )

    @validator("result", "metadata")
    def validate_mappings(cls, value: Dict[str, Any], field) -> Dict[str, Any]:
        return validate_mapping(value, field.name)


class Decision(ResearchModel):
    """The next step selected from an observation and its rationale."""

    decision_id: StrictStr = Field(min_length=1, max_length=128)
    observation_id: StrictStr = Field(min_length=1, max_length=128)
    decision: StrictStr = Field(min_length=1, max_length=10000)
    rationale: StrictStr = Field(min_length=1, max_length=10000)
    next_step: Optional[StrictStr] = Field(default=None, max_length=10000)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    _validate_ids = validator("decision_id", "observation_id", allow_reuse=True)(
        lambda value, field: validate_identifier(value, field.name)
    )

    @validator("metadata")
    def validate_metadata(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return validate_mapping(value, "metadata")
