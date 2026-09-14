from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Union


class CodexModelError(ValueError):
    """Raised before a task is sent when model routing is invalid."""


@dataclass(frozen=True)
class ModelRecord:
    id: str
    model: str
    display_name: str = ""
    description: str = ""
    is_default: bool = False
    hidden: bool = False
    default_reasoning_effort: Optional[str] = None
    supported_reasoning_efforts: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ModelRecord":
        efforts = value.get("supportedReasoningEfforts", value.get("supported_reasoning_efforts", [])) or []
        normalized: list[str] = []
        for item in efforts:
            if isinstance(item, dict):
                item = item.get("reasoningEffort", item.get("reasoning_effort"))
            if isinstance(item, str) and item:
                normalized.append(item)
        default = value.get("defaultReasoningEffort", value.get("default_reasoning_effort"))
        model_id = value.get("id") or value.get("model")
        model_name = value.get("model") or model_id
        if not isinstance(model_id, str) or not model_id:
            raise CodexModelError("model/list returned a model without an id")
        return cls(
            id=model_id,
            model=model_name if isinstance(model_name, str) else model_id,
            display_name=str(value.get("displayName", value.get("display_name", "")) or ""),
            description=str(value.get("description", "") or ""),
            is_default=bool(value.get("isDefault", value.get("is_default", False))),
            hidden=bool(value.get("hidden", False)),
            default_reasoning_effort=default if isinstance(default, str) and default else None,
            supported_reasoning_efforts=tuple(dict.fromkeys(normalized)),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "display_name": self.display_name,
            "description": self.description,
            "is_default": self.is_default,
            "hidden": self.hidden,
            "default_reasoning_effort": self.default_reasoning_effort,
            "supported_reasoning_efforts": list(self.supported_reasoning_efforts),
        }


class CodexModelCatalog:
    """Runtime model catalog returned by the app-server ``model/list`` API."""

    def __init__(self, models: Iterable[Union[ModelRecord, dict[str, Any]]] = ()):
        self.models = tuple(item if isinstance(item, ModelRecord) else ModelRecord.from_dict(item) for item in models)

    @classmethod
    def from_response(cls, response: dict[str, Any]) -> "CodexModelCatalog":
        data = response.get("data", []) if isinstance(response, dict) else []
        if not isinstance(data, list):
            raise CodexModelError("model/list response data must be a list")
        return cls(data)

    def find(self, model: str) -> Optional[ModelRecord]:
        return next((item for item in self.models if model in {item.id, item.model}), None)

    def default(self) -> Optional[ModelRecord]:
        return next((item for item in self.models if item.is_default), None) or (self.models[0] if self.models else None)

    def validate(self, model: Optional[str], reasoning_effort: Optional[str], *, policy: str = "inherit") -> tuple[Optional[str], Optional[str]]:
        if policy not in {"inherit", "explicit"}:
            raise CodexModelError(f"unsupported routing policy: {policy}")
        if policy == "explicit" and (not model or not reasoning_effort):
            raise CodexModelError("explicit routing requires both model and reasoning_effort")
        selected = self.find(model) if model else self.default()
        if model and selected is None:
            raise CodexModelError(f"model is not available in model/list: {model}")
        if reasoning_effort:
            if selected is None:
                raise CodexModelError("cannot validate reasoning_effort because model/list is empty")
            if selected.supported_reasoning_efforts and reasoning_effort not in selected.supported_reasoning_efforts:
                raise CodexModelError(
                    f"reasoning_effort {reasoning_effort!r} is not supported by model {selected.id!r}; "
                    f"supported: {', '.join(selected.supported_reasoning_efforts)}"
                )
        return (selected.id if model and selected else model, reasoning_effort)

    def validate_combination(self, model: Optional[str], reasoning_effort: Optional[str], *, policy: str = "inherit") -> tuple[Optional[str], Optional[str]]:
        return self.validate(model, reasoning_effort, policy=policy)

    def public_dict(self) -> dict[str, Any]:
        return {"models": [item.public_dict() for item in self.models]}
