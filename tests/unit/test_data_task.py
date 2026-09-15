from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from bridge.config import AllowedCommand, BridgeConfig, ProjectConfig
from bridge.commands import CommandExecutionError
from bridge.orchestration.event_bus import TaskEventBus
from bridge.orchestration.task_runner import TaskRunner
from bridge.research.artifact import Artifact
from bridge.research.data_task import (
    DataTaskHandler,
    DataTaskSpec,
    DataTaskValidationError,
    FakeDataAdapter,
)
from bridge.research.dataset import DatasetManifest
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
                allowed_commands={"process": AllowedCommand(argv=["fake-data-processor"])},
            )
        },
    )
    config.config_path = tmp_path / "config.local.yaml"
    return config


def _manifest() -> DatasetManifest:
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


def _spec(task_id: str = "data-task-1") -> DataTaskSpec:
    return DataTaskSpec(
        task_id=task_id,
        dataset_manifest=_manifest(),
        input_artifacts=[_artifact("input-1", "source-task-1", "dataset", "inputs/source.json")],
        processing_command="process",
        output_artifacts=[_artifact("output-1", task_id, "processed_dataset", "outputs/processed.json")],
        metadata={"purpose": "unit-test"},
    )


def _runtime_task(spec: DataTaskSpec, *, project: str = "demo") -> dict:
    return {
        "task_type": "data",
        "task_id": spec.task_id,
        "project": project,
        "data_task_spec": json.loads(spec.json()),
    }


def _runner(tmp_path: Path, adapter: FakeDataAdapter, spec: DataTaskSpec):
    config = _config(tmp_path)
    project_root = config.project("demo").root
    (project_root / "inputs").mkdir()
    (project_root / "inputs/source.json").write_text("{}", encoding="utf-8")
    store = TaskStore(config.state_root)
    store.create_task(
        spec.task_id,
        project="demo",
        task_type="data",
        data_task_spec=json.loads(spec.json()),
    )
    handler = DataTaskHandler(config, store=store, adapter=adapter)
    return store, TaskRunner(store=store, handlers={"data": handler}), config


def test_data_task_success_lifecycle_artifacts_metrics_and_observation(tmp_path):
    spec = _spec()
    adapter = FakeDataAdapter()
    store, runner, config = _runner(tmp_path, adapter, spec)

    result = runner.run(spec.task_id)

    assert result.success is True
    assert result.review_ready is True
    assert store.get_task(spec.task_id)["state"] == "WAITING_REVIEW"
    assert adapter.calls[0]["command_id"] == "process"
    assert (config.project("demo").root / "outputs/processed.json").is_file()
    assert {item["kind"] for item in result.artifacts} == {"processed_dataset"}
    assert {item["name"] for item in result.metadata["metrics"]} == {
        "coverage",
        "file_count",
        "processing_time",
    }
    assert result.metadata["observation"]["experiment_id"] == spec.task_id
    events = TaskEventBus(store, spec.task_id).events(after_seq=0, limit=100)
    assert any(event["type"] == "data_task_validating" for event in events)
    assert [(event["data"].get("from"), event["data"].get("to")) for event in events if event["type"] == "state_changed"] == [
        ("QUEUED", "PREPARING"),
        ("PREPARING", "RUNNING"),
        ("RUNNING", "WAITING_REVIEW"),
    ]


def test_data_task_command_failure_uses_failed_lifecycle(tmp_path):
    spec = _spec("data-task-command-failure")
    adapter = FakeDataAdapter(returncode=2)
    store, runner, _ = _runner(tmp_path, adapter, spec)

    result = runner.run(spec.task_id)

    assert result.success is False
    assert store.get_task(spec.task_id)["state"] == "FAILED"
    assert store.get_task(spec.task_id)["data_task_error"] == "command_failed"
    assert "fake data processing failed" in store.task_path(spec.task_id, "stdout.log").read_text(encoding="utf-8")


def test_data_task_missing_output_fails_validation(tmp_path):
    spec = _spec("data-task-missing-output")
    adapter = FakeDataAdapter(skip_outputs=True)
    store, runner, _ = _runner(tmp_path, adapter, spec)

    with pytest.raises(DataTaskValidationError, match="output artifacts"):
        runner.run(spec.task_id)

    assert store.get_task(spec.task_id)["state"] == "FAILED"
    assert store.get_task(spec.task_id)["data_task_stage"] == "VALIDATING"


def test_data_task_rejects_contract_errors_and_path_traversal_before_adapter(tmp_path):
    invalid_spec = {
        "task_id": "data-task-invalid",
        "dataset_manifest": json.loads(_manifest().json()),
        "input_artifacts": [
            {
                "artifact_id": "input-1",
                "task_id": "source-task-1",
                "type": "dataset",
                "path": "../outside.json",
                "metadata": {},
                "created_at": CREATED_AT.isoformat(),
            }
        ],
        "processing_command": "python -c print(1)",
        "output_artifacts": [],
    }
    with pytest.raises((ValidationError, ValueError)):
        DataTaskSpec.parse_obj(invalid_spec)

    spec = _spec("data-task-unregistered")
    task = _runtime_task(spec)
    task["data_task_spec"]["processing_command"] = "unregistered"
    adapter = FakeDataAdapter()
    store, runner, _ = _runner(tmp_path, adapter, spec)
    store.update_task(spec.task_id, data_task_spec=task["data_task_spec"])

    with pytest.raises(CommandExecutionError, match="not allowed or not registered"):
        runner.run(spec.task_id)

    assert adapter.calls == []
    assert store.get_task(spec.task_id)["state"] == "FAILED"


def test_data_task_rejects_missing_input_artifact(tmp_path):
    spec = _spec("data-task-missing-input")
    config = _config(tmp_path)
    store = TaskStore(config.state_root)
    store.create_task(
        spec.task_id,
        project="demo",
        task_type="data",
        data_task_spec=json.loads(spec.json()),
    )
    adapter = FakeDataAdapter()
    handler = DataTaskHandler(config, store=store, adapter=adapter)

    with pytest.raises(DataTaskValidationError, match="input artifact"):
        handler.execute(_runtime_task(spec))

    assert adapter.calls == []
