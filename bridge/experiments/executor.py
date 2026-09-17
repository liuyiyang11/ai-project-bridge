from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from ..collectors.artifact_collector import collect_artifacts
from ..commands import CommandExecutionError, run_registered_command
from ..security import SecurityError, ensure_safe_project_name, ensure_safe_relative_path, resolve_under
from ..store.task_store import TaskStore
from .artifacts import ArtifactContract
from .metrics import MetricCollector
from .runtime import ExperimentRuntime, ExperimentRuntimeRegistry


class ExperimentExecutor:
    """Run one pre-registered experiment command and collect public outputs."""

    def __init__(self, config: Any, store: TaskStore, *, runtime_registry: ExperimentRuntimeRegistry | None = None):
        self.config = config
        self.store = store
        self.runtime_registry = runtime_registry or ExperimentRuntimeRegistry()

    def execute(self, task: dict[str, Any]) -> TaskResult:
        from ..orchestration.models import TaskResult

        project_name, command_id, task_id = self._validate_task(task)
        project = self.config.project(project_name)
        if "experiment" not in getattr(project, "capabilities", []):
            raise ValueError(f"project {project_name!r} does not support task type 'experiment'")
        if command_id not in project.allowed_commands:
            raise CommandExecutionError(f"command is not allowed or not registered: {command_id}")
        project_root = Path(project.root).resolve()
        if not project_root.is_dir():
            raise RuntimeError(f"registered project root does not exist: {project_root}")

        runtime = self.runtime_registry.current(task_id)
        owns_runtime = runtime is None
        if runtime is None:
            runtime = self.runtime_registry.register(task_id)
        try:
            if self._cancel_requested(task_id, runtime):
                return self._interrupted_result()
            command_result = run_registered_command(
                project,
                command_id,
                project_root,
                self.config.python_executable,
                runtime=runtime,
                runtime_registry=self.runtime_registry,
            )
            if self._cancel_requested(task_id, runtime):
                return self._interrupted_result()

            stdout = command_result.stdout or ""
            stderr = command_result.stderr or ""
            self.store.append_stdout(task_id, stdout)
            self.store.append_stderr(task_id, stderr)

            bundle_dir = self.store.task_dir(task_id) / "experiment_bundle"
            logs_dir = bundle_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            max_log_bytes = int(self.config.limits.max_artifact_file_mb) * 1024 * 1024
            stdout_size = self._write_log(logs_dir / "stdout.log", stdout, max_log_bytes)
            stderr_size = self._write_log(logs_dir / "stderr.log", stderr, max_log_bytes)
            log_artifacts = [
                ArtifactContract.log_artifact("logs/stdout.log", stdout_size),
                ArtifactContract.log_artifact("logs/stderr.log", stderr_size),
            ]

            if command_result.returncode != 0:
                raise RuntimeError(f"configured experiment command failed with code {command_result.returncode}")

            collected = collect_artifacts(project_root, project.artifact_dirs, bundle_dir, self.config.limits)
            artifacts = log_artifacts + ArtifactContract.from_records(collected)
            metric_sources = [
                resolve_under(project_root, item["source"])
                for item in collected
                if isinstance(item.get("source"), str)
                and self._is_metrics_artifact(item["source"])
            ]
            metric_limit = int(self.config.limits.max_artifact_file_mb) * 1024 * 1024
            metrics = MetricCollector(max_file_bytes=metric_limit).collect(metric_sources, root=project_root)
            return TaskResult(
                success=True,
                review_ready=True,
                message=f"Experiment command {command_id} completed; collected {len(artifacts)} artifacts.",
                artifacts=artifacts,
                metadata={
                    "experiment_command_id": command_id,
                    "experiment_returncode": int(command_result.returncode),
                    "artifact_contract": ArtifactContract.VERSION,
                    "metrics": metrics,
                },
            )
        finally:
            if owns_runtime:
                self.runtime_registry.detach(task_id)

    def _cancel_requested(self, task_id: str, runtime: ExperimentRuntime) -> bool:
        return runtime.cancel_event.is_set() or str(self.store.get_task(task_id).get("state", "")) == "INTERRUPTED"

    @staticmethod
    def _interrupted_result() -> TaskResult:
        from ..orchestration.models import TaskResult

        return TaskResult(success=True, review_ready=False, message="task interrupted")

    @staticmethod
    def _validate_task(task: dict[str, Any]) -> tuple[str, str, str]:
        if not isinstance(task, dict):
            raise ValueError("experiment task must be an object")
        if task.get("task_type") != "experiment":
            raise ValueError("experiment executor received an invalid task type")
        task_id = task.get("task_id")
        project = task.get("project")
        command_id = task.get("command_id")
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task_id):
            raise ValueError("experiment task requires a task_id")
        if not isinstance(project, str) or not project.strip():
            raise ValueError("experiment task requires a project")
        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("experiment task requires a command_id")
        try:
            project_name = ensure_safe_project_name(project.strip())
        except SecurityError as exc:
            raise ValueError(str(exc)) from exc
        command_name = command_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", command_name):
            raise ValueError("experiment task command_id must be a safe registered identifier")
        return project_name, command_name, task_id

    @staticmethod
    def _write_log(path: Path, text: str, max_bytes: int) -> int:
        encoded = text.encode("utf-8", errors="replace")
        bounded = encoded[:max_bytes]
        path.write_bytes(bounded)
        return len(bounded)

    @staticmethod
    def _is_metrics_artifact(source: str) -> bool:
        try:
            safe = ensure_safe_relative_path(source).replace("\\", "/")
        except SecurityError:
            return False
        return Path(safe).suffix.casefold() in {".json", ".csv", ".txt"} and "confusion" not in safe.casefold()
