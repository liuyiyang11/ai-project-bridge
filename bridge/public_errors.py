"""Stable, caller-facing errors and final public-data sanitization.

Exceptions in the runtime can contain paths, command lines, credentials, or
implementation details.  This module is deliberately small and dependency
free so every public boundary can convert those exceptions to the same safe
shape without changing the successful result contract.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from .tunnel_profile import sanitize_diagnostics


PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
TASK_NOT_FOUND = "TASK_NOT_FOUND"
INVALID_REQUEST = "INVALID_REQUEST"
COMMAND_FAILED = "COMMAND_FAILED"
COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
CODEX_START_FAILED = "CODEX_START_FAILED"
CODEX_RUNTIME_FAILED = "CODEX_RUNTIME_FAILED"
PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
INTERNAL_ERROR = "INTERNAL_ERROR"

_KNOWN_CODES = frozenset(
    {
        PROJECT_NOT_FOUND,
        TASK_NOT_FOUND,
        INVALID_REQUEST,
        COMMAND_FAILED,
        COMMAND_TIMEOUT,
        CODEX_START_FAILED,
        CODEX_RUNTIME_FAILED,
        PERSISTENCE_FAILED,
        INTERNAL_ERROR,
    }
)
_PROJECT_NOT_FOUND = re.compile(r"^project is not registered:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,127})\s*\Z", re.IGNORECASE)
_TASK_NOT_FOUND = re.compile(r"^task does not exist:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,127})\s*\Z", re.IGNORECASE)
_COMMAND_NOT_FOUND = re.compile(r"^command is not allowed or not registered:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,127})\s*\Z", re.IGNORECASE)
_PUBLIC_PROJECT_NOT_FOUND = re.compile(r"^project '[A-Za-z0-9][A-Za-z0-9._-]{0,127}' is not registered\.\Z", re.IGNORECASE)
_PUBLIC_TASK_NOT_FOUND = re.compile(r"^Task '[A-Za-z0-9][A-Za-z0-9._-]{0,127}' does not exist\.\Z")
_PUBLIC_COMMAND_NOT_FOUND = re.compile(r"^command '[A-Za-z0-9][A-Za-z0-9._-]{0,127}' is not allowed or not registered\.\Z", re.IGNORECASE)
_SAFE_CODEX_FAILURE = re.compile(r"^(?:Codex turn failed|saved thread cannot be resumed)\Z")
_SAFE_COMMAND_FAILURE = re.compile(r"^configured (?:experiment )?command failed with code -?\d+\Z", re.IGNORECASE)
_TOKEN_VALUE = re.compile(
    r"(?i)(\b(?:api[_-]?key|admin[_-]?key|token)\b\s*(?:[:=]\s*|is\s+|\s+))[^\s,;]+"
)
_OBVIOUS_PUBLIC_TOKEN = re.compile(r"(?i)\b(?:gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}\b")

_TRACEBACK_KEYS = frozenset(
    {
        "cause",
        "exception",
        "raw_exception",
        "repr",
        "stack",
        "stacktrace",
        "traceback",
        "__cause__",
        "__context__",
    }
)
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
    }
)


@dataclass(frozen=True)
class PublicError:
    """The only error shape allowed across the public Bridge boundary."""

    error_code: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.error_code, str) or self.error_code not in _KNOWN_CODES:
            object.__setattr__(self, "error_code", INTERNAL_ERROR)
        object.__setattr__(self, "message", _public_message_for_code(self.error_code, self.message))

    def as_dict(self) -> dict[str, str]:
        return {"error_code": self.error_code, "message": self.message}

    to_dict = as_dict


def _canonical_message(error_code: str) -> str:
    return {
        PROJECT_NOT_FOUND: "The requested project is not registered.",
        TASK_NOT_FOUND: "The requested task does not exist.",
        INVALID_REQUEST: "Invalid request.",
        COMMAND_FAILED: "The registered command failed.",
        COMMAND_TIMEOUT: "The registered command timed out.",
        CODEX_START_FAILED: "Codex could not be started. Check the local Codex installation.",
        CODEX_RUNTIME_FAILED: "The Codex task failed.",
        PERSISTENCE_FAILED: "The task could not be saved safely.",
        INTERNAL_ERROR: "The task failed unexpectedly. Check local Bridge developer logs.",
    }.get(error_code, "The task failed unexpectedly. Check local Bridge developer logs.")


def _public_message_for_code(error_code: str, message: Any) -> str:
    """Keep only known, intentionally safe dynamic messages for a code."""

    candidate = message if isinstance(message, str) else ""
    if candidate == _canonical_message(error_code):
        return candidate
    if error_code == CODEX_RUNTIME_FAILED and candidate == "The Codex task timed out.":
        return candidate
    if error_code == PROJECT_NOT_FOUND:
        if _PUBLIC_PROJECT_NOT_FOUND.fullmatch(candidate):
            return candidate
        match = _PROJECT_NOT_FOUND.match(candidate)
        if match:
            return f"project '{match.group(1)}' is not registered."
    elif error_code == TASK_NOT_FOUND:
        if _PUBLIC_TASK_NOT_FOUND.fullmatch(candidate):
            return candidate
        match = _TASK_NOT_FOUND.match(candidate)
        if match:
            return f"Task '{match.group(1)}' does not exist."
    elif error_code == COMMAND_FAILED:
        if _PUBLIC_COMMAND_NOT_FOUND.fullmatch(candidate):
            return candidate
        match = _COMMAND_NOT_FOUND.match(candidate)
        if match:
            return f"command '{match.group(1)}' is not allowed or not registered."
        if _SAFE_COMMAND_FAILURE.fullmatch(candidate):
            return candidate
    elif error_code == CODEX_RUNTIME_FAILED and _SAFE_CODEX_FAILURE.fullmatch(candidate):
        return candidate
    elif error_code == INVALID_REQUEST and candidate == "input contains invalid Unicode scalar value":
        return candidate
    return _canonical_message(error_code)


def public_error_from_message(message: Any, *, default_code: str = INTERNAL_ERROR) -> PublicError:
    """Classify a legacy/public string without returning the original text."""

    candidate = message if isinstance(message, str) else ""
    for code in _KNOWN_CODES:
        if candidate == _canonical_message(code):
            return PublicError(code, candidate)
    if candidate == "The Codex task timed out.":
        return PublicError(CODEX_RUNTIME_FAILED, candidate)
    if _PUBLIC_PROJECT_NOT_FOUND.fullmatch(candidate):
        return PublicError(PROJECT_NOT_FOUND, candidate)
    if _PUBLIC_TASK_NOT_FOUND.fullmatch(candidate):
        return PublicError(TASK_NOT_FOUND, candidate)
    if _PUBLIC_COMMAND_NOT_FOUND.fullmatch(candidate):
        return PublicError(COMMAND_FAILED, candidate)
    if _PROJECT_NOT_FOUND.match(candidate):
        return PublicError(PROJECT_NOT_FOUND, _public_message_for_code(PROJECT_NOT_FOUND, candidate))
    if _TASK_NOT_FOUND.match(candidate):
        return PublicError(TASK_NOT_FOUND, _public_message_for_code(TASK_NOT_FOUND, candidate))
    if _COMMAND_NOT_FOUND.match(candidate):
        return PublicError(COMMAND_FAILED, _public_message_for_code(COMMAND_FAILED, candidate))
    if candidate == "input contains invalid Unicode scalar value":
        return PublicError(INVALID_REQUEST, candidate)
    lowered = candidate.casefold()
    if "timed out" in lowered or "timeout" in lowered:
        if "codex" in lowered or "app-server" in lowered or "task turn" in lowered:
            return PublicError(CODEX_RUNTIME_FAILED, "The Codex task timed out.")
        return PublicError(COMMAND_TIMEOUT, _canonical_message(COMMAND_TIMEOUT))
    if "codex" in lowered and ("not available" in lowered or "could not be started" in lowered):
        return PublicError(CODEX_START_FAILED, _canonical_message(CODEX_START_FAILED))
    if _SAFE_CODEX_FAILURE.fullmatch(candidate):
        return PublicError(CODEX_RUNTIME_FAILED, candidate)
    if _SAFE_COMMAND_FAILURE.fullmatch(candidate):
        return PublicError(COMMAND_FAILED, candidate)
    if default_code not in _KNOWN_CODES:
        default_code = INTERNAL_ERROR
    return PublicError(default_code, _canonical_message(default_code))


def public_error_from_exception(error: BaseException, *, context: Optional[str] = None) -> PublicError:
    """Map a runtime exception to a safe, stable error.

    The exception text is inspected only for classification.  It is never
    copied into the returned message unless it matches one of the explicit
    safe-message allowlists above.
    """

    name = type(error).__name__
    raw = str(error)
    lowered = raw.casefold()
    context_value = (context or "").casefold()

    if name == "SecurityError":
        if raw == "input contains invalid Unicode scalar value":
            return PublicError(INVALID_REQUEST, raw)
        return PublicError(INVALID_REQUEST, _canonical_message(INVALID_REQUEST))
    if name in {"ValidationError", "McpToolError"}:
        return public_error_from_message(raw, default_code=INVALID_REQUEST)
    if name in {"ConfigError"} and _PROJECT_NOT_FOUND.match(raw):
        return PublicError(PROJECT_NOT_FOUND, _public_message_for_code(PROJECT_NOT_FOUND, raw))
    if name in {"CommandExecutionError", "CalledProcessError"} or context_value == "command":
        if "timed out" in lowered or "timeout" in lowered:
            return PublicError(COMMAND_TIMEOUT, _canonical_message(COMMAND_TIMEOUT))
        if _COMMAND_NOT_FOUND.match(raw):
            return PublicError(COMMAND_FAILED, _public_message_for_code(COMMAND_FAILED, raw))
        if _SAFE_COMMAND_FAILURE.fullmatch(raw):
            return PublicError(COMMAND_FAILED, raw)
        return PublicError(COMMAND_FAILED, _canonical_message(COMMAND_FAILED))
    if name in {"TimeoutExpired", "CodexProtocolTimeout"} or "timed out" in lowered or "timeout" in lowered:
        if context_value == "command":
            return PublicError(COMMAND_TIMEOUT, _canonical_message(COMMAND_TIMEOUT))
        return PublicError(CODEX_RUNTIME_FAILED, "The Codex task timed out.")
    if name in {"CodexUnavailableError"} or ("codex" in lowered and "unavailable" in lowered) or (
        name == "CodexProcessError" and ("not available" in lowered or context_value in {"codex_start", "start"})
    ):
        return PublicError(CODEX_START_FAILED, _canonical_message(CODEX_START_FAILED))
    if name == "CodexProcessError":
        if "could not be started" in lowered or "not available" in lowered:
            return PublicError(CODEX_START_FAILED, _canonical_message(CODEX_START_FAILED))
        if "timed out" in lowered or "timeout" in lowered:
            return PublicError(CODEX_RUNTIME_FAILED, "The Codex task timed out.")
        return PublicError(CODEX_RUNTIME_FAILED, _canonical_message(CODEX_RUNTIME_FAILED))
    if name in {"CodexRequestError", "CodexResumeMismatchError"} or context_value == "codex":
        return PublicError(CODEX_RUNTIME_FAILED, _canonical_message(CODEX_RUNTIME_FAILED))
    if name in {"OSError", "JSONDecodeError"} and context_value in {"persistence", "store"}:
        return PublicError(PERSISTENCE_FAILED, _canonical_message(PERSISTENCE_FAILED))
    if name in {"SessionTransitionError", "InvalidTaskTransition", "CodexModelError"} and context_value in {"mcp", "request"}:
        return PublicError(INVALID_REQUEST, _canonical_message(INVALID_REQUEST))
    if name in {"ValueError", "TypeError", "AssertionError"} and context_value in {"mcp", "request"}:
        return public_error_from_message(raw, default_code=INVALID_REQUEST)
    if _SAFE_CODEX_FAILURE.fullmatch(raw):
        return PublicError(CODEX_RUNTIME_FAILED, raw)
    if _SAFE_COMMAND_FAILURE.fullmatch(raw):
        return PublicError(COMMAND_FAILED, raw)
    return public_error_from_message(raw)


def sanitize_public_text(value: Any, *, limit: int = 16000) -> str:
    """Redact secrets, absolute paths, and traceback text from public strings."""

    text = str(value)
    text = sanitize_diagnostics(text)
    text = _TOKEN_VALUE.sub(r"\1[redacted]", text)
    text = _OBVIOUS_PUBLIC_TOKEN.sub("[redacted]", text)
    text = re.sub(r"(?is)traceback\s*\(most recent call last\):.*", "[traceback redacted]", text)
    text = re.sub(r"(?i)(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\\\)[^\r\n,;)]*", "<absolute-path>", text)
    text = re.sub(r"(?<![A-Za-z0-9_:/])/(?:[^\s,;)]+/)*[^\s,;)]+", "<absolute-path>", text)
    return text[:limit]


def sanitize_public_value(value: Any, *, depth: int = 0, key: Optional[str] = None) -> Any:
    """Recursively sanitize persisted/event values while retaining safe data."""

    if depth > 8:
        return "<truncated>"
    if isinstance(value, PublicError):
        return value.as_dict()
    if isinstance(value, BaseException):
        return "[exception redacted]"
    key_name = str(key or "").casefold()
    if key_name in _TRACEBACK_KEYS:
        return "[redacted]"
    if key_name in _SECRET_KEYS or any(part in key_name for part in ("token", "secret", "password", "credential", "api_key", "authorization")):
        return "[redacted]"
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            item_key = str(raw_key)
            item_key_folded = item_key.casefold()
            if item_key_folded in _TRACEBACK_KEYS:
                continue
            if item_key_folded in _SECRET_KEYS or any(part in item_key_folded for part in ("token", "secret", "password", "credential", "api_key", "authorization")):
                output[item_key] = "[redacted]"
            elif item_key_folded in {"error", "failure", "last_error"}:
                output[item_key] = None if item is None else sanitize_error_payload(item)
            else:
                output[item_key] = sanitize_public_value(item, depth=depth + 1, key=item_key)
        return output
    if isinstance(value, list):
        return [sanitize_public_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, tuple):
        return [sanitize_public_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return sanitize_public_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_public_text(value, limit=1000)


def sanitize_error_payload(value: Any) -> dict[str, Any]:
    """Reduce any error event to ``error_code``/``message`` plus safe context."""

    if isinstance(value, PublicError):
        return value.as_dict()
    if isinstance(value, Mapping):
        code = value.get("error_code")
        if not isinstance(code, str) or code not in _KNOWN_CODES:
            public = public_error_from_message(value.get("message"))
        else:
            public = PublicError(code, _public_message_for_code(code, value.get("message")))
        payload = public.as_dict()
        previous_state = value.get("previous_state")
        if isinstance(previous_state, str) and re.fullmatch(r"[A-Z_]{2,32}", previous_state):
            payload["previous_state"] = previous_state
        retryable = value.get("retryable")
        if isinstance(retryable, bool):
            payload["retryable"] = retryable
        return payload
    return PublicError(INTERNAL_ERROR, _canonical_message(INTERNAL_ERROR)).as_dict()


def sanitize_public_status(status: Mapping[str, Any]) -> dict[str, Any]:
    """Final projection for status responses, including legacy raw snapshots."""

    # Keep the successful status shape and values unchanged.  Only failure
    # fields are projected here; event streams have their own recursive
    # sanitizer because they can carry nested diagnostic payloads.
    output = {}
    for raw_key, value in status.items():
        key = str(raw_key)
        folded = key.casefold()
        if folded in _TRACEBACK_KEYS:
            continue
        if folded in {"error", "failure"}:
            output[key] = sanitize_error_payload(value)
        else:
            output[key] = value
    raw_error = status.get("last_error")
    if raw_error is not None:
        if isinstance(raw_error, Mapping):
            safe_payload = sanitize_error_payload(raw_error)
            public = PublicError(safe_payload["error_code"], safe_payload["message"])
        else:
            code = status.get("error_code")
            if isinstance(code, str) and code in _KNOWN_CODES:
                public = PublicError(code, _public_message_for_code(code, raw_error))
            else:
                public = public_error_from_message(raw_error)
        output["last_error"] = public.message
        output["error"] = public.as_dict()
    else:
        output["last_error"] = None
        output.pop("error", None)
    output.pop("error_code", None)
    return output


def sanitize_public_events(events: Any) -> list[dict[str, Any]]:
    """Final projection for event responses, including pre-fix journal rows."""

    if not isinstance(events, list):
        return []
    output: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        item = {str(key): sanitize_public_value(value, key=str(key)) for key, value in event.items()}
        if event.get("type") == "error":
            item["data"] = sanitize_error_payload(event.get("data"))
        output.append(item)
    return output
