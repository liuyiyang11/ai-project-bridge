from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, TextIO, Union

from ..config import ConfigError, load_config
from .tools import BridgeMcpTools, McpToolError


class McpStdioServer:
    """Minimal MCP stdio transport; no HTTP listener is created."""

    protocol_version = "2024-11-05"

    def __init__(self, tools: BridgeMcpTools):
        self.tools = tools
        self.initialized = False

    def handle_message(self, message: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not isinstance(message, dict):
            return self._error(None, -32600, "JSON-RPC message must be an object")
        method = message.get("method")
        message_id = message.get("id")
        if not isinstance(method, str):
            return self._error(message_id, -32600, "JSON-RPC method is required")
        if method == "initialize":
            self.initialized = True
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            requested = params.get("protocolVersion")
            version = requested if isinstance(requested, str) and requested else self.protocol_version
            return self._result(
                message_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ai-project-bridge", "version": "0.2.0"},
                },
            )
        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method == "ping":
            return self._result(message_id, {})
        if method == "tools/list":
            return self._result(message_id, {"tools": self.tools.definitions()})
        if method == "tools/call":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            name = params.get("name")
            arguments = params.get("arguments")
            try:
                if arguments is not None and not isinstance(arguments, dict):
                    raise McpToolError("tool arguments must be an object")
                value = self.tools.call(name, arguments)
                result: dict[str, Any] = {
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                    "structuredContent": value,
                    "isError": False,
                }
            except McpToolError as exc:
                result = {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                }
            return self._result(message_id, result)
        return self._error(message_id, -32601, f"method not found: {method}")

    def serve(self, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> None:
        input_stream = stdin or sys.stdin
        output_stream = stdout or sys.stdout
        for line in input_stream:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("message must be an object")
                response = self.handle_message(message)
            except (json.JSONDecodeError, ValueError) as exc:
                response = self._error(None, -32700, f"invalid JSON-RPC message: {exc}")
            if response is not None:
                output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                output_stream.flush()

    def close(self) -> None:
        """Close the runtime owned by the server, if it exposes a close hook."""
        supervisor = getattr(self.tools, "supervisor", None)
        close = getattr(supervisor, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    @staticmethod
    def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message[:2000]}}


MCPStdioServer = McpStdioServer


def build_parser() -> argparse.ArgumentParser:
    """Build the standalone ``python -m bridge.mcp.server`` parser."""
    parser = argparse.ArgumentParser(
        prog="python -m bridge.mcp.server",
        description="Serve the AI Project Bridge MCP tools over stdio.",
    )
    parser.add_argument(
        "--config",
        default="config.local.yaml",
        type=Path,
        help="path to the local Bridge configuration (default: config.local.yaml)",
    )
    return parser


def create_configured_server(config_path: Union[str, Path] = "config.local.yaml") -> McpStdioServer:
    """Load local configuration and construct the MCP server.

    This function is deliberately kept separate from ``main`` so tests and
    embedding code can verify server construction without touching process
    stdio.  The configuration is local startup policy; MCP callers only see
    registered project IDs and never receive project roots.
    """
    config = load_config(config_path)
    return McpStdioServer(BridgeMcpTools(config=config))


def main(argv: Optional[list[str]] = None) -> int:
    """Run the MCP stdio server as a normal Python module entry point."""
    args = build_parser().parse_args(argv)
    try:
        server = create_configured_server(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        server.serve()
    except (BrokenPipeError, KeyboardInterrupt):
        # A client closing stdio is a normal shutdown path for a local MCP
        # process.  Do not write protocol noise to stdout during shutdown.
        return 0
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
