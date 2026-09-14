from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, validator

from .security import SecurityError, ensure_safe_relative_path


class ConfigError(ValueError):
    """Raised for missing, malformed, or unsafe local configuration."""


class AllowedCommand(BaseModel):
    class Config:
        extra = "forbid"

    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=3600, ge=1, le=86400)

    @validator("argv")
    def validate_argv(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("argv entries must be non-empty strings")
        return value


class ProjectConfig(BaseModel):
    class Config:
        extra = "forbid"

    kind: Literal["code", "presentation", "experiment-review"]
    root: Path
    repo: str
    allowed_commands: dict[str, AllowedCommand] = Field(default_factory=dict)
    artifact_dirs: list[str] = Field(default_factory=list)

    @validator("repo")
    def validate_repo(cls, value: str) -> str:
        if "/" not in value or value.startswith("/") or " " in value:
            raise ValueError("repo must be in OWNER/REPOSITORY form")
        return value

    @validator("artifact_dirs")
    def validate_artifact_dirs(cls, value: list[str]) -> list[str]:
        try:
            return [ensure_safe_relative_path(item) for item in value]
        except SecurityError as exc:
            raise ValueError(str(exc)) from exc


class BundleLimits(BaseModel):
    class Config:
        extra = "forbid"

    max_artifact_file_mb: int = Field(default=25, ge=1, le=1024)
    max_bundle_mb: int = Field(default=100, ge=1, le=4096)
    max_images: int = Field(default=20, ge=0, le=1000)


class BridgeConfig(BaseModel):
    class Config:
        extra = "forbid"

    control_repo: str
    poll_seconds: int = Field(default=30, ge=1, le=86400)
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)
    limits: BundleLimits = Field(default_factory=BundleLimits)
    state_dir: str = ".bridge"
    gh_binary: str = "gh"
    codex_binary: str = "codex"
    config_path: Path = Field(default=Path("config.local.yaml"), exclude=True)

    @validator("control_repo")
    def validate_control_repo(cls, value: str) -> str:
        if "/" not in value or value.startswith("/") or " " in value:
            raise ValueError("control_repo must be in OWNER/REPOSITORY form")
        return value

    @validator("state_dir")
    def validate_state_dir(cls, value: str) -> str:
        try:
            return ensure_safe_relative_path(value)
        except SecurityError as exc:
            raise ValueError(str(exc)) from exc

    def project(self, name: str) -> ProjectConfig:
        try:
            return self.projects[name]
        except KeyError as exc:
            raise ConfigError(f"project is not registered: {name}") from exc

    @property
    def state_root(self) -> Path:
        return (self.config_path.parent / self.state_dir).resolve()


def load_config(path: str | Path = "config.local.yaml") -> BridgeConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config = BridgeConfig.parse_obj(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"invalid config: {exc}") from exc
    config.config_path = config_path
    for name, project in config.projects.items():
        if not name or name in {".", ".."} or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in name):
            raise ConfigError(f"unsafe project name: {name!r}")
        project.root = project.root.expanduser().resolve()
    return config
