from __future__ import annotations

import json
import threading

import pytest

from bridge.orchestration.event_bus import TaskEventBus
from bridge.store.task_store import TaskStore, utc_now


class _RecordingTransitionStore(TaskStore):
    def __init__(self, root):
        super().__init__(root)
        self.transition_calls = []

    def _apply_transition(self, task_id, from_state, to_state, reason, *, updates=None):
        self.transition_calls.append((task_id, from_state, to_state, reason, updates))
        return super()._apply_transition(task_id, from_state, to_state, reason, updates=updates)


class _BlockingSnapshotStore(TaskStore):
    """Pause one metadata write after it has read the durable snapshot."""

    def __init__(self, root):
        super().__init__(root)
        self.metadata_write_started = threading.Event()
        self.release_metadata_write = threading.Event()
        self._block_metadata_write = True

    def _write_compat_files(self, task_id, state, *, task_yaml):
        if threading.current_thread().name == "metadata-writer" and self._block_metadata_write:
            self._block_metadata_write = False
            self.metadata_write_started.set()
            assert self.release_metadata_write.wait(timeout=2)
        return super()._write_compat_files(task_id, state, task_yaml=task_yaml)


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


def test_event_bus_transition_keeps_event_cursor_snapshot_and_updates_consistent(tmp_path):
    root = tmp_path / ".bridge"
    store = _RecordingTransitionStore(root)
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")

    event = TaskEventBus(store, "task-1").transition(
        "QUEUED",
        "PREPARING",
        "worker accepted task",
        updates={"recovery_checked": True},
    )

    snapshot = TaskStore(root).get_task("task-1")
    assert snapshot["state"] == "PREPARING"
    assert snapshot["last_event_seq"] == event["seq"]
    assert snapshot["recovery_checked"] is True
    assert TaskStore(root).list_events("task-1", after_seq=0, limit=10) == [event]
    assert store.transition_calls == [
        ("task-1", "QUEUED", "PREPARING", "worker accepted task", {"recovery_checked": True, "event_seq": event["seq"], "last_event_seq": event["seq"]})
    ]


def test_event_bus_rejects_invalid_state_transition(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")

    with pytest.raises(Exception, match="invalid task transition"):
        TaskEventBus(store, "task-1").transition("QUEUED", "RUNNING", "skipped preparation")


def test_event_bus_rejects_raw_state_changed_events(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")

    with pytest.raises(ValueError, match="transition"):
        TaskEventBus(store, "task-1").emit("state_changed", {"from": "QUEUED", "to": "RUNNING"})

    assert store.get_task("task-1")["state"] == "QUEUED"
    assert store.all_events("task-1") == []


def test_event_bus_rejects_reserved_transition_updates_before_writing_event(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")

    with pytest.raises(ValueError, match="reserved"):
        TaskEventBus(store, "task-1").transition(
            "QUEUED",
            "PREPARING",
            "worker accepted task",
            updates={"state": "RUNNING"},
        )

    assert store.get_task("task-1")["state"] == "QUEUED"
    assert store.get_task("task-1")["last_event_seq"] == 0
    assert store.all_events("task-1") == []

    with pytest.raises(ValueError, match="reserved"):
        TaskEventBus(store, "task-1").transition(
            "QUEUED",
            "PREPARING",
            "worker accepted task",
            updates={"to": "RUNNING"},
        )

    assert store.get_task("task-1")["state"] == "QUEUED"
    assert store.all_events("task-1") == []


def test_event_bus_transition_cannot_be_overwritten_by_concurrent_metadata_write(tmp_path):
    store = _BlockingSnapshotStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    bus = TaskEventBus(store, "task-1")
    bus.transition("QUEUED", "PREPARING", "worker accepted task")
    bus.transition("PREPARING", "RUNNING", "task execution started")

    metadata_writer = threading.Thread(
        target=lambda: store.update_task("task-1", thread_id="thread-1"),
        name="metadata-writer",
    )
    metadata_writer.start()
    assert store.metadata_write_started.wait(timeout=1)

    transition_finished = threading.Event()

    def transition():
        bus.transition("RUNNING", "WAITING_REVIEW", "review ready", updates={"review_ready": True})
        transition_finished.set()

    transition_writer = threading.Thread(target=transition, name="transition-writer")
    transition_writer.start()
    assert not transition_finished.wait(timeout=0.1)
    store.release_metadata_write.set()
    metadata_writer.join(timeout=2)
    transition_writer.join(timeout=2)

    assert not metadata_writer.is_alive()
    assert not transition_writer.is_alive()
    snapshot = store.get_task("task-1")
    assert snapshot["state"] == "WAITING_REVIEW"
    assert snapshot["thread_id"] == "thread-1"
    assert snapshot["last_event_seq"] == max(event["seq"] for event in store.all_events("task-1"))


def test_state_machine_exposes_unknown_recovery_state():
    from bridge.codex.session import SessionState
    from bridge.orchestration.state_machine import TaskStateMachine

    assert SessionState.UNKNOWN.value == "UNKNOWN"
    assert TaskStateMachine.can_transition("RUNNING", "UNKNOWN")
    assert TaskStateMachine.can_transition("UNKNOWN", "RUNNING")


def test_event_bus_reconciles_journaled_transition_and_keeps_sequence_monotonic(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    store.append_event(
        "task-1",
        {
            "seq": 1,
            "task_id": "task-1",
            "type": "state_changed",
            "time": "2026-01-01T00:00:00+00:00",
            "data": {"from": "QUEUED", "to": "PREPARING", "reason": "journaled"},
        },
    )
    bus = TaskEventBus(store, "task-1")

    bus.reconcile()
    event = bus.emit("worker_started", {})

    assert store.get_task("task-1")["state"] == "PREPARING"
    assert event["seq"] == 2
    assert store.get_task("task-1")["last_event_seq"] == 2
