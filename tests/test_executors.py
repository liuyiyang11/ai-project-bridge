from types import SimpleNamespace
from pathlib import Path

import pytest

from bridge.codex.runner import CodexResumeMismatchError
from bridge.config import load_config
from bridge.executors import ExecutionContext
from bridge.executors.code import CodeExecutor
from bridge.executors.experiment_review import ExperimentReviewExecutor
from bridge.executors.presentation import PresentationExecutor
from bridge.github import GhClient, Issue
from bridge.task_parser import parse_task_body
from bridge.task_store import TaskStore
from bridge.worktree import WorktreeManager


class FakeRunner:
    def __init__(self, filename="changed.py"):
        self.filename = filename
        self.calls = []

    def start_task(self, prompt, cwd, events_path):
        self.calls.append(("start", prompt, cwd))
        (cwd / self.filename).write_text("print('changed')\n", encoding="utf-8")
        events_path.write_text('{"type":"thread.started","thread_id":"fake-thread"}\n', encoding="utf-8")
        return SimpleNamespace(thread_id="fake-thread", exit_code=0, final_message="Fake Codex summary", stdout="json\n", stderr="")

    def resume_task(self, thread_id, prompt, cwd, events_path):
        self.calls.append(("resume", prompt, cwd))
        (cwd / self.filename).write_text("print('reworked')\n", encoding="utf-8")
        events_path.write_text('{"type":"thread.started","thread_id":"fake-thread"}\n', encoding="utf-8", append=False) if False else None
        return SimpleNamespace(thread_id=thread_id, exit_code=0, final_message="Fake rework summary", stdout="json\n", stderr="")


class NoPushManager(WorktreeManager):
    def push(self, cwd, branch):
        self.assert_publishable_branch(branch)


class FakeGitHub:
    def create_draft_pr(self, branch, base, title, body):
        assert branch.startswith("ai/issue-")
        assert "No automatic merge" in body
        return "https://github.com/owner/demo/pull/9"


class NoPrControlGitHub:
    def create_draft_pr(self, *args, **kwargs):
        raise AssertionError("control repo client must not create a project PR")


class FakeRenderer:
    def render(self, pptx_path, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "final.pptx").write_bytes(pptx_path.read_bytes())
        return {"final_pptx": "final.pptx", "final_pdf": None, "slides_png": [], "contact_sheet": None, "capabilities": "fake", "errors": []}


class FullFakeRenderer:
    def render(self, pptx_path, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "final.pptx").write_bytes(pptx_path.read_bytes())
        (output_dir / "final.pdf").write_bytes(b"pdf")
        (output_dir / "slides_png").mkdir()
        (output_dir / "slides_png" / "slide_001.png").write_bytes(b"slide")
        (output_dir / "contact_sheet.png").write_bytes(b"contact")
        return {
            "final_pptx": "final.pptx",
            "final_pdf": "final.pdf",
            "slides_png": ["slides_png/slide_001.png"],
            "contact_sheet": "contact_sheet.png",
            "capabilities": "fake",
            "errors": [],
        }


def make_repo(tmp_path):
    import subprocess

    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def make_config(tmp_path, repo, kind="code", command="quick_test", project_repo="owner/demo"):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        f"""control_repo: owner/bridge
trusted_github_logins: [trusted-user]
projects:
  demo:
    kind: {kind}
    root: {repo.as_posix()}
    repo: {project_repo}
    allowed_commands:
      {command}:
        argv: [python, -c, 'print(\"ok\")']
    artifact_dirs: [outputs]
""",
        encoding="utf-8",
    )
    return load_config(config_path)


def test_code_executor_runs_fake_codex_in_isolated_worktree(tmp_path):
    repo = make_repo(tmp_path)
    config = make_config(tmp_path, repo)
    store = TaskStore(config.state_root)
    issue = Issue(10, "Code", "", "https://github/issues/10", {"ai-task", "task:code", "status:running"})
    task = parse_task_body("""<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: code
project: demo
title: Change demo
goal: Change it.
```
""")
    store.initialize(10, "task", {"status": "running", "project": "demo", "task_type": "code"})
    runner = FakeRunner()
    executor = CodeExecutor(runner=runner, manager_factory=lambda project, root: NoPushManager(project.root, root))
    context = ExecutionContext(config, issue, task, config.project("demo"), store, store.task_dir(10), FakeGitHub(), FakeGitHub(), runner)

    result = executor.execute(context)

    assert result["pr_url"].endswith("/pull/9")
    assert result["changed_files"] == ["changed.py"]
    assert store.load_state(10)["thread_id"] == "fake-thread"
    assert (store.task_dir(10) / "review_bundle" / "diff.patch").is_file()
    assert not (repo / "changed.py").exists()


