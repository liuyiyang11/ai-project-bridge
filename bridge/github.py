from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional


class GhError(RuntimeError):
    """Raised for unavailable or failed GitHub CLI operations."""


def find_executable(name: str) -> Optional[str]:
    if Path(name).is_file():
        return str(Path(name).resolve())
    found = shutil.which(name)
    if found:
        return found
    if name.lower() == "gh" and Path(r"C:\Program Files\GitHub CLI\gh.exe").is_file():
        return str(Path(r"C:\Program Files\GitHub CLI\gh.exe"))
    if name.lower() in {"codex", "codex.exe"}:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            install_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
            candidates = [child / "codex.exe" for child in install_root.iterdir() if child.is_dir() and (child / "codex.exe").is_file()] if install_root.is_dir() else []
            if candidates:
                return str(max(candidates, key=lambda path: path.stat().st_mtime).resolve())
    return None


@dataclass(frozen=True)
class IssueComment:
    id: str
    body: str
    created_at: str = ""


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    url: str
    labels: set[str] = field(default_factory=set)
    comments: list[IssueComment] = field(default_factory=list)


_STATUS_LABELS = {"status:ready", "status:running", "status:review", "status:failed", "status:approved"}


class GhClient:
    def __init__(self, binary: str, repo: str, run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None):
        self.binary = binary
        self.repo = repo
        self._run_fn = run or subprocess.run

    def _run(self, args: Iterable[str], *, input: Optional[str] = None) -> subprocess.CompletedProcess[str]:
        argv = [self.binary, *args]
        try:
            result = self._run_fn(argv, input=input, capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, check=False)
        except FileNotFoundError as exc:
            raise GhError("GitHub CLI (gh) is not installed or is not on PATH") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise GhError(f"gh command failed: {detail}")
        return result

    def auth_status(self) -> subprocess.CompletedProcess[str]:
        return self._run(["auth", "status"])

    def list_candidate_issues(self) -> list[Issue]:
        result = self._run(
            [
                "issue",
                "list",
                "--repo",
                self.repo,
                "--state",
                "open",
                "--label",
                "ai-task",
                "--limit",
                "100",
                "--json",
                "number,title,body,url,labels",
            ]
        )
        return [self._issue_from_json(item) for item in json.loads(result.stdout or "[]")]

    def view_issue(self, number: int) -> Issue:
        result = self._run(
            [
                "issue",
                "view",
                str(number),
                "--repo",
                self.repo,
                "--json",
                "number,title,body,url,labels,comments",
            ]
        )
        return self._issue_from_json(json.loads(result.stdout))

    def comment(self, number: int, body: str) -> None:
        self._run(["issue", "comment", str(number), "--repo", self.repo, "--body-file", "-"], input=body)

    def set_status(self, number: int, status: str) -> None:
        label = f"status:{status}"
        if label not in _STATUS_LABELS:
            raise GhError(f"unsupported status label: {status}")
        args = ["issue", "edit", str(number), "--repo", self.repo, "--add-label", label]
        args.extend([flag for old in sorted(_STATUS_LABELS - {label}) for flag in ("--remove-label", old)])
        self._run(args)

    def create_draft_pr(self, branch: str, base: str, title: str, body: str) -> str:
        if branch in {"main", "master"} or not re.fullmatch(r"ai/issue-[0-9]+", branch):
            raise GhError(f"refusing unsafe branch for PR: {branch}")
        result = self._run(
            [
                "pr",
                "create",
                "--repo",
                self.repo,
                "--head",
                branch,
                "--base",
                base,
                "--title",
                title,
                "--body-file",
                "-",
                "--draft",
            ],
            input=body,
        )
        return (result.stdout or "").strip().splitlines()[-1]

    def ensure_labels(self, labels: Iterable[str]) -> list[str]:
        existing_result = self._run(["label", "list", "--repo", self.repo, "--limit", "1000", "--json", "name"])
        existing = {item["name"] for item in json.loads(existing_result.stdout or "[]")}
        created: list[str] = []
        for label in labels:
            if label in existing:
                continue
            self._run(["label", "create", label, "--repo", self.repo, "--color", "6f42c1", "--description", "AI Project Bridge label"])
            created.append(label)
        return created

    @staticmethod
    def _issue_from_json(item: dict) -> Issue:
        comments = [
            IssueComment(str(comment.get("id") or comment.get("databaseId") or ""), comment.get("body", ""), comment.get("createdAt", ""))
            for comment in item.get("comments", [])
        ]
        return Issue(
            number=int(item["number"]),
            title=item.get("title", ""),
            body=item.get("body", "") or "",
            url=item.get("url", ""),
            labels={label.get("name", "") for label in item.get("labels", [])},
            comments=comments,
        )
