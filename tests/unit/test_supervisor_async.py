from __future__ import annotations

from pathlib import Path
import threading
import time

import pytest

from bridge.codex.fake_app_server import FakeAppServer
from bridge.codex.session import CodexSessionManager
from bridge.config import BridgeConfig, ProjectConfig
from bridge.orchestration.event_bus import TaskEventBus
from bridge.orchestration.models import TaskResult
from bridge.orchestration.supervisor import TaskSupervisor
from bridge.orchestration.task_runner import TaskRunner
from bridge.store.task_store import TaskStore


class ManualQueue:
    def __init__(self):
        self.jobs = {}
        self.submitted_task_ids = []

    def submit(self, task_id, callable_, *args, **kwargs):
        self.jobs[task_id] = (callable_, args, kwargs)
        self.submitted_task_ids.append(task_id)
        return None

    def run_submitted(self, task_id):
        callable_, args, kwargs = self.jobs[task_id]
        return callable_(*args, **kwargs)

    def shutdown(self, **kwargs):
        return None


class DuplicateRejectingQueue(ManualQueue):
    def submit(self, task_id, callable_, *args, **kwargs):
        if task_id in self.jobs:
            raise ValueError(f"task is already queued or running: {task_id}")
        return super().submit(task_id, callable_, *args, **kwargs)


def make_supervisor(tmp_path, *, store=None, queue=None, client_factory=None):
    state_root = tmp_path / ".bridge"
    config = BridgeConfig(
        control_repo="owner/bridge",
        trusted_github_logins={"trusted-user"},
        projects={
            "demo": ProjectConfig(
                capabilities=["code"],
                root=tmp_path,
                repo="owner/demo",
            )
        },
    )
    config.config_path = tmp_path / "config.local.yaml"
    store = store or TaskStore(state_root)
    fake = FakeAppServer(auto_complete=True)
    factory = client_factory or (lambda **kwargs: _attach(fake, kwargs))
    manager = CodexSessionManager(config, store=store, client_factory=factory)
    queue = queue or ManualQueue()
    supervisor = TaskSupervisor(
        config,
        store=store,
        session_manager=manager,
        worker_queue=queue,
        worktree_factory=lambda project, task_id: _worktree(state_root, task_id),
    )
    return supervisor, queue, store


def _worktree(root, task_id):
    path = root / "worktrees" / task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _attach(fake, kwargs):
    fake.notification_handler = kwargs.get("notification_handler")
    fake.process_error_handler = kwargs.get("process_error_handler")
    return fake


def test_start_code_task_returns_queued_before_worker_runs(tmp_path):
    supervisor, queue, store = make_supervisor(tmp_path)

    response = supervisor.start_code_task("demo", "make a safe change")

    assert response == {"task_id": response["task_id"], "state": "QUEUED", "project": "demo"}
    assert store.get_task(response["task_id"])["state"] == "QUEUED"
    assert queue.submitted_task_ids == [response["task_id"]]


def test_worker_later_advances_fake_code_task_to_review(tmp_path):
    supervisor, queue, store = make_supervisor(tmp_path)
    task_id = supervisor.start_code_task("demo", "make a safe change")["task_id"]

    result = queue.run_submitted(task_id)

    snapshot = store.get_task(task_id)
    assert result == TaskResult(
        success=True,
        review_ready=True,
        message="Fake app-server completed.",
        artifacts=[],
        metadata={
            "thread_id": "fake-thread",
            "turn_id": "fake-turn-1",
            "changed_files": [],
        },
    )
    assert snapshot["state"] == "WAITING_REVIEW"
    assert snapshot["thread_id"] == "fake-thread"


def test_runtime_code_accept_keeps_control_response_and_snapshot_in_sync(tmp_path):
    supervisor, queue, store = make_supervisor(tmp_path)
    task_id = supervisor.start_code_task("demo", "make a safe change")["task_id"]
    queue.run_submitted(task_id)

    response = supervisor.control_task(task_id, "accept")

    assert response["state"] == "COMPLETED"
    assert store.get_task(task_id)["state"] == "COMPLETED"
    states = [
        (event["data"].get("from"), event["data"].get("to"))
        for event in store.all_events(task_id)
        if event["type"] == "state_changed"
    ]
    assert states[-1] == ("WAITING_REVIEW", "COMPLETED")


