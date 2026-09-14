from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

from .collectors.presentation_renderer import PresentationRenderer
from .config import ConfigError, load_config
from .codex.runner import CodexRunner
from .codex.session import CodexSessionManager
from .dispatcher import Dispatcher
from .github import GhClient, GhError, find_executable
from .task_store import TaskStore
from .worktree import WorktreeError, WorktreeManager


LABELS = [
    "ai-task",
    "task:code",
    "task:presentation",
    "task:experiment-review",
    "status:ready",
    "status:running",
    "status:review",
    "status:failed",
    "status:approved",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge", description="Safe local AI Project Bridge")
    parser.add_argument("--config", default=None, help="path to config.local.yaml")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "run", "run-once", "setup-github"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", dest="command_config", default=None)
    mcp = commands.add_parser("mcp-stdio", help="serve the local MCP tool surface over stdio")
    mcp.add_argument("--config", dest="command_config", default=None)
    show = commands.add_parser("show-task")
    show.add_argument("issue_number", type=int)
    show.add_argument("--config", dest="command_config", default=None)
    retry = commands.add_parser("retry")
    retry.add_argument("issue_number", type=int)
    retry.add_argument("--config", dest="command_config", default=None)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.command_config or args.config or "config.local.yaml"
    if args.command == "doctor":
        return doctor(config_path)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "show-task":
        return show_task(config, args.issue_number)
    if args.command == "retry":
        return retry(config, args.issue_number)
    if args.command == "setup-github":
        return setup_github(config)
    if args.command == "mcp-stdio":
        from .mcp.server import McpStdioServer
        from .mcp.tools import BridgeMcpTools

        McpStdioServer(BridgeMcpTools(config=config)).serve()
        return 0
    if args.command in {"run", "run-once"}:
        return run_bridge(config, once=args.command == "run-once")
    return 2


def make_github_client(config):
    binary = find_executable(config.gh_binary) or config.gh_binary
    return GhClient(binary, config.control_repo)


def doctor(config_path: Union[str, Path]) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python", sys.version_info >= (3, 9), sys.version.split()[0]))
    checks.append(("Git", bool(shutil.which("git")), shutil.which("git") or "not found"))
    config = None
    try:
        config = load_config(config_path)
        checks.append(("Configuration", True, str(Path(config_path).resolve())))
    except ConfigError as exc:
        checks.append(("Configuration", False, str(exc)))
    gh_binary = find_executable("gh")
    checks.append(("GitHub CLI", gh_binary is not None, gh_binary or "not found"))
    if gh_binary:
        try:
            GhClient(gh_binary, "owner/placeholder").auth_status()
            checks.append(("gh auth", True, "authenticated"))
        except GhError as exc:
            checks.append(("gh auth", False, str(exc)))
    else:
        checks.append(("gh auth", False, "GitHub CLI is not installed or not on PATH"))
    codex_runner = CodexRunner(config.codex_binary if config else "codex")
    checks.append(("Codex CLI", codex_runner.executable is not None, codex_runner.executable or "not found"))
    if config:
        if config.python_executable:
            checks.append(("Configured Python", config.python_executable.is_file(), str(config.python_executable)))
        for name, project in config.projects.items():
            checks.extend(project_repository_checks(name, project))
    capabilities = PresentationRenderer().detect_capabilities()
    # These are optional presentation capabilities.  Report them explicitly
    # without making ordinary code/experiment Bridge health depend on Office.
    checks.append(("PowerPoint COM", True, "available" if capabilities.powerpoint_com else "unavailable"))
    checks.append(("LibreOffice", True, "available" if capabilities.libreoffice else "unavailable"))
    checks.append(("PDF-to-PNG renderer", True, "available" if capabilities.pdf_to_png else "unavailable"))
    for name, ok, detail in checks:
        print(f"[{ 'OK' if ok else 'FAIL' }] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def subprocess_git_ok(root: Path) -> bool:
    import subprocess

    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, check=False)
    return result.returncode == 0


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )


def normalize_remote_repo(url: str) -> Optional[str]:
    """Normalize a GitHub remote URL to OWNER/REPOSITORY without reading credentials."""
    value = url.strip()
    if not value:
        return None
    if value.startswith("git@"):
        host_and_path = value[4:].split(":", 1)
        if len(host_and_path) != 2 or host_and_path[0].casefold() != "github.com":
            return None
        path = host_and_path[1]
    else:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https", "ssh", "git"} or (parsed.hostname or "").casefold() != "github.com":
            return None
        path = parsed.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        return None
    return "/".join(parts)


