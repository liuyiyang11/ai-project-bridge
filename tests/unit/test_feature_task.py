from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from bridge.commands import CommandExecutionError
from bridge.config import AllowedCommand, BridgeConfig, ProjectConfig
from bridge.orchestration.event_bus import TaskEventBus
from bridge.orchestration.task_runner import TaskRunner
from bridge.research.artifact import Artifact
from bridge.research.dataset import DatasetManifest
from bridge.research.feature_task import (
    FakeFeatureAdapter,
    FeatureTaskHandler,
    FeatureTaskSpec,
    FeatureTaskValidationError,
)
from bridge.store.task_store import TaskStore


CREATED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _config(tmp_path: Path) -> BridgeConfig:
    project_root = tmp_path / "project"
    project_root.mkdir()
    config = BridgeConfig(
        control_repo="owner/bridge",
        trusted_github_logins={"tester"},
        projects={
            "demo": ProjectConfig(
                capabilities=["experiment"],
                root=project_root,
                repo="owner/demo",
                allowed_commands={"build-features": AllowedCommand(argv=["fake-feature-processor"])},
            )
        },
    )
    config.config_path = tmp_path / "config.local.yaml"
    return config


def _dataset() -> DatasetManifest:
    return DatasetManifest(
        dataset_id="dataset-1",
        name="generic-input",
        source="test-fixture",
        version="1",
        resolution="native",
        projection="test-grid",
        time_range={"start": "t0", "end": "t1"},
        spatial_extent={"bbox": [0, 0, 1, 1]},
    )


def _artifact(artifact_id: str, task_id: str, artifact_type: str, path: str) -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        task_id=task_id,
        type=artifact_type,
        path=path,
        metadata={"dataset_id": "dataset-1"},
        created_at=CREATED_AT,
    )


def _spec(task_id: str = "feature-task-1") -> FeatureTaskSpec:
    return FeatureTaskSpec(
        task_id=task_id,
        input_dataset=_dataset(),
        input_artifacts=[
            _artifact("input-dataset", "data-task-1", "dataset", "inputs/dataset.json"),
            _artifact("input-feature", "data-task-2", "feature", "inputs/context.json"),
        ],
        feature_schema={
            "feature_a": {"source": "input-dataset", "transform": "identity"},
            "feature_b": {"source": "input-feature", "transform": "normalized"},
        },
        processing_command="build-features",
        output_artifacts=[
            _artifact("feature-output", task_id, "feature", "outputs/features.json")
        ],
        metadata={"purpose": "unit-test"},
    )


def _runtime_task(spec: FeatureTaskSpec, *, project: str = "demo") -> dict:
    return {
        "task_type": "feature",
        "task_id": spec.task_id,
        "project": project,
        "feature_task_spec": json.loads(spec.json()),
    }


def _runner(tmp_path: Path, adapter: FakeFeatureAdapter, spec: FeatureTaskSpec):
    config = _config(tmp_path)
    project_root = config.project("demo").root
    (project_root / "inputs").mkdir()
    (project_root / "inputs/dataset.json").write_text("{}", encoding="utf-8")
    (project_root / "inputs/context.json").write_text("{}", encoding="utf-8")
    store = TaskStore(config.state_root)
    store.create_task(
        spec.task_id,
        project="demo",
        task_type="feature",
        feature_task_spec=json.loads(spec.json()),
    )
    handler = FeatureTaskHandler(config, store=store, adapter=adapter)
    return store, TaskRunner(store=store, handlers={"feature": handler}), config


def test_feature_task_contract_validation_is_domain_neutral():
    spec = _spec()

    restored = FeatureTaskSpec.parse_raw(spec.json())

    assert restored == spec
    assert set(spec.feature_schema) == {"feature_a", "feature_b"}
    assert spec.input_dataset.dataset_id == "dataset-1"
    with pytest.raises((ValidationError, ValueError)):
        FeatureTaskSpec.parse_obj(
            {
                **json.loads(spec.json()),
                "processing_command": "python -c print(1)",
            }
        )


def test_feature_task_success_lifecycle_generates_artifacts_metrics_and_observation(tmp_path):
    spec = _spec()
    adapter = FakeFeatureAdapter()
    store, runner, config = _runner(tmp_path, adapter, spec)

    result = runner.run(spec.task_id)

    assert result.success is True
    assert result.review_ready is True
    assert store.get_task(spec.task_id)["state"] == "WAITING_REVIEW"
    assert adapter.calls[0]["command_id"] == "build-features"
    assert (config.project("demo").root / "outputs/features.json").is_file()
    assert result.artifacts[0]["kind"] == "feature"
    assert {item["name"] for item in result.metadata["metrics"]} == {
        "coverage",
        "file_count",
        "feature_count",
        "processing_time",
    }
    assert result.metadata["artifacts"][0]["type"] == "feature"
    assert result.metadata["observation"]["experiment_id"] == spec.task_id
    events = TaskEventBus(store, spec.task_id).events(after_seq=0, limit=100)
    assert any(event["type"] == "feature_task_validating" for event in events)
    assert store.get_task(spec.task_id)["feature_task_stage"] == "VALIDATED"


def test_feature_task_command_failure_uses_failed_lifecycle(tmp_path):
    spec = _spec("feature-task-command-failure")
    adapter = FakeFeatureAdapter(returncode=3)
    store, runner, _ = _runner(tmp_path, adapter, spec)

    result = runner.run(spec.task_id)

    assert result.success is False
    assert store.get_task(spec.task_id)["state"] == "FAILED"
    assert store.get_task(spec.task_id)["feature_task_error"] == "command_failed"
    assert "fake feature processing failed" in store.task_path(spec.task_id, "stdout.log").read_text(encoding="utf-8")


def test_feature_task_missing_output_fails_validation(tmp_path):
    spec = _spec("feature-task-missing-output")
    adapter = FakeFeatureAdapter(skip_outputs=True)
    store, runner, _ = _runner(tmp_path, adapter, spec)

    with pytest.raises(FeatureTaskValidationError, match="output artifacts"):
        runner.run(spec.task_id)

    assert store.get_task(spec.task_id)["state"] == "FAILED"
    assert store.get_task(spec.task_id)["feature_task_stage"] == "VALIDATING"


def test_feature_task_rejects_illegal_path_and_unregistered_command(tmp_path):
    spec = _spec("feature-task-security")
    raw = json.loads(spec.json())
    raw["input_artifacts"][0]["path"] = "../outside.json"
    with pytest.raises((ValidationError, ValueError)):
        FeatureTaskSpec.parse_obj(raw)

    adapter = FakeFeatureAdapter()
    store, runner, _ = _runner(tmp_path, adapter, spec)
    stored = json.loads(spec.json())
    stored["processing_command"] = "not-registered"
    store.update_task(spec.task_id, feature_task_spec=stored)

    with pytest.raises(CommandExecutionError, match="not allowed or not registered"):
        runner.run(spec.task_id)

    assert adapter.calls == []
    assert store.get_task(spec.task_id)["state"] == "FAILED"


def test_feature_task_rejects_missing_input_artifact_before_adapter(tmp_path):
    spec = _spec("feature-task-missing-input")
    config = _config(tmp_path)
    store = TaskStore(config.state_root)
    store.create_task(
        spec.task_id,
        project="demo",
        task_type="feature",
        feature_task_spec=json.loads(spec.json()),
    )
    adapter = FakeFeatureAdapter()
    handler = FeatureTaskHandler(config, store=store, adapter=adapter)

    with pytest.raises(FeatureTaskValidationError, match="input artifact"):
        handler.execute(_runtime_task(spec))

    assert adapter.calls == []
