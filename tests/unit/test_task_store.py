from __future__ import annotations

import json

import pytest

from bridge.store.task_store import TaskStore, utc_now


def test_create_task_writes_queued_task_json_and_legacy_files(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    task = store.create_task(
        "task-1",
        project="demo",
        task_type="code",
        instruction="make a change",
        acceptance=["tests pass"],
    )

    assert task["task_id"] == "task-1"
    assert task["state"] == "QUEUED"
    task_dir = tmp_path / ".bridge" / "tasks" / "task-1"
    assert json.loads((task_dir / "task.json").read_text(encoding="utf-8"))["events_file"] == "events.jsonl"
    assert (task_dir / "state.json").is_file()
    assert (task_dir / "task.yaml").is_file()


def test_update_and_transition_task_keep_snapshot_and_legacy_projection_in_sync(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")

    store.update_task("task-1", thread_id="thread-1", stage="starting turn")
    store.transition_task("task-1", "QUEUED", "PREPARING", "worker accepted task")

    assert store.get_task("task-1")["thread_id"] == "thread-1"
    assert store.get_task("task-1")["state"] == "PREPARING"
    assert store.load_state("task-1")["state"] == "PREPARING"


def test_events_append_with_sequence_and_reload_from_new_store_instance(tmp_path):
    root = tmp_path / ".bridge"
    store = TaskStore(root)
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    store.append_event("task-1", {"seq": 1, "task_id": "task-1", "type": "task_created", "time": utc_now(), "data": {}})
    store.save_artifacts("task-1", [{"path": "hello.py", "size": 20, "kind": "source", "hash": "abc"}])

    reloaded = TaskStore(root)
    assert reloaded.list_events("task-1", after_seq=0, limit=10)[0]["seq"] == 1
    assert reloaded.get_task("task-1")["artifact_manifest"] == "result.json"
    assert reloaded.task_path("task-1", "result.json").is_file()


def test_task_store_routes_lifecycle_status_changes_through_transition(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")

    with pytest.raises(ValueError, match="lifecycle status"):
        store.update_task("task-1", status="RUNNING")


def test_task_store_create_always_starts_queued(tmp_path):
    store = TaskStore(tmp_path / ".bridge")

    with pytest.raises(ValueError, match="lifecycle fields"):
        store.create_task("task-1", project="demo", instruction="wait", state="RUNNING")
