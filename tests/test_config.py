from pathlib import Path

import pytest

from bridge.config import ConfigError, load_config


def write_config(path: Path, root: Path) -> None:
    path.write_text(
        f"""control_repo: owner/bridge
trusted_github_logins: [trusted-user]
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


def test_config_supports_multiple_capabilities_and_migrates_legacy_kind(tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        f"""control_repo: owner/bridge
trusted_github_logins: [trusted-user]
projects:
  research:
    capabilities: [code, experiment-review]
    root: {(tmp_path / 'research').as_posix()}
    repo: owner/research
  legacy:
    kind: presentation
    root: {(tmp_path / 'legacy').as_posix()}
    repo: owner/legacy
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.project("research").capabilities == ["code", "experiment-review"]
    assert config.project("legacy").capabilities == ["presentation"]
    assert config.is_trusted_github_login("TRUSTED-USER")


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


def test_config_rejects_empty_trusted_github_logins(tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("control_repo: owner/bridge\ntrusted_github_logins: []\nprojects: {}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="trusted_github_logins"):
        load_config(config_path)

