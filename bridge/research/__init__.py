"""Project-agnostic research data contracts."""

from .artifact import ARTIFACT_TYPES, Artifact, ArtifactType
from .data_task import DataTaskHandler, DataTaskSpec, FakeDataAdapter, RegisteredDataAdapter
from .dataset import DatasetManifest
from .evidence import Decision, ExperimentObservation, Hypothesis
from .experiment import ExperimentSpec
from .metric import MetricRecord

__all__ = [
    "ARTIFACT_TYPES",
    "Artifact",
    "ArtifactType",
    "DataTaskHandler",
    "DataTaskSpec",
    "DatasetManifest",
    "Decision",
    "ExperimentObservation",
    "ExperimentSpec",
    "FakeDataAdapter",
    "Hypothesis",
    "MetricRecord",
    "RegisteredDataAdapter",
]
