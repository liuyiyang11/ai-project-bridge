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
    project_repos: list[str] = field(default_factory=list)

    def list_candidate_issues(self):
        return self.issues

    def view_issue(self, number):
        return self.views.get(number, next(issue for issue in self.issues if issue.number == number))

    def set_status(self, number, status):
        self.statuses.append((number, status))

    def comment(self, number, body):
        self.comments.append((number, body))

    def for_repo(self, repo):
        self.project_repos.append(repo)
        return self


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
trusted_github_logins: [trusted-user]
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
    fake_github = FakeGitHub([Issue(1, "Demo", TASK_BODY, "https://github/issues/1", {"ai-task", "task:code", "status:ready"}, author_login="trusted-user")])
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
    initial = Issue(3, "Demo", TASK_BODY, "https://github/issues/3", {"ai-task", "task:code", "status:ready"}, author_login="trusted-user")
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
        [IssueComment("c-1", "<!-- AI_BRIDGE_REWORK -->\n```yaml\ninstruction: Fix it.\n```", author_login="trusted-user")],
    )
    fake_github.issues = [rework]
    fake_github.views[3] = rework
    result = dispatcher.run_once()
    again = dispatcher.run_once()

    assert result[0]["status"] == "review"
    assert again == []
    assert fake_executor.calls == [(3, None), (3, "Fix it.")]
    assert dispatcher.store.load_state(3)["processed_rework_comment_ids"] == ["c-1"]


def test_dispatcher_ignores_rework_from_untrusted_author_without_failing_task(tmp_path):
    config_path = tmp_path / "config.local.yaml"
    write_config(config_path, tmp_path / "demo")
    config = load_config(config_path)
    initial = Issue(4, "Demo", TASK_BODY, "https://github/issues/4", {"ai-task", "task:code", "status:ready"}, author_login="trusted-user")
    fake_github = FakeGitHub([initial])
    fake_executor = FakeExecutor()
    dispatcher = Dispatcher(config, fake_github, store=TaskStore(config.state_root), executors={"code": fake_executor})
    dispatcher.run_once()
    rework = Issue(
        4,
        "Demo",
        TASK_BODY,
        initial.url,
        {"ai-task", "task:code", "status:review"},
        [IssueComment("c-untrusted", "<!-- AI_BRIDGE_REWORK -->\n```yaml\ninstruction: Do something unsafe.\n```", author_login="intruder")],
        author_login="trusted-user",
    )
    fake_github.issues = [rework]
    fake_github.views[4] = rework

    result = dispatcher.run_once()

    assert result == []
    assert fake_executor.calls == [(4, None)]
    assert dispatcher.store.load_state(4)["status"] == "review"
    assert (4, "failed") not in fake_github.statuses


def test_dispatcher_accepts_multiple_project_capabilities(tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        f"""control_repo: owner/bridge
trusted_github_logins: [trusted-user]
projects:
  demo:
    capabilities: [code, experiment-review]
    root: {(tmp_path / 'demo').as_posix()}
    repo: owner/project-repo
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    experiment_body = TASK_BODY.replace("task_type: code", "task_type: experiment-review").replace("title: Demo task", "title: Experiment task\ncommand_id: evaluate")
    issues = [
        Issue(5, "Code", TASK_BODY, "https://github/issues/5", {"ai-task", "task:code", "status:ready"}, author_login="trusted-user"),
        Issue(6, "Experiment", experiment_body, "https://github/issues/6", {"ai-task", "task:experiment-review", "status:ready"}, author_login="trusted-user"),
    ]
    fake_github = FakeGitHub(issues)
    code_executor = FakeExecutor()
    experiment_executor = FakeExecutor()
    dispatcher = Dispatcher(config, fake_github, store=TaskStore(config.state_root), executors={"code": code_executor, "experiment-review": experiment_executor})

    outcomes = dispatcher.run_once()

    assert [outcome["status"] for outcome in outcomes] == ["review", "review"]
    assert code_executor.calls == [(5, None)]
    assert experiment_executor.calls == [(6, None)]
    assert fake_github.project_repos == ["owner/project-repo", "owner/project-repo"]

