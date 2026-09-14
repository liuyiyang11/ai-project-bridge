from pathlib import Path

import pytest

from bridge.config import ConfigError, load_config
from bridge.github import find_executable


def test_find_executable_falls_back_to_codex_install_directory(tmp_path, monkeypatch):
    install = tmp_path / "OpenAI" / "Codex" / "bin" / "version-1"
    install.mkdir(parents=True)
    executable = install / "codex.exe"
    executable.write_bytes(b"fake")
    monkeypatch.setattr("bridge.github.shutil.which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert Path(find_executable("codex")).resolve() == executable.resolve()


def test_config_yaml_windows_double_quote_error_has_actionable_hint(tmp_path):
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        'control_repo: "owner/bridge"\nprojects: {}\nroot: "E:\\AI project bridge"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="single quotes"):
        load_config(config_path)

