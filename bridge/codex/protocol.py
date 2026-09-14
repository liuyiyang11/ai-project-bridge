from __future__ import annotations

"""Small, dependency-free JSON-RPC helpers for Codex app-server.

The app-server uses newline-delimited JSON over stdio.  This module deliberately
does not model the full generated protocol: the Bridge only accepts the small
subset needed by its research workflow and treats unknown fields as opaque
data.  Raw reasoning text is filtered before it can reach the orchestration
layer.
"""

from dataclasses import dataclass
from typing import Any, Optional


class CodexProtocolError(RuntimeError):
    """The app-server returned malformed or unsuccessful JSON-RPC data."""


class CodexProtocolTimeout(CodexProtocolError):
    """A JSON-RPC response did not arrive before the configured deadline."""


class CodexProcessError(CodexProtocolError):
    """The app-server process exited while a request was in flight."""

    def __init__(self, message: str, *, returncode: Optional[int] = None, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        suffix = f" (exit code {returncode})" if returncode is not None else ""
        if stderr.strip():
            suffix += f": {stderr.strip()[-1000:]}"
        super().__init__(message + suffix)


class CodexRequestError(CodexProtocolError):
    """A JSON-RPC error response."""

    def __init__(self, code: Any, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"Codex app-server error {code}: {message}")


@dataclass(frozen=True)
class JsonRpcMessage:
    """Parsed JSON-RPC message with only the fields used by the Bridge."""

    value: dict[str, Any]

    @property
    def message_id(self) -> Any:
        return self.value.get("id")

    @property
    def method(self) -> Optional[str]:
        method = self.value.get("method")
        return method if isinstance(method, str) else None

    @property
    def params(self) -> dict[str, Any]:
        params = self.value.get("params")
        return params if isinstance(params, dict) else {}

    @property
    def is_response(self) -> bool:
        return "id" in self.value and ("result" in self.value or "error" in self.value)

    @property
    def is_request_or_notification(self) -> bool:
        return isinstance(self.method, str)


RAW_REASONING_METHODS = frozenset(
    {
        "item/reasoning/textDelta",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded",
    }
)


def parse_message(line: str) -> JsonRpcMessage:
    import json

    try:
        value = json.loads(line)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CodexProtocolError(f"invalid app-server JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CodexProtocolError("app-server JSON-RPC message must be an object")
    return JsonRpcMessage(value)


def request_payload(message_id: int, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}}


def notification_payload(method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    value = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        value["params"] = params
    return value


def safe_notification(message: JsonRpcMessage) -> Optional[dict[str, Any]]:
    """Return a bounded protocol notification safe for Bridge consumers.

    The Bridge may retain agent-facing messages, command output, file changes,
    and status data.  It must never expose or persist raw chain-of-thought.
    """

    method = message.method
    if not method or method in RAW_REASONING_METHODS:
        return None
    params = message.params
    if method == "item/agentMessage/delta":
        allowed = {key: params[key] for key in ("delta", "itemId", "threadId", "turnId") if key in params}
        return {"method": method, "params": allowed}
    return {"method": method, "params": _bounded_copy(params)}


def _bounded_copy(value: Any, depth: int = 0) -> Any:
    """Copy protocol metadata without accidentally persisting giant payloads."""

    if depth > 8:
        return "<truncated>"
    if isinstance(value, dict):
        item_type = value.get("type")
        if isinstance(item_type, str) and "reasoning" in item_type.casefold():
            return {"type": "reasoning_redacted"}
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"textDelta", "reasoning", "rawReasoning", "chainOfThought"}:
                continue
            result[str(key)] = _bounded_copy(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_bounded_copy(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:16000]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:1000]
