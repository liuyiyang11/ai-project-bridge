from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from ..codex.runner import CodexResumeMismatchError
from ..collectors.git_collector import collect_git_state
from ..commands import run_registered_command
from ..config import ProjectConfig
from ..security import ensure_safe_relative_path, resolve_under
from ..task_store import utc_now
from ..worktree import WorktreeManager
from . import ExecutionContext


def _value(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


class CodeExecutor:
    def __init__(self, runner: Any = None, manager_factory: Optional[Callable[[ProjectConfig, Path], WorktreeManager]] = None, *, publish: bool = True, session_manager: Any = None, manage_task_lifecycle: bool = True):
        self.runner = runner
        self.manager_factory = manager_factory
        self.publish = publish
        self.session_manager = session_manager
        self.manage_task_lifecycle = bool(manage_task_lifecycle)

    def execute(
        self,
        context: ExecutionContext,
        rework_instruction: Optional[str] = None,
        resume_thread_id: Optional[str] = None,
    ) -> dict:
        return self._execute_codex(
            context,
            self._prompt(context, rework_instruction),
            rework_instruction,
            resume_thread_id=resume_thread_id,
        )

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
        return WorktreeManager(
            context.project.root,
            context.config.state_root / "worktrees",
            context.project.remote,
            context.project.base_branch,
        )

    def _prepare_review_artifacts(self, context: ExecutionContext, worktree: Path, bundle_dir: Path, codex_result: Any) -> dict:
        """Hook for task types that must prepare files before the branch is committed."""
        return {}

    def _execute_codex(
        self,
        context: ExecutionContext,
        prompt: str,
        rework_instruction: Optional[str],
        *,
        resume_thread_id: Optional[str] = None,
    ) -> dict:
        manager = self._manager(context)
        info = manager.prepare(context.task.project, context.issue.number)
        context.store.update_state(context.issue.number, worktree_path=str(info.path), branch=info.branch, base_branch=info.base_branch, base_head=info.base_head)
        session_manager = self.session_manager or getattr(context, "session_manager", None)
        runner = self.runner or context.runner
        state = context.store.load_state(context.issue.number)
        requested_thread_id = resume_thread_id or (state.get("thread_id") if rework_instruction and state.get("thread_id") else None)
        if requested_thread_id is not None and (not isinstance(requested_thread_id, str) or not requested_thread_id):
            raise RuntimeError("Codex resume thread id must be a non-empty string")
        run_kind = "rework" if requested_thread_id else "initial"
        run_record = context.store.begin_run(context.issue.number, run_kind, requested_thread_id=requested_thread_id)
        events_path = context.store.task_dir(context.issue.number) / run_record["events_path"]
        try:
            if session_manager is not None:
                result = session_manager.run_task(
                    str(context.issue.number),
                    context.task.project,
                    info.path,
                    prompt,
                    model=getattr(context.task, "model", None),
                    reasoning_effort=getattr(context.task, "reasoning_effort", None),
                    resume_thread_id=requested_thread_id,
                    events_path=events_path,
                    manage_task_lifecycle=self.manage_task_lifecycle,
                )
            elif requested_thread_id:
                result = runner.resume_task(requested_thread_id, prompt, info.path, events_path)
            else:
                result = runner.start_task(prompt, info.path, events_path)
        except Exception as exc:
            context.store.finish_run(context.issue.number, run_record["number"], status="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        context.store.append_stdout(context.issue.number, _value(result, "stdout", ""))
        context.store.append_stderr(context.issue.number, _value(result, "stderr", ""))
        thread_id = _value(result, "thread_id")
        if requested_thread_id is not None and thread_id != requested_thread_id:
            mismatch = CodexResumeMismatchError(requested_thread_id, thread_id)
            context.store.finish_run(context.issue.number, run_record["number"], status="failed", returned_thread_id=thread_id, error=str(mismatch))
            raise mismatch
        if thread_id:
            context.store.update_state(context.issue.number, thread_id=thread_id)
        exit_code = int(_value(result, "exit_code", 1))
        completed_run = context.store.finish_run(
            context.issue.number,
            run_record["number"],
            status="completed" if exit_code == 0 else "failed",
            returned_thread_id=thread_id,
            exit_code=exit_code,
        )
        if exit_code != 0:
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
        prepared = self._prepare_review_artifacts(context, info.path, bundle_dir, result)
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
        current_state = context.store.load_state(context.issue.number)
        return {
            "thread_id": thread_id,
            "final_message": _value(result, "final_message", ""),
            "changed_files": git_state["changed_files"],
            "tests": tests,
            "artifact_list": prepared.get("artifact_list", []),
            "bundle_dir": str(bundle_dir),
            "pr_url": pr_url,
            "commit": commit,
            "run": completed_run,
            "runs": current_state.get("runs", []),
            **{key: value for key, value in prepared.items() if key != "artifact_list"},
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


class RuntimeCodeExecutor:
    """Run a queued code task without GitHub publication side effects."""

    def __init__(self, config: Any, store: Any, session_manager: Any, worktree_preparer: Callable[[str, str], Any]):
        self.config = config
        self.store = store
        self.session_manager = session_manager
        self.worktree_preparer = worktree_preparer

    def execute(self, task: dict[str, Any]) -> "TaskResult":
        task_id = str(task["task_id"])
        project_name = str(task["project"])
        instruction = str(task.get("rework_instruction") or task.get("instruction", ""))
        if self.store.get_task(task_id).get("state") == "INTERRUPTED":
            from ..orchestration.models import TaskResult

            return TaskResult(success=True, review_ready=False, message="task interrupted")
        resume_thread_id = task.get("resume_thread_id")
        if resume_thread_id is not None and (not isinstance(resume_thread_id, str) or not resume_thread_id):
            raise RuntimeError("recovery task has an invalid saved thread id")
        if isinstance(resume_thread_id, str):
            saved_worktree = task.get("worktree_path", task.get("worktree"))
            if not isinstance(saved_worktree, str) or not saved_worktree:
                raise RuntimeError("recovery task has no saved worktree")
            worktree_info = task
            worktree = Path(saved_worktree).resolve()
            state_root = getattr(self.config, "state_root", None)
            if state_root is None:
                raise RuntimeError("recovery task has no trusted Bridge worktree root")
            try:
                worktree_root = (Path(state_root) / "worktrees").resolve()
                relative = worktree.relative_to(worktree_root)
            except (OSError, ValueError) as exc:
                raise RuntimeError("recovery task worktree is outside the trusted Bridge worktree root") from exc
            if relative == Path("."):
                raise RuntimeError("recovery task worktree is not a trusted Bridge worktree")
        else:
            worktree_info = self.worktree_preparer(project_name, task_id)
            worktree = Path(_value(worktree_info, "path", worktree_info)).resolve()
        if not worktree.is_dir():
            raise RuntimeError(f"prepared worktree does not exist: {worktree}")
        self.store.update_task(
            task_id,
            worktree=str(worktree),
            worktree_path=str(worktree),
            branch=_value(worktree_info, "branch"),
            base_branch=_value(worktree_info, "base_branch"),
            base_head=_value(worktree_info, "base_head"),
        )
        run_kind = "rework" if isinstance(resume_thread_id, str) and task.get("rework_instruction") else "initial"
        run = self.store.begin_run(task_id, run_kind, requested_thread_id=resume_thread_id if run_kind == "rework" else None)
        events_path = self.store.task_dir(task_id) / run["events_path"]
        try:
            record = self.session_manager.start_task(
                task_id,
                project_name,
                worktree,
                instruction,
                model=task.get("model"),
                reasoning_effort=task.get("reasoning_effort"),
                resume_thread_id=resume_thread_id,
                events_path=events_path,
                manage_task_lifecycle=False,
            )
            self.store.update_task(task_id, thread_id=getattr(record, "thread_id", None), turn_id=getattr(record, "turn_id", None))
            timeout = getattr(getattr(self.config, "codex", None), "turn_timeout_seconds", 86400)
            result = self.session_manager.wait_for_completion(task_id, timeout=float(timeout))
            status = self.session_manager.status(task_id)
            self.store.update_task(
                task_id,
                thread_id=status.get("thread_id"),
                turn_id=status.get("turn_id"),
                changed_files=list(status.get("changed_files", [])),
            )
            artifacts = []
            for changed in list(status.get("changed_files", []))[:100]:
                try:
                    relative = ensure_safe_relative_path(str(changed))
                    artifact_path = resolve_under(worktree, relative)
                except Exception:
                    continue
                if artifact_path.is_file():
                    artifacts.append({"path": relative, "size": artifact_path.stat().st_size, "kind": "file"})
            exit_code = int(getattr(result, "exit_code", 1))
            interrupted = exit_code == 130 and status.get("state") == "INTERRUPTED"
            self.store.finish_run(
                task_id,
                run["number"],
                status="completed" if exit_code == 0 else "interrupted" if interrupted else "failed",
                returned_thread_id=getattr(result, "thread_id", None),
                returned_turn_id=getattr(result, "turn_id", None),
                exit_code=exit_code,
            )
            # Import lazily: ``bridge.orchestration`` exposes Supervisor from
            # its package initializer, which also imports this executor.
            from ..orchestration.models import TaskResult

            metadata = {
                "thread_id": status.get("thread_id"),
                "turn_id": status.get("turn_id"),
                "changed_files": list(status.get("changed_files", [])),
            }
            if interrupted:
                return TaskResult(
                    success=True,
                    review_ready=False,
                    message="task interrupted",
                    artifacts=artifacts,
                    metadata=metadata,
                )
            if exit_code != 0:
                raise RuntimeError(f"Codex task exited with code {result.exit_code}")
            return TaskResult(
                success=True,
                review_ready=status.get("state") == "WAITING_REVIEW",
                message=str(getattr(result, "final_message", "")),
                artifacts=artifacts,
                metadata=metadata,
            )
        except Exception as exc:
            if self.store.get_task(task_id).get("state") == "INTERRUPTED":
                self.store.finish_run(task_id, run["number"], status="interrupted")
                from ..orchestration.models import TaskResult

                return TaskResult(success=True, review_ready=False, message="task interrupted")
            self.store.finish_run(task_id, run["number"], status="failed", error=f"{type(exc).__name__}: {exc}")
            raise

