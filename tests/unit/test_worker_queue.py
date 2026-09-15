from __future__ import annotations

from threading import Event

import pytest

from bridge.orchestration.worker import WorkerQueue


def test_worker_queue_submits_callable_and_exposes_future():
    queue = WorkerQueue(max_workers=1)
    try:
        future = queue.submit("task-1", lambda: "done")

        assert queue.future("task-1") is future
        assert future.result(timeout=2) == "done"
        assert future.done() is True
        assert not hasattr(queue, "status")
    finally:
        queue.shutdown(wait=True)


def test_worker_queue_preserves_worker_exception_on_future():
    queue = WorkerQueue(max_workers=1)
    try:
        def fail():
            raise RuntimeError("boom")

        future = queue.submit("task-1", fail)

        assert queue.future("task-1") is future
        with pytest.raises(RuntimeError, match="boom"):
            future.result(timeout=2)
        assert future.exception(timeout=2).args == ("boom",)
    finally:
        queue.shutdown(wait=True)


def test_worker_queue_cancels_a_not_yet_started_future():
    started = Event()
    release = Event()
    queue = WorkerQueue(max_workers=1)
    try:
        running = queue.submit("running", lambda: (started.set(), release.wait(timeout=2)))
        assert started.wait(timeout=2)
        cancelled = queue.submit("cancelled", lambda: "not run")

        assert queue.cancel("cancelled") is True
        assert cancelled.cancelled() is True

        release.set()
        running.result(timeout=2)
    finally:
        release.set()
        queue.shutdown(wait=True)
