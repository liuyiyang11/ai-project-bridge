from __future__ import annotations

import json
from pathlib import Path
import sys
from threading import Event

from bridge.config import AllowedCommand, BridgeConfig, BundleLimits, ProjectConfig
from bridge.orchestration.event_bus import TaskEventBus
from bridge.orchestration.models import TaskResult
from bridge.orchestration.supervisor import TaskSupervisor
from bridge.orchestration.task_runner import TaskRunner
from bridge.orchestration.worker import WorkerQueue
from bridge.store.task_store import TaskStore


class _LateResultHandler:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def execute(self, task: dict) -> TaskResult:
        self.started.set()
        assert self.release.wait(timeout=2)
        return TaskResult(
            success=True,
            review_ready=True,
            message="late result",
            metadata={"late_marker": "must-not-persist"},
            artifacts=[{"path": "late.log", "kind": "logs", "size": 7}],
        )


def _config(tmp_path: Path, argv: list[str] | None = None) -> BridgeConfig:
    config = BridgeConfig(
        control_repo="owner/bridge",
        trusted_github_logins={"trusted-user"},
        python_executable=Path(sys.executable),
        limits=BundleLimits(max_artifact_file_mb=1, max_bundle_mb=5, max_images=5),
        projects={
            "demo": ProjectConfig(
                capabilities=["experiment"],
                root=tmp_path,
                repo="owner/demo",
                allowed_commands={
                    "run": AllowedCommand(argv=argv or ["python", "-c", "pass"]),
                },
            )
        },
    )
    config.config_path = tmp_path / "config.local.yaml"
    return config


def _blocking_supervisor(tmp_path: Path, handler: _LateResultHandler):
    store = TaskStore(tmp_path / ".bridge")
    queue = WorkerQueue(max_workers=1)
    runner = TaskRunner(store=store, handlers={"experiment": handler})
    supervisor = TaskSupervisor(_config(tmp_path), store=store, worker_queue=queue, task_runner=runner)
    return supervisor, queue, store


def test_running_experiment_shutdown_fences_late_result(tmp_path: Path) -> None:
    handler = _LateResultHandler()
    supervisor, queue, store = _blocking_supervisor(tmp_path, handler)
    future = None
    try:
        supervisor.start_experiment_task("demo", "run", task_id="running-experiment")
        future = queue.future("running-experiment")
        assert future is not None
        assert handler.started.wait(timeout=2)
        assert future.running() is True
        assert queue.cancel("running-experiment") is False

        supervisor.close()

        assert store.get_task("running-experiment")["state"] == "INTERRUPTED"
        assert future.done() is False
        supervisor.close()
        handler.release.set()
        result = future.result(timeout=2)

        snapshot = store.get_task("running-experiment")
        events = store.all_events("running-experiment")
        assert result.message == "task interrupted"
        assert snapshot["state"] == "INTERRUPTED"
        assert "late_marker" not in snapshot
        assert json.loads(store.task_path("running-experiment", "result.json").read_text(encoding="utf-8")) == {}
        assert not any(event["type"] == "artifact_created" for event in events)
        assert not any(event.get("data", {}).get("to") in {"WAITING_REVIEW", "COMPLETED", "FAILED"} for event in events if event["type"] == "state_changed")
    finally:
        handler.release.set()
        if future is not None:
            future.result(timeout=2)
        queue.shutdown(wait=True, cancel_futures=True)


def test_late_experiment_failure_cannot_replace_interrupted(tmp_path: Path) -> None:
    class _LateFailureHandler(_LateResultHandler):
        def execute(self, task: dict) -> TaskResult:
            self.started.set()
            assert self.release.wait(timeout=2)
            raise RuntimeError("late failure")

    handler = _LateFailureHandler()
    supervisor, queue, store = _blocking_supervisor(tmp_path, handler)
    future = None
    try:
        supervisor.start_experiment_task("demo", "run", task_id="late-failure")
        future = queue.future("late-failure")
        assert future is not None
        assert handler.started.wait(timeout=2)

        supervisor.close()
        handler.release.set()
        result = future.result(timeout=2)

        assert result.message == "task interrupted"
        assert store.get_task("late-failure")["state"] == "INTERRUPTED"
        assert not any(event.get("data", {}).get("to") == "FAILED" for event in store.all_events("late-failure") if event["type"] == "state_changed")
    finally:
        handler.release.set()
        if future is not None:
            future.result(timeout=2)
        queue.shutdown(wait=True, cancel_futures=True)


