from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from bridge.codex.fake_app_server import FakeAppServer
from bridge.codex.session import CodexSessionManager
from bridge.config import BridgeConfig, ProjectConfig
from bridge.mcp.server import McpStdioServer
from bridge.mcp.tools import BridgeMcpTools, McpToolError
from bridge.orchestration.supervisor import TaskSupervisor
from bridge.task_store import TaskStore


def make_bridge(tmp_path):
    state_root = tmp_path / ".bridge"
    config = BridgeConfig(
        control_repo="owner/bridge",
        trusted_github_logins={"trusted-user"},
        projects={
            "demo": ProjectConfig(
                capabilities=["code", "experiment-review", "presentation"],
                root=tmp_path,
                repo="owner/demo",
                allowed_commands={"evaluate": {"argv": ["python", "-c", "print('ok')"]}},
            )
        },
    )
    config.config_path = tmp_path / "config.local.yaml"
    store = TaskStore(state_root)
    fake = FakeAppServer(auto_complete=True)
    manager = CodexSessionManager(config, store=store, client_factory=lambda **kwargs: _attach(fake, kwargs))
    supervisor = TaskSupervisor(config, store=store, session_manager=manager, worktree_factory=lambda project, task_id: _worktree(state_root, task_id))
    return config, store, supervisor


def _worktree(root, task_id):
    path = root / "worktrees" / task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _attach(fake, kwargs):
    fake.notification_handler = kwargs.get("notification_handler")
    fake.process_error_handler = kwargs.get("process_error_handler")
    return fake


def test_mcp_exposes_nine_tools_and_safe_task_lifecycle(tmp_path):
    _, _, supervisor = make_bridge(tmp_path)
    tools = BridgeMcpTools(supervisor=supervisor)
    assert len(tools.definitions()) == 9
    assert tools.call("bridge_list_projects") == {"projects": [{"id": "demo", "capabilities": ["code", "experiment-review", "presentation"]}]}

    started = tools.call("bridge_start_code_task", {"project": "demo", "instruction": "change code", "acceptance": ["tests pass"]})
    task_id = started["task_id"]
    assert started["state"] == "QUEUED"
    future = supervisor.worker_queue.future(task_id)
    assert future is not None
    future.result(timeout=2)
    status = tools.call("bridge_task_status", {"task_id": task_id})
    assert set(status) >= {"state", "stage", "thread_id", "turn_id", "changed_files", "review_ready", "last_event_seq"}
    events = tools.call("bridge_task_events", {"task_id": task_id, "after_seq": 0, "limit": 3})
    assert len(events["events"]) == 3
    assert tools.call("bridge_control_task", {"task_id": task_id, "action": "accept"})["state"] == "COMPLETED"


def test_mcp_rejects_absolute_paths_unknown_commands_and_unknown_projects(tmp_path):
    _, _, supervisor = make_bridge(tmp_path)
    tools = BridgeMcpTools(supervisor=supervisor)
    with pytest.raises(McpToolError):
        tools.call("bridge_start_presentation_task", {"project": "demo", "instruction": "slides", "brief": str(tmp_path / "brief.md")})
    with pytest.raises(McpToolError):
        tools.call("bridge_start_code_task", {"project": "missing", "instruction": "do it"})
    with pytest.raises(McpToolError, match="not allowed"):
        tools.call("bridge_start_experiment_review", {"project": "demo", "command_id": "arbitrary"})
    with pytest.raises(McpToolError):
        tools.call("bridge_start_code_task", {"project": "demo", "instruction": "do it", "unexpected": True})


def test_mcp_stdio_round_trip_has_no_non_json_stdout(tmp_path):
    _, _, supervisor = make_bridge(tmp_path)
    server = McpStdioServer(BridgeMcpTools(supervisor=supervisor))
    incoming = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        + "\n"
    )
    outgoing = io.StringIO()
    server.serve(incoming, outgoing)
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert responses[0]["result"]["serverInfo"]["name"] == "ai-project-bridge"
    assert len(responses[1]["result"]["tools"]) == 9
