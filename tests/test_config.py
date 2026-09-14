from pathlib import Path

import pytest

from bridge.config import ConfigError, load_config


def write_config(path: Path, root: Path) -> None:
    path.write_text(
        f"""control_repo: owner/bridge
poll_seconds: 5
projects:
  demo:
    kind: code
    root: {root.as_posix()}
    repo: owner/demo
    allowed_commands:
      quick_test:
        argv: [python, -c, 'print(\"ok\")']
    artifact_dirs: [outputs]
""",
        encoding="utf-8",
    )


def test_load_config_resolves_registered_project(tmp_path):
    config_path = tmp_path / "config.local.yaml"
    project_root = tmp_path / "demo"
    project_root.mkdir()
    write_config(config_path, project_root)

    config = load_config(config_path)
    project = config.project("demo")

    assert project.root == project_root.resolve()
    assert project.repo == "owner/demo"
    assert project.allowed_commands["quick_test"].argv[:2] == ["python", "-c"]


def test_unregistered_project_is_rejected(tmp_path):
    config_path = tmp_path / "config.local.yaml"
    write_config(config_path, tmp_path / "demo")
    config = load_config(config_path)

    with pytest.raises(ConfigError, match="not registered"):
        config.project("missing")


def test_config_rejects_arbitrary_shell_setting(tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        """control_repo: owner/bridge
projects: {}
shell: powershell
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="config"):
        load_config(config_path)