def project_repository_checks(name: str, project) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    root = Path(project.root)
    is_git = root.is_dir() and subprocess_git_ok(root)
    checks.append((f"Project {name} repository", is_git, str(root) if is_git else f"not a Git repository: {root}"))
    if not is_git:
        checks.extend(
            [
                (f"Project {name} remote {project.remote}", False, "repository check unavailable"),
                (f"Project {name} base branch", False, "repository check unavailable"),
            ]
        )
        return checks

    manager = WorktreeManager(root, root / ".bridge-worktrees", project.remote, project.base_branch)
    remote_url_result = _git(root, "remote", "get-url", project.remote)
    remote_url = (remote_url_result.stdout or "").strip()
    remote_ok = remote_url_result.returncode == 0 and bool(remote_url)
    checks.append((f"Project {name} remote {project.remote}", remote_ok, remote_url or "remote does not exist"))
    normalized = normalize_remote_repo(remote_url) if remote_ok else None
    expected = project.repo.casefold()
    checks.append(
        (
            f"Project {name} remote/repo",
            normalized is not None and normalized.casefold() == expected,
            f"configured {project.repo}; remote {normalized or remote_url or 'unavailable'}",
        )
    )
    if not remote_ok or normalized is None or normalized.casefold() != expected:
        checks.append((f"Project {name} base branch", False, "cannot validate until the configured remote matches project.repo"))
        return checks
    try:
        base_branch = manager.default_branch()
        base_ref = manager.base_ref(base_branch)
        base_ok = not WorktreeManager._is_generated_issue_branch(base_branch)
        detail = f"{base_branch} ({base_ref})" if base_ok else f"refusing generated issue branch: {base_branch}"
    except WorktreeError as exc:
        base_ok = False
        detail = str(exc)
    checks.append((f"Project {name} base branch", base_ok, detail))
    return checks


def validate_project_repositories(config) -> None:
    failures = [f"{label}: {detail}" for name, project in config.projects.items() for label, ok, detail in project_repository_checks(name, project) if not ok]
    if failures:
        raise ConfigError("project repository checks failed; refusing to execute tasks: " + "; ".join(failures))


def run_bridge(config, *, once: bool) -> int:
    try:
        validate_project_repositories(config)
        github = make_github_client(config)
        store = TaskStore(config.state_root)
        if config.codex.backend == "app-server":
            runner = None
            session_manager = CodexSessionManager(config, store=store)
        else:
            runner = CodexRunner(config.codex_binary)
            session_manager = None
        dispatcher = Dispatcher(config, github, store=store, runner=runner, session_manager=session_manager)
        if once:
            print(json.dumps(dispatcher.run_once(), ensure_ascii=False, indent=2))
            return 0
        print(f"Bridge polling {config.control_repo} every {config.poll_seconds}s. Press Ctrl+C to stop.")
        while True:
            outcomes = dispatcher.run_once()
            if outcomes:
                print(json.dumps(outcomes, ensure_ascii=False, indent=2))
            time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        print("Bridge stopped.")
        return 0
    except (ConfigError, GhError, RuntimeError) as exc:
        print(f"Bridge error: {exc}", file=sys.stderr)
        return 1


def show_task(config, issue_number: int) -> int:
    store = TaskStore(config.state_root)
    if not store.exists(issue_number):
        print(f"No local task state for issue {issue_number}.", file=sys.stderr)
        return 1
    value = {"state": store.load_state(issue_number), "result": json.loads(store.task_path(issue_number, "result.json").read_text(encoding="utf-8"))}
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def retry(config, issue_number: int) -> int:
    store = TaskStore(config.state_root)
    if not store.exists(issue_number):
        print(f"No local task state for issue {issue_number}.", file=sys.stderr)
        return 1
    github = make_github_client(config)
    store.reset_for_retry(issue_number)
    github.set_status(issue_number, "ready")
    github.comment(issue_number, "Bridge retry requested locally; the task is ready for another run.")
    print(f"Issue {issue_number} reset to ready.")
    return 0


def setup_github(config) -> int:
    try:
        created = make_github_client(config).ensure_labels(LABELS)
    except GhError as exc:
        print(f"GitHub setup failed: {exc}", file=sys.stderr)
        return 1
    print("Created labels: " + (", ".join(created) if created else "none; all labels already exist"))
    return 0
