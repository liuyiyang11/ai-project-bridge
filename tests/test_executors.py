from types import SimpleNamespace

from bridge.config import load_config
from bridge.executors import ExecutionContext
from bridge.executors.code import CodeExecutor
from bridge.executors.experiment_review import ExperimentReviewExecutor
from bridge.executors.presentation import PresentationExecutor
from bridge.github import Issue
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


class FakeRenderer:
    def render(self, pptx_path, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "final.pptx").write_bytes(pptx_path.read_bytes())
        return {"final_pptx": "final.pptx", "final_pdf": None, "slides_png": [], "contact_sheet": None, "capabilities": "fake", "errors": []}


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


def make_config(tmp_path, repo, kind="code", command="quick_test"):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        f"""control_repo: owner/bridge
projects:
  demo:
    kind: {kind}
    root: {repo.as_posix()}
    repo: owner/demo
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
    context = ExecutionContext(config, issue, task, config.project("demo"), store, store.task_dir(10), FakeGitHub(), runner)

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
    context = ExecutionContext(config, issue, task, config.project("demo"), store, store.task_dir(11), FakeGitHub(), None)

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
    context = ExecutionContext(config, issue, task, config.project("demo"), store, store.task_dir(13), FakeGitHub(), None)

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
    context = ExecutionContext(config, issue, task, config.project("demo"), store, store.task_dir(12), FakeGitHub(), runner)

    result = executor.execute(context)

    assert result["presentation"]["final_pptx"] == "final.pptx"
    assert (store.task_dir(12) / "review_bundle" / "presentation" / "final.pptx").is_file()
