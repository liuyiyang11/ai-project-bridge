from __future__ import annotations

import io
import json

from bridge.codex.fake_app_server import FakeAppServer
from bridge.codex.session import CodexSessionManager
from bridge.config import BridgeConfig, ProjectConfig
from bridge.mcp.server import McpStdioServer
from bridge.mcp.tools import BridgeMcpTools
from bridge.orchestration.supervisor import TaskSupervisor
from bridge.task_store import TaskStore


def _attach(fake: FakeAppServer, kwargs):
    fake.notification_handler = kwargs.get("notification_handler")
    fake.process_error_handler = kwargs.get("process_error_handler")
    return fake


def _worktree(state_root, task_id):
    path = state_root / "worktrees" / task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _make_server(tmp_path):
    state_root = tmp_path / ".bridge"
    config = BridgeConfig(
        control_repo="owner/bridge",
        trusted_github_logins={"trusted-user"},
        projects={
            "UNetMamba": ProjectConfig(
                capabilities=["code"],
                root=tmp_path,
                repo="owner/UNetMamba",
            )
        },
    )
    config.config_path = tmp_path / "config.local.yaml"
    store = TaskStore(state_root)
    fake_app_server = FakeAppServer(auto_complete=True)
    session_manager = CodexSessionManager(
        config,
        store=store,
        client_factory=lambda **kwargs: _attach(fake_app_server, kwargs),
    )
    supervisor = TaskSupervisor(
        config,
        store=store,
        session_manager=session_manager,
        worktree_factory=lambda project, task_id: _worktree(state_root, task_id),
    )
    return McpStdioServer(BridgeMcpTools(supervisor=supervisor)), supervisor


def test_mcp_client_flow_uses_fake_codex_and_supervisor_boundary(tmp_path):
    server, supervisor = _make_server(tmp_path)
    try:
        outgoing = io.StringIO()
        request_id = 0

        def request(method, params=None):
            nonlocal request_id
            request_id += 1
            message = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            incoming = io.StringIO(json.dumps(message, ensure_ascii=False) + "\n")
            server.serve(incoming, outgoing)
            response = json.loads(outgoing.getvalue().splitlines()[-1])
            return response["result"]

        initialized = request(
            "initialize",
            {
                "protocolVersion": server.protocol_version,
                "clientInfo": {"name": "fake-chatgpt-desktop", "version": "test"},
                "capabilities": {},
            },
        )
        assert initialized["capabilities"]["tools"] == {}
        server.serve(
            io.StringIO(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"),
            outgoing,
        )

        listed = request("tools/list", {})
        assert "bridge_list_projects" in {tool["name"] for tool in listed["tools"]}

        projects = request("tools/call", {"name": "bridge_list_projects", "arguments": {}})
        assert projects["isError"] is False
        assert projects["structuredContent"] == {"projects": [{"id": "UNetMamba", "capabilities": ["code"]}]}

        started = request(
            "tools/call",
            {
                "name": "bridge_start_code_task",
                "arguments": {
                    "project": "UNetMamba",
                    "instruction": "make a safe change",
                    "acceptance": ["tests pass"],
                },
            },
        )
        task_id = started["structuredContent"]["task_id"]
        assert started["structuredContent"]["state"] == "QUEUED"

        future = supervisor.worker_queue.future(task_id)
        assert future is not None
        future.result(timeout=2)

        status = request("tools/call", {"name": "bridge_task_status", "arguments": {"task_id": task_id}})
        assert status["structuredContent"]["state"] == "WAITING_REVIEW"
        assert status["structuredContent"]["thread_id"] == "fake-thread"

        events = request(
            "tools/call",
            {"name": "bridge_task_events", "arguments": {"task_id": task_id, "after_seq": 0, "limit": 100}},
        )
        event_types = {event["type"] for event in events["structuredContent"]["events"]}
        assert {"task_created", "state_changed"}.issubset(event_types)

        controlled = request(
            "tools/call",
            {"name": "bridge_control_task", "arguments": {"task_id": task_id, "action": "accept"}},
        )
        assert controlled["structuredContent"]["state"] == "COMPLETED"
    finally:
        server.close()
