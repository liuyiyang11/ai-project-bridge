import json
from dataclasses import dataclass, field

from bridge.config import load_config
from bridge.dispatcher import Dispatcher
from bridge.github import Issue, IssueComment
from bridge.task_store import TaskStore


TASK_BODY = """<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: code
project: demo
title: Demo task
goal: Change the demo safely.
instructions: Keep the API stable.
acceptance:
  - tests pass
```
"""


@dataclass
class FakeGitHub:
    issues: list[Issue]
    views: dict[int, Issue] = field(default_factory=dict)
    statuses: list[tuple[int, str]] = field(default_factory=list)
    comments: list[tuple[int, str]] = field(default_factory=list)

    def list_candidate_issues(self):
        return self.issues

    def view_issue(self, number):
        return self.views.get(number, next(issue for issue in self.issues if issue.number == number))

    def set_status(self, number, status):
        self.statuses.append((number, status))

    def comment(self, number, body):
        self.comments.append((number, body))


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, context, rework_instruction=None):
        self.calls.append((context.issue.number, rework_instruction))
        bundle = context.task_dir / "review_bundle"
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "summary.md").write_text("fake result\n", encoding="utf-8")
        return {
            "thread_id": "thread-demo",
            "final_message": "Fake runner completed.",
            "changed_files": ["demo.py"],
            "tests": [{"command_id": "quick_test", "returncode": 0}],
            "artifact_list": [],
            "bundle_dir": str(bundle),
            "pr_url": "https://github.com/owner/demo/pull/1",
        }


def write_config(path, root):
    path.write_text(
        f"""control_repo: owner/bridge
projects:
  demo:
    kind: code
    root: {root.as_posix()}
    repo: owner/demo
""",
        encoding="utf-8",
    )


def test_dispatcher_transitions_ready_to_review_and_skips_duplicate(tmp_path):
    config_path = tmp_path / "config.local.yaml"
    write_config(config_path, tmp_path / "demo")
    config = load_config(config_path)
    fake_github = FakeGitHub([Issue(1, "Demo", TASK_BODY, "https://github/issues/1", {"ai-task", "task:code", "status:ready"})])
    fake_executor = FakeExecutor()
    dispatcher = Dispatcher(config, fake_github, store=TaskStore(config.state_root), executors={"code": fake_executor})

    first = dispatcher.run_once()
    second = dispatcher.run_once()

    assert first[0]["status"] == "review"
    assert second == []
    assert len(fake_executor.calls) == 1
    assert fake_github.statuses == [(1, "running"), (1, "review")]
    state = dispatcher.store.load_state(1)
    assert state["status"] == "review"
    manifest = json.loads((config.state_root / "tasks" / "1" / "review_bundle" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["issue_number"] == 1
    assert manifest["pr_url"].endswith("/pull/1")


def test_dispatcher_marks_unregistered_project_failed(tmp_path):
    config_path = tmp_path / "config.local.yaml"
    write_config(config_path, tmp_path / "demo")
    config = load_config(config_path)
    body = TASK_BODY.replace("project: demo", "project: missing")
    fake_github = FakeGitHub([Issue(2, "Bad", body, "https://github/issues/2", {"ai-task", "task:code", "status:ready"})])
    dispatcher = Dispatcher(config, fake_github, store=TaskStore(config.state_root), executors={"code": FakeExecutor()})

    result = dispatcher.run_once()

    assert result[0]["status"] == "failed"
    assert dispatcher.store.load_state(2)["status"] == "failed"
    assert fake_github.statuses[-1] == (2, "failed")


def test_dispatcher_processes_each_rework_comment_once(tmp_path):
    config_path = tmp_path / "config.local.yaml"
    write_config(config_path, tmp_path / "demo")
    config = load_config(config_path)
    initial = Issue(3, "Demo", TASK_BODY, "https://github/issues/3", {"ai-task", "task:code", "status:ready"})
    fake_github = FakeGitHub([initial])
    fake_executor = FakeExecutor()
    dispatcher = Dispatcher(config, fake_github, store=TaskStore(config.state_root), executors={"code": fake_executor})
    dispatcher.run_once()

    rework = Issue(
        3,
        "Demo",
        TASK_BODY,
        initial.url,
        {"ai-task", "task:code", "status:review"},
        [IssueComment("c-1", "<!-- AI_BRIDGE_REWORK -->\n```yaml\ninstruction: Fix it.\n```")],
    )
    fake_github.issues = [rework]
    fake_github.views[3] = rework
    result = dispatcher.run_once()
    again = dispatcher.run_once()

    assert result[0]["status"] == "review"
    assert again == []
    assert fake_executor.calls == [(3, None), (3, "Fix it.")]
    assert dispatcher.store.load_state(3)["processed_rework_comment_ids"] == ["c-1"]

