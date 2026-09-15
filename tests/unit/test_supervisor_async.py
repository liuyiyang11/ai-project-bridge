from __future__ import annotations

from pathlib import Path

import pytest

from bridge.codex.fake_app_server import FakeAppServer
from bridge.codex.session import CodexSessionManager
from bridge.config import BridgeConfig, ProjectConfig
from bridge.orchestration.supervisor import TaskSupervisor
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

    assert response["state"] == "QUEUED"
    assert response["project"] == "demo"
    assert store.get_task(response["task_id"])["state"] == "QUEUED"
    assert queue.submitted_task_ids == [response["task_id"]]


def test_worker_later_advances_fake_code_task_to_review(tmp_path):
    supervisor, queue, store = make_supervisor(tmp_path)
    task_id = supervisor.start_code_task("demo", "make a safe change")["task_id"]

    queue.run_submitted(task_id)

    snapshot = store.get_task(task_id)
    assert snapshot["state"] == "WAITING_REVIEW"
    assert snapshot["thread_id"] == "fake-thread"


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
    store.transition_task("running", "QUEUED", "PREPARING", "test preparation")
    store.transition_task("running", "PREPARING", "RUNNING", "test execution")
    store.update_task("running", thread_id="thread-1")

    supervisor, queue, _ = make_supervisor(tmp_path, store=store)

    assert queue.submitted_task_ids == ["queued"]
    assert store.get_task("running")["state"] == "UNKNOWN"
