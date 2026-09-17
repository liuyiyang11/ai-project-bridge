from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


class SecurityError(ValueError):
    """Raised when a remote value crosses a local safety boundary."""


def validate_unicode_scalars(value: Any, *, path: str = "input") -> None:
    """Reject lone UTF-16 surrogate code points before serialization.

    Python strings can contain surrogate code points even though they are not
    valid Unicode scalar values.  Validate recursively at each input boundary
    without stringifying unknown object types.
    """

    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise SecurityError("input contains invalid Unicode scalar value")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            validate_unicode_scalars(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            validate_unicode_scalars(key, path=f"{path}.key")
            validate_unicode_scalars(item, path=f"{path}[value]")


def ensure_safe_relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise SecurityError("path must be a string")
    raw = value.strip()
    if not raw or "\x00" in raw:
        raise SecurityError("path must be a non-empty relative path")

    if Path(raw).is_absolute() or PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        raise SecurityError(f"path must be relative: {value!r}")

    parts = raw.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise SecurityError(f"path traversal is not allowed: {value!r}")
    if any(part == "" for part in parts[:-1]):
        raise SecurityError(f"path contains an empty component: {value!r}")
    return raw


def resolve_under(root: Path, relative_path: str) -> Path:
    safe = ensure_safe_relative_path(relative_path)
    root_resolved = Path(root).expanduser().resolve()
    candidate = (root_resolved / Path(safe)).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise SecurityError(f"resolved path escapes root: {relative_path!r}") from exc
    return candidate


def ensure_safe_project_name(value: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise SecurityError("project must be a non-empty name")
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in value):
        raise SecurityError(f"unsafe project name: {value!r}")
    return value

