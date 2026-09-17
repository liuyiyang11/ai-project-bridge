from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
from typing import Any, Iterator, Optional

import pytest

from bridge.codex.fake_app_server import FakeAppServer
from bridge.codex.session import CodexSessionManager, SessionState
from bridge.config import BridgeConfig, ProjectConfig
from bridge.executors.code import RuntimeCodeExecutor
from bridge.orchestration.supervisor import TaskSupervisor
from bridge.orchestration.task_runner import CodeTaskHandler, TaskRunner
from bridge.orchestration.worker import WorkerQueue
from bridge.store.task_store import TaskStore


class _ShutdownFakeAppServer(FakeAppServer):
    """In-process app-server that stays inside ``wait_for_completion``."""

    def __init__(self) -> None:
        super().__init__(
            models=[
                {
                    "id": "shutdown-test-model",
                    "model": "shutdown-test-model",
                    "displayName": "Shutdown test model",
                    "description": "Minimal in-process test model",
                    "isDefault": True,
                    "hidden": False,
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "medium", "description": "medium"},
                    ],
                }
            ],
            thread_id="shutdown-test-thread",
            turn_id="shutdown-test-turn",
            auto_complete=False,
        )
        self.turn_started_event = Event()
        self.wait_entered_event = Event()
        self.close_calls = 0
        self.close_called = False
        self.raise_on_close = False

    def turn_start(self, thread_id: str, instruction: str, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(("turn/start", {"threadId": thread_id, "instruction": instruction, **kwargs}))
        self.emit(
            "turn/started",
            {"threadId": thread_id, "turn": {"id": self.turn_id, "status": "inProgress"}},
        )
        self.turn_started_event.set()
        return {"turn": {"id": self.turn_id, "status": "inProgress"}}

    def drain_notifications(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self.wait_entered_event.set()
        return []

    def close(self) -> None:
        self.close_calls += 1
        self.close_called = True
        super().close()
        if self.raise_on_close:
            raise RuntimeError("fake client close failed")


@dataclass
class _ActiveRuntime:
    tempdir: TemporaryDirectory
    store: TaskStore
    manager: CodexSessionManager
    queue: WorkerQueue
    supervisor: TaskSupervisor
    fake: _ShutdownFakeAppServer
    task_id: str
    worktree: Path
    future: Any = None
    record: Any = None
    remaining_bridge_workers: tuple[str, ...] = ()


def _attach_fake(fake: _ShutdownFakeAppServer, kwargs: dict[str, Any]) -> _ShutdownFakeAppServer:
    fake.notification_handler = kwargs.get("notification_handler")
    fake.process_error_handler = kwargs.get("process_error_handler")
    return fake


@contextmanager
def _active_runtime() -> Iterator[_ActiveRuntime]:
    """Build one real worker/session chain and always release its worker."""

    tempdir = TemporaryDirectory()
    runtime: Optional[_ActiveRuntime] = None
    primary_error: Optional[BaseException] = None
    cleanup_error: Optional[BaseException] = None
    try:
        root = Path(tempdir.name)
        state_root = root / ".bridge"
        config = BridgeConfig(
            control_repo="owner/bridge",
            trusted_github_logins={"trusted-user"},
            projects={
                "demo": ProjectConfig(
                    capabilities=["code"],
                    root=root,
                    repo="owner/demo",
                )
            },
        )
        config.config_path = root / "config.local.yaml"
        store = TaskStore(state_root)
        fake = _ShutdownFakeAppServer()
        manager = CodexSessionManager(
            config,
            store=store,
            client_factory=lambda **kwargs: _attach_fake(fake, kwargs),
        )
        queue = WorkerQueue(max_workers=1)
        worktree = state_root / "worktrees" / "shutdown-task"
        worktree.mkdir(parents=True)
        executor = RuntimeCodeExecutor(
            config,
            store,
            manager,
            lambda project_name, task_id: worktree,
        )
        runner = TaskRunner(store=store, handlers={"code": CodeTaskHandler(executor)})
        supervisor = TaskSupervisor(
            config,
            store=store,
            session_manager=manager,
            worktree_factory=lambda project_name, task_id: worktree,
            worker_queue=queue,
            task_runner=runner,
            code_executor=executor,
        )
        task_id = "shutdown-task"
        runtime = _ActiveRuntime(
            tempdir=tempdir,
            store=store,
            manager=manager,
            queue=queue,
            supervisor=supervisor,
            fake=fake,
            task_id=task_id,
            worktree=worktree,
        )
        supervisor.start_code_task(
            "demo",
            "hold the in-process Codex turn open",
            task_id=task_id,
            worktree=worktree,
        )
        runtime.future = queue.future(task_id)
        yield runtime
    except BaseException as exc:
        primary_error = exc
    finally:
        if runtime is not None:
            record = runtime.record or runtime.manager.sessions.get(runtime.task_id)
            if record is not None and not record.completion.is_set():
                with runtime.manager._lock:
                    record.interrupt_requested = True
                    record.state = SessionState.INTERRUPTED
                record.completion.set()

            try:
                if runtime.future is not None:
                    runtime.future.result(timeout=1.0)
            except BaseException as exc:
                cleanup_error = exc

            try:
                runtime.queue.shutdown(wait=True, cancel_futures=True)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc

            threads = list(getattr(runtime.queue._executor, "_threads", ()))
            runtime.remaining_bridge_workers = tuple(thread.name for thread in threads if thread.is_alive())
            if runtime.remaining_bridge_workers:
                cleanup_error = cleanup_error or RuntimeError(
                    f"bridge worker threads remain alive: {runtime.remaining_bridge_workers}"
                )
        tempdir.cleanup()

    if primary_error is not None:
        raise primary_error
    if cleanup_error is not None:
        raise cleanup_error


def _await_active(runtime: _ActiveRuntime) -> tuple[Any, Any]:
    assert runtime.fake.turn_started_event.wait(timeout=1.0)
    assert runtime.fake.wait_entered_event.wait(timeout=1.0)
    record = runtime.manager.sessions[runtime.task_id]
    runtime.record = record
    future = runtime.future
    assert future is not None
    assert record.state == SessionState.RUNNING
    assert runtime.store.get_task(runtime.task_id)["state"] == SessionState.RUNNING.value
    assert record.completion.is_set() is False
    assert future.done() is False
    return record, future


def test_supervisor_close_interrupts_active_runtime_task_and_wakes_worker() -> None:
    with _active_runtime() as runtime:
        record, future = _await_active(runtime)

        started = monotonic()
        runtime.supervisor.close()
        close_duration = monotonic() - started
        assert close_duration < 1.0

        durable_state = runtime.store.get_task(runtime.task_id)
        assert record.state == SessionState.INTERRUPTED, (
            "shutdown must interrupt the active session; "
            f"record_state={record.state.value}; "
            f"durable_state={durable_state['state']}; "
            f"completion={record.completion.is_set()}; "
            f"client_close_calls={runtime.fake.close_calls}; "
            f"future_done={future.done()}"
        )
        assert record.interrupt_requested is True
        assert record.completion.is_set() is True
        assert durable_state["state"] == SessionState.INTERRUPTED.value
        assert durable_state["review_ready"] is False
        assert runtime.fake.close_called is True
        future.result(timeout=0.25)
        assert future.done() is True
        assert runtime.store.get_task(runtime.task_id)["state"] != SessionState.FAILED.value


def test_shutdown_terminal_state_rejects_late_turn_completion() -> None:
    with _active_runtime() as runtime:
        record, future = _await_active(runtime)
        runtime.supervisor.close()

        assert record.state == SessionState.INTERRUPTED
        before = runtime.store.get_task(runtime.task_id)
        runtime.fake.emit(
            "turn/completed",
            {
                "threadId": "shutdown-test-thread",
                "turn": {"id": "shutdown-test-turn", "status": "completed", "items": []},
            },
        )
        after = runtime.store.get_task(runtime.task_id)
        assert after["state"] == SessionState.INTERRUPTED.value
        assert after["state"] == before["state"]
        assert record.state == SessionState.INTERRUPTED
        future.result(timeout=0.25)
        assert future.done() is True


def test_supervisor_close_is_idempotent_after_active_shutdown() -> None:
    with _active_runtime() as runtime:
        record, future = _await_active(runtime)
        runtime.supervisor.close()
        runtime.supervisor.close()

        assert record.state == SessionState.INTERRUPTED
        assert record.completion.is_set() is True
        assert runtime.fake.close_calls == 1
        future.result(timeout=0.25)
        assert future.done() is True


def test_shutdown_does_not_interrupt_session_already_completed_before_claim() -> None:
    with _active_runtime() as runtime:
        record, future = _await_active(runtime)
        runtime.fake.emit(
            "turn/completed",
            {
                "threadId": "shutdown-test-thread",
                "turn": {"id": "shutdown-test-turn", "status": "completed", "items": []},
            },
        )
        future.result(timeout=1.0)
        assert record.state == SessionState.WAITING_REVIEW
        assert runtime.store.get_task(runtime.task_id)["state"] == SessionState.WAITING_REVIEW.value

        runtime.supervisor.close()

        assert record.state == SessionState.WAITING_REVIEW
        assert runtime.store.get_task(runtime.task_id)["state"] == SessionState.WAITING_REVIEW.value
        assert runtime.store.get_task(runtime.task_id)["review_ready"] is True
        assert runtime.fake.close_calls == 1


def test_shutdown_wakes_waiter_before_client_close_failure() -> None:
    with _active_runtime() as runtime:
        record, future = _await_active(runtime)
        runtime.fake.raise_on_close = True

        with pytest.raises(RuntimeError, match="fake client close failed"):
            runtime.supervisor.close()

        assert record.state == SessionState.INTERRUPTED
        assert runtime.store.get_task(runtime.task_id)["state"] == SessionState.INTERRUPTED.value
        assert record.completion.is_set() is True
        future.result(timeout=0.25)
        assert future.done() is True