def test_runtime_code_continue_reenters_event_bus_lifecycle(tmp_path):
    supervisor, queue, store = make_supervisor(tmp_path)
    task_id = supervisor.start_code_task("demo", "make a safe change")["task_id"]
    queue.run_submitted(task_id)

    response = supervisor.control_task(task_id, "continue", "make the follow-up change")

    assert response["state"] == "WAITING_REVIEW"
    assert store.get_task(task_id)["state"] == "WAITING_REVIEW"
    states = [
        (event["data"].get("from"), event["data"].get("to"))
        for event in store.all_events(task_id)
        if event["type"] == "state_changed"
    ]
    assert states[-2:] == [("WAITING_REVIEW", "RUNNING"), ("RUNNING", "WAITING_REVIEW")]


def test_runtime_code_interrupt_remains_interrupted_not_failed(tmp_path):
    fake = FakeAppServer(auto_complete=False)
    supervisor, queue, store = make_supervisor(tmp_path, client_factory=lambda **kwargs: _attach(fake, kwargs))
    task_id = supervisor.start_code_task("demo", "make a safe change")["task_id"]
    worker_result = []
    worker_errors = []

    def run_worker():
        try:
            worker_result.append(queue.run_submitted(task_id))
        except Exception as exc:
            worker_errors.append(exc)

    worker = threading.Thread(target=run_worker)
    worker.start()
    deadline = time.monotonic() + 1
    while not any(name == "turn/start" for name, _ in fake.requests) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert any(name == "turn/start" for name, _ in fake.requests)

    response = supervisor.control_task(task_id, "interrupt")
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert worker_errors == []
    assert worker_result[0].success is True
    assert response["state"] == "INTERRUPTED"
    assert store.get_task(task_id)["state"] == "INTERRUPTED"


def test_interrupt_during_runner_startup_is_durable_before_a_session_exists(tmp_path):
    class BlockingHandler:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def execute(self, task):
            self.started.set()
            assert self.release.wait(timeout=2)
            return TaskResult(success=True, review_ready=True, message="should not replace interrupt")

    supervisor, queue, store = make_supervisor(tmp_path)
    handler = BlockingHandler()
    supervisor.task_runner = TaskRunner(store=store, handlers={"code": handler})
    task_id = supervisor.start_code_task("demo", "make a safe change")["task_id"]
    worker_result = []

    worker = threading.Thread(target=lambda: worker_result.append(queue.run_submitted(task_id)))
    worker.start()
    assert handler.started.wait(timeout=1)
    try:
        response = supervisor.control_task(task_id, "interrupt")
    finally:
        handler.release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert response["state"] == "INTERRUPTED"
    assert worker_result[0].success is True
    assert store.get_task(task_id)["state"] == "INTERRUPTED"


def test_worker_failure_is_persisted_as_failed(tmp_path):
    class FailingFake(FakeAppServer):
        def start(self):
            raise RuntimeError("codex unavailable")

    fake = FailingFake(auto_complete=True)
    supervisor, queue, store = make_supervisor(tmp_path, client_factory=lambda **kwargs: _attach(fake, kwargs))
    task_id = supervisor.start_code_task("demo", "make a safe change")["task_id"]

    with pytest.raises(RuntimeError, match="codex unavailable"):
        queue.run_submitted(task_id)

    assert store.get_task(task_id)["state"] == "FAILED"
    assert any(item["type"] == "error" for item in store.list_events(task_id, after_seq=0, limit=100))


def test_restart_marks_active_task_unknown_and_requeues_queued(tmp_path):
    state_root = tmp_path / ".bridge"
    store = TaskStore(state_root)
    store.create_task("queued", project="demo", task_type="code", instruction="queued")
    store.create_task("running", project="demo", task_type="code", instruction="running")
    bus = TaskEventBus(store, "running")
    bus.transition("QUEUED", "PREPARING", "test preparation")
    bus.transition("PREPARING", "RUNNING", "test execution")
    store.update_task("running", thread_id="thread-1")

    supervisor, queue, _ = make_supervisor(tmp_path, store=store)

    assert queue.submitted_task_ids == ["queued"]
    assert store.get_task("running")["state"] == "UNKNOWN"


