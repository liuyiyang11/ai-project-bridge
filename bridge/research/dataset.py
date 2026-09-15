from __future__ import annotations

from typing import Any, Dict

from pydantic import Field, StrictStr, validator

from ._base import ResearchModel, validate_identifier, validate_mapping


class DatasetManifest(ResearchModel):
    """Project-agnostic description of a dataset used by an experiment."""

    dataset_id: StrictStr = Field(min_length=1, max_length=128)
    name: StrictStr = Field(min_length=1, max_length=512)
    source: StrictStr = Field(min_length=1, max_length=4096)
    version: StrictStr = Field(min_length=1, max_length=128)
    resolution: StrictStr = Field(min_length=1, max_length=256)
    projection: StrictStr = Field(min_length=1, max_length=256)
    time_range: Dict[str, Any]
    spatial_extent: Dict[str, Any]
    metadata: Dict[str, Any] = Field(default_factory=dict)

    _validate_id = validator("dataset_id", allow_reuse=True)(
        lambda value, field: validate_identifier(value, field.name)
    )

    @validator("time_range", "spatial_extent", "metadata")
    def validate_mappings(cls, value: Dict[str, Any], field) -> Dict[str, Any]:
        return validate_mapping(value, field.name)
