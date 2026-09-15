from __future__ import annotations

import re
from typing import Any, Mapping

from pydantic import BaseModel


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ResearchModel(BaseModel):
    """Shared serialization and validation policy for research contracts."""

    class Config:
        extra = "forbid"
        anystr_strip_whitespace = True
        validate_assignment = True

    def to_dict(self) -> dict[str, Any]:
        """Return the Python representation used by artifact manifests."""

        return self.dict()

    def to_json(self) -> str:
        """Return a JSON representation suitable for durable storage."""

        return self.json(ensure_ascii=False)


def validate_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{field_name} must be a safe identifier containing only letters, numbers, '.', '_' or '-'"
        )
    return value


def validate_mapping(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    if any(not isinstance(key, str) or not key.strip() for key in value):
        raise ValueError(f"{field_name} keys must be non-empty strings")
    return dict(value)
