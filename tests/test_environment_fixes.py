from pathlib import Path
import os
import subprocess

import pytest

from bridge.config import ConfigError, load_config
from bridge.github import find_executable


def test_find_executable_falls_back_to_codex_install_directory(tmp_path, monkeypatch):
    old_install = tmp_path / "OpenAI" / "Codex" / "bin" / "version-1"
    new_install = tmp_path / "OpenAI" / "Codex" / "bin" / "version-2"
    old_install.mkdir(parents=True)
    new_install.mkdir(parents=True)
    old_executable = old_install / "codex.exe"
    executable = new_install / "codex.exe"
    old_executable.write_bytes(b"old")
    executable.write_bytes(b"new")
    os.utime(old_executable, (1, 1))
    os.utime(executable, (2, 2))
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


def test_configured_python_can_start_bridge_cli():
    python = Path(r"C:\Users\29833\.conda\envs\py10\python.exe")
    if not python.is_file():
        pytest.skip(f"configured Python is not installed: {python}")
    result = subprocess.run([str(python), "-m", "bridge", "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace")

    assert result.returncode == 0, result.stderr
    assert "run-once" in result.stdout


def test_windows_scripts_pin_configured_python():
    for name in ("install.ps1", "doctor.ps1", "run.ps1"):
        content = (Path("scripts") / name).read_text(encoding="utf-8")
        assert r"C:\Users\29833\.conda\envs\py10\python.exe" in content
