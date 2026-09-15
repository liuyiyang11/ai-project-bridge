from pathlib import Path

import pytest

from bridge.commands import CommandExecutionError, run_registered_command
from bridge.config import AllowedCommand, ProjectConfig


def test_registered_command_runs_without_shell(tmp_path, monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = "hello\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()

    monkeypatch.setattr("bridge.commands.subprocess.run", fake_run)
    project = ProjectConfig(
        kind="code",
        root=tmp_path,
        repo="owner/demo",
        allowed_commands={"quick_test": AllowedCommand(argv=["python", "-c", "print(1)"])},
    )
    result = run_registered_command(project, "quick_test", tmp_path)

    assert result.returncode == 0
    assert calls[0][0] == ["python", "-c", "print(1)"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["cwd"] == tmp_path


def test_registered_python_command_uses_configured_interpreter(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("bridge.commands.subprocess.run", fake_run)
    project = ProjectConfig(
        kind="code",
        root=tmp_path,
        repo="owner/demo",
        allowed_commands={"quick_test": {"argv": ["python", "-m", "pytest", "-q"]}},
    )
    configured_python = Path(r"C:\Users\29833\.conda\envs\py10\python.exe")

    run_registered_command(project, "quick_test", tmp_path, configured_python)

    assert calls[0][0][0] == str(configured_python.resolve())


def test_unknown_registered_command_is_rejected(tmp_path):
    project = ProjectConfig(kind="code", root=tmp_path, repo="owner/demo")
    with pytest.raises(CommandExecutionError, match="not allowed"):
        run_registered_command(project, "arbitrary", tmp_path)

