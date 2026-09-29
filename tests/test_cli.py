import json
from types import SimpleNamespace

from bridge.cli import main
from bridge.cli import project_repository_checks, run_bridge
from bridge.config import load_config
from bridge.task_store import TaskStore


class _FakeCliSupervisor:
    def __init__(self, close_error=None):
        self.close_calls = 0
        self.close_error = close_error

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _FakeCliDispatcher:
    def __init__(self, supervisor, outcome=None, error=None):
        self.supervisor = supervisor
        self.outcome = outcome if outcome is not None else []
        self.error = error
        self.run_once_calls = 0

    def run_once(self):
        self.run_once_calls += 1
        if self.error is not None:
            raise self.error
        return self.outcome


def _cli_runtime_config(tmp_path):
    return SimpleNamespace(
        state_root=tmp_path / ".bridge",
        codex=SimpleNamespace(backend="app-server"),
        codex_binary="codex",
        control_repo="owner/bridge",
        poll_seconds=1,
    )


def _patch_cli_runtime(monkeypatch, dispatcher):
    monkeypatch.setattr("bridge.cli.validate_project_repositories", lambda config: None)
    monkeypatch.setattr("bridge.cli.make_github_client", lambda config: object())
    monkeypatch.setattr("bridge.cli.Dispatcher", lambda *args, **kwargs: dispatcher)


def test_cli_normal_exit_closes_supervisor(tmp_path, monkeypatch):
    supervisor = _FakeCliSupervisor()
    dispatcher = _FakeCliDispatcher(supervisor, outcome=[])
    _patch_cli_runtime(monkeypatch, dispatcher)

    assert run_bridge(_cli_runtime_config(tmp_path), once=True) == 0
    assert supervisor.close_calls == 1


def test_cli_run_once_closes_supervisor(tmp_path, monkeypatch):
    supervisor = _FakeCliSupervisor()
    dispatcher = _FakeCliDispatcher(supervisor, outcome=[])
    _patch_cli_runtime(monkeypatch, dispatcher)
    monkeypatch.setattr("bridge.cli.load_config", lambda config_path: _cli_runtime_config(tmp_path))

    assert main(["run-once"]) == 0
    assert supervisor.close_calls == 1


def test_cli_keyboard_interrupt_closes_supervisor(tmp_path, monkeypatch, capsys):
    supervisor = _FakeCliSupervisor()
    dispatcher = _FakeCliDispatcher(supervisor, error=KeyboardInterrupt())
    _patch_cli_runtime(monkeypatch, dispatcher)

    assert run_bridge(_cli_runtime_config(tmp_path), once=False) == 0
    assert supervisor.close_calls == 1
    assert "Bridge stopped." in capsys.readouterr().out


def test_cli_exception_closes_supervisor_and_preserves_primary_error(tmp_path, monkeypatch, capsys):
    supervisor = _FakeCliSupervisor(close_error=RuntimeError("shutdown boom"))
    dispatcher = _FakeCliDispatcher(supervisor, error=RuntimeError("runtime boom"))
    _patch_cli_runtime(monkeypatch, dispatcher)

    assert run_bridge(_cli_runtime_config(tmp_path), once=False) == 1
    assert supervisor.close_calls == 1
    error = capsys.readouterr().err
    assert "Bridge error: runtime boom" in error
    assert "Bridge shutdown error: shutdown boom" in error


def test_show_task_prints_persisted_state(tmp_path, capsys):
    config = tmp_path / "config.local.yaml"
    config.write_text("control_repo: owner/bridge\ntrusted_github_logins: [trusted-user]\nprojects: {}\n", encoding="utf-8")
    store = TaskStore(tmp_path / ".bridge")
    store.initialize(8, "task", {"status": "review", "project": "demo"})

    assert main(["--config", str(config), "show-task", "8"]) == 0
    output = capsys.readouterr().out
    assert '"status": "review"' in output


def test_doctor_reports_missing_config_and_tool_in_plain_language(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("bridge.cli.find_executable", lambda name: None)

    result = main(["--config", str(tmp_path / "missing.yaml"), "doctor"])
    output = capsys.readouterr().out

    assert result != 0
    assert "config" in output.lower()
    assert "GitHub CLI" in output


def test_retry_resets_local_state_and_status(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.local.yaml"
    config.write_text("control_repo: owner/bridge\ntrusted_github_logins: [trusted-user]\nprojects: {}\n", encoding="utf-8")
    store = TaskStore(tmp_path / ".bridge")
    store.initialize(9, "task", {"status": "failed", "project": "demo"})

    class FakeGh:
        def __init__(self, *args):
            self.calls = []

        def set_status(self, number, status):
            self.calls.append((number, status))

        def comment(self, number, body):
            self.calls.append((number, body))

    fake = FakeGh()
    monkeypatch.setattr("bridge.cli.make_github_client", lambda config: fake)

    assert main(["--config", str(config), "retry", "9"]) == 0
    assert store.load_state(9)["status"] == "ready"
    assert fake.calls[0] == (9, "ready")
    assert "ready" in capsys.readouterr().out


def test_doctor_project_checks_detect_remote_repo_mismatch(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/owner/actual.git"], cwd=repo, check=True, capture_output=True)
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        f"""control_repo: owner/bridge
trusted_github_logins: [trusted-user]
projects:
  demo:
    kind: code
    root: {repo.as_posix()}
    repo: owner/expected
    base_branch: main
""",
        encoding="utf-8",
    )

    checks = project_repository_checks("demo", load_config(config_path).project("demo"))

    assert any(label == "Project demo remote/repo" and not ok for label, ok, _ in checks)


def test_bridge_refuses_to_run_when_project_remote_does_not_match(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/owner/actual.git"], cwd=repo, check=True, capture_output=True)
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        f"""control_repo: owner/bridge
trusted_github_logins: [trusted-user]
projects:
  demo:
    kind: code
    root: {repo.as_posix()}
    repo: owner/expected
    base_branch: main
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    monkeypatch.setattr("bridge.cli.make_github_client", lambda config: (_ for _ in ()).throw(AssertionError("GitHub must not be used")))

    assert run_bridge(config, once=True) == 1

