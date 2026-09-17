from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .config import ProjectConfig

if TYPE_CHECKING:
    from .experiments.runtime import ExperimentRuntime, ExperimentRuntimeRegistry


class CommandExecutionError(RuntimeError):
    """Raised when a configured deterministic command cannot be run."""


def run_registered_command(
    project: ProjectConfig,
    command_id: str,
    cwd: Path,
    python_executable: Optional[Path] = None,
    *,
    runtime: Optional[ExperimentRuntime] = None,
    runtime_registry: Optional[ExperimentRuntimeRegistry] = None,
) -> subprocess.CompletedProcess[str]:
    from .experiments.runtime import terminate_process_bounded

    command = project.allowed_commands.get(command_id)
    if command is None:
        raise CommandExecutionError(f"command is not allowed or not registered: {command_id}")
    argv = list(command.argv)
    if python_executable and argv and Path(argv[0]).name.lower() in {"python", "python.exe", "py", "py.exe"}:
        argv[0] = str(Path(python_executable).resolve())
    try:
        process = subprocess.Popen(
            argv,
            cwd=Path(cwd),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if runtime is not None:
            if runtime_registry is None:
                terminate_process_bounded(process)
                raise CommandExecutionError("experiment runtime registry is required for process tracking")
            runtime_registry.attach_process(runtime.task_id, process)
        try:
            stdout, stderr = process.communicate(timeout=command.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            terminate_process_bounded(
                process,
                timeout=runtime_registry.cleanup_timeout if runtime_registry is not None else 1.0,
            )
            raise CommandExecutionError(f"configured command {command_id!r} failed to start or timed out: {exc}") from exc
        except BaseException:
            terminate_process_bounded(
                process,
                timeout=runtime_registry.cleanup_timeout if runtime_registry is not None else 1.0,
            )
            raise
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandExecutionError(f"configured command {command_id!r} failed to start or timed out: {exc}") from exc
    return result
