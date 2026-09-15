from __future__ import annotations

from typing import Any, Dict

from pydantic import Field, StrictStr, validator

from ._base import ResearchModel, validate_identifier, validate_mapping


class ExperimentSpec(ResearchModel):
    """Reproducible linkage between a dataset, feature set, model, and code."""

    experiment_id: StrictStr = Field(min_length=1, max_length=128)
    dataset_id: StrictStr = Field(min_length=1, max_length=128)
    feature_id: StrictStr = Field(min_length=1, max_length=128)
    model: StrictStr = Field(min_length=1, max_length=512)
    config: Dict[str, Any] = Field(default_factory=dict)
    code_version: StrictStr = Field(min_length=1, max_length=256)

    _validate_ids = validator(
        "experiment_id", "dataset_id", "feature_id", allow_reuse=True
    )(lambda value, field: validate_identifier(value, field.name))

    @validator("config")
    def validate_config(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return validate_mapping(value, "config")
