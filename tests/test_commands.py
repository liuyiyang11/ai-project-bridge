from pathlib import Path
import subprocess

import pytest

from bridge.commands import CommandExecutionError, run_registered_command
from bridge.config import AllowedCommand, ProjectConfig


def test_registered_command_runs_without_shell(tmp_path, monkeypatch):
    calls = []

    class Process:
        returncode = 0

        def communicate(self, timeout=None):
            return "hello\n", ""

        def poll(self):
            return self.returncode

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    monkeypatch.setattr("bridge.commands.subprocess.Popen", fake_popen)
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
    assert calls[0][1]["stdout"] is subprocess.PIPE
    assert calls[0][1]["stderr"] is subprocess.PIPE


def test_registered_python_command_uses_configured_interpreter(tmp_path, monkeypatch):
    calls = []

    class Process:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

        def poll(self):
            return self.returncode

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    monkeypatch.setattr("bridge.commands.subprocess.Popen", fake_popen)
    project = ProjectConfig(
        kind="code",
        root=tmp_path,
        repo="owner/demo",
        allowed_commands={"quick_test": {"argv": ["python", "-m", "pytest", "-q"]}},
    )
    configured_python = Path(r"C:\Users\29833\.conda\envs\py10\python.exe")

    run_registered_command(project, "quick_test", tmp_path, configured_python)

    assert calls[0][0][0] == str(configured_python.resolve())


def test_registered_command_timeout_terminates_child(tmp_path, monkeypatch):
    class Process:
        returncode = None
        terminate_calls = 0

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(["python"], timeout)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminate_calls += 1
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    process = Process()
    monkeypatch.setattr("bridge.commands.subprocess.Popen", lambda argv, **kwargs: process)
    project = ProjectConfig(
        kind="code",
        root=tmp_path,
        repo="owner/demo",
        allowed_commands={"quick_test": AllowedCommand(argv=["python", "-c", "pass"], timeout_seconds=1)},
    )

    with pytest.raises(CommandExecutionError, match="timed out"):
        run_registered_command(project, "quick_test", tmp_path)

    assert process.terminate_calls == 1


def test_registered_command_communicate_exception_cleans_up_and_preserves_error(tmp_path, monkeypatch):
    class Process:
        returncode = None
        terminate_calls = 0
        kill_calls = 0

        def communicate(self, timeout=None):
            raise RuntimeError("communicate failed")

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminate_calls += 1
            self.returncode = -15

        def kill(self):
            self.kill_calls += 1

        def wait(self, timeout=None):
            return self.returncode

    process = Process()
    monkeypatch.setattr("bridge.commands.subprocess.Popen", lambda argv, **kwargs: process)
    project = ProjectConfig(
        kind="code",
        root=tmp_path,
        repo="owner/demo",
        allowed_commands={"quick_test": AllowedCommand(argv=["python", "-c", "pass"])},
    )

    with pytest.raises(RuntimeError, match="communicate failed"):
        run_registered_command(project, "quick_test", tmp_path)

    assert process.terminate_calls == 1
    assert process.kill_calls == 0


def test_registered_command_communicate_exception_uses_kill_fallback(tmp_path, monkeypatch):
    class Process:
        returncode = None
        terminate_calls = 0
        kill_calls = 0
        wait_calls = 0

        def communicate(self, timeout=None):
            raise RuntimeError("communicate failed")

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminate_calls += 1

        def kill(self):
            self.kill_calls += 1
            self.returncode = -9

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(["python"], timeout)
            return self.returncode

    process = Process()
    monkeypatch.setattr("bridge.commands.subprocess.Popen", lambda argv, **kwargs: process)
    project = ProjectConfig(
        kind="code",
        root=tmp_path,
        repo="owner/demo",
        allowed_commands={"quick_test": AllowedCommand(argv=["python", "-c", "pass"])},
    )

    with pytest.raises(RuntimeError, match="communicate failed"):
        run_registered_command(project, "quick_test", tmp_path)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 2


def test_registered_command_cleanup_failure_does_not_replace_primary_error(tmp_path, monkeypatch):
    class Process:
        returncode = None
        terminate_calls = 0
        kill_calls = 0

        def communicate(self, timeout=None):
            raise RuntimeError("primary")

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminate_calls += 1
            raise RuntimeError("terminate failed")

        def kill(self):
            self.kill_calls += 1
            raise RuntimeError("kill failed")

        def wait(self, timeout=None):
            raise RuntimeError("wait failed")

    process = Process()
    monkeypatch.setattr("bridge.commands.subprocess.Popen", lambda argv, **kwargs: process)
    project = ProjectConfig(
        kind="code",
        root=tmp_path,
        repo="owner/demo",
        allowed_commands={"quick_test": AllowedCommand(argv=["python", "-c", "pass"])},
    )

    with pytest.raises(RuntimeError, match="primary"):
        run_registered_command(project, "quick_test", tmp_path)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_registered_command_keyboard_interrupt_cleans_up_and_reraises(tmp_path, monkeypatch):
    class Process:
        returncode = None
        terminate_calls = 0

        def communicate(self, timeout=None):
            raise KeyboardInterrupt()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminate_calls += 1
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    process = Process()
    monkeypatch.setattr("bridge.commands.subprocess.Popen", lambda argv, **kwargs: process)
    project = ProjectConfig(
        kind="code",
        root=tmp_path,
        repo="owner/demo",
        allowed_commands={"quick_test": AllowedCommand(argv=["python", "-c", "pass"])},
    )

    with pytest.raises(KeyboardInterrupt):
        run_registered_command(project, "quick_test", tmp_path)

    assert process.terminate_calls == 1


def test_unknown_registered_command_is_rejected(tmp_path):
    project = ProjectConfig(kind="code", root=tmp_path, repo="owner/demo")
    with pytest.raises(CommandExecutionError, match="not allowed"):
        run_registered_command(project, "arbitrary", tmp_path)

