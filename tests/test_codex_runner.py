import json
import subprocess

import pytest

from bridge.codex.runner import CodexRunner, CodexUnavailableError


class FakeProcess:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.returncode = 0

    def communicate(self, input=None, timeout=None):
        assert input
        self.kwargs["stdout"].write if False else None
        return (
            '{"type":"thread.started","thread_id":"thread-123"}\n'
            '{"type":"turn.completed","message":"Finished safely"}\n',
            "",
        )


def test_codex_runner_uses_workspace_write_json_and_stdin(tmp_path):
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess(argv, **kwargs)

    final_file = tmp_path / "final.txt"
    runner = CodexRunner("codex", popen=fake_popen, available=True)
    result = runner.start_task("make the change", tmp_path, tmp_path / "events.jsonl", final_file=final_file)

    argv = calls[0][0]
    assert argv[:2] == ["codex", "exec"]
    assert "--json" in argv
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert argv[-1] == "-"
    assert result.thread_id == "thread-123"
    assert result.exit_code == 0
    assert "thread.started" in (tmp_path / "events.jsonl").read_text(encoding="utf-8")


def test_codex_runner_resume_uses_saved_thread_id(tmp_path):
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return FakeProcess(argv, **kwargs)

    runner = CodexRunner("codex", popen=fake_popen, available=True)
    runner.resume_task("thread-123", "rework", tmp_path, tmp_path / "events.jsonl")

    assert calls[0][:4] == ["codex", "exec", "resume", "thread-123"]
    assert "--json" in calls[0]
    assert "--dangerously-bypass-approvals-and-sandbox" not in calls[0]


def test_missing_codex_is_clear(tmp_path):
    runner = CodexRunner("not-a-real-codex", available=False)
    with pytest.raises(CodexUnavailableError, match="Codex CLI"):
        runner.start_task("task", tmp_path, tmp_path / "events.jsonl")

