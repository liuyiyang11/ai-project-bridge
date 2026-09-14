from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .config import BridgeConfig, ConfigError
from .executors import ExecutionContext
from .executors.code import CodeExecutor
from .executors.experiment_review import ExperimentReviewExecutor
from .executors.presentation import PresentationExecutor
from .github import GhClient, Issue
from .task_parser import TaskParseError, parse_rework_comment, parse_task_body
from .task_store import TaskStore, utc_now


TASK_LABELS = {"task:code": "code", "task:presentation": "presentation", "task:experiment-review": "experiment-review"}
ACTIVE_STATUSES = {"running", "review", "approved"}


class Dispatcher:
    def __init__(self, config: BridgeConfig, github: Any, *, store: Optional[TaskStore] = None, runner: Any = None, executors: Optional[dict[str, Any]] = None):
        self.config = config
        self.github: Any = github
        self.store = store or TaskStore(config.state_root)
        self.runner = runner
        self.executors = executors or {
            "code": CodeExecutor(runner=runner),
            "presentation": PresentationExecutor(runner=runner),
            "experiment-review": ExperimentReviewExecutor(),
        }

    def run_once(self) -> list[dict]:
        outcomes: list[dict] = []
        for issue in self.github.list_candidate_issues():
            if "ai-task" not in issue.labels:
                continue
            try:
                outcome = self._process_issue(issue)
            except Exception as exc:
                outcome = self._record_failure(issue, exc)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def _process_issue(self, issue: Issue) -> Optional[dict]:
        status = self._status(issue.labels)
        if status == "ready":
            return self._process_new(issue)
        if status == "review":
            return self._process_rework(issue)
        return None

    def _process_new(self, issue: Issue) -> Optional[dict]:
        if self.store.exists(issue.number):
            state = self.store.load_state(issue.number)
            if state.get("status") in ACTIVE_STATUSES or state.get("status") == "failed":
                return None
        try:
            task = parse_task_body(issue.body)
            task_type = self._task_type_from_labels(issue.labels)
            if task.task_type != task_type:
                raise TaskParseError(f"task type {task.task_type!r} does not match Issue label task:{task_type}")
            project = self.config.project(task.project)
            if task.task_type not in project.capabilities:
                raise ConfigError(f"project {task.project!r} does not support task type {task.task_type!r}")
            if not self.config.is_trusted_github_login(issue.author_login):
                return None
        except Exception:
            raise
        if not self.store.exists(issue.number):
            self.store.initialize(
                issue.number,
                issue.body,
                {
                    "status": "ready",
                    "project": task.project,
                    "task_type": task.task_type,
                    "title": task.title,
                    "issue_url": issue.url,
                    "author_login": issue.author_login,
                },
            )
        if not self.store.claim_new(issue.number):
            return None
        self.github.set_status(issue.number, "running")
        self.github.comment(issue.number, "Bridge accepted this task and started local execution.")
        return self._execute(issue, task, project, rework_instruction=None)

    def _process_rework(self, issue: Issue) -> Optional[dict]:
        if not self.store.exists(issue.number):
            return None
        state = self.store.load_state(issue.number)
        if state.get("status") != "review":
            return None
        if not self.config.is_trusted_github_login(issue.author_login or state.get("author_login")):
            return None
        full_issue = self.github.view_issue(issue.number)
        for comment in reversed(full_issue.comments):
            if "AI_BRIDGE_REWORK" not in comment.body:
                continue
            if not self.config.is_trusted_github_login(comment.author_login):
                continue
            if not self.store.claim_rework_comment(issue.number, comment.id):
                continue
            try:
                instruction = parse_rework_comment(comment.body)
                task = parse_task_body(self.store.task_path(issue.number, "task.yaml").read_text(encoding="utf-8"))
                project = self.config.project(task.project)
                if task.task_type not in project.capabilities:
                    raise ConfigError(f"project {task.project!r} does not support task type {task.task_type!r}")
                if task.task_type not in {"code", "presentation"}:
                    raise RuntimeError("only code and presentation tasks support Codex rework in V0.1")
                self.github.set_status(issue.number, "running")
                self.github.comment(issue.number, f"Bridge accepted rework comment {comment.id} and resumed the local task.")
                return self._execute(issue, task, project, rework_instruction=instruction)
            except Exception as exc:
                return self._record_failure(issue, exc, preserve_review=False)
        return None

    def _execute(self, issue: Issue, task: Any, project: Any, rework_instruction: Optional[str]) -> dict:
        executor = self.executors[task.task_type]
        context = ExecutionContext(
            self.config,
            issue,
            task,
            project,
            self.store,
            self.store.task_dir(issue.number),
            self.github,
            self.github.for_repo(project.repo),
            self.runner,
        )
        try:
            result = executor.execute(context, rework_instruction=rework_instruction)
            result = dict(result or {})
            result["status"] = "review"
            self._write_bundle(issue, task, result)
            self.store.write_result(issue.number, result)
            self.store.update_state(issue.number, status="review", finished_at=utc_now(), thread_id=result.get("thread_id"), pr_url=result.get("pr_url"), last_error=None)
            self.github.set_status(issue.number, "review")
            summary = str(result.get("final_message") or "Task completed; review bundle is ready.").strip().replace("\n", " ")[:500]
            self.github.comment(issue.number, f"Bridge completed the task and prepared review artifacts. {summary}")
            return {"issue_number": issue.number, "status": "review", **result}
        except Exception:
            raise

    def _record_failure(self, issue: Issue, error: Exception, *, preserve_review: bool = False) -> dict:
        message = f"{type(error).__name__}: {error}"
        if not self.store.exists(issue.number):
            self.store.initialize(issue.number, issue.body, {"status": "failed", "issue_url": issue.url})
        state = self.store.update_state(issue.number, status="review" if preserve_review else "failed", finished_at=utc_now(), last_error=message)
        failure_result = {"status": "review" if preserve_review else "failed", "error": message}
        if state.get("runs"):
            failure_result["runs"] = state["runs"]
            failure_result["run"] = state["runs"][-1]
        self.store.write_result(issue.number, failure_result)
        try:
            self.github.set_status(issue.number, "review" if preserve_review else "failed")
            self.github.comment(issue.number, f"Bridge {'could not complete rework' if preserve_review else 'rejected or failed this task'}: {message[:700]}")
        except Exception:
            pass
        return {"issue_number": issue.number, "status": "review" if preserve_review else "failed", "error": message}

    def _write_bundle(self, issue: Issue, task: Any, result: dict) -> None:
        bundle_dir = Path(result.get("bundle_dir") or (self.store.task_dir(issue.number) / "review_bundle"))
        bundle_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "task_id": f"issue-{issue.number}",
            "issue_number": issue.number,
            "task_type": task.task_type,
            "project": task.project,
            "source_issue": task.source_issue,
            "status": "review",
            "started_at": self.store.load_state(issue.number).get("started_at"),
            "finished_at": utc_now(),
            "codex_thread_id": result.get("thread_id"),
            "runs": result.get("runs", self.store.load_state(issue.number).get("runs", [])),
            "changed_files": result.get("changed_files", []),
            "tests": result.get("tests", []),
            "artifact_list": result.get("artifact_list", []),
            "pr_url": result.get("pr_url"),
            "known_limitations": result.get("known_limitations", ""),
        }
        (bundle_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary = bundle_dir / "summary.md"
        if not summary.exists():
            summary.write_text(
                f"# Bridge review bundle\n\n- Issue: #{issue.number}\n- Task type: `{task.task_type}`\n- Project: `{task.project}`\n- Status: `review`\n\n"
                + str(result.get("final_message") or "Deterministic review completed.")
                + "\n",
                encoding="utf-8",
            )

    @staticmethod
    def _status(labels: set[str]) -> Optional[str]:
        statuses = [label.removeprefix("status:") for label in labels if label in {f"status:{name}" for name in ("ready", "running", "review", "failed", "approved")}]
        return statuses[0] if len(statuses) == 1 else None

    @staticmethod
    def _task_type_from_labels(labels: set[str]) -> str:
        matches = [TASK_LABELS[label] for label in labels if label in TASK_LABELS]
        if len(matches) != 1:
            raise TaskParseError("Issue must have exactly one task type label")
        return matches[0]

