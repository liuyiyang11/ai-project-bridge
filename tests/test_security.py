from pathlib import Path

import pytest

from bridge.security import SecurityError, ensure_safe_relative_path, resolve_under


def test_resolve_under_accepts_child_path(tmp_path):
    target = resolve_under(tmp_path, "assets/image.png")
    assert target == (tmp_path / "assets" / "image.png").resolve()


@pytest.mark.parametrize("value", ["../escape", "assets/../../escape", "C:/Windows/System32", "/absolute/path"])
def test_resolve_under_rejects_escape_and_absolute_paths(tmp_path, value):
    with pytest.raises(SecurityError):
        resolve_under(tmp_path, value)


def test_safe_relative_path_rejects_empty_and_dotdot():
    with pytest.raises(SecurityError):
        ensure_safe_relative_path("../x")
    with pytest.raises(SecurityError):
        ensure_safe_relative_path("")