def test_running_experiment_subprocess_is_terminated_on_shutdown(tmp_path: Path) -> None:
    child = tmp_path / "blocking_child.py"
    child.write_text(
        "from threading import Event\n"
        "Event().wait()\n",
        encoding="utf-8",
    )
    config = _config(tmp_path, ["python", str(child)])
    store = TaskStore(config.state_root)
    queue = WorkerQueue(max_workers=1)
    supervisor = TaskSupervisor(config, store=store, worker_queue=queue)
    future = None
    runtime = None
    try:
        supervisor.start_experiment_task("demo", "run", task_id="subprocess-experiment")
        future = queue.future("subprocess-experiment")
        assert future is not None
        runtime = supervisor._experiment_runtime.wait_for_runtime("subprocess-experiment", timeout=2)
        assert runtime is not None
        assert runtime.process_attached.wait(timeout=2)
        assert runtime.process is not None

        supervisor.close()
        result = future.result(timeout=2)

        assert result.message == "task interrupted"
        assert store.get_task("subprocess-experiment")["state"] == "INTERRUPTED"
        assert runtime.process.poll() is not None
    finally:
        if future is not None and not future.done():
            supervisor.close()
            runtime = runtime or supervisor._experiment_runtime.current("subprocess-experiment")
            if runtime is not None and runtime.process is not None and runtime.process.poll() is None:
                from bridge.experiments.runtime import terminate_process_bounded

                terminate_process_bounded(runtime.process)
            future.result(timeout=2)
        queue.shutdown(wait=True, cancel_futures=True)


class _FakeProcess:
    def __init__(self, *, timeout_once: bool = False) -> None:
        self.timeout_once = timeout_once
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: list[float | None] = []
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self._returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.timeout_once and self.terminate_calls and self.kill_calls == 0:
            from subprocess import TimeoutExpired

            raise TimeoutExpired("fake", timeout)
        self._returncode = self._returncode if self._returncode is not None else -15
        return self._returncode


def test_runtime_terminate_success_and_kill_fallback() -> None:
    from bridge.experiments.runtime import ExperimentRuntimeRegistry

    registry = ExperimentRuntimeRegistry(cleanup_timeout=0.01)
    success = _FakeProcess()
    registry.register("success")
    registry.attach_process("success", success)
    assert registry.request_cancel("success") is True
    assert success.terminate_calls == 1
    assert success.kill_calls == 0

    fallback = _FakeProcess(timeout_once=True)
    registry.register("fallback")
    registry.attach_process("fallback", fallback)
    assert registry.request_cancel("fallback") is True
    assert fallback.terminate_calls == 1
    assert fallback.kill_calls == 1


def test_runtime_cancellation_before_process_attach_is_not_lost() -> None:
    from bridge.experiments.runtime import ExperimentRuntimeRegistry

    registry = ExperimentRuntimeRegistry(cleanup_timeout=0.01)
    runtime = registry.register("race")
    assert registry.request_cancel("race") is True
    process = _FakeProcess()

    registry.attach_process("race", process)

    assert runtime.cancel_event.is_set() is True
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


def test_restart_recovery_still_marks_running_experiment_unknown(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = TaskStore(config.state_root)
    store.create_task("recovered", project="demo", task_type="experiment", command_id="run")
    bus = TaskEventBus(store, "recovered")
    bus.transition("QUEUED", "PREPARING", "previous process")
    bus.transition("PREPARING", "RUNNING", "previous process")

    queue = WorkerQueue(max_workers=1)
    supervisor = TaskSupervisor(config, store=store, worker_queue=queue)
    try:
        assert store.get_task("recovered")["state"] == "UNKNOWN"
    finally:
        supervisor.close()
        queue.shutdown(wait=True, cancel_futures=True)
