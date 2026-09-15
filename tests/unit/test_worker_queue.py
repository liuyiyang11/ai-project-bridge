from __future__ import annotations

import pytest

from bridge.orchestration.worker import WorkerQueue


def test_worker_queue_submits_callable_and_exposes_future():
    queue = WorkerQueue(max_workers=1)
    try:
        future = queue.submit("task-1", lambda: "done")
        assert future.result(timeout=2) == "done"
        assert queue.status("task-1")["state"] == "COMPLETED"
    finally:
        queue.shutdown(wait=True)


def test_worker_queue_preserves_worker_exception_on_future():
    queue = WorkerQueue(max_workers=1)
    try:
        def fail():
            raise RuntimeError("boom")

        future = queue.submit("task-1", fail)
        with pytest.raises(RuntimeError, match="boom"):
            future.result(timeout=2)
        assert queue.status("task-1")["state"] == "FAILED"
    finally:
        queue.shutdown(wait=True)

