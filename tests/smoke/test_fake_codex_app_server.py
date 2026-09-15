from __future__ import annotations

from bridge.codex.fake_app_server import FakeAppServer
from bridge.codex.session import CodexSessionManager
from bridge.orchestration.supervisor import TaskSupervisor
from bridge.store.task_store import TaskStore

from tests.unit.test_supervisor_async import ManualQueue, _attach, _worktree
from tests.unit.test_supervisor_async import make_supervisor as _make_supervisor


def test_fake_codex_app_server_completes_full_async_bridge_chain(tmp_path):
    supervisor, queue, store = _make_supervisor(tmp_path)
    task_id = supervisor.start_code_task("demo", "make a safe change")["task_id"]

    queue.run_submitted(task_id)

    snapshot = store.get_task(task_id)
    assert snapshot["state"] == "WAITING_REVIEW"
    assert snapshot["thread_id"] == "fake-thread"
    assert snapshot["turn_id"] == "fake-turn-1"
    event_types = {item["type"] for item in store.list_events(task_id, after_seq=0, limit=100)}
    assert {"task_created", "state_changed", "thread_started", "turn_started", "agent_message", "turn_completed"} <= event_types
