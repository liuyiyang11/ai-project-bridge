from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from ..commands import run_registered_command
from ..collectors.artifact_collector import collect_artifacts
from ..config import ProjectConfig
from ..task_parser import parse_task_body
from ..worktree import WorktreeManager
from . import ExecutionContext


class SampleSelector:
    """Reserved interface for future worst/regression/improvement/random strategies."""

    def select(self, records: list[dict], strategy: str = "random", limit: int = 10) -> list[dict]:
        return records[:limit]


class ExperimentReviewExecutor:
    def __init__(self, sample_selector: Optional[SampleSelector] = None, manager_factory=None, *, publish: bool = True):
        self.sample_selector = sample_selector or SampleSelector()
        self.manager_factory = manager_factory
        self.publish = publish

    def execute(self, context: ExecutionContext, rework_instruction: Optional[str] = None) -> dict:
        if rework_instruction:
            raise RuntimeError("experiment-review tasks do not resume Codex threads in V0.1")
        evaluate_root = self._evaluation_root(context)
        command_result = run_registered_command(context.project, context.task.command_id or "", evaluate_root, context.config.python_executable)
        context.store.append_stdout(context.issue.number, command_result.stdout or "")
        context.store.append_stderr(context.issue.number, command_result.stderr or "")
        tests = [{"command_id": context.task.command_id, "returncode": command_result.returncode, "stdout": command_result.stdout[-4000:], "stderr": command_result.stderr[-4000:]}]
        if command_result.returncode != 0:
            raise RuntimeError(f"configured experiment command failed with code {command_result.returncode}")
        bundle_dir = context.task_dir / "review_bundle"
        records = collect_artifacts(evaluate_root, context.project.artifact_dirs, bundle_dir, context.config.limits)
        summary = bundle_dir / "summary.md"
        summary.write_text(
            f"# Experiment review\n\nCommand: `{context.task.command_id}`\n\nCollected artifacts: {len(records)}\n\n"
            + ("\n".join(f"- `{item['source']}` ({item['bytes']} bytes)" for item in records) or "No bounded artifacts were eligible for collection.")
            + "\n",
            encoding="utf-8",
        )
        (bundle_dir / "metrics.json").write_text(json.dumps({"command_id": context.task.command_id, "artifacts": records}, indent=2), encoding="utf-8")
        pr_url = None
        limitation = "No automatic worst-sample or regression selection is performed in V0.1."
        if self.publish:
            bundle_bytes = sum(path.stat().st_size for path in bundle_dir.rglob("*") if path.is_file())
            if bundle_bytes > context.config.limits.max_bundle_mb * 1024 * 1024:
                limitation += f" Bundle was not uploaded because it is {bundle_bytes} bytes, above max_bundle_mb={context.config.limits.max_bundle_mb}."
            else:
                pr_url = self._publish_bundle(context, bundle_dir)
        else:
            limitation += " Bundle publication was disabled by the caller."
        return {
            "thread_id": None,
            "final_message": f"Experiment command {context.task.command_id} completed. {limitation}",
            "changed_files": [],
            "tests": tests,
            "artifact_list": records + [{"path": "summary.md", "kind": "summary"}, {"path": "metrics.json", "kind": "metrics"}],
            "bundle_dir": str(bundle_dir),
            "pr_url": pr_url,
            "known_limitations": limitation,
        }

    def _publish_bundle(self, context: ExecutionContext, bundle_dir: Path) -> Optional[str]:
        total_bytes = sum(path.stat().st_size for path in bundle_dir.rglob("*") if path.is_file())
        max_bytes = context.config.limits.max_bundle_mb * 1024 * 1024
        if total_bytes > max_bytes:
            return None
        manager = (
            self.manager_factory(context.project, context.config.state_root / "worktrees")
            if self.manager_factory
            else WorktreeManager(
                context.project.root,
                context.config.state_root / "worktrees",
                context.project.remote,
                context.project.base_branch,
            )
        )
        info = manager.prepare(context.task.project, context.issue.number)
        target = info.path / "review_bundle"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(bundle_dir, target)
        manager.commit(info.path, f"ai: issue {context.issue.number} experiment review")
        manager.push(info.path, info.branch)
        body = f"Control task: {context.config.control_repo}#{context.issue.number}\n\nBounded experiment review bundle for `{context.task.project}`.\n\nBundle size: {total_bytes} bytes.\n\nNo automatic merge is performed.\n"
        return context.project_github.create_draft_pr(info.branch, info.base_branch, f"Review: {context.task.title}", body)

    def _evaluation_root(self, context: ExecutionContext) -> Path:
        source_issue = context.task.source_issue
        if source_issue is None:
            return context.project.root

        if not context.store.exists(source_issue):
            raise RuntimeError(f"source code task does not exist locally: issue {source_issue}")
        source_state = context.store.load_state(source_issue)
        source_yaml = context.store.task_path(source_issue, "task.yaml").read_text(encoding="utf-8")
        source_task = parse_task_body(source_yaml)
        if source_task.project != context.task.project or source_state.get("project") != context.task.project:
            raise RuntimeError(f"source issue {source_issue} belongs to a different project")
        if source_task.task_type != "code" or source_state.get("task_type") != "code":
            raise RuntimeError(f"source issue {source_issue} is not a code task")

        worktree_value = source_state.get("worktree_path")
        branch = source_state.get("branch")
        if not isinstance(worktree_value, str) or not worktree_value or not Path(worktree_value).is_absolute():
            raise RuntimeError(f"source issue {source_issue} has no trusted worktree_path")
        manager = (
            self.manager_factory(context.project, context.config.state_root / "worktrees")
            if self.manager_factory
            else WorktreeManager(
                context.project.root,
                context.config.state_root / "worktrees",
                context.project.remote,
                context.project.base_branch,
            )
        )
        candidate = Path(worktree_value).resolve()
        expected = (context.config.state_root / "worktrees" / f"{context.task.project}-issue-{int(source_issue)}").resolve()
        if candidate != expected or branch != manager.branch_for_issue(source_issue):
            raise RuntimeError(f"source issue {source_issue} has an untrusted worktree_path")
        if not candidate.is_dir():
            raise RuntimeError(f"source issue {source_issue} worktree no longer exists: {candidate}")
        check = manager._git(["rev-parse", "--show-toplevel"], cwd=candidate, check=False)
        current_branch = manager._git(["branch", "--show-current"], cwd=candidate, check=False).stdout.strip()
        if check.returncode != 0 or current_branch != branch or not manager.is_registered_worktree(candidate, branch):
            raise RuntimeError(f"source issue {source_issue} worktree is not a trusted Git worktree")
        return candidate
