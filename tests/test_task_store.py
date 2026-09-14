import json

from bridge.task_store import TaskStore


def test_initialize_creates_complete_task_directory(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.initialize(42, "version: 1\ntask_type: code\n", {"status": "ready", "project": "demo"})

    task_dir = tmp_path / ".bridge" / "tasks" / "42"
    assert all((task_dir / name).is_file() for name in ["task.yaml", "state.json", "events.jsonl", "stdout.log", "stderr.log", "result.json"])
    assert store.load_state(42)["status"] == "ready"


def test_state_updates_are_atomic_and_events_are_jsonl(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.initialize(1, "task", {"status": "ready"})

    store.update_state(1, status="running", thread_id="thread-1")
    store.append_event(1, {"type": "started", "thread_id": "thread-1"})
    store.append_event(1, {"type": "completed", "exit_code": 0})

    assert store.load_state(1)["thread_id"] == "thread-1"
    events = [json.loads(line) for line in store.task_path(1, "events.jsonl").read_text().splitlines()]
    assert [event["type"] for event in events] == ["started", "completed"]


def test_start_claim_and_rework_comment_are_idempotent(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.initialize(7, "task", {"status": "ready"})

    assert store.claim_new(7) is True
    assert store.claim_new(7) is False
    store.update_state(7, status="review")
    assert store.claim_rework_comment(7, "comment-1") is True
    assert store.claim_rework_comment(7, "comment-1") is False
    store.update_state(7, status="review")
    assert store.claim_rework_comment(7, "comment-2") is True
