from __future__ import annotations

from threading import Event, Thread, current_thread
from typing import Any, Optional

import pytest

from bridge.codex.session import CodexSessionManager
from bridge.config import AllowedCommand, BridgeConfig, ProjectConfig
from bridge.orchestration.event_bus import TaskEventBus
from bridge.orchestration.models import TaskResult
from bridge.orchestration.supervisor import TaskSupervisor
from bridge.orchestration.task_runner import TaskRunner
from bridge.orchestration.worker import WorkerQueue
from bridge.store.task_store import TaskStore


class _BlockingHandler:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def execute(self, task: dict[str, Any]) -> TaskResult:
        self.started.set()
        self.release.wait(timeout=2)
        return TaskResult(success=True, review_ready=True, message="blocking task completed")


class _RecordingQueue:
    def __init__(self) -> None:
        self.submitted_task_ids: list[str] = []

    def submit(self, task_id: str, callable_: Any, *args: Any, **kwargs: Any) -> None:
        self.submitted_task_ids.append(task_id)

    def shutdown(self, **kwargs: Any) -> None:
        return None


def _config(tmp_path) -> BridgeConfig:
    config = BridgeConfig(
        control_repo="owner/bridge",
        trusted_github_logins={"trusted-user"},
        projects={
            "demo": ProjectConfig(
                capabilities=["code", "experiment"],
                root=tmp_path,
                repo="owner/demo",
                allowed_commands={"noop": AllowedCommand(argv=["python", "-c", "pass"])},
            )
        },
    )
    config.config_path = tmp_path / "config.local.yaml"
    return config


def _supervisor(
    tmp_path,
    store: TaskStore,
    queue: Any,
    *,
    handler: Optional[_BlockingHandler] = None,
) -> TaskSupervisor:
    config = _config(tmp_path)
    manager = CodexSessionManager(config, store=store)
    if handler is None:
        runner = TaskRunner(store=store, handlers={})
    else:
        runner = TaskRunner(
            store=store,
            handlers={"code": handler, "experiment": handler},
        )
    return TaskSupervisor(
        config,
        store=store,
        session_manager=manager,
        worker_queue=queue,
        task_runner=runner,
    )


def _assert_recovery_does_not_submit(tmp_path, store: TaskStore) -> None:
    recovery_queue = _RecordingQueue()
    recovered = _supervisor(tmp_path, store, recovery_queue)
    try:
        recovered.recover_tasks()
        assert recovery_queue.submitted_task_ids == []
    finally:
        recovered.close()


def test_supervisor_close_persists_cancelled_for_queued_code_task(tmp_path) -> None:
    store = TaskStore(tmp_path / ".bridge")
    queue = WorkerQueue(max_workers=1)
    handler = _BlockingHandler()
    supervisor = _supervisor(tmp_path, store, queue, handler=handler)
    running_future = None

    try:
        supervisor.start_code_task("demo", "occupy worker", task_id="running")
        running_future = queue.future("running")
        assert running_future is not None
        assert handler.started.wait(timeout=2)

        supervisor.start_code_task("demo", "must be cancelled", task_id="queued")
        queued_future = queue.future("queued")
        assert queued_future is not None
        assert store.get_task("queued")["state"] == "QUEUED"
        assert queued_future.running() is False
        assert queued_future.done() is False

        supervisor.close()

        assert queued_future.cancelled() is True
        assert store.get_task("queued")["state"] == "CANCELLED"
        assert store.get_task("queued")["review_ready"] is False
        handler.release.set()
        running_future.result(timeout=2)
        _assert_recovery_does_not_submit(tmp_path, store)
    finally:
        handler.release.set()
        if running_future is not None:
            running_future.result(timeout=2)
        queue.shutdown(wait=True, cancel_futures=True)


