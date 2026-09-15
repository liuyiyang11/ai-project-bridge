from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Mapping, Optional, Protocol, Union

from pydantic import Field, StrictStr, root_validator, validator

from ..commands import CommandExecutionError, run_registered_command
from ..security import SecurityError, ensure_safe_project_name, resolve_under
from ..store.task_store import TaskStore
from ._base import ResearchModel, validate_identifier, validate_mapping
from .artifact import Artifact
from .dataset import DatasetManifest
from .metric import MetricRecord


class DataTaskValidationError(ValueError):
    """Raised when a data task cannot be safely validated or materialized."""


class DataTaskSpec(ResearchModel):
    """Validated input/output contract for one data-processing task.

    ``processing_command`` is a registered command identifier, never a shell
    command or an argv supplied by the task author.
    """

    task_id: StrictStr = Field(min_length=1, max_length=128)
    dataset_manifest: DatasetManifest
    input_artifacts: List[Artifact] = Field(..., min_items=1, max_items=1000)
    processing_command: StrictStr = Field(min_length=1, max_length=128)
    output_artifacts: List[Artifact] = Field(..., min_items=1, max_items=1000)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    _validate_task_id = validator("task_id", allow_reuse=True)(
        lambda value, field: validate_identifier(value, field.name)
    )
    _validate_processing_command = validator("processing_command", allow_reuse=True)(
        lambda value, field: validate_identifier(value, field.name)
    )

    @validator("metadata")
    def validate_metadata(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        return validate_mapping(value, "metadata")

    @root_validator
    def validate_artifact_linkage(cls, values: dict[str, Any]) -> dict[str, Any]:
        task_id = values.get("task_id")
        input_artifacts = values.get("input_artifacts") or []
        output_artifacts = values.get("output_artifacts") or []
        if not task_id:
            return values

        for artifact in output_artifacts:
            if artifact.task_id != task_id:
                raise ValueError("output artifact task_id must match data task task_id")

        for label, artifacts in (("input", input_artifacts), ("output", output_artifacts)):
            ids = [artifact.artifact_id for artifact in artifacts]
            paths = [artifact.path for artifact in artifacts]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} artifacts must have unique artifact_id values")
            if len(paths) != len(set(paths)):
                raise ValueError(f"{label} artifacts must have unique paths")

        dataset_manifest = values.get("dataset_manifest")
        if dataset_manifest is not None:
            for artifact in input_artifacts:
                declared_dataset_id = artifact.metadata.get("dataset_id")
                if declared_dataset_id is not None and declared_dataset_id != dataset_manifest.dataset_id:
                    raise ValueError("input artifact dataset_id does not match dataset_manifest")
        return values


class DataAdapter(Protocol):
    """Adapter boundary for registered data commands."""

    def run(
        self,
        *,
        project: Any,
        command_id: str,
        cwd: Path,
        spec: DataTaskSpec,
        python_executable: Optional[Path],
    ) -> subprocess.CompletedProcess[str]:
        ...


class RegisteredDataAdapter:
    """Run the project's already-registered command through the safe runner."""

    def run(
        self,
        *,
        project: Any,
        command_id: str,
        cwd: Path,
        spec: DataTaskSpec,
        python_executable: Optional[Path],
    ) -> subprocess.CompletedProcess[str]:
        del spec
        return run_registered_command(project, command_id, cwd, python_executable)