def test_experiment_review_is_deterministic_and_excludes_checkpoint(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "outputs").mkdir()
    (repo / "outputs" / "metrics.json").write_text('{"accuracy": 0.8}', encoding="utf-8")
    (repo / "outputs" / "model.ckpt").write_bytes(b"secret checkpoint")
    config = make_config(tmp_path, repo, kind="experiment-review", command="evaluate")
    store = TaskStore(config.state_root)
    issue = Issue(11, "Review", "", "https://github/issues/11", {"ai-task", "task:experiment-review", "status:running"})
    task = parse_task_body("""<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: experiment-review
project: demo
title: Review
command_id: evaluate
```
""")
    store.initialize(11, "task", {"status": "running", "project": "demo", "task_type": "experiment-review"})
    context = ExecutionContext(config, issue, task, config.project("demo"), store, store.task_dir(11), FakeGitHub(), FakeGitHub(), None)

    result = ExperimentReviewExecutor(publish=False).execute(context)

    assert result["thread_id"] is None
    assert any(item["source"] == "outputs/metrics.json" for item in result["artifact_list"])
    assert all(not item["source"].endswith(".ckpt") for item in result["artifact_list"] if "source" in item)


def test_small_experiment_bundle_can_be_published_as_review_pr(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "outputs").mkdir()
    (repo / "outputs" / "metrics.json").write_text('{"accuracy": 0.8}', encoding="utf-8")
    config = make_config(tmp_path, repo, kind="experiment-review", command="evaluate")
    store = TaskStore(config.state_root)
    issue = Issue(13, "Review", "", "https://github/issues/13", {"ai-task", "task:experiment-review", "status:running"})
    task = parse_task_body("""<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: experiment-review
project: demo
title: Review
command_id: evaluate
```
""")
    store.initialize(13, "task", {"status": "running", "project": "demo", "task_type": "experiment-review"})
    context = ExecutionContext(config, issue, task, config.project("demo"), store, store.task_dir(13), FakeGitHub(), FakeGitHub(), None)

    result = ExperimentReviewExecutor(
        manager_factory=lambda project, root: NoPushManager(project.root, root),
        publish=True,
    ).execute(context)

    assert result["pr_url"].endswith("/pull/9")
    assert (store.task_dir(13) / "review_bundle" / "summary.md").is_file()


def test_presentation_executor_uses_renderer_after_codex(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "brief.md").write_text("brief", encoding="utf-8")
    config = make_config(tmp_path, repo, kind="presentation")
    store = TaskStore(config.state_root)
    issue = Issue(12, "Slides", "", "https://github/issues/12", {"ai-task", "task:presentation", "status:running"})
    task = parse_task_body("""<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: presentation
project: demo
title: Slides
brief: brief.md
```
""")
    store.initialize(12, "task", {"status": "running", "project": "demo", "task_type": "presentation"})
    runner = FakeRunner("deck.pptx")
    executor = PresentationExecutor(runner=runner, manager_factory=lambda project, root: NoPushManager(project.root, root), renderer=FakeRenderer())
    context = ExecutionContext(config, issue, task, config.project("demo"), store, store.task_dir(12), FakeGitHub(), FakeGitHub(), runner)

    result = executor.execute(context)

    assert result["presentation"]["final_pptx"] == "final.pptx"
    assert (store.task_dir(12) / "review_bundle" / "presentation" / "final.pptx").is_file()


def test_code_executor_creates_pr_in_project_repo_and_references_control_task(tmp_path):
    repo = make_repo(tmp_path)
    config = make_config(tmp_path, repo, project_repo="owner/project-repo")
    store = TaskStore(config.state_root)
    issue = Issue(14, "Code", "", "https://github.com/owner/bridge/issues/14", {"ai-task", "task:code", "status:running"})
    task = parse_task_body("""<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: code
project: demo
title: Change demo
```
""")
    store.initialize(14, "task", {"status": "running", "project": "demo", "task_type": "code"})
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "https://github.com/owner/project-repo/pull/14\n", "stderr": ""})()

    runner = FakeRunner()
    project_github = GhClient("gh", "owner/project-repo", run=fake_run)
    executor = CodeExecutor(runner=runner, manager_factory=lambda project, root: NoPushManager(project.root, root))
    context = ExecutionContext(config, issue, task, config.project("demo"), store, store.task_dir(14), NoPrControlGitHub(), project_github, runner)

    result = executor.execute(context)

    assert result["pr_url"].endswith("/pull/14")
    assert config.project("demo").repo == project_github.repo
    assert "owner/project-repo" in calls[0][0]
    assert "Control task: owner/bridge#14" in calls[0][1]["input"]
    assert "Closes #14" not in calls[0][1]["input"]


