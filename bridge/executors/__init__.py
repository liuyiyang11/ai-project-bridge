from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import BridgeConfig, ProjectConfig
from ..github import GhClient, Issue
from ..task_parser import BridgeTask
from ..task_store import TaskStore


@dataclass
class ExecutionContext:
    config: BridgeConfig
    issue: Issue
    task: BridgeTask
    project: ProjectConfig
    store: TaskStore
    task_dir: Path
    github: Any
    runner: Any