def test_restart_requeues_only_a_safely_resumable_active_code_task(tmp_path):
    state_root = tmp_path / ".bridge"
    store = TaskStore(state_root)
    task_id = "resumable"
    worktree = state_root / "worktrees" / task_id
    worktree.mkdir(parents=True)
    store.create_task(task_id, project="demo", task_type="code", instruction="resume safely")
    bus = TaskEventBus(store, task_id)
    bus.transition("QUEUED", "PREPARING", "test preparation")
    bus.transition("PREPARING", "RUNNING", "test execution")
    store.update_task(task_id, thread_id="saved-thread", worktree=str(worktree), worktree_path=str(worktree))
    fake = FakeAppServer(auto_complete=True)

    supervisor, queue, _ = make_supervisor(
        tmp_path,
        store=store,
        client_factory=lambda **kwargs: _attach(fake, kwargs),
    )

    assert queue.submitted_task_ids == [task_id]
    assert store.get_task(task_id)["state"] == "RUNNING"

    queue.run_submitted(task_id)

    assert store.get_task(task_id)["state"] == "WAITING_REVIEW"
    assert any(name == "thread/resume" and args["threadId"] == "saved-thread" for name, args in fake.requests)


def test_restart_resumes_a_safely_persisted_preparing_task(tmp_path):
    state_root = tmp_path / ".bridge"
    store = TaskStore(state_root)
    task_id = "preparing-resume"
    worktree = state_root / "worktrees" / task_id
    worktree.mkdir(parents=True)
    store.create_task(task_id, project="demo", task_type="code", instruction="resume safely")
    TaskEventBus(store, task_id).transition("QUEUED", "PREPARING", "test preparation")
    store.update_task(task_id, thread_id="saved-thread", worktree=str(worktree), worktree_path=str(worktree))
    fake = FakeAppServer(auto_complete=True)

    supervisor, queue, _ = make_supervisor(
        tmp_path,
        store=store,
        client_factory=lambda **kwargs: _attach(fake, kwargs),
    )

    assert queue.submitted_task_ids == [task_id]
    queue.run_submitted(task_id)
    assert store.get_task(task_id)["state"] == "WAITING_REVIEW"
    assert any(name == "thread/resume" and args["threadId"] == "saved-thread" for name, args in fake.requests)


def test_recovery_does_not_mark_an_already_submitted_active_task_unknown(tmp_path):
    state_root = tmp_path / ".bridge"
    store = TaskStore(state_root)
    task_id = "already-submitted"
    worktree = state_root / "worktrees" / task_id
    worktree.mkdir(parents=True)
    store.create_task(task_id, project="demo", task_type="code", instruction="resume safely")
    bus = TaskEventBus(store, task_id)
    bus.transition("QUEUED", "PREPARING", "test preparation")
    bus.transition("PREPARING", "RUNNING", "test execution")
    store.update_task(task_id, thread_id="saved-thread", worktree=str(worktree), worktree_path=str(worktree))

    supervisor, queue, _ = make_supervisor(tmp_path, store=store, queue=DuplicateRejectingQueue())
    supervisor.recover_tasks()

    assert queue.submitted_task_ids == [task_id]
    assert store.get_task(task_id)["state"] == "RUNNING"


def test_restart_marks_a_failed_resume_unknown_instead_of_failed(tmp_path):
    class ResumeFailureFake(FakeAppServer):
        def thread_resume(self, thread_id, **kwargs):
            raise RuntimeError("saved thread cannot be resumed")

    state_root = tmp_path / ".bridge"
    store = TaskStore(state_root)
    task_id = "unresumable"
    worktree = state_root / "worktrees" / task_id
    worktree.mkdir(parents=True)
    store.create_task(task_id, project="demo", task_type="code", instruction="resume safely")
    bus = TaskEventBus(store, task_id)
    bus.transition("QUEUED", "PREPARING", "test preparation")
    bus.transition("PREPARING", "RUNNING", "test execution")
    store.update_task(task_id, thread_id="saved-thread", worktree=str(worktree), worktree_path=str(worktree))
    fake = ResumeFailureFake(auto_complete=True)

    supervisor, queue, _ = make_supervisor(
        tmp_path,
        store=store,
        client_factory=lambda **kwargs: _attach(fake, kwargs),
    )

    result = queue.run_submitted(task_id)

    assert result.success is False
    assert store.get_task(task_id)["state"] == "UNKNOWN"
    assert any(
        item["type"] == "error" and "cannot be resumed" in item["data"]["message"]
        for item in store.list_events(task_id, after_seq=0, limit=100)
    )