def test_experiment_review_creates_pr_in_project_repo_and_references_control_task(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "outputs").mkdir()
    (repo / "outputs" / "metrics.json").write_text('{"accuracy": 0.8}', encoding="utf-8")
    config = make_config(tmp_path, repo, kind="experiment-review", command="evaluate", project_repo="owner/project-repo")
    store = TaskStore(config.state_root)
    issue = Issue(15, "Review", "", "https://github.com/owner/bridge/issues/15", {"ai-task", "task:experiment-review", "status:running"})
    task = parse_task_body("""<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: experiment-review
project: demo
title: Review
command_id: evaluate
```
""")
    store.initialize(15, "task", {"status": "running", "project": "demo", "task_type": "experiment-review"})
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "https://github.com/owner/project-repo/pull/15\n", "stderr": ""})()

    project_github = GhClient("gh", "owner/project-repo", run=fake_run)
    context = ExecutionContext(config, issue, task, config.project("demo"), store, store.task_dir(15), NoPrControlGitHub(), project_github, None)

    result = ExperimentReviewExecutor(manager_factory=lambda project, root: NoPushManager(project.root, root)).execute(context)

    assert result["pr_url"].endswith("/pull/15")
    assert config.project("demo").repo == project_github.repo
    assert "owner/project-repo" in calls[0][0]
    assert "Control task: owner/bridge#15" in calls[0][1]["input"]
    assert "Closes #15" not in calls[0][1]["input"]


def test_experiment_review_uses_candidate_worktree_from_source_code_task(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "outputs").mkdir()
    (repo / "outputs" / "root-only.json").write_text("old", encoding="utf-8")
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        f"""control_repo: owner/bridge
trusted_github_logins: [trusted-user]
projects:
  demo:
    capabilities: [code, experiment-review]
    root: {repo.as_posix()}
    repo: owner/demo
    allowed_commands:
      evaluate:
        argv: [python, -c, 'print(\"candidate\")']
    artifact_dirs: [outputs]
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    store = TaskStore(config.state_root)
    source_body = """<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: code
project: demo
title: Candidate code
```
"""
    source = WorktreeManager(repo, config.state_root / "worktrees").prepare("demo", 20)
    (source.path / "outputs").mkdir()
    (source.path / "outputs" / "candidate-only.json").write_text("candidate", encoding="utf-8")
    store.initialize(
        20,
        source_body,
        {
            "status": "review",
            "project": "demo",
            "task_type": "code",
            "worktree_path": str(source.path),
            "branch": source.branch,
        },
    )
    review_task = parse_task_body(
        """<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: experiment-review
project: demo
title: Review candidate
command_id: evaluate
source_issue: 20
```
"""
    )
    store.initialize(21, "task", {"status": "running", "project": "demo", "task_type": "experiment-review"})
    context = ExecutionContext(config, Issue(21, "Review", "", "https://github/issues/21", set()), review_task, config.project("demo"), store, store.task_dir(21), FakeGitHub(), FakeGitHub(), None)

    result = ExperimentReviewExecutor(publish=False).execute(context)

    assert any(item["source"] == "outputs/candidate-only.json" for item in result["artifact_list"])
    assert not (repo / "outputs" / "candidate-only.json").exists()


def test_experiment_review_rejects_source_issue_from_another_project(tmp_path):
    repo = make_repo(tmp_path)
    config = make_config(tmp_path, repo, kind="experiment-review", command="evaluate")
    store = TaskStore(config.state_root)
    source_body = """<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: code
