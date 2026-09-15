from __future__ import annotations

import csv
import json
from pathlib import Path
import shutil
import sys

from bridge.config import AllowedCommand, BridgeConfig, BundleLimits, ProjectConfig
from bridge.experiments.artifacts import ArtifactContract
from bridge.experiments.metrics import MetricCollector
from bridge.orchestration.event_bus import TaskEventBus
from bridge.orchestration.supervisor import TaskSupervisor
from bridge.orchestration.worker import WorkerQueue
from bridge.store.task_store import TaskStore


FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_train.py"


def _config(tmp_path: Path) -> BridgeConfig:
    project_root = tmp_path / "project"
    project_root.mkdir()
    shutil.copy2(FIXTURE, project_root / "fake_train.py")
    config = BridgeConfig(
        control_repo="owner/bridge",
        trusted_github_logins={"trusted-user"},
        python_executable=Path(sys.executable),
        limits=BundleLimits(max_artifact_file_mb=1, max_bundle_mb=5, max_images=5),
        projects={
            "demo": ProjectConfig(
                capabilities=["experiment"],
                root=project_root,
                repo="owner/demo",
                allowed_commands={"train": AllowedCommand(argv=["python", "fake_train.py"])},
                artifact_dirs=["outputs", "logs", "predictions"],
            )
        },
    )
    config.config_path = tmp_path / "config.local.yaml"
    return config


def test_metric_collector_normalizes_json_csv_and_txt(tmp_path):
    json_path = tmp_path / "metrics.json"
    json_path.write_text(
        json.dumps(
            {
                "metrics": [
                    {
                        "metric_name": "accuracy",
                        "value": 0.9,
                        "step": 2,
                        "metadata": {"split": "val", "chain_of_thought": "must not persist"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    csv_path = tmp_path / "metrics.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("metric_name,value,step,metadata\nloss,0.2,2,{\"split\":\"val\"}\n")
    txt_path = tmp_path / "metrics.txt"
    txt_path.write_text("f1: 0.75 step=3\n# ignored\n", encoding="utf-8")

    records = MetricCollector().collect([json_path, csv_path, txt_path], root=tmp_path)

    assert records == [
        {"metric_name": "loss", "value": 0.2, "step": 2, "metadata": {"split": "val"}},
        {"metric_name": "accuracy", "value": 0.9, "step": 2, "metadata": {"split": "val"}},
        {"metric_name": "f1", "value": 0.75, "step": 3, "metadata": {}},
    ]


def test_artifact_contract_exposes_only_public_experiment_kinds():
    records = ArtifactContract.from_records(
        [
            {"source": "outputs/metrics.json", "path": "artifacts/outputs/metrics.json", "bytes": 12, "kind": "file"},
            {"source": "outputs/confusion_matrix.csv", "path": "artifacts/outputs/confusion_matrix.csv", "bytes": 18, "kind": "file"},
            {"source": "logs/train.log", "path": "artifacts/logs/train.log", "bytes": 8, "kind": "file"},
            {"source": "predictions/prediction.png", "path": "artifacts/predictions/prediction.png", "bytes": 8, "kind": "image"},
        ]
    )

    assert {item["kind"] for item in records} == {"metrics", "logs", "prediction_image", "confusion_matrix"}
    assert all("chain_of_thought" not in item for item in records)


def test_experiment_task_uses_shared_runtime_and_persists_outputs(tmp_path):
    config = _config(tmp_path)
    store = TaskStore(config.state_root)
    queue = WorkerQueue(max_workers=1)
    supervisor = TaskSupervisor(config, store=store, worker_queue=queue)

    try:
        response = supervisor.start_experiment_task("demo", "train", task_id="experiment-1")
        assert response == {"task_id": "experiment-1", "state": "QUEUED", "project": "demo"}
        assert store.get_task("experiment-1")["state"] == "QUEUED"

        result = supervisor.wait_for_task("experiment-1", timeout=10)
        snapshot = store.get_task("experiment-1")
        artifacts = supervisor.task_artifacts("experiment-1", limit=100)
        events = TaskEventBus(store, "experiment-1").events(after_seq=0, limit=100)

        assert result.success is True
        assert result.review_ready is True
        assert snapshot["state"] == "WAITING_REVIEW"
        assert snapshot["review_ready"] is True
        assert snapshot["metrics"] == [
            {"metric_name": "accuracy", "value": 0.875, "step": 1, "metadata": {"split": "validation"}},
            {"metric_name": "loss", "value": 0.125, "step": 1, "metadata": {"split": "validation"}},
        ]
        assert "fake training stdout" in store.task_path("experiment-1", "stdout.log").read_text(encoding="utf-8")
        assert "fake training stderr" in store.task_path("experiment-1", "stderr.log").read_text(encoding="utf-8")
        assert {item["kind"] for item in artifacts} == {"metrics", "logs", "prediction_image", "confusion_matrix"}
        assert any(event["type"] == "artifact_created" for event in events)
        assert [(event["data"].get("from"), event["data"].get("to")) for event in events if event["type"] == "state_changed" and event["data"].get("from") is not None] == [
            ("QUEUED", "PREPARING"),
            ("PREPARING", "RUNNING"),
            ("RUNNING", "WAITING_REVIEW"),
        ]
    finally:
        supervisor.close()
