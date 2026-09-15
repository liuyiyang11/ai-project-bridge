from __future__ import annotations

from typing import Any

from ..github import Issue
from ..task_parser import parse_task_body
from . import ExecutionContext
from .code import CodeExecutor, _value


class GitHubCodeTaskExecutor:
    """Adapt the mature GitHub code executor to the shared runtime.

    GitHub remains responsible for supplying a validated Issue and publishing
    its result, while TaskSupervisor/TaskRunner own queueing and lifecycle.
    The wrapped CodeExecutor therefore runs with lifecycle management off;
    TaskRunner performs the single durable RUNNING -> WAITING_REVIEW
    transition after this executor returns.
    """

    def __init__(self, config: Any, store: Any, github: Any, *, runner: Any = None, session_manager: Any = None):
        self.config = config
        self.store = store
        self.github = github
        self.runner = runner
        self.session_manager = session_manager
        self.executor = CodeExecutor(
            runner=runner,
            session_manager=session_manager,
            manage_task_lifecycle=False,
        )

    def execute(self, task: dict[str, Any]) -> "TaskResult":
        if not isinstance(task, dict) or task.get("task_type") != "code":
            raise ValueError("GitHub code executor requires a code task object")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id or not isinstance(task.get("project"), str) or not task["project"].strip():
            raise ValueError("GitHub code task has an invalid task id or project")
        body = task.get("github_issue_body")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("GitHub code task is missing its validated Issue body")
        issue_number = task.get("github_issue_number")
        if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0:
            raise ValueError("GitHub code task is missing a valid Issue number")

        bridge_task = parse_task_body(body)
        project = self.config.project(bridge_task.project)
        issue = Issue(
            number=issue_number,
            title=str(task.get("github_issue_title") or bridge_task.title),
            body=body,
            url=str(task.get("github_issue_url") or ""),
            labels={"ai-task", "task:code", "status:running"},
            author_login=task.get("github_author_login"),
        )
        context = ExecutionContext(
            self.config,
            issue,
            bridge_task,
            project,
            self.store,
            self.store.task_dir(task_id),
            self.github,
            self.github.for_repo(project.repo),
            self.runner,
            self.session_manager,
        )
        result = self.executor.execute(
            context,
            rework_instruction=task.get("rework_instruction"),
            resume_thread_id=task.get("resume_thread_id"),
        )
        from ..orchestration.models import TaskResult

        # Keep the complete, bounded publication payload durable so the
        # transport can finish the GitHub side after the shared worker returns.
        return TaskResult(
            success=True,
            review_ready=True,
            message=str(_value(result, "final_message", "")),
            metadata={"github_result": dict(result or {})},
        )
