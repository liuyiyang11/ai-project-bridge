from .tools import BridgeMcpTools

__all__ = ["BridgeMcpTools", "MCPStdioServer", "McpStdioServer"]


def __getattr__(name):
    # Keep the public re-exports without importing ``server`` while
    # ``python -m bridge.mcp.server`` is being launched.  Eagerly importing
    # it makes runpy emit a warning because the target module is already in
    # sys.modules before execution.
    if name in {"MCPStdioServer", "McpStdioServer"}:
        from .server import MCPStdioServer, McpStdioServer

        return MCPStdioServer if name == "MCPStdioServer" else McpStdioServer
    raise AttributeError(name)
