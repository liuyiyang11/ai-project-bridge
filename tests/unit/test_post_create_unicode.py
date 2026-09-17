from __future__ import annotations

from types import SimpleNamespace

import pytest

from bridge.codex.runner import CodexRunner
from bridge.codex.session import CodexSessionManager, SessionRecord, SessionState
from bridge.dispatcher import Dispatcher
from bridge.orchestration.event_bus import TaskEventBus
from bridge.orchestration.models import TaskResult
from bridge.orchestration.task_runner import TaskRunner
from bridge.security import SecurityError
from bridge.store.task_store import TaskStore


HIGH_SURROGATE = "bad\ud800"
LOW_SURROGATE = "bad\udc00"


def _store(tmp_path, task_id="task-1"):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task(task_id, project="demo", task_type="code", instruction="safe")
    return store


def _snapshot(store: TaskStore, task_id="task-1"):
    directory = store.task_dir(task_id)
    return {name: (directory / name).read_bytes() for name in TaskStore._FILES}


def _running(store: TaskStore, task_id="task-1"):
    bus = TaskEventBus(store, task_id)
    bus.transition("QUEUED", "PREPARING", "prepare")
    bus.transition("PREPARING", "RUNNING", "run")
    return bus


@pytest.mark.parametrize(
    "value",
    ["ASCII", "中文", "emoji 🚀✅", "replacement �", "combining e\u0301", "non-BMP 𝄞"],
)
def test_valid_post_create_text_remains_supported(tmp_path, value):
    store = _store(tmp_path)

    store.append_event("task-1", {"type": "message", "data": {"value": value}})
    store.update_task("task-1", metadata={"value": value})
    store.write_result("task-1", {"message": value, "nested": [value]})
    store.append_stdout("task-1", value)
    store.append_stderr("task-1", value)

    assert value in store.task_path("task-1", "stdout.log").read_text(encoding="utf-8")
    assert value in store.task_path("task-1", "stderr.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("value", [HIGH_SURROGATE, LOW_SURROGATE])
def test_append_event_rejects_surrogate_before_mutation(tmp_path, value):
    store = _store(tmp_path)
    before = _snapshot(store)

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        store.append_event("task-1", {"type": "message", "data": {"value": value}})

    assert _snapshot(store) == before
    assert store.get_task("task-1")["last_event_seq"] == 0


@pytest.mark.parametrize("method", ["append_stdout", "append_stderr"])
@pytest.mark.parametrize("value", [HIGH_SURROGATE, LOW_SURROGATE])
def test_append_log_rejects_surrogate_before_open(tmp_path, method, value):
    store = _store(tmp_path)
    name = "stdout.log" if method == "append_stdout" else "stderr.log"
    before = store.task_path("task-1", name).read_bytes()

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        getattr(store, method)("task-1", value)

    assert store.task_path("task-1", name).read_bytes() == before


def test_update_task_rejects_nested_surrogate_without_snapshot_mutation(tmp_path):
    store = _store(tmp_path)
    before = _snapshot(store)

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        store.update_task("task-1", metadata={"nested": [HIGH_SURROGATE]})

    assert _snapshot(store) == before


def test_update_task_missing_yaml_does_not_create_empty_compat_file(tmp_path):
    store = _store(tmp_path)
    yaml_path = store.task_path("task-1", "task.yaml")
    yaml_path.unlink()

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        store.update_task("task-1", instruction=HIGH_SURROGATE)

    assert not yaml_path.exists()


def test_write_result_rejects_nested_surrogate_before_replacing_result(tmp_path):
    store = _store(tmp_path)
    before = store.task_path("task-1", "result.json").read_bytes()

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        store.write_result("task-1", {"nested": {"message": LOW_SURROGATE}})

    assert store.task_path("task-1", "result.json").read_bytes() == before


@pytest.mark.parametrize("field", ["path", "kind", "source", "hash"])
def test_save_artifacts_rejects_surrogate_in_persisted_field(tmp_path, field):
    store = _store(tmp_path)
    artifact = {"path": "artifact.txt", "kind": "file", "source": "source.txt", "hash": "hash"}
    artifact[field] = HIGH_SURROGATE
    before = store.task_path("task-1", "result.json").read_bytes()

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        store.save_artifacts("task-1", [artifact])

    assert store.task_path("task-1", "result.json").read_bytes() == before


def test_save_artifacts_preserves_existing_ignored_field_contract(tmp_path):
    store = _store(tmp_path)

    store.save_artifacts(
        "task-1",
        [{"path": "artifact.txt", "kind": "file", "description": HIGH_SURROGATE}],
    )

    assert "description" not in store.task_path("task-1", "result.json").read_text(encoding="utf-8")


def test_persist_result_preflights_metadata_and_artifacts_as_one_operation(tmp_path):
    store = _store(tmp_path)
    bus = _running(store)
    before = _snapshot(store)

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        bus.persist_result_if_active(
            metadata={"safe_metadata": "must not persist"},
            artifacts=[{"path": HIGH_SURROGATE, "kind": "file"}],
        )

    assert _snapshot(store) == before
    assert "safe_metadata" not in store.get_task("task-1")
    events = store.all_events("task-1")
    assert len(events) == 2
    assert all(event["type"] != "artifact_created" for event in events)


def test_transition_reason_rejects_surrogate_before_event_or_state_change(tmp_path):
    store = _store(tmp_path)
    bus = TaskEventBus(store, "task-1")
    before = _snapshot(store)

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        bus.transition("QUEUED", "PREPARING", HIGH_SURROGATE)

    assert _snapshot(store) == before
    assert store.get_task("task-1")["state"] == "QUEUED"
    assert store.all_events("task-1") == []


def test_begin_run_rejects_bad_thread_before_creating_run_file(tmp_path):
    store = _store(tmp_path)
    before = _snapshot(store)

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        store.begin_run("task-1", "initial", requested_thread_id=LOW_SURROGATE)

    assert _snapshot(store) == before
    assert not (store.task_dir("task-1") / "runs").exists()


def test_finish_run_rejects_bad_error_before_snapshot_mutation(tmp_path):
    store = _store(tmp_path)
    run = store.begin_run("task-1", "initial")
    before = _snapshot(store)

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        store.finish_run("task-1", run["number"], error=HIGH_SURROGATE)

    assert _snapshot(store) == before


def test_task_runner_rejects_bad_result_and_marks_task_failed_safely(tmp_path):
    store = _store(tmp_path)

    class Handler:
        def execute(self, task):
            return TaskResult(success=True, review_ready=True, message="safe", metadata={"nested": [HIGH_SURROGATE]})

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        TaskRunner(store=store, handlers={"code": Handler()}).run("task-1")

    state = store.get_task("task-1")
    assert state["state"] == "FAILED"
    assert "input contains invalid Unicode scalar value" in state["last_error"]
    assert HIGH_SURROGATE not in state["last_error"]
    assert all(HIGH_SURROGATE not in repr(event) for event in store.all_events("task-1"))


def test_codex_escaped_surrogate_is_rejected_before_run_event_append(tmp_path):
    class Process:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return '{"type":"turn.completed","final_message":"bad\\ud800"}\n', ""

    runner = CodexRunner(popen=lambda *args, **kwargs: Process(), available=True)
    events = tmp_path / "runs" / "001-initial.events.jsonl"

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        runner.start_task("safe", tmp_path, events)

    assert not events.exists()


def test_codex_session_rejects_bad_event_before_run_event_append(tmp_path):
    manager = CodexSessionManager()
    events = tmp_path / "runs" / "001-initial.events.jsonl"
    manager.sessions["task-1"] = SessionRecord(
        task_id="task-1",
        project="demo",
        worktree=tmp_path,
        state=SessionState.RUNNING,
        events_path=events,
    )

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        manager._on_event("task-1", {"method": "item/agentMessage/delta", "params": {"delta": LOW_SURROGATE}})

    assert not events.exists()


def test_dispatcher_bundle_preflights_manifest_before_mkdir(tmp_path):
    store = _store(tmp_path, 1)
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher.store = store
    issue = SimpleNamespace(number=1)
    task = SimpleNamespace(task_type="code", project="demo", source_issue=None)

    with pytest.raises(SecurityError, match=r"^input contains invalid Unicode scalar value$"):
        dispatcher._write_bundle(issue, task, {"artifact_list": [{"path": HIGH_SURROGATE}]})

    assert not (store.task_dir(1) / "review_bundle").exists()
