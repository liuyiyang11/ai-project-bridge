from __future__ import annotations

import json

import pytest

from bridge.commands import CommandExecutionError
from bridge.mcp.server import McpStdioServer
from bridge.mcp.tools import BridgeMcpTools, McpToolError
from bridge.orchestration.event_bus import TaskEventBus
from bridge.orchestration.task_runner import TaskRunner
from bridge.public_errors import public_error_from_exception, sanitize_public_text
from bridge.security import SecurityError
from bridge.store.task_store import TaskStore


class _FailingHandler:
    def __init__(self, error: Exception):
        self.error = error

    def execute(self, task):
        raise self.error


def _store(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="run")
    return store


def test_task_runner_never_persists_path_token_or_traceback(tmp_path):
    store = _store(tmp_path)
    raw_path = r"F:\private\workspace\secret.py"
    raw_token = "sk-test-secret"
    error = RuntimeError(f"failed at {raw_path} token={raw_token}")

    with pytest.raises(RuntimeError):
        TaskRunner(store=store, handlers={"code": _FailingHandler(error)}).run("task-1")

    state_text = store.task_path("task-1", "state.json").read_text(encoding="utf-8")
    events_text = store.task_path("task-1", "events.jsonl").read_text(encoding="utf-8")
    result_text = store.task_path("task-1", "result.json").read_text(encoding="utf-8")
    for persisted in (state_text, events_text, result_text):
        assert raw_path not in persisted
        assert raw_token not in persisted
        assert "traceback" not in persisted.casefold()

    state = store.get_task("task-1")
    assert state["error_code"] == "INTERNAL_ERROR"
    assert state["last_error"] == "The task failed unexpectedly. Check local Bridge developer logs."
    error_events = [event for event in store.all_events("task-1") if event["type"] == "error"]
    assert error_events == [
        {
            "seq": error_events[0]["seq"],
            "task_id": "task-1",
            "type": "error",
            "time": error_events[0]["time"],
            "data": {
                "error_code": "INTERNAL_ERROR",
                "message": "The task failed unexpectedly. Check local Bridge developer logs.",
            },
        }
    ]


def test_event_bus_sanitizes_nested_error_values_before_append(tmp_path):
    store = _store(tmp_path)
    raw_path = r"C:\private\workspace\secret.py"
    raw_token = "sk-test-secret"

    TaskEventBus(store, "task-1").emit(
        "diagnostic",
        {
            "nested": {
                "message": f"failed at {raw_path} token={raw_token}",
                "path": raw_path,
                "token": raw_token,
                "traceback": "Traceback (most recent call last):\n  ...",
            }
        },
    )

    event_text = store.task_path("task-1", "events.jsonl").read_text(encoding="utf-8")
    assert raw_path not in event_text
    assert raw_token not in event_text
    assert "traceback" not in event_text.casefold()
    event = TaskEventBus(store, "task-1").events(after_seq=0, limit=100)[0]
    assert event["data"]["nested"]["token"] == "[redacted]"


def test_timeout_is_a_stable_public_error_without_command_details():
    raw = r"configured command 'quick_test' failed to start or timed out: F:\private\workspace\secret.py token=sk-test-secret"
    public = public_error_from_exception(CommandExecutionError(raw), context="command")

    assert public.error_code == "COMMAND_TIMEOUT"
    assert public.message == "The registered command timed out."
    assert raw not in public.message
    assert sanitize_public_text("https://example.com/path") == "https://example.com/path"


def test_unicode_security_error_has_only_the_safe_message():
    public = public_error_from_exception(SecurityError("input contains invalid Unicode scalar value"), context="mcp")
    assert public.error_code == "INVALID_REQUEST"
    assert public.message == "input contains invalid Unicode scalar value"

    malformed = public_error_from_exception(SecurityError("bad\ud800"), context="mcp")
    assert malformed.error_code == "INVALID_REQUEST"
    assert "\ud800" not in malformed.message


class _LegacyLeakingSupervisor:
    raw_path = r"C:\private\workspace\secret.py"
    raw_token = "sk-test-secret"
    raw_error = f"RuntimeError: failed at {raw_path} token={raw_token}"

    def task_status(self, task_id):
        return {
            "task_id": task_id,
            "project": "demo",
            "state": "FAILED",
            "stage": "failed",
            "current_action": "failed",
            "changed_files": [],
            "review_ready": False,
            "last_event_seq": 1,
            "last_error": self.raw_error,
        }

    def task_events(self, task_id, *, after_seq=0, limit=100):
        return [
            {
                "seq": 1,
                "task_id": task_id,
                "type": "diagnostic",
                "data": {
                    "nested": {
                        "message": self.raw_error,
                        "path": self.raw_path,
                        "token": self.raw_token,
                        "traceback": "Traceback (most recent call last): raw",
                    }
                },
            },
            {
                "seq": 2,
                "task_id": task_id,
                "type": "error",
                "data": {"message": self.raw_error, "traceback": "Traceback (most recent call last): raw"},
            },
        ]


def test_mcp_status_and_events_are_final_defense_for_legacy_raw_data():
    supervisor = _LegacyLeakingSupervisor()
    tools = BridgeMcpTools(supervisor=supervisor)

    status = tools.call("bridge_task_status", {"task_id": "task-1"})
    events = tools.call("bridge_task_events", {"task_id": "task-1"})
    public_json = json.dumps({"status": status, "events": events}, ensure_ascii=False)

    assert supervisor.raw_path not in public_json
    assert supervisor.raw_token not in public_json
    assert "traceback" not in public_json.casefold()
    assert status["error"]["error_code"] == "INTERNAL_ERROR"
    assert events["events"][1]["data"] == {
        "error_code": "INTERNAL_ERROR",
        "message": "The task failed unexpectedly. Check local Bridge developer logs.",
    }


class _RaisingSupervisor:
    def start_code_task(self, *args, **kwargs):
        raise RuntimeError(r"failed at C:\private\secret.py token=sk-test-secret")


def test_mcp_tool_error_drops_raw_exception_cause_and_message():
    with pytest.raises(McpToolError) as caught:
        BridgeMcpTools(supervisor=_RaisingSupervisor()).call(
            "bridge_start_code_task",
            {"project": "demo", "instruction": "run"},
        )

    assert caught.value.__cause__ is None
    assert "secret.py" not in str(caught.value)
    assert "sk-test-secret" not in str(caught.value)
    assert "traceback" not in str(caught.value).casefold()


def test_mcp_server_error_response_has_no_raw_exception():
    response = McpStdioServer(BridgeMcpTools(supervisor=_RaisingSupervisor())).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "bridge_start_code_task",
                "arguments": {"project": "demo", "instruction": "run"},
            },
        }
    )

    serialized = json.dumps(response, ensure_ascii=False)
    assert "secret.py" not in serialized
    assert "sk-test-secret" not in serialized
    assert "traceback" not in serialized.casefold()
    assert response["result"]["isError"] is True
