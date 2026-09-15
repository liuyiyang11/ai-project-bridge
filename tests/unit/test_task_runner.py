from __future__ import annotations

import pytest

from bridge.orchestration.event_bus import TaskEventBus
from bridge.orchestration.models import TaskResult
from bridge.orchestration.task_runner import TaskRunner
from bridge.store.task_store import TaskStore


class _Handler:
    def __init__(self, store, *, result=None, error=None):
        self.store = store
        self.result = result or TaskResult(success=True, review_ready=True, message="review ready")
        self.error = error
        self.calls = []
        self.states = []

    def execute(self, task):
        self.calls.append(task["task_id"])
        self.states.append(self.store.get_task(task["task_id"])["state"])
        if self.error:
            raise self.error
        return self.result


def _store(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    return store


def test_task_runner_advances_queued_task_to_waiting_review(tmp_path):
    store = _store(tmp_path)
    handler = _Handler(store)
    runner = TaskRunner(store=store, handlers={"code": handler})

    result = runner.run("task-1")

    assert result == TaskResult(success=True, review_ready=True, message="review ready")
    assert store.get_task("task-1")["state"] == "WAITING_REVIEW"
    assert handler.calls == ["task-1"]
    assert handler.states == ["RUNNING"]
    states = [
        (event["data"].get("from"), event["data"].get("to"))
        for event in TaskEventBus(store, "task-1").events(after_seq=0, limit=100)
        if event["type"] == "state_changed"
    ]
    assert states[-3:] == [("QUEUED", "PREPARING"), ("PREPARING", "RUNNING"), ("RUNNING", "WAITING_REVIEW")]


def test_task_runner_converts_a_failed_result_to_failed_state(tmp_path):
    store = _store(tmp_path)
    failure = TaskResult(success=False, message="Codex turn failed")
    handler = _Handler(store, result=failure)
    runner = TaskRunner(store=store, handlers={"code": handler})

    result = runner.run("task-1")

    assert result is failure
    assert store.get_task("task-1")["state"] == "FAILED"
    states = [
        (event["data"].get("from"), event["data"].get("to"))
        for event in TaskEventBus(store, "task-1").events(after_seq=0, limit=100)
        if event["type"] == "state_changed"
    ]
    assert states[-3:] == [("QUEUED", "PREPARING"), ("PREPARING", "RUNNING"), ("RUNNING", "FAILED")]
    assert any(event["type"] == "error" and event["data"]["message"] == "Codex turn failed" for event in TaskEventBus(store, "task-1").events(after_seq=0, limit=100))
    assert "NoneType: None" not in store.get_task("task-1")["last_error"]


def test_task_runner_leaves_success_without_review_ready_running(tmp_path):
    store = _store(tmp_path)
    result = TaskResult(success=True, review_ready=False, message="turn still active")
    runner = TaskRunner(store=store, handlers={"code": _Handler(store, result=result)})

    assert runner.run("task-1") is result
    assert store.get_task("task-1")["state"] == "RUNNING"
    states = [
        (event["data"].get("from"), event["data"].get("to"))
        for event in TaskEventBus(store, "task-1").events(after_seq=0, limit=100)
        if event["type"] == "state_changed"
    ]
    assert states[-2:] == [("QUEUED", "PREPARING"), ("PREPARING", "RUNNING")]


def test_task_runner_rejects_lifecycle_metadata_from_a_handler(tmp_path):
    store = _store(tmp_path)
    result = TaskResult(success=True, metadata={"review_ready": True, "last_event_seq": 0})
    runner = TaskRunner(store=store, handlers={"code": _Handler(store, result=result)})

    with pytest.raises(ValueError, match="lifecycle"):
        runner.run("task-1")

    snapshot = store.get_task("task-1")
    assert snapshot["state"] == "FAILED"
    assert snapshot["review_ready"] is False
    assert snapshot["last_event_seq"] == max(event["seq"] for event in TaskEventBus(store, "task-1").events(after_seq=0, limit=100))


def test_task_runner_has_one_exception_exit_to_failed(tmp_path):
    store = _store(tmp_path)
    runner = TaskRunner(store=store, handlers={"code": _Handler(store, error=RuntimeError("codex unavailable"))})

    with pytest.raises(RuntimeError, match="codex unavailable"):
        runner.run("task-1")

    assert store.get_task("task-1")["state"] == "FAILED"
    events = TaskEventBus(store, "task-1").events(after_seq=0, limit=100)
    assert any(event["type"] == "error" for event in events)
    assert "traceback" in store.get_task("task-1")["last_error"]
