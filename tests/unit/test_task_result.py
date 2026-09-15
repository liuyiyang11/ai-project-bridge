from __future__ import annotations

from bridge.orchestration.models import TaskResult


def test_task_result_represents_successful_review_ready_execution():
    result = TaskResult(
        success=True,
        review_ready=True,
        message="Codex turn completed",
        artifacts=[{"path": "bridge/example.py", "kind": "file", "size": 12}],
        metadata={"thread_id": "thread-1"},
    )

    assert result.success is True
    assert result.review_ready is True
    assert result.message == "Codex turn completed"
    assert result.artifacts == [{"path": "bridge/example.py", "kind": "file", "size": 12}]
    assert result.metadata == {"thread_id": "thread-1"}


def test_task_result_represents_a_handled_failure_without_review():
    result = TaskResult(success=False, message="Codex reported a failed turn")

    assert result.success is False
    assert result.review_ready is False
    assert result.message == "Codex reported a failed turn"
    assert result.artifacts == []
    assert result.metadata == {}


def test_task_result_defaults_do_not_share_artifacts_or_metadata():
    first = TaskResult(success=True)
    second = TaskResult(success=True)

    first.artifacts.append({"path": "first.py"})
    first.metadata["thread_id"] = "thread-1"

    assert second.artifacts == []
    assert second.metadata == {}
