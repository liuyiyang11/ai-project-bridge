from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from bridge.research.artifact import Artifact
from bridge.research.dataset import DatasetManifest
from bridge.research.evidence import Decision, ExperimentObservation, Hypothesis
from bridge.research.experiment import ExperimentSpec
from bridge.research.metric import MetricRecord


CREATED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def test_research_models_round_trip_through_json_serialization():
    dataset = DatasetManifest(
        dataset_id="dataset-v1",
        name="regional-observations",
        source="registry://regional-observations",
        version="2026.09",
        resolution="daily / 1 km",
        projection="EPSG:4326",
        time_range={"start": "2026-01-01", "end": "2026-01-31"},
        spatial_extent={"west": 110.0, "south": 20.0, "east": 111.0, "north": 21.0},
        metadata={"provider": "example", "tags": ["research", "v1"]},
    )
    experiment = ExperimentSpec(
        experiment_id="exp-001",
        dataset_id=dataset.dataset_id,
        feature_id="features-v2",
        model="baseline-model",
        config={"seed": 7, "epochs": 5},
        code_version="git:abc123",
    )
    metric = MetricRecord(
        name="validation-score",
        value=0.875,
        unit="1",
        task_id="task-001",
        metadata={"split": "validation"},
    )
    artifact = Artifact(
        artifact_id="artifact-001",
        task_id="task-001",
        type="metric",
        path="experiment_bundle/metrics.json",
        metadata={"metric": metric.name, "experiment_id": experiment.experiment_id},
        created_at=CREATED_AT,
    )

    observation = ExperimentObservation(
        observation_id="obs-001",
        experiment_id=experiment.experiment_id,
        task_id=metric.task_id,
        action="Run the baseline with the registered configuration",
        result={"status": "completed", "summary": "baseline recorded"},
        metrics=[metric],
        artifacts=[artifact],
    )
    hypothesis = Hypothesis(
        hypothesis_id="hyp-001",
        experiment_id=experiment.experiment_id,
        statement="The revised feature set improves validation performance.",
        rationale="The additional feature group captures the expected signal.",
    )
    decision = Decision(
        decision_id="decision-001",
        observation_id=observation.observation_id,
        decision="Proceed to a controlled comparison.",
        rationale="The baseline result is recorded and reproducible.",
        next_step="Run the same configuration with the comparison feature set.",
    )

    serialized = json.loads(decision.to_json())
    assert serialized["observation_id"] == observation.observation_id
    assert json.loads(artifact.json())["created_at"] == CREATED_AT.isoformat()
    assert Artifact.parse_raw(artifact.to_json()) == artifact
    assert DatasetManifest.parse_raw(dataset.to_json()) == dataset
    assert ExperimentSpec.parse_raw(experiment.to_json()) == experiment
    assert MetricRecord.parse_raw(metric.to_json()) == metric
    assert Hypothesis.parse_raw(hypothesis.to_json()) == hypothesis
    assert ExperimentObservation.parse_raw(observation.to_json()) == observation
    assert Decision.parse_raw(decision.to_json()) == decision


def test_artifact_preserves_metadata_and_normalizes_safe_relative_paths():
    artifact = Artifact(
        artifact_id="a-1",
        task_id="t-1",
        type="processed_dataset",
        path=r"outputs\processed\dataset.parquet",
        metadata={"columns": ["x", "y"], "preprocessing": {"scaled": True}},
        created_at=CREATED_AT,
    )

    assert artifact.path == "outputs/processed/dataset.parquet"
    assert artifact.metadata == {
        "columns": ["x", "y"],
        "preprocessing": {"scaled": True},
    }
    assert artifact.to_dict()["metadata"]["preprocessing"]["scaled"] is True


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Artifact(
            artifact_id="a-1",
            task_id="t-1",
            type="unknown",
            path="outputs/a.bin",
            created_at=CREATED_AT,
        ),
        lambda: Artifact(
            artifact_id="a-1",
            task_id="t-1",
            type="dataset",
            path="../outside.csv",
            created_at=CREATED_AT,
        ),
        lambda: MetricRecord(name="score", value=float("nan"), task_id="t-1"),
        lambda: ExperimentSpec(
            experiment_id="exp-1",
            dataset_id="dataset-1",
            feature_id="feature-1",
            model="baseline",
            code_version="abc123",
            unexpected=True,
        ),
    ],
)
def test_research_contract_validation_rejects_unsafe_or_malformed_values(factory):
    with pytest.raises((ValidationError, ValueError)):
        factory()


def test_experiment_and_evidence_linkage_is_explicit():
    dataset = DatasetManifest(
        dataset_id="dataset-1",
        name="generic-dataset",
        source="local-registry",
        version="1",
        resolution="native",
        projection="local-grid",
        time_range={"start": "t0", "end": "t1"},
        spatial_extent={"bbox": [0, 0, 1, 1]},
    )
    experiment = ExperimentSpec(
        experiment_id="experiment-1",
        dataset_id=dataset.dataset_id,
        feature_id="feature-1",
        model="model-1",
        code_version="commit-1",
    )
    observation = ExperimentObservation(
        observation_id="observation-1",
        experiment_id=experiment.experiment_id,
        action="evaluate model-1",
        result={"outcome": "baseline"},
    )
    decision = Decision(
        decision_id="decision-1",
        observation_id=observation.observation_id,
        decision="continue",
        rationale="The result supports the next comparison.",
    )

    assert experiment.dataset_id == dataset.dataset_id
    assert observation.experiment_id == experiment.experiment_id
    assert decision.observation_id == observation.observation_id
