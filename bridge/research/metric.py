from __future__ import annotations

import math
from typing import Any, Dict, Union

from pydantic import Field, StrictFloat, StrictInt, StrictStr, validator

from ._base import ResearchModel, validate_identifier, validate_mapping


MetricValue = Union[StrictInt, StrictFloat]


class MetricRecord(ResearchModel):
    """One scalar measurement produced by a task.

    Metric names are intentionally unconstrained beyond being non-empty; names
    such as RMSE or mIoU are data, not part of this contract.
    """

    name: StrictStr = Field(min_length=1, max_length=256)
    value: MetricValue
    unit: StrictStr = ""
    task_id: StrictStr = Field(min_length=1, max_length=128)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    _validate_task_id = validator("task_id", allow_reuse=True)(
        lambda value, field: validate_identifier(value, field.name)
    )

    @validator("value")
    def validate_value(cls, value: MetricValue) -> MetricValue:
        if isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError("metric value must be a finite number")
        return value

    @validator("metadata")
    def validate_metadata(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return validate_mapping(value, "metadata")
