from __future__ import annotations

from types import SimpleNamespace

import pytest

from bridge.executors.code import RuntimeCodeExecutor
from bridge.store.task_store import TaskStore


def test_runtime_executor_rejects_a_recovery_worktree_outside_bridge_root(tmp_path):
    state_root = tmp_path / ".bridge"
    store = TaskStore(state_root)
    store.create_task("task-1", project="demo", task_type="code", instruction="resume safely")
    outside = tmp_path / "outside-worktree"
    outside.mkdir()
    executor = RuntimeCodeExecutor(
        SimpleNamespace(state_root=state_root),
        store,
        session_manager=object(),
        worktree_preparer=lambda project, task_id: pytest.fail("recovery must not prepare a new worktree"),
    )

    with pytest.raises(RuntimeError, match="trusted Bridge worktree"):
        executor.execute(
            {
                "task_id": "task-1",
                "project": "demo",
                "instruction": "resume safely",
                "resume_thread_id": "saved-thread",
                "worktree_path": str(outside),
            }
        )

