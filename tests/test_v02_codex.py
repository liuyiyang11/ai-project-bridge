from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import io

import pytest

from bridge.codex.app_server import CodexAppServerClient
from bridge.codex.fake_app_server import FakeAppServer
from bridge.codex.model_catalog import CodexModelError
from bridge.codex.session import CodexSessionManager, SessionState, SessionTransitionError
from bridge.task_store import TaskStore


class _LineStream:
    def __init__(self):
        import queue

        self.lines = queue.Queue()

    def put(self, value):
        self.lines.put(value)

    def readline(self):
        return self.lines.get(timeout=2)


class _Stdin:
    def __init__(self, stdout):
        import json

        self.stdout = stdout
        self.messages = []

    def write(self, line):
        import json

        message = json.loads(line)
        self.messages.append(message)
        if "id" not in message:
            return len(line)
        method = message["method"]
        params = message.get("params", {})
        result = {}
        if method == "initialize":
            result = {"serverInfo": {"name": "fake", "version": "test"}}
        elif method == "model/list":
            result = {"data": [{"id": "m1", "model": "m1", "isDefault": True, "hidden": False, "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "medium", "description": ""}]}]}
        elif method == "thread/start":
            result = {"thread": {"id": "thread-1"}}
        elif method == "thread/resume":
            result = {"thread": {"id": params["threadId"]}}
        elif method == "turn/start":
            result = {"turn": {"id": "turn-1", "status": "inProgress"}}
        elif method == "turn/steer":
            result = {"turnId": params["expectedTurnId"]}
        elif method == "turn/interrupt":
            result = {}
        self.stdout.put(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
        return len(line)

    def flush(self):
        return None

    def close(self):
        return None


class _Process:
    def __init__(self):
        self.stdout = _LineStream()
        self.stderr = io.StringIO()
        self.stdin = _Stdin(self.stdout)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0
        self.stdout.put("")

    def wait(self, timeout=None):
        return self.returncode


def _config(tmp_path, policy="inherit"):
    return SimpleNamespace(
        codex_binary="codex",
        codex=SimpleNamespace(routing_policy=policy, request_timeout_seconds=2, initialize_timeout_seconds=2, turn_timeout_seconds=2),
    )


def test_app_server_client_uses_shellless_stdio_json_rpc(tmp_path):
    process = _Process()
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    client = CodexAppServerClient("codex", cwd=tmp_path, popen=popen, available=True, request_timeout=1)
    client.start()
    client.model_list()
    client.thread_start()
    client.thread_resume("thread-1")
    client.turn_start("thread-1", "do it", reasoning_effort="medium")
    client.turn_steer("thread-1", "turn-1", "focus")
    client.turn_interrupt("thread-1", "turn-1")

    assert calls[0][0][1:] == ["app-server", "--stdio"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["cwd"] == str(tmp_path.resolve())
    assert any(item["method"] == "initialized" for item in process.stdin.messages)
    turn = next(item for item in process.stdin.messages if item.get("method") == "turn/start")
    assert turn["params"]["effort"] == "medium"
    client.close()


def test_session_manager_lifecycle_and_safe_events(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.initialize("task-1", "task", {"status": "QUEUED", "state": "QUEUED", "project": "demo"})
    fake = FakeAppServer(auto_complete=True)
    manager = CodexSessionManager(_config(tmp_path), store=store, client_factory=lambda **kwargs: _attach(fake, kwargs))

    record = manager.start_task("task-1", "demo", tmp_path, "make a safe change")

    assert record.state == SessionState.WAITING_REVIEW
    assert record.process is fake
    assert record.thread_id == "fake-thread"
    assert manager.status("task-1")["review_ready"] is True
    events = store.read_events("task-1")
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert any(event["type"] == "agent_message" for event in events)
    assert all("reasoning" not in str(event).lower() for event in events)

    manager.continue_task("task-1", "continue with tests")
    assert manager.status("task-1")["state"] == SessionState.WAITING_REVIEW.value
    manager.accept_task("task-1")
    assert manager.status("task-1")["state"] == SessionState.COMPLETED.value


def test_session_manager_enforces_control_states(tmp_path):
    fake = FakeAppServer(auto_complete=False)
    manager = CodexSessionManager(_config(tmp_path), client_factory=lambda **kwargs: _attach(fake, kwargs))
    record = manager.start_task("task-2", "demo", tmp_path, "wait")
    assert record.state == SessionState.RUNNING
    manager.steer_task("task-2", "keep going")
    manager.interrupt_task("task-2")
    with pytest.raises(SessionTransitionError, match="cannot steer"):
        manager.steer_task("task-2", "too late")


def test_invalid_model_and_effort_are_rejected_before_thread_start(tmp_path):
    fake = FakeAppServer(auto_complete=False)
    manager = CodexSessionManager(_config(tmp_path), client_factory=lambda **kwargs: _attach(fake, kwargs))
    with pytest.raises(CodexModelError, match="model is not available"):
        manager.start_task("task-3", "demo", tmp_path, "wait", model="missing")
    assert not any(name == "thread/start" for name, _ in fake.requests)

    fake2 = FakeAppServer(auto_complete=False)
    manager2 = CodexSessionManager(_config(tmp_path), client_factory=lambda **kwargs: _attach(fake2, kwargs))
    with pytest.raises(CodexModelError, match="reasoning_effort"):
        manager2.start_task("task-4", "demo", tmp_path, "wait", reasoning_effort="ultra")
    assert not any(name == "thread/start" for name, _ in fake2.requests)


def _attach(fake, kwargs):
    fake.notification_handler = kwargs.get("notification_handler")
    fake.process_error_handler = kwargs.get("process_error_handler")
    return fake