project: other
title: Other code
```
"""
    store.initialize(20, source_body, {"status": "review", "project": "other", "task_type": "code", "worktree_path": str(tmp_path / "candidate"), "branch": "ai/issue-20"})
    review_task = parse_task_body(
        """<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: experiment-review
project: demo
title: Review candidate
command_id: evaluate
source_issue: 20
```
"""
    )
    store.initialize(21, "task", {"status": "running", "project": "demo", "task_type": "experiment-review"})
    context = ExecutionContext(config, Issue(21, "Review", "", "https://github/issues/21", set()), review_task, config.project("demo"), store, store.task_dir(21), FakeGitHub(), FakeGitHub(), None)

    with pytest.raises(RuntimeError, match="different project"):
        ExperimentReviewExecutor(publish=False).execute(context)


def test_presentation_review_artifacts_are_committed_to_the_same_issue_branch(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "brief.md").write_text("brief", encoding="utf-8")
    config = make_config(tmp_path, repo, kind="presentation")
    store = TaskStore(config.state_root)
    issue = Issue(22, "Slides", "", "https://github/issues/22", {"ai-task", "task:presentation", "status:running"})
    task = parse_task_body(
        """<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: presentation
project: demo
title: Slides
brief: brief.md
```
"""
    )
    store.initialize(22, "task", {"status": "running", "project": "demo", "task_type": "presentation"})
    runner = FakeRunner("deck.pptx")
    executor = PresentationExecutor(
        runner=runner,
        manager_factory=lambda project, root: NoPushManager(project.root, root),
        renderer=FullFakeRenderer(),
    )
    context = ExecutionContext(config, issue, task, config.project("demo"), store, store.task_dir(22), FakeGitHub(), FakeGitHub(), runner)

    result = executor.execute(context)

    branch_worktree = Path(store.load_state(22)["worktree_path"])
    for path in ("final.pptx", "final.pdf", "contact_sheet.png", "slides_png/slide_001.png"):
        assert (branch_worktree / "review_bundle" / "presentation" / path).is_file()
    assert {item["path"] for item in result["artifact_list"]} >= {
        "presentation/final.pptx",
        "presentation/final.pdf",
        "presentation/contact_sheet.png",
        "presentation/slides_png/slide_001.png",
    }


def test_code_rework_keeps_append_only_run_event_files_and_state_records(tmp_path):
    repo = make_repo(tmp_path)
    config = make_config(tmp_path, repo)
    store = TaskStore(config.state_root)
    issue = Issue(23, "Code", "", "https://github/issues/23", {"ai-task", "task:code", "status:running"})
    task = parse_task_body(
        """<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: code
project: demo
title: Change demo
```
"""
    )
    store.initialize(23, "task", {"status": "running", "project": "demo", "task_type": "code"})
    runner = FakeRunner()
    executor = CodeExecutor(runner=runner, manager_factory=lambda project, root: NoPushManager(project.root, root), publish=False)
    context = ExecutionContext(config, issue, task, config.project("demo"), store, store.task_dir(23), FakeGitHub(), FakeGitHub(), runner)

    executor.execute(context)
    executor.execute(context, rework_instruction="Fix the change.")

    state = store.load_state(23)
    assert [run["kind"] for run in state["runs"]] == ["initial", "rework"]
    assert (store.task_dir(23) / "runs" / "001-initial.events.jsonl").is_file()
    assert (store.task_dir(23) / "runs" / "002-rework.events.jsonl").is_file()
    assert state["runs"][1]["requested_thread_id"] == "fake-thread"


def test_code_resume_mismatch_keeps_original_thread_in_state(tmp_path):
    repo = make_repo(tmp_path)
    config = make_config(tmp_path, repo)
    store = TaskStore(config.state_root)
    issue = Issue(24, "Code", "", "https://github/issues/24", {"ai-task", "task:code", "status:review"})
    task = parse_task_body(
        """<!-- AI_BRIDGE_TASK -->
```yaml
version: 1
task_type: code
project: demo
title: Change demo
```
"""
    )
    store.initialize(
        24,
        "task",
        {"status": "review", "project": "demo", "task_type": "code", "thread_id": "saved-thread"},
    )

    class MismatchRunner:
        def resume_task(self, thread_id, prompt, cwd, events_path):
            raise CodexResumeMismatchError(thread_id, "new-thread")

    executor = CodeExecutor(
        runner=MismatchRunner(),
        manager_factory=lambda project, root: NoPushManager(project.root, root),
        publish=False,
    )
    context = ExecutionContext(config, issue, task, config.project("demo"), store, store.task_dir(24), FakeGitHub(), FakeGitHub(), None)

    with pytest.raises(CodexResumeMismatchError):
        executor.execute(context, rework_instruction="Fix it.")

    state = store.load_state(24)
    assert state["thread_id"] == "saved-thread"
    assert state["runs"][0]["requested_thread_id"] == "saved-thread"
    assert state["runs"][0]["status"] == "failed"
