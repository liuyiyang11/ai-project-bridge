from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Optional, Union

from .collectors.presentation_renderer import PresentationRenderer
from .config import ConfigError, load_config
from .codex.runner import CodexRunner
from .dispatcher import Dispatcher
from .github import GhClient, GhError, find_executable
from .task_store import TaskStore


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
            is_git = project.root.is_dir() and (subprocess_git_ok(project.root))
            checks.append((f"Project {name}", is_git, f"{project.root} ({project.repo})" if is_git else f"not a Git repository: {project.root}"))
    capabilities = PresentationRenderer().detect_capabilities()
    checks.append(("Presentation renderer", True, capabilities.summary))
    for name, ok, detail in checks:
        print(f"[{ 'OK' if ok else 'FAIL' }] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def subprocess_git_ok(root: Path) -> bool:
    import subprocess

    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, check=False)
    return result.returncode == 0


def run_bridge(config, *, once: bool) -> int:
    try:
        github = make_github_client(config)
        runner = CodexRunner(config.codex_binary)
        dispatcher = Dispatcher(config, github, runner=runner)
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
    except (GhError, RuntimeError) as exc:
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
