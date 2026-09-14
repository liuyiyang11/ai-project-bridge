from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from ..collectors.git_collector import collect_git_state
from ..commands import run_registered_command
from ..config import ProjectConfig
from ..task_store import utc_now
from ..worktree import WorktreeManager
from . import ExecutionContext


def _value(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


class CodeExecutor:
    def __init__(self, runner: Any = None, manager_factory: Optional[Callable[[ProjectConfig, Path], WorktreeManager]] = None, *, publish: bool = True):
        self.runner = runner
        self.manager_factory = manager_factory
        self.publish = publish

    def execute(self, context: ExecutionContext, rework_instruction: Optional[str] = None) -> dict:
        return self._execute_codex(context, self._prompt(context, rework_instruction), rework_instruction)

    def _prompt(self, context: ExecutionContext, rework_instruction: Optional[str]) -> str:
        task = context.task
        parts = [
            "You are executing a validated AI Project Bridge task in the current workspace.",
            "Work only inside the current workspace. Do not push, merge, modify main/master, read credentials, or access unrelated paths.",
            f"Task title: {task.title}",
            f"Goal: {task.goal or '(not separately specified)'}",
            f"Instructions: {task.instructions or '(not separately specified)'}",
            "Acceptance criteria:\n- " + "\n- ".join(task.acceptance) if task.acceptance else "Acceptance criteria: none supplied; preserve existing interfaces.",
        ]
        if rework_instruction:
            parts.extend(["This is a rework pass. Apply only this reviewed instruction:", rework_instruction])
        return "\n\n".join(parts)

    def _manager(self, context: ExecutionContext) -> WorktreeManager:
        if self.manager_factory:
            return self.manager_factory(context.project, context.config.state_root / "worktrees")
        return WorktreeManager(context.project.root, context.config.state_root / "worktrees")

    def _execute_codex(self, context: ExecutionContext, prompt: str, rework_instruction: Optional[str]) -> dict:
        manager = self._manager(context)
        info = manager.prepare(context.task.project, context.issue.number)
        context.store.update_state(context.issue.number, worktree_path=str(info.path), branch=info.branch, base_branch=info.base_branch, base_head=info.base_head)
        events_path = context.store.task_path(context.issue.number, "events.jsonl")
        runner = self.runner or context.runner
        if rework_instruction and context.store.load_state(context.issue.number).get("thread_id"):
            result = runner.resume_task(context.store.load_state(context.issue.number)["thread_id"], prompt, info.path, events_path)
        else:
            result = runner.start_task(prompt, info.path, events_path)
        context.store.append_stdout(context.issue.number, _value(result, "stdout", ""))
        context.store.append_stderr(context.issue.number, _value(result, "stderr", ""))
        thread_id = _value(result, "thread_id")
        if thread_id:
            context.store.update_state(context.issue.number, thread_id=thread_id)
        if int(_value(result, "exit_code", 1)) != 0:
            raise RuntimeError(f"Codex exited with code {_value(result, 'exit_code')}: {_value(result, 'final_message', '')}")

        tests: list[dict] = []
        quick_test = context.project.allowed_commands.get("quick_test")
        if quick_test:
            command_result = run_registered_command(context.project, "quick_test", info.path, context.config.python_executable)
            context.store.append_stdout(context.issue.number, command_result.stdout or "")
            context.store.append_stderr(context.issue.number, command_result.stderr or "")
            tests.append({"command_id": "quick_test", "returncode": command_result.returncode, "stdout": command_result.stdout[-4000:], "stderr": command_result.stderr[-4000:]})
            if command_result.returncode != 0:
                raise RuntimeError(f"configured quick_test failed with code {command_result.returncode}")

        bundle_dir = context.task_dir / "review_bundle"
        git_state = collect_git_state(info.path, bundle_dir)
        if not git_state["changed_files"]:
            raise RuntimeError("Codex completed without any changed files")
        commit = manager.commit(info.path, f"ai: issue {context.issue.number} {context.task.title}")
        pr_url = context.store.load_state(context.issue.number).get("pr_url")
        if self.publish:
            manager.push(info.path, info.branch)
            if not pr_url:
                body = self._pr_body(context, result, git_state["changed_files"], tests)
                pr_url = context.project_github.create_draft_pr(info.branch, info.base_branch, context.task.title, body)
        return {
            "thread_id": thread_id,
            "final_message": _value(result, "final_message", ""),
            "changed_files": git_state["changed_files"],
            "tests": tests,
            "artifact_list": [],
            "bundle_dir": str(bundle_dir),
            "pr_url": pr_url,
            "commit": commit,
            "known_limitations": "Diff is captured from the task worktree; binary artifacts are not embedded unless explicitly collected.",
        }

    @staticmethod
    def _pr_body(context: ExecutionContext, result: Any, changed_files: list[str], tests: list[dict]) -> str:
        test_lines = [f"- `{item['command_id']}`: exit {item['returncode']}" for item in tests] or ["- No configured quick_test command"]
        return "\n".join(
            [
                f"Control task: {context.config.control_repo}#{context.issue.number}",
                "",
                "## Codex summary",
                _value(result, "final_message", "(no final summary)"),
                "",
                "## Changed files",
                *[f"- `{path}`" for path in changed_files],
                "",
                "## Tests",
                *test_lines,
                "",
                "## Known limitations",
                "No automatic merge is performed.",
            ]
        )

