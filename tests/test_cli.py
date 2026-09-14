import json

from bridge.cli import main
from bridge.task_store import TaskStore


def test_show_task_prints_persisted_state(tmp_path, capsys):
    config = tmp_path / "config.local.yaml"
    config.write_text("control_repo: owner/bridge\nprojects: {}\n", encoding="utf-8")
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
    config.write_text("control_repo: owner/bridge\nprojects: {}\n", encoding="utf-8")
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

