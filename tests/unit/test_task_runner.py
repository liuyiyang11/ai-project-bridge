from __future__ import annotations

import pytest

from bridge.orchestration.event_bus import TaskEventBus
from bridge.orchestration.task_runner import TaskRunner
from bridge.store.task_store import TaskStore


class _Handler:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def execute(self, task):
        self.calls.append(task["task_id"])
        if self.error:
            raise self.error
        return {"state": "WAITING_REVIEW"}


def _store(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    return store


def test_task_runner_advances_queued_task_to_waiting_review(tmp_path):
    store = _store(tmp_path)
    handler = _Handler()
    runner = TaskRunner(store=store, handlers={"code": handler})

    result = runner.run("task-1")

    assert result["state"] == "WAITING_REVIEW"
    assert store.get_task("task-1")["state"] == "WAITING_REVIEW"
    assert handler.calls == ["task-1"]


def test_task_runner_has_one_exception_exit_to_failed(tmp_path):
    store = _store(tmp_path)
    runner = TaskRunner(store=store, handlers={"code": _Handler(RuntimeError("codex unavailable"))})

    with pytest.raises(RuntimeError, match="codex unavailable"):
        runner.run("task-1")

    assert store.get_task("task-1")["state"] == "FAILED"
    events = TaskEventBus(store, "task-1").events(after_seq=0, limit=100)
    assert any(event["type"] == "error" for event in events)
    assert "traceback" in store.get_task("task-1")["last_error"]

