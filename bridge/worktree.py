from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class WorktreeError(RuntimeError):
    """Raised for unsafe or failed git worktree operations."""


@dataclass(frozen=True)
class WorktreeInfo:
    project: str
    issue_number: int
    branch: str
    path: Path
    base_branch: str
    base_head: str


class WorktreeManager:
    def __init__(self, repo_root: Path, worktree_root: Path, remote: str = "origin", base_branch: Optional[str] = None):
        self.repo_root = Path(repo_root).resolve()
        self.worktree_root = Path(worktree_root).resolve()
        self.remote = remote
        self.configured_base_branch = base_branch

    def _git(self, args: list[str], cwd: Optional[Path] = None, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["git", *args], cwd=cwd or self.repo_root, capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, check=False)
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "git command failed").strip()
            raise WorktreeError(f"git {' '.join(args)} failed: {detail}")
        return result

    def ensure_repository(self) -> None:
        result = self._git(["rev-parse", "--show-toplevel"], check=False)
        if result.returncode != 0:
            raise WorktreeError(f"project root is not a Git repository: {self.repo_root}")

    def default_branch(self) -> str:
        if self.configured_base_branch:
            return self.configured_base_branch

        remote_head = self._git(["symbolic-ref", "--short", f"refs/remotes/{self.remote}/HEAD"], check=False).stdout.strip()
        prefix = f"{self.remote}/"
        if remote_head.startswith(prefix):
            branch = remote_head.removeprefix(prefix)
            if not self._is_generated_issue_branch(branch):
                return branch
            raise WorktreeError(f"remote default branch cannot be a generated issue branch: {branch}")

        remote_exists = self._git(["remote", "get-url", self.remote], check=False).returncode == 0
        if remote_exists:
            remote_info = self._git(["ls-remote", "--symref", self.remote, "HEAD"], check=False).stdout
            match = re.search(r"ref:\s+refs/heads/([^\s]+)\s+HEAD", remote_info)
            if match and not self._is_generated_issue_branch(match.group(1)):
                return match.group(1)
            raise WorktreeError(f"could not determine a safe default branch from remote {self.remote!r}")

        # Standalone temporary repositories used by tests may not have a remote.
        # Only the conventional protected names are safe fallbacks; never use an
        # arbitrary current feature branch as the base.
        current = self._git(["branch", "--show-current"], check=False).stdout.strip()
        if current in {"main", "master"}:
            return current
        raise WorktreeError("could not determine remote default branch; configure base_branch")

    @staticmethod
    def _is_generated_issue_branch(branch: str) -> bool:
        return re.fullmatch(r"ai/issue-[0-9]+", branch) is not None

    def base_ref(self, branch: Optional[str] = None) -> str:
        branch = branch or self.default_branch()
        local_ref = f"refs/heads/{branch}"
        if self._git(["show-ref", "--verify", local_ref], check=False).returncode == 0:
            return branch
        remote_ref = f"refs/remotes/{self.remote}/{branch}"
        if self._git(["show-ref", "--verify", remote_ref], check=False).returncode == 0:
            return f"{self.remote}/{branch}"
        raise WorktreeError(f"base branch does not exist locally or on remote {self.remote!r}: {branch}")

    def remote_url(self) -> Optional[str]:
        result = self._git(["remote", "get-url", self.remote], check=False)
        value = (result.stdout or "").strip()
        return value or None

    def is_registered_worktree(self, path: Path, branch: str) -> bool:
        expected_path = os.path.normcase(str(Path(path).resolve()))
        current_path: Optional[str] = None
        result = self._git(["worktree", "list", "--porcelain"], check=False)
        for line in (result.stdout or "").splitlines():
            if line.startswith("worktree "):
                current_path = os.path.normcase(str(Path(line.removeprefix("worktree ")).resolve()))
            elif line == f"branch refs/heads/{branch}" and current_path == expected_path:
                return True
        return False

    def head(self) -> str:
        return self._git(["rev-parse", "HEAD"]).stdout.strip()

    @staticmethod
    def branch_for_issue(issue_number: int) -> str:
        if int(issue_number) <= 0:
            raise WorktreeError("issue number must be positive")
        return f"ai/issue-{int(issue_number)}"

    @staticmethod
    def assert_publishable_branch(branch: str) -> None:
        if branch in {"main", "master"}:
            raise WorktreeError(f"protected branch cannot be pushed: {branch}")
        if not re.fullmatch(r"ai/issue-[0-9]+", branch):
            raise WorktreeError(f"Bridge only publishes generated issue branches: {branch}")

    def prepare(self, project: str, issue_number: int) -> WorktreeInfo:
        self.ensure_repository()
        branch = self.branch_for_issue(issue_number)
        self.assert_publishable_branch(branch)
        base_branch = self.default_branch()
        base_ref = self.base_ref(base_branch)
        base_head = self._git(["rev-parse", base_ref]).stdout.strip()
        path = self.worktree_root / f"{project}-issue-{int(issue_number)}"
        self.worktree_root.mkdir(parents=True, exist_ok=True)

        if path.exists():
            check = self._git(["rev-parse", "--show-toplevel"], cwd=path, check=False)
            if check.returncode != 0:
                raise WorktreeError(f"worktree path exists but is not a Git worktree: {path}")
            existing_branch = self._git(["branch", "--show-current"], cwd=path).stdout.strip()
            if existing_branch != branch:
                raise WorktreeError(f"existing worktree has branch {existing_branch!r}, expected {branch!r}")
        else:
            branch_exists = self._git(["show-ref", "--verify", f"refs/heads/{branch}"], check=False).returncode == 0
            if branch_exists:
                self._git(["worktree", "add", str(path), branch])
            else:
                self._git(["worktree", "add", "-b", branch, str(path), base_ref])
        return WorktreeInfo(project, int(issue_number), branch, path, base_branch, base_head)

    def cleanup(self, info: WorktreeInfo) -> None:
        if info.path.exists():
            self._git(["worktree", "remove", "--force", str(info.path)])
        self._git(["worktree", "prune"], check=False)

    def changed_files(self, cwd: Path) -> list[str]:
        output = self._git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=cwd).stdout
        changed: list[str] = []
        for line in output.splitlines():
            if len(line) >= 4:
                value = line[3:]
                if " -> " in value:
                    value = value.split(" -> ", 1)[1]
                changed.append(value)
        return changed

    def diff_stat(self, cwd: Path) -> str:
        return self._git(["diff", "--stat"], cwd=cwd).stdout.strip()

    def diff(self, cwd: Path) -> str:
        return self._git(["diff"], cwd=cwd).stdout

    def commit(self, cwd: Path, message: str) -> str:
        self._git(["add", "-A"], cwd=cwd)
        staged = self._git(["diff", "--cached", "--quiet"], cwd=cwd, check=False)
        if staged.returncode == 0:
            raise WorktreeError("no changes to commit")
        self._git(["commit", "-m", message], cwd=cwd)
        return self._git(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()

    def push(self, cwd: Path, branch: str) -> None:
        self.assert_publishable_branch(branch)
        self._git(["push", "--set-upstream", self.remote, branch], cwd=cwd)