def test_supervisor_close_persists_cancelled_for_queued_experiment_task(tmp_path) -> None:
    store = TaskStore(tmp_path / ".bridge")
    queue = WorkerQueue(max_workers=1)
    handler = _BlockingHandler()
    supervisor = _supervisor(tmp_path, store, queue, handler=handler)
    running_future = None

    try:
        supervisor.start_code_task("demo", "occupy worker", task_id="running")
        running_future = queue.future("running")
        assert running_future is not None
        assert handler.started.wait(timeout=2)

        supervisor.start_experiment_task("demo", "noop", task_id="queued-experiment")
        queued_future = queue.future("queued-experiment")
        assert queued_future is not None
        assert store.get_task("queued-experiment")["state"] == "QUEUED"
        assert queued_future.running() is False
        assert queued_future.done() is False

        supervisor.close()

        assert queued_future.cancelled() is True
        assert store.get_task("queued-experiment")["state"] == "CANCELLED"
        assert store.get_task("queued-experiment")["review_ready"] is False
    finally:
        handler.release.set()
        if running_future is not None:
            running_future.result(timeout=2)
        queue.shutdown(wait=True, cancel_futures=True)


def test_recover_tasks_does_not_requeue_cancelled_task(tmp_path) -> None:
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("cancelled", project="demo", task_type="code", instruction="already cancelled")
    TaskEventBus(store, "cancelled").transition("QUEUED", "CANCELLED", "bridge shutdown")

    queue = _RecordingQueue()
    supervisor = _supervisor(tmp_path, store, queue)
    try:
        supervisor.recover_tasks()
        assert queue.submitted_task_ids == []
        assert store.get_task("cancelled")["state"] == "CANCELLED"
    finally:
        supervisor.close()


def test_shutdown_does_not_cancel_future_that_already_started(tmp_path) -> None:
    store = TaskStore(tmp_path / ".bridge")
    queue = WorkerQueue(max_workers=1)
    supervisor = _supervisor(tmp_path, store, queue)
    started = Event()
    release = Event()
    future = None

    def block_without_advancing_durable_state() -> None:
        started.set()
        release.wait(timeout=2)

    try:
        store.create_task("race", project="demo", task_type="code", instruction="started before lifecycle transition")
        future = queue.submit("race", block_without_advancing_durable_state)
        assert started.wait(timeout=2)
        assert future.running() is True
        assert queue.cancel("race") is False
        assert store.get_task("race")["state"] == "QUEUED"

        supervisor.close()

        assert future.cancelled() is False
        assert store.get_task("race")["state"] == "QUEUED"
    finally:
        release.set()
        if future is not None:
            future.result(timeout=2)
        queue.shutdown(wait=True, cancel_futures=True)


def test_new_worker_submission_is_rejected_after_shutdown_starts(tmp_path) -> None:
    store = TaskStore(tmp_path / ".bridge")
    queue = WorkerQueue(max_workers=1)
    supervisor = _supervisor(tmp_path, store, queue)

    try:
        supervisor.close()

        with pytest.raises(RuntimeError, match="supervisor is shutting down"):
            supervisor.start_code_task("demo", "must not be submitted", task_id="late")

        assert store.exists("late") is False
    finally:
        queue.shutdown(wait=True, cancel_futures=True)


def test_recovery_cannot_submit_after_shutdown_boundary(tmp_path) -> None:
    store = TaskStore(tmp_path / ".bridge")
    queue = _RecordingQueue()
    supervisor = _supervisor(tmp_path, store, queue)
    store.create_task("recover-race", project="demo", task_type="code", instruction="recover later")
    recovery_read = Event()
    release_recovery = Event()
    original_get_task = store.get_task
    recovery_thread = None

    def gated_get_task(task_id: str) -> dict[str, Any]:
        state = original_get_task(task_id)
        if task_id == "recover-race" and current_thread() is recovery_thread:
            recovery_read.set()
            assert release_recovery.wait(timeout=2)
        return state

    store.get_task = gated_get_task  # type: ignore[method-assign]
    recovery_thread = Thread(target=supervisor.recover_tasks, name="recovery-race")
    try:
        recovery_thread.start()
        assert recovery_read.wait(timeout=2)

        supervisor.close()
        release_recovery.set()
        recovery_thread.join(timeout=2)

        assert recovery_thread.is_alive() is False
        assert queue.submitted_task_ids == []
        assert store.get_task("recover-race")["state"] == "QUEUED"
        assert not any(
            event["type"] == "state_changed"
            and event["data"].get("to") in {"CANCELLED", "FAILED"}
            for event in store.all_events("recover-race")
        )
    finally:
        release_recovery.set()
        if recovery_thread is not None:
            recovery_thread.join(timeout=2)
        supervisor.close()
