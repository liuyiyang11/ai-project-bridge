from __future__ import annotations

from typing import Any

from ..dispatcher import Dispatcher
from .base import BaseTransport


class GitHubTransport(BaseTransport):
    """Compatibility adapter for the existing GitHub Issue dispatcher.

    Phase 6 will move Issue parsing and publication behind this adapter.  For
    Phase 1–3 the mature Dispatcher remains the implementation while the core
    Supervisor and MCP path stay GitHub-independent.
    """

    def __init__(self, dispatcher: Dispatcher):
        self.dispatcher = dispatcher

    def serve(self) -> list[dict[str, Any]]:
        return self.dispatcher.run_once()
