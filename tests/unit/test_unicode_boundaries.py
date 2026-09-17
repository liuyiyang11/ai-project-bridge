from __future__ import annotations

import pytest

from bridge.mcp.server import McpStdioServer
from bridge.mcp.tools import BridgeMcpTools, McpToolError
from bridge.security import SecurityError, validate_unicode_scalars

from .test_supervisor_async import make_supervisor


@pytest.mark.parametrize(
    "value",
    [
        "ASCII",
        "中文输入",
        "emoji 😀",
        "replacement �",
        ["中文", {"nested": [None, True, 3, 1.5]}],
        {"中文": ("emoji 😀",)},
    ],
)
def test_unicode_scalar_validator_accepts_valid_scalars(value):
    validate_unicode_scalars(value)


@pytest.mark.parametrize("value", ["bad\ud800", "bad\udfff", {"nested": ["bad\ud800"]}, {"bad\udfff": "value"}])
def test_unicode_scalar_validator_rejects_lone_surrogates_without_reflecting_input(value):
    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        validate_unicode_scalars(value, path="request")


@pytest.mark.parametrize("field", ["instruction", "acceptance", "model", "reasoning_effort", "metadata"])
def test_task_store_rejects_surrogates_before_creating_any_directory(tmp_path, field):
    from bridge.store.task_store import TaskStore

    store = TaskStore(tmp_path / "空间 with spaces" / ".bridge")
    values = {
        "instruction": "bad\ud800",
        "acceptance": ["bad\ud800"],
        "model": "bad\ud800",
        "reasoning_effort": "bad\ud800",
        "metadata": {"nested": ["bad\ud800"]},
    }
    task_id = f"unicode-{field}"

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        store.create_task(task_id, project="demo", **{field: values[field]})

    assert not store.task_dir(task_id).exists()
    assert not (store.root / "tasks").exists()


def test_mcp_surrogate_input_is_safe_error_and_never_reaches_queue(tmp_path, monkeypatch):
    supervisor, queue, store = make_supervisor(tmp_path)
    task_id = "task-unicode-regression"
    monkeypatch.setattr(supervisor, "_new_task_id", lambda: task_id)
    tools = BridgeMcpTools(supervisor=supervisor)

    with pytest.raises(McpToolError, match=r"^input contains invalid Unicode scalar value$"):
        tools.call("bridge_start_code_task", {"project": "demo", "instruction": "bad\ud800"})

    assert queue.submitted_task_ids == []
    assert not store.task_dir(task_id).exists()


def test_mcp_server_error_has_no_structured_content_for_surrogate_input(tmp_path, monkeypatch):
    supervisor, queue, store = make_supervisor(tmp_path)
    task_id = "task-unicode-server-regression"
    monkeypatch.setattr(supervisor, "_new_task_id", lambda: task_id)
    server = McpStdioServer(BridgeMcpTools(supervisor=supervisor))

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "bridge_start_code_task",
                "arguments": {"project": "demo", "instruction": "bad\udfff"},
            },
        }
    )

    result = response["result"]
    assert result["isError"] is True
    assert "structuredContent" not in result
    assert result["content"][0]["text"] == "input contains invalid Unicode scalar value"
    assert queue.submitted_task_ids == []
    assert not store.task_dir(task_id).exists()
