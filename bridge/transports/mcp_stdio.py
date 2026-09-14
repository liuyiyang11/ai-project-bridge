from __future__ import annotations

from typing import Any, Optional, TextIO

from ..mcp.server import McpStdioServer
from ..mcp.tools import BridgeMcpTools
from .base import BaseTransport


class MCPStdioTransport(BaseTransport):
    """MCP stdio adapter around the transport-independent Bridge tools."""

    def __init__(self, *, tools: Optional[BridgeMcpTools] = None, server: Optional[McpStdioServer] = None, config: Any = None):
        self.server = server or McpStdioServer(tools or BridgeMcpTools(config=config))

    def serve(self, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> None:
        self.server.serve(stdin, stdout)


McpStdioTransport = MCPStdioTransport
