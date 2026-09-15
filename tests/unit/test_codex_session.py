from __future__ import annotations

from types import SimpleNamespace

import pytest

from bridge.codex.fake_app_server import FakeCodexAppServer
from bridge.codex.session import CodexSessionManager, SessionState, SessionTransitionError
from bridge.orchestration.event_bus import TaskEventBus
from bridge.store.task_store import TaskStore


def _config():
    return SimpleNamespace(
        codex_binary="codex",
        codex=SimpleNamespace(
            routing_policy="inherit",
            request_timeout_seconds=2,
            initialize_timeout_seconds=2,
            turn_timeout_seconds=2,
        ),
    )


def _attach(fake, kwargs):
    fake.notification_handler = kwargs.get("notification_handler")
    fake.process_error_handler = kwargs.get("process_error_handler")
    return fake


class _DeferredFake(FakeCodexAppServer):
    """Expose notifications only when the session explicitly pumps them."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._pending_notifications = []

    def emit(self, method, params=None):
        event = {"method": method, "params": params or {}}
        self.notifications.append(event)
        self._pending_notifications.append(event)

    def drain_notifications(self, *, limit=100):
        events = self._pending_notifications[:limit]
        del self._pending_notifications[:limit]
        for event in events:
            if self.notification_handler:
                self.notification_handler(event)
        return events


class _QuietFake(FakeCodexAppServer):
    def emit(self, method, params=None):
        self.notifications.append({"method": method, "params": params or {}})


def test_session_start_persists_thread_and_turn_ids(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    fake = FakeCodexAppServer(auto_complete=True)
    manager = CodexSessionManager(_config(), store=store, client_factory=lambda **kwargs: _attach(fake, kwargs))

    record = manager.start_task("task-1", "demo", tmp_path, "wait")

    assert record.state == SessionState.WAITING_REVIEW
    assert store.get_task("task-1")["thread_id"] == "fake-thread"
    assert store.get_task("task-1")["turn_id"] == "fake-turn-1"


def test_runtime_managed_session_keeps_durable_lifecycle_with_the_runner(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    bus = TaskEventBus(store, "task-1")
    bus.transition("QUEUED", "PREPARING", "worker accepted task")
    bus.transition("PREPARING", "RUNNING", "task execution started")
    fake = FakeCodexAppServer(auto_complete=True)
    manager = CodexSessionManager(_config(), store=store, client_factory=lambda **kwargs: _attach(fake, kwargs))

    record = manager.start_task("task-1", "demo", tmp_path, "wait", manage_task_lifecycle=False)

    assert record.state == SessionState.WAITING_REVIEW
    snapshot = store.get_task("task-1")
    assert snapshot["state"] == "RUNNING"
    assert snapshot["review_ready"] is False
    assert snapshot["thread_id"] == "fake-thread"
    assert snapshot["turn_id"] == "fake-turn-1"


def test_runtime_managed_session_preserves_the_existing_event_cursor(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    bus = TaskEventBus(store, "task-1")
    bus.transition("QUEUED", "PREPARING", "worker accepted task")
    bus.transition("PREPARING", "RUNNING", "task execution started")
    fake = _QuietFake(auto_complete=False)
    manager = CodexSessionManager(_config(), store=store, client_factory=lambda **kwargs: _attach(fake, kwargs))

    manager.start_task("task-1", "demo", tmp_path, "wait", manage_task_lifecycle=False)

    snapshot = store.get_task("task-1")
    assert snapshot["state"] == "RUNNING"
    assert snapshot["event_seq"] == snapshot["last_event_seq"] == 2


def test_wait_for_completion_pumps_deferred_app_server_notifications(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    fake = _DeferredFake(auto_complete=True)
    manager = CodexSessionManager(_config(), store=store, client_factory=lambda **kwargs: _attach(fake, kwargs))

    manager.start_task("task-1", "demo", tmp_path, "wait")
    result = manager.wait_for_completion("task-1", timeout=1)

    assert result.exit_code == 0
    assert manager.status("task-1")["state"] == SessionState.WAITING_REVIEW.value
    assert fake._pending_notifications == []


def test_session_resume_uses_persisted_thread_id(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="continue")
    bus = TaskEventBus(store, "task-1")
    bus.transition("QUEUED", "PREPARING", "test preparation")
    bus.transition("PREPARING", "RUNNING", "test execution")
    bus.transition("RUNNING", "WAITING_REVIEW", "test review")
    store.update_task("task-1", thread_id="saved-thread")
    fake = FakeCodexAppServer(auto_complete=False)
    manager = CodexSessionManager(_config(), store=store, client_factory=lambda **kwargs: _attach(fake, kwargs))

    manager.resume_task("task-1", "demo", tmp_path, "saved-thread", "continue")

    assert any(name == "thread/resume" and args["threadId"] == "saved-thread" for name, args in fake.requests)


def test_session_steer_and_interrupt_use_active_turn(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    fake = FakeCodexAppServer(auto_complete=False)
    manager = CodexSessionManager(_config(), store=store, client_factory=lambda **kwargs: _attach(fake, kwargs))
    manager.start_task("task-1", "demo", tmp_path, "wait")

    manager.steer_task("task-1", "focus")
    manager.interrupt_task("task-1")

    assert any(name == "turn/steer" for name, _ in fake.requests)
    assert any(name == "turn/interrupt" for name, _ in fake.requests)
    assert manager.status("task-1")["state"] == SessionState.INTERRUPTED.value


def test_restart_rejects_saved_worktree_outside_bridge_root(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait", thread_id="saved-thread")
    outside = tmp_path / "outside"
    outside.mkdir()
    store.update_task("task-1", worktree_path=str(outside))
    config = _config()
    config.state_root = tmp_path / ".bridge"
    config.project = lambda name: SimpleNamespace(capabilities=["code"])
    manager = CodexSessionManager(config, store=store, client_factory=lambda **kwargs: FakeCodexAppServer())

    with pytest.raises(SessionTransitionError, match="outside the Bridge worktree root"):
        manager.restart_task("task-1", "continue")
