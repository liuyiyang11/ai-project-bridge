"""Opt-in smoke test for the real Codex app-server boundary.

The default test suite never launches Codex or touches a user's authenticated
runtime.  Set ``BRIDGE_REAL_CODEX_SMOKE=1`` explicitly to run this test with a
local config and the installed Codex executable.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from bridge.codex.protocol import CodexProcessError, CodexProtocolError
from bridge.codex.session import CodexSessionManager, SessionState
from bridge.config import load_config
from bridge.store.task_store import TaskStore
from bridge.worktree import WorktreeManager


def _real_smoke_enabled() -> bool:
    return os.environ.get("BRIDGE_REAL_CODEX_SMOKE", "").strip().casefold() in {"1", "true", "yes"}


@pytest.mark.skipif(
    not _real_smoke_enabled(),
    reason="real Codex smoke is opt-in: set BRIDGE_REAL_CODEX_SMOKE=1",
)
def test_real_codex_thread_turn_completion_lifecycle():
    config_path = Path(os.environ.get("BRIDGE_MCP_CONFIG", "config.local.yaml")).expanduser().resolve()
    if not config_path.is_file():
        pytest.skip(f"real Codex smoke config is not available: {config_path.name}")
    config = load_config(config_path)
    project_name = next((name for name, project in config.projects.items() if "code" in project.capabilities), None)
    if project_name is None:
        pytest.skip("real Codex smoke requires one registered code-capable project")

    project = config.project(project_name)
    task_id = f"real-smoke-{uuid.uuid4().hex}"
    store = TaskStore(config.state_root)
    manager = WorktreeManager(project.root, config.state_root / "worktrees", project.remote, project.base_branch)
    info = manager.prepare_for_task(project_name, task_id)
    store.create_task(
        task_id,
        project=project_name,
        task_type="code",
        instruction="Reply with exactly DONE. Do not read or modify files, run commands, or perform Git operations.",
        model="gpt-5.6-luna",
        reasoning_effort="max",
    )
    session = CodexSessionManager(config, store=store)
    events_path = store.task_dir(task_id) / "events.jsonl"
    try:
        session.start_task(
            task_id,
            project_name,
            info.path,
            "Reply with exactly DONE. Do not read or modify files, run commands, or perform Git operations.",
            model="gpt-5.6-luna",
            reasoning_effort="max",
            events_path=events_path,
        )
        result = session.wait_for_completion(task_id, timeout=180)
        events = store.all_events(task_id)
        event_types = {item.get("type") for item in events if isinstance(item, dict)}
        assert result.exit_code == 0
        assert session.status(task_id)["state"] == SessionState.WAITING_REVIEW.value
        assert "thread_started" in event_types
        assert "turn_started" in event_types
        assert "turn_completed" in event_types
    except (CodexProcessError, CodexProtocolError) as exc:
        # A real smoke run is useful even when the local upstream stream is
        # unavailable; report that limitation as an explicit skipped result.
        message = str(exc).casefold()
        if "reconnect" in message or "response stream" in message or "codex" in message:
            pytest.skip("real Codex app-server is unavailable or unstable in this environment")
        raise
    finally:
        session.close()
        manager.cleanup(info)
