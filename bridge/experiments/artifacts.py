from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Optional

from ..security import SecurityError, ensure_safe_relative_path


ARTIFACT_KINDS = frozenset({"metrics", "logs", "prediction_image", "confusion_matrix"})
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_METRIC_SUFFIXES = frozenset({".json", ".csv", ".txt"})


def classify_artifact(path: str) -> str:
    """Map a bounded, project-relative artifact path to the public contract."""

    safe = ensure_safe_relative_path(path).replace("\\", "/")
    parsed = PurePosixPath(safe)
    parts = {part.casefold() for part in parsed.parts}
    name = parsed.name.casefold()
    if "confusion" in name or "confusion" in parts:
        return "confusion_matrix"
    if parsed.suffix.casefold() in _IMAGE_SUFFIXES:
        return "prediction_image"
    if parsed.suffix.casefold() == ".log" or "log" in parts or "logs" in parts:
        return "logs"
    if parsed.suffix.casefold() in _METRIC_SUFFIXES:
        return "metrics"
    raise ValueError(f"artifact is outside the experiment contract: {path!r}")


@dataclass(frozen=True)
class ExperimentArtifact:
    """The durable, public description of one experiment artifact."""

    path: str
    kind: str
    size: int
    source: Optional[str] = None

    def __post_init__(self) -> None:
        try:
            safe_path = ensure_safe_relative_path(self.path)
        except SecurityError as exc:
            raise ValueError(str(exc)) from exc
        if self.kind not in ARTIFACT_KINDS:
            raise ValueError(f"unsupported experiment artifact kind: {self.kind!r}")
        if isinstance(self.size, bool) or int(self.size) < 0:
            raise ValueError("artifact size must be a non-negative integer")
        object.__setattr__(self, "path", safe_path.replace("\\", "/"))
        object.__setattr__(self, "size", int(self.size))
        if self.source is not None:
            try:
                source = ensure_safe_relative_path(self.source)
            except SecurityError as exc:
                raise ValueError(str(exc)) from exc
            object.__setattr__(self, "source", source.replace("\\", "/"))

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"path": self.path, "kind": self.kind, "size": self.size, "bytes": self.size}
        if self.source is not None:
            result["source"] = self.source
        return result


class ArtifactContract:
    """Normalize collector output without changing the existing collector."""

    VERSION = "v0.3"

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in records:
            if not isinstance(record, Mapping):
                continue
            path = record.get("path")
            source = record.get("source")
            if not isinstance(path, str):
                continue
            kind = classify_artifact(str(source or path))
            if kind not in ARTIFACT_KINDS:
                continue
            if path in seen:
                continue
            size = record.get("size", record.get("bytes", 0))
            if isinstance(size, bool) or not isinstance(size, (int, float)):
                continue
            artifact = ExperimentArtifact(path=path, kind=kind, size=int(size), source=source if isinstance(source, str) else None)
            output.append(artifact.as_dict())
            seen.add(artifact.path)
        return output

    @classmethod
    def log_artifact(cls, path: str, size: int) -> dict[str, Any]:
        artifact = ExperimentArtifact(path=path, kind="logs", size=size)
        return artifact.as_dict()
