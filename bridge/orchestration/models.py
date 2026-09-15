from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskResult:
    """The explicit outcome returned by every asynchronous task handler."""

    success: bool
    review_ready: bool = False
    message: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
