from __future__ import annotations

import json

import pytest

from bridge.orchestration.event_bus import TaskEventBus
from bridge.store.task_store import TaskStore, utc_now


def test_event_bus_assigns_monotonic_sequences_and_persists_snapshot_cursor(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    bus = TaskEventBus(store, "task-1")

    first = bus.emit("task_created", {"project": "demo"})
    second = bus.emit("agent_message", {"message": "safe summary", "reasoning": "remove this"})

    assert [first["seq"], second["seq"]] == [1, 2]
    assert first["task_id"] == "task-1" and "time" in first
    assert "reasoning" not in json.dumps(second)
    assert store.get_task("task-1")["last_event_seq"] == 2
    assert [item["seq"] for item in store.list_events("task-1", after_seq=1, limit=10)] == [2]


def test_event_bus_transition_validates_and_persists_state_changed_event(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    bus = TaskEventBus(store, "task-1")

    bus.transition("QUEUED", "PREPARING", "worker accepted task")

    assert store.get_task("task-1")["state"] == "PREPARING"
    event = store.list_events("task-1", after_seq=0, limit=10)[0]
    assert event["type"] == "state_changed"
    assert event["data"]["from"] == "QUEUED"


def test_event_bus_rejects_invalid_state_transition(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")

    with pytest.raises(Exception, match="invalid task transition"):
        TaskEventBus(store, "task-1").transition("QUEUED", "RUNNING", "skipped preparation")


def test_state_machine_exposes_unknown_recovery_state():
    from bridge.codex.session import SessionState
    from bridge.orchestration.state_machine import TaskStateMachine

    assert SessionState.UNKNOWN.value == "UNKNOWN"
    assert TaskStateMachine.can_transition("RUNNING", "UNKNOWN")
    assert TaskStateMachine.can_transition("UNKNOWN", "RUNNING")