class FakeDataAdapter:
    """Deterministic in-memory test adapter that writes declared output files.

    It never invokes a process.  The handler still verifies the command ID
    against ``allowed_commands`` before this adapter is called, so tests cover
    the same registration boundary as the real adapter.
    """

    def __init__(self, *, returncode: int = 0, skip_outputs: bool = False):
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            raise ValueError("returncode must be an integer")
        self.returncode = returncode
        self.skip_outputs = skip_outputs
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        *,
        project: Any,
        command_id: str,
        cwd: Path,
        spec: DataTaskSpec,
        python_executable: Optional[Path],
    ) -> subprocess.CompletedProcess[str]:
        del python_executable
        if command_id not in project.allowed_commands:
            raise CommandExecutionError(f"command is not allowed or not registered: {command_id}")
        self.calls.append({"command_id": command_id, "cwd": Path(cwd), "task_id": spec.task_id})
        if self.returncode != 0:
            return subprocess.CompletedProcess(
                args=[command_id],
                returncode=self.returncode,
                stdout="fake data processing failed",
                stderr="fake adapter failure",
            )
        if not self.skip_outputs:
            for artifact in spec.output_artifacts:
                output_path = resolve_under(Path(cwd), artifact.path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    json.dumps(
                        {
                            "source_dataset_id": spec.dataset_manifest.dataset_id,
                            "artifact_id": artifact.artifact_id,
                            "processed_by": command_id,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
        return subprocess.CompletedProcess(
            args=[command_id],
            returncode=0,
            stdout="fake data processing completed",
            stderr="",
        )


class DataTaskHandler:
    """Execute a validated data task below the existing TaskRunner boundary."""

    def __init__(
        self,
        config: Any,
        store: Optional[TaskStore] = None,
        adapter: Optional[DataAdapter] = None,
        *,
        default_project: Optional[str] = None,
    ):
        self.config = config
        self.store = store
        self.adapter = adapter or RegisteredDataAdapter()
        self.default_project = default_project

    def execute(self, task: Union[Mapping[str, Any], DataTaskSpec]) -> Any:
        """Validate and execute one runtime task mapping.

        The method accepts the shared runtime shape: ``task_type=data``,
        ``project``, and ``data_task_spec``.  A top-level ``command`` or
        ``argv`` is rejected explicitly; only the spec's registered command
        identifier is accepted.
        """

        from ..orchestration.models import TaskResult

        if isinstance(task, DataTaskSpec):
            if self.default_project is None:
                raise DataTaskValidationError(
                    "direct DataTaskSpec execution requires default_project"
                )
            task = {
                "task_type": "data",
                "task_id": task.task_id,
                "project": self.default_project,
                "data_task_spec": task,
            }
        spec, project_name = self._validate_task(task)
        project = self.config.project(project_name)
        if spec.processing_command not in project.allowed_commands:
            raise CommandExecutionError(
                f"command is not allowed or not registered: {spec.processing_command}"
            )
        project_root = Path(project.root).resolve()
        if not project_root.is_dir():
            raise DataTaskValidationError(f"registered project root does not exist: {project_root}")

        input_paths = self._validate_inputs(spec, project_root)
        output_paths = self._validate_output_declarations(spec, project_root)
        started = time.perf_counter()
        command_result = self.adapter.run(
            project=project,
            command_id=spec.processing_command,
            cwd=project_root,
            spec=spec,
            python_executable=getattr(self.config, "python_executable", None),
        )
        stdout = getattr(command_result, "stdout", "") or ""
        stderr = getattr(command_result, "stderr", "") or ""
        self._persist_logs(spec.task_id, stdout, stderr)
        returncode = getattr(command_result, "returncode", None)
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            raise DataTaskValidationError("data adapter returned an invalid return code")
        if returncode != 0:
            return TaskResult(
                success=False,
                message=f"data processing command {spec.processing_command!r} failed with code {returncode}",
                metadata={
                    "data_task_stage": "RUNNING",
                    "data_task_error": "command_failed",
                    "data_task_returncode": returncode,
                },
            )

        self._mark_validating(spec.task_id, spec.processing_command)
        self._validate_outputs(output_paths)
        elapsed = max(time.perf_counter() - started, 0.0)
        artifacts = self._build_artifacts(spec, output_paths)
        metrics = self._build_metrics(spec, len(input_paths), len(artifacts), elapsed)
        observation = self._build_observation(spec, artifacts, metrics, returncode)
        serialized_spec = self._json_dict(spec)
        serialized_artifacts = [self._json_dict(item) for item in artifacts]
        serialized_metrics = [self._json_dict(item) for item in metrics]
        serialized_observation = self._json_dict(observation)
        manifest = [
            {
                "artifact_id": artifact.artifact_id,
                "type": artifact.type,
                "kind": artifact.type,
                "path": artifact.path,
                "size": output_paths[artifact.artifact_id].stat().st_size,
                "metadata": artifact.metadata,
            }
            for artifact in artifacts
        ]
        return TaskResult(
            success=True,
            review_ready=True,
            message=f"Data task {spec.task_id} completed; validated {len(artifacts)} output artifacts.",
            artifacts=manifest,
            metadata={
                "data_task_stage": "VALIDATED",
                "data_task_spec": serialized_spec,
                "artifacts": serialized_artifacts,
                "data_task_artifacts": serialized_artifacts,
                "data_task_observation": serialized_observation,
                "metrics": serialized_metrics,
                "observation": serialized_observation,
                "data_task_returncode": returncode,
            },
        )

    def _validate_task(self, task: Mapping[str, Any]) -> tuple[DataTaskSpec, str]:
        if not isinstance(task, Mapping):
            raise DataTaskValidationError("data task must be an object")
        if task.get("task_type") != "data":
            raise DataTaskValidationError("data task handler received an invalid task type")
        if "command" in task or "argv" in task:
            raise DataTaskValidationError("data tasks cannot provide command or argv")
        raw_spec = task.get("data_task_spec", task.get("data_task"))
        if raw_spec is None:
            raw_spec = {
                field_name: task.get(field_name)
                for field_name in DataTaskSpec.__fields__
                if field_name in task
            }
        try:
            spec = raw_spec if isinstance(raw_spec, DataTaskSpec) else DataTaskSpec.parse_obj(raw_spec)
        except (TypeError, ValueError) as exc:
            raise DataTaskValidationError(f"invalid data task contract: {exc}") from exc
        task_id = task.get("task_id")
        if task_id != spec.task_id:
            raise DataTaskValidationError("runtime task_id must match data task spec task_id")
        project_name = task.get("project") or self.default_project
        if not isinstance(project_name, str):
            raise DataTaskValidationError("data task requires a project")
        try:
            project_name = ensure_safe_project_name(project_name.strip())
        except SecurityError as exc:
            raise DataTaskValidationError(str(exc)) from exc
        return spec, project_name

    @staticmethod
    def _validate_inputs(spec: DataTaskSpec, project_root: Path) -> list[Path]:
        paths: list[Path] = []
        for artifact in spec.input_artifacts:
            try:
                path = resolve_under(project_root, artifact.path)
            except SecurityError as exc:
                raise DataTaskValidationError(str(exc)) from exc
            if not path.is_file():
                raise DataTaskValidationError(f"input artifact does not exist: {artifact.path}")
            paths.append(path)
        return paths

    @staticmethod
    def _validate_output_declarations(spec: DataTaskSpec, project_root: Path) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        for artifact in spec.output_artifacts:
            try:
                paths[artifact.artifact_id] = resolve_under(project_root, artifact.path)
            except SecurityError as exc:
                raise DataTaskValidationError(str(exc)) from exc
        return paths

    def _persist_logs(self, task_id: str, stdout: str, stderr: str) -> None:
        if self.store is None:
            return
        self.store.append_stdout(task_id, str(stdout))
        self.store.append_stderr(task_id, str(stderr))

    def _mark_validating(self, task_id: str, command_id: str) -> None:
        if self.store is None:
            return
        self.store.update_task(task_id, data_task_stage="VALIDATING")
        from ..orchestration.event_bus import TaskEventBus

        TaskEventBus(self.store, task_id).emit(
            "data_task_validating",
            {"stage": "VALIDATING", "command_id": command_id},
        )

    @staticmethod
    def _validate_outputs(output_paths: Mapping[str, Path]) -> None:
        missing = [artifact_id for artifact_id, path in output_paths.items() if not path.is_file()]
        if missing:
            raise DataTaskValidationError(f"output artifacts do not exist: {', '.join(missing)}")

    @staticmethod
    def _build_artifacts(spec: DataTaskSpec, output_paths: Mapping[str, Path]) -> list[Artifact]:
        created_at = datetime.now(timezone.utc)
        artifacts: list[Artifact] = []
        for declaration in spec.output_artifacts:
            metadata = dict(declaration.metadata)
            metadata.setdefault("dataset_id", spec.dataset_manifest.dataset_id)
            artifacts.append(
                Artifact(
                    artifact_id=declaration.artifact_id,
                    task_id=spec.task_id,
                    type=declaration.type,
                    path=declaration.path,
                    metadata=metadata,
                    created_at=created_at,
                )
            )
        return artifacts

    @staticmethod
    def _build_metrics(
        spec: DataTaskSpec,
        input_count: int,
        output_count: int,
        processing_time: float,
    ) -> list[MetricRecord]:
        metadata = {"dataset_id": spec.dataset_manifest.dataset_id}
        return [
            MetricRecord(name="coverage", value=1.0 if output_count else 0.0, unit="ratio", task_id=spec.task_id, metadata=metadata),
            MetricRecord(name="file_count", value=output_count, unit="files", task_id=spec.task_id, metadata={**metadata, "input_file_count": input_count}),
            MetricRecord(name="processing_time", value=processing_time, unit="seconds", task_id=spec.task_id, metadata=metadata),
        ]

    @staticmethod
    def _build_observation(
        spec: DataTaskSpec,
        artifacts: list[Artifact],
        metrics: list[MetricRecord],
        returncode: int,
    ) -> Any:
        from .evidence import ExperimentObservation

        observation_id = f"{spec.task_id[:124]}-obs"
        return ExperimentObservation(
            observation_id=observation_id,
            experiment_id=spec.task_id,
            task_id=spec.task_id,
            action=f"run registered data processing command {spec.processing_command}",
            result={
                "status": "completed",
                "returncode": returncode,
                "dataset_id": spec.dataset_manifest.dataset_id,
                "output_artifact_count": len(artifacts),
            },
            metrics=metrics,
            artifacts=artifacts,
        )

    @staticmethod
    def _json_dict(value: ResearchModel) -> dict[str, Any]:
        return json.loads(value.json(ensure_ascii=False))
