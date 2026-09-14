from __future__ import annotations

import subprocess
from pathlib import Path

from .config import ProjectConfig


class CommandExecutionError(RuntimeError):
    """Raised when a configured deterministic command cannot be run."""


def run_registered_command(project: ProjectConfig, command_id: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    command = project.allowed_commands.get(command_id)
    if command is None:
        raise CommandExecutionError(f"command is not allowed or not registered: {command_id}")
    try:
        result = subprocess.run(
            command.argv,
            cwd=Path(cwd),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=command.timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandExecutionError(f"configured command {command_id!r} failed to start or timed out: {exc}") from exc
    return result
