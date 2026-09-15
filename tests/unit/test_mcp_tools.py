from __future__ import annotations

import pytest

from bridge.mcp.tools import BridgeMcpTools, McpToolError
from bridge.orchestration.supervisor import TaskSupervisor

from .test_supervisor_async import make_supervisor


class _CodeTaskOnlySupervisor:
    def __init__(self):
        self.calls = []

    def start_code_task(self, project, instruction, acceptance=None, *, model=None, reasoning_effort=None):
        self.calls.append((project, instruction, acceptance, model, reasoning_effort))
        return {"task_id": "task-from-code-entrypoint", "state": "QUEUED", "project": project}

    def start_task(self, *args, **kwargs):
        raise AssertionError("MCP code startup must not route through start_task")


def test_mcp_start_calls_supervisor_and_returns_queued(tmp_path):
    supervisor, queue, store = make_supervisor(tmp_path)
    tools = BridgeMcpTools(supervisor=supervisor)

    result = tools.call("bridge_start_code_task", {"project": "demo", "instruction": "make a change"})

    assert result["state"] == "QUEUED"
    assert queue.submitted_task_ids == [result["task_id"]]
    assert store.get_task(result["task_id"])["state"] == "QUEUED"


def test_mcp_code_tool_calls_the_dedicated_code_entrypoint():
    supervisor = _CodeTaskOnlySupervisor()

    result = BridgeMcpTools(supervisor=supervisor).call(
        "bridge_start_code_task",
        {"project": "demo", "instruction": "make a change", "acceptance": ["tests pass"]},
    )

    assert result == {"task_id": "task-from-code-entrypoint", "state": "QUEUED", "project": "demo"}
    assert supervisor.calls == [("demo", "make a change", ["tests pass"], None, None)]


def test_mcp_rejects_invalid_project(tmp_path):
    supervisor, _, _ = make_supervisor(tmp_path)

    with pytest.raises(McpToolError, match="project"):
        BridgeMcpTools(supervisor=supervisor).call("bridge_start_code_task", {"project": "missing", "instruction": "do it"})


def test_mcp_rejects_missing_instruction(tmp_path):
    supervisor, _, _ = make_supervisor(tmp_path)

    with pytest.raises(McpToolError):
        BridgeMcpTools(supervisor=supervisor).call("bridge_start_code_task", {"project": "demo"})


def test_mcp_rejects_absolute_project_path(tmp_path):
    supervisor, _, _ = make_supervisor(tmp_path)

    with pytest.raises(McpToolError):
        BridgeMcpTools(supervisor=supervisor).call(
            "bridge_start_code_task",
            {"project": str(tmp_path), "instruction": "do it"},
        )


def test_mcp_artifacts_all_returns_bounded_manifest_shape(tmp_path):
    supervisor, _, store = make_supervisor(tmp_path)
    task_id = "task-artifacts"
    store.create_task(task_id, project="demo", task_type="code", instruction="inspect")
    store.save_artifacts(task_id, [{"path": "hello.py", "bytes": 12, "kind": "source", "hash": "abc", "content": "large"}])

    result = BridgeMcpTools(supervisor=supervisor).call("bridge_task_artifacts", {"task_id": task_id, "kind": "all"})

    assert result["artifacts"] == [{"path": "hello.py", "kind": "source", "hash": "abc", "size": 12}]
